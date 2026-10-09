# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Commit-message enrichment (D27).

Crux builds the PR title and description from the branch's commit subjects
(D19), and a human-typed commit message is usually terse ("fix", "wip") —
bad material. The post-commit hook spawns a detached ``crux enrich-commit``
right after a human commit; this module rewrites the message from the
commit's own diff (one LLM pass) and amends the commit in place, before any
push, so the improved message is what reaches GitHub.

A commit authored by Claude Code (a ``Co-Authored-By: … Claude`` trailer)
already carries a thorough message and is left alone; so is a commit Crux
already amended (the ``Amended-by: Crux`` trailer — which is also the
recursion guard, since the amend fires post-commit again). The amend never
runs when it could lose work or rewrite shared history: HEAD moved, staged
changes present, a rebase/merge/cherry-pick in progress, a merge commit, or
a commit already on any remote. Those volatile guards re-run after the slow
LLM call, immediately before the amend.

The human's own message is never rewritten: it stays verbatim at the top of
the amended message, and Crux's expansion (a ``Crux: <subject>`` line plus
body bullets) is pasted BELOW it. Downstream consumers that build PR metadata
from commits (D19) call :func:`effective_subject` to prefer the Crux-written
subject over the terse human one.

D28 closes the commit-then-push-immediately race: the pre-push hook runs
:func:`ensure_enriched` in the foreground BEFORE any refs transfer. Every
outgoing human commit still missing its Crux summary is enriched right there
(the whole un-pushed chain is rebuilt with ``git commit-tree`` — message-only
rewrites, trees/authors/parents preserved, index and working tree untouched,
compare-and-swap on the branch ref). Because that changes the shas git
already resolved for the push, the hook then STOPS the push with a clear
"push again" notice; the retry finds everything enriched and sails through.

Public API:
    enrich(info, cfg, sha) -> str | None   (new subject when amended, else None)
    ensure_enriched(info, cfg, refs=None, notify=None) -> bool
                                           (True => shas stale, stop this push)
    effective_subject(message) -> str      (Crux subject when enriched, else line 1)
"""
from __future__ import annotations

import logging
import re
from importlib import resources
from pathlib import Path

from crux.gitio import GitError, run_git
from crux.llm import claude_json
from crux.models import Config, CruxError, LlmError, NotLoggedInError, RepoInfo

log = logging.getLogger("crux.commitmsg")

__all__ = ["COMMIT_TRAILER", "ENRICH_PREFIX", "effective_subject", "enrich",
           "ensure_enriched"]

PROMPT_PATH = resources.files("crux") / "prompts" / "commit.md"

# Trailer stamped on every message Crux amends. Both the post-commit hook and
# skip_reason() check it, so the amend's own post-commit firing stops here.
COMMIT_TRAILER = "Amended-by: Crux"
# Line prefix marking the Crux-written subject inside an amended message.
# The human's own message stays verbatim above it; PR metadata built from
# commits (D19) prefers this line via effective_subject().
ENRICH_PREFIX = "Crux: "

# A Claude Code commit is recognized by its co-author trailer; its message is
# already thorough, so Crux never touches it (D27).
_CLAUDE_RE = re.compile(r"^co-authored-by:.*\bclaude\b",
                        re.IGNORECASE | re.MULTILINE)
_TRAILER_RE = re.compile(r"^amended-by:\s*crux\s*$",
                         re.IGNORECASE | re.MULTILINE)

# How much of the commit's diff the prompt gets to see.
_DIFF_MAX_LINES = 400
# git convention: subjects at most ~72 chars.
_SUBJECT_MAX = 72
_BULLETS_MAX = 5

# .git entries whose presence means a rebase/merge/cherry-pick/revert is in
# flight — never amend mid-surgery.
_IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                "CHERRY_PICK_HEAD", "REVERT_HEAD")

# D28: the most outgoing commits the pre-push check will even look at. More
# than this means something unusual (first push of a big history, no
# remote-tracking refs) — the push proceeds untouched rather than firing a
# pile of LLM calls in a hook.
_ENSURE_MAX = 30


# ---------------------------------------------------------------------------
# git probes
# ---------------------------------------------------------------------------

def _head_sha(root: str) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=root).strip()


def message_of(root: str, sha: str) -> str:
    return run_git(["log", "-1", "--format=%B", sha], cwd=root)


def _index_clean(root: str) -> bool:
    """True when nothing is staged — an amend would sweep staged work into
    the commit, so a dirty index blocks it."""
    try:
        run_git(["diff", "--cached", "--quiet"], cwd=root)
        return True
    except GitError:
        return False


def _operation_in_progress(root: str) -> bool:
    for name in _IN_PROGRESS:
        try:
            path = run_git(["rev-parse", "--git-path", name], cwd=root).strip()
        except GitError:
            return True  # cannot even ask git: touch nothing
        if path and Path(root, path).exists():
            return True
    return False


def _on_a_remote(root: str, sha: str) -> bool:
    """True when the commit is reachable from any remote-tracking ref —
    amending it would fork local history away from the pushed branch."""
    try:
        out = run_git(["branch", "-r", "--contains", sha], cwd=root)
    except GitError:
        return True  # cannot verify: assume pushed, never rewrite
    return bool(out.strip())


def _is_merge(root: str, sha: str) -> bool:
    try:
        out = run_git(["rev-list", "--parents", "-n", "1", sha], cwd=root)
    except GitError:
        return True
    return len(out.split()) > 2


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def skip_reason(info: RepoInfo, cfg: Config, sha: str) -> str | None:
    """Stable reasons this commit should never be enriched, or None."""
    if not cfg.commit_enrich:
        return "disabled by [commit] enrich = false"
    msg = message_of(info.root, sha)
    if _CLAUDE_RE.search(msg):
        return "Claude-authored commit; its message is already thorough"
    if _TRAILER_RE.search(msg):
        return "already amended by Crux"
    if _is_merge(info.root, sha):
        return "merge commit"
    return None


def _volatile_guard(root: str, sha: str) -> str | None:
    """Reasons the AMEND must not happen right now. Checked twice: before the
    LLM call, and again after it — the call takes long enough for the user to
    have committed again, staged something, started a rebase, or pushed."""
    if _head_sha(root) != sha:
        return "HEAD moved since the commit"
    if _operation_in_progress(root):
        return "a rebase/merge/cherry-pick is in progress"
    if not _index_clean(root):
        return "staged changes present (amend would sweep them in)"
    if _on_a_remote(root, sha):
        return "commit is already on a remote"
    return None


# ---------------------------------------------------------------------------
# prompt + message assembly
# ---------------------------------------------------------------------------

def _commit_diff(root: str, sha: str) -> str:
    text = run_git(["show", "--no-color", "--format=", "--unified=3", sha],
                   cwd=root)
    lines = text.splitlines()
    if len(lines) > _DIFF_MAX_LINES:
        extra = len(lines) - _DIFF_MAX_LINES
        lines = lines[:_DIFF_MAX_LINES] + [f"[... truncated {extra} more lines]"]
    return "\n".join(lines)


def build_prompt(info: RepoInfo, sha: str, original: str) -> str:
    try:
        template = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise CruxError(f"prompt template missing: {PROMPT_PATH}") from exc
    try:
        return template.format(
            branch=info.branch,
            original=original.strip() or "(no message)",
            diff=_commit_diff(info.root, sha),
        )
    except (KeyError, IndexError, ValueError) as exc:
        # A literal brace in the template must be doubled ({{ }}) for .format.
        raise CruxError(
            f"prompt template {PROMPT_PATH} has a bad placeholder: {exc}") from exc


def _coerce(data: dict) -> tuple[str, list[str]]:
    subject = " ".join(str(data.get("subject") or "").split())
    raw = data.get("bullets")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    bullets = [" ".join(str(b).split()).lstrip("-• ").strip() for b in raw]
    return subject[:_SUBJECT_MAX].rstrip(" ."), [b for b in bullets if b][:_BULLETS_MAX]


def build_message(subject: str, bullets: list[str], original: str) -> str:
    """The amended message: the human's message verbatim on top, Crux's
    expansion pasted below it, and the trailer that marks (and guards) it."""
    parts = [original.strip() or subject]
    crux_part = f"{ENRICH_PREFIX}{subject}"
    if bullets:
        crux_part += "\n\n" + "\n".join(f"- {b}" for b in bullets)
    parts.append(crux_part)
    parts.append(COMMIT_TRAILER)
    return "\n\n".join(parts) + "\n"


def effective_subject(message: str) -> str:
    """The subject PR metadata should use for this commit (D19/D27).

    For a Crux-amended message, that is the Crux-written subject (the human's
    own line above it is usually terse — the whole reason it was amended);
    for everything else, the message's first line.
    """
    stripped = message.strip()
    if not stripped:
        return ""
    if _TRAILER_RE.search(stripped):
        for line in stripped.splitlines():
            if line.startswith(ENRICH_PREFIX):
                rest = line[len(ENRICH_PREFIX):].strip()
                if rest:
                    return rest
    return stripped.splitlines()[0].strip()


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def enrich(info: RepoInfo, cfg: Config, sha: str) -> str | None:
    """Rewrite commit *sha*'s message from its diff and amend it in place.

    Returns the new subject when the commit was amended, else None. Raises
    CruxError/LlmError on hard failures (missing template, claude broken);
    the CLI catches those and logs — a hook never sees a nonzero exit.
    """
    reason = skip_reason(info, cfg, sha) or _volatile_guard(info.root, sha)
    if reason is not None:
        log.info("enrich-commit: leaving %s alone — %s", sha[:12], reason)
        return None

    original = message_of(info.root, sha)
    data = claude_json(build_prompt(info, sha, original), cfg)
    subject, bullets = _coerce(data)
    if not subject:
        log.info("enrich-commit: no usable subject for %s; not amending", sha[:12])
        return None

    # The LLM call was slow; make sure the world did not move underneath us.
    reason = _volatile_guard(info.root, sha)
    if reason is not None:
        log.info("enrich-commit: aborting amend of %s — %s", sha[:12], reason)
        return None

    # --no-verify: pre-commit/commit-msg hooks already accepted this commit;
    # only its message changes. post-commit still fires — the trailer stops it.
    run_git(["commit", "--amend", "--allow-empty", "--no-verify",
             "-m", build_message(subject, bullets, original)], cwd=info.root)
    log.info("enrich-commit: amended %s -> %r", sha[:12], subject)
    return subject


# ---------------------------------------------------------------------------
# D28: pre-push safety net — every outgoing commit enriched BEFORE refs move
# ---------------------------------------------------------------------------

def _needs_enrichment(root: str, sha: str) -> bool:
    msg = message_of(root, sha)
    return (not _CLAUDE_RE.search(msg)
            and not _TRAILER_RE.search(msg)
            and not _is_merge(root, sha))


def _outgoing(root: str, tip: str) -> list[str]:
    """Commits reachable from *tip* but not on any remote, oldest first.
    Children always after their parents (--topo-order), so the chain can be
    rebuilt front to back."""
    out = run_git(["rev-list", "--reverse", "--topo-order", tip,
                   "--not", "--remotes"], cwd=root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _parents(root: str, sha: str) -> list[str]:
    return run_git(["rev-list", "--parents", "-n", "1", sha],
                   cwd=root).split()[1:]


def _author_env(root: str, sha: str) -> dict[str, str]:
    name, email, date = run_git(
        ["log", "-1", "--format=%an%x00%ae%x00%aD", sha], cwd=root
    ).split("\x00")
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": date}


def _rebuild_chain(root: str, ref: str, chain: list[str],
                   new_messages: dict[str, str]) -> bool:
    """Rebuild *chain* (oldest first, tip last) with *new_messages* swapped in.

    ``git commit-tree`` reuses each commit's tree and author verbatim, so
    only messages change; the index and working tree are never touched. The
    final ``update-ref`` is compare-and-swap on the old tip: if anything else
    (say, a detached enrich-commit finishing late) moved the ref meanwhile,
    the whole rebuild is discarded — but the caller still reports the push's
    shas as stale, because they are. Returns True when the ref moved.
    """
    mapping: dict[str, str] = {}
    for sha in chain:
        parents = _parents(root, sha)
        new_parents = [mapping.get(p, p) for p in parents]
        message = new_messages.get(sha)
        if message is None and new_parents == parents:
            continue  # untouched prefix keeps its sha
        if message is None:
            message = message_of(root, sha)
        args = ["commit-tree", f"{sha}^{{tree}}"]
        for parent in new_parents:
            args += ["-p", parent]
        args += ["-m", message.rstrip("\n")]
        mapping[sha] = run_git(args, cwd=root, env=_author_env(root, sha)).strip()
    old_tip, new_tip = chain[-1], mapping.get(chain[-1])
    if new_tip is None:
        return False
    try:
        run_git(["update-ref", "-m", "crux: enrich commit messages (D28)",
                 ref, new_tip, old_tip], cwd=root)
    except GitError as exc:
        log.warning("ensure-enriched: %s moved during the rewrite; "
                    "discarding it (%s)", ref, exc)
        return False
    log.info("ensure-enriched: %s %s -> %s (%d message(s) expanded)",
             ref, old_tip[:12], new_tip[:12], len(new_messages))
    return True


def _current_branch_ref(root: str) -> str | None:
    try:
        return run_git(["symbolic-ref", "--quiet", "HEAD"], cwd=root).strip() or None
    except GitError:
        return None  # detached HEAD


def ensure_enriched(info: RepoInfo, cfg: Config,
                    refs: list[tuple[str, str]] | None = None,
                    notify=None) -> bool:
    """D28: make sure every outgoing human commit carries its Crux summary
    BEFORE a push moves any refs.

    *refs* is the pre-push hook's stdin, parsed: (local_ref, local_sha) pairs
    git is about to push. Without it (manual invocation), the current branch
    is checked. Returns True when the shas git resolved for this push are
    STALE — some ref was rewritten (by this call, or by a detached
    enrich-commit landing mid-push) — and the push must be stopped and
    re-run. False means the push may proceed.

    LLM failures never stop a push: a commit whose enrichment fails is pushed
    terse (logged), which beats trapping every push behind a broken claude.
    """
    if not cfg.commit_enrich:
        return False
    if _operation_in_progress(info.root):
        return False
    if refs is None:
        ref = _current_branch_ref(info.root)
        if ref is None:
            return False
        refs = [(ref, _head_sha(info.root))]

    stale = False
    for ref, pushed_sha in refs:
        if not ref.startswith("refs/heads/") or set(pushed_sha) == {"0"}:
            continue  # tags, notes, branch deletions
        if _ref_value(info.root, ref) != pushed_sha:
            # The ref moved after git resolved the push (a detached
            # enrich-commit landed mid-push): this push would send a
            # superseded commit. Stop it; no LLM work needed here.
            stale = True
            continue
        try:
            chain = _outgoing(info.root, pushed_sha)
        except GitError as exc:
            log.warning("ensure-enriched: cannot list outgoing commits for "
                        "%s: %s", ref, exc)
            continue
        if not chain:
            continue
        if len(chain) > _ENSURE_MAX:
            log.info("ensure-enriched: %s has %d un-pushed commits (> %d); "
                     "leaving them alone", ref, len(chain), _ENSURE_MAX)
            continue
        needs = [s for s in chain if _needs_enrichment(info.root, s)]
        if not needs:
            continue
        if notify:
            notify(f"✍️ Crux: expanding {len(needs)} commit message(s) before "
                   "the push — this takes a moment, then push again")
        new_messages: dict[str, str] = {}
        for sha in needs:
            original = message_of(info.root, sha)
            try:
                data = claude_json(build_prompt(info, sha, original), cfg)
            except NotLoggedInError as exc:
                # Not logged in fails identically for every commit — say so
                # once and push the chain terse rather than skipping silently.
                if notify:
                    notify(f"⚠️  Crux: {exc}")
                log.warning("ensure-enriched: %s (pushing commits terse)", exc)
                break
            except (LlmError, CruxError) as exc:
                log.warning("ensure-enriched: skipping %s (%s)", sha[:12], exc)
                continue
            subject, bullets = _coerce(data)
            if subject:
                new_messages[sha] = build_message(subject, bullets, original)
        if new_messages:
            _rebuild_chain(info.root, ref, chain, new_messages)
        # Rewritten by us, moved under us, or both: either way the pushed
        # sha no longer matches the ref — stop this push.
        if _ref_value(info.root, ref) != pushed_sha:
            stale = True
    return stale


def _ref_value(root: str, ref: str) -> str:
    try:
        return run_git(["rev-parse", "--verify", ref], cwd=root).strip()
    except GitError:
        return ""
