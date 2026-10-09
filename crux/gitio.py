# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Git plumbing for Crux: repo discovery and unified-diff parsing.

Public API (DESIGN.md module contracts):
    run_git(args, cwd=None) -> str
    git_version() -> tuple[int, int, int] | None
    repo_info(cwd=None, base_ref=None) -> RepoInfo
    with_base(info, base_branch) -> RepoInfo
    diff_hunks(info) -> list[Hunk]
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import replace

from crux.models import CruxError, Hunk, RepoInfo

log = logging.getLogger("crux.gitio")


class GitError(CruxError):
    """Raised when the git binary is missing or a git command fails."""


def run_git(args: list[str], cwd: str | None = None,
            env: dict[str, str] | None = None,
            timeout: float | None = None) -> str:
    """Run ``git <args>`` and return stdout with trailing newlines stripped.

    *env* entries are laid over the inherited environment (used to preserve
    GIT_AUTHOR_* when rebuilding commits, D28). *timeout* (seconds) bounds the
    call — every git command here is local and instant except the two that
    talk to the remote (fetching a base this clone has no ref for, and the D34
    PR base), which must never hang a run; local plumbing passes None. Raises
    GitError if git is not installed, times out, or exits nonzero.
    """
    full_env = None if env is None else {**os.environ, **env}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=full_env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",  # diff output may contain non-UTF-8 bytes
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:2])} timed out after "
                       f"{timeout}s") from exc
    except OSError as exc:
        raise GitError(f"could not run git: {exc}") from exc
    if proc.returncode != 0:
        cmd = "git " + " ".join(args)
        raise GitError(f"{cmd} failed with code {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


def _try_git(args: list[str], cwd: str | None) -> str | None:
    try:
        return run_git(args, cwd=cwd)
    except GitError:
        return None


def git_version() -> tuple[int, int, int] | None:
    """The installed git as (major, minor, patch), or None if it cannot tell.

    Parses the leading dotted number of `git --version`, which is all every
    build agrees on: "git version 2.34.1", "git version 2.39.3 (Apple
    Git-146)", "git version 2.45.1.windows.1". None — no git, or output this
    does not recognise — means "unknown", never "too old": a caller gating on
    a version must let the real command speak for itself then.
    """
    out = _try_git(["--version"], cwd=None)
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out or "")
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


# ---------------------------------------------------------------------------
# repo_info
# ---------------------------------------------------------------------------

def _parse_owner_repo(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from an origin URL.

    Handles scp-style (git@host:owner/repo.git), ssh://, and http(s):// forms.
    Returns ("", "") when the URL is missing or not owner/repo shaped, so the
    D13 allowlist check fails closed.
    """
    url = url.strip()
    if not url:
        return "", ""
    if "://" in url:
        rest = url.split("://", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else ""
    elif re.match(r"^[^/]+:", url):
        # scp-like syntax: [user@]host:path
        path = url.split(":", 1)[1]
    else:
        path = url
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", ""


# git's own record of where a branch came from: the OLDEST entry in the
# branch's reflog is written at creation as "branch: Created from <start>".
_CREATED_FROM_RE = re.compile(r"^branch: Created from (.+)$")


def branch_start_point(branch: str, cwd: str,
                       fresh_only: bool = False) -> str | None:
    """The branch *branch* was created from, per git's own reflog (D34).

    `checkout -b`/`switch -c` write "branch: Created from <start-point>" as the
    branch's first reflog entry, whatever the start point was — so this catches
    the forms `@{-1}` cannot: ``git switch -c child parent`` and ``git checkout
    -b child origin/parent`` both leave HEAD somewhere other than the parent
    the branch actually forked from.

    Returns a branch name (remote prefix stripped: "origin/parent" -> "parent"),
    or None when the start point was HEAD (the caller resolves that via @{-1}),
    a raw sha or tag, a branch that no longer exists, or the reflog has been
    pruned.

    *fresh_only* additionally requires the creation entry to be the branch's
    ONLY entry — i.e. the branch was just made. The post-checkout hook needs
    that: git gives a creation and a plain switch identical arguments, and the
    hook must tag only the former.
    """
    out = _try_git(["reflog", "show", "--format=%gs", f"refs/heads/{branch}"],
                   cwd)
    if not out:
        return None
    # Newest first, so the creation entry is the last line.
    lines = [ln for ln in out.split("\n") if ln.strip()]
    if not lines or (fresh_only and len(lines) > 1):
        return None
    match = _CREATED_FROM_RE.match(lines[-1].strip())
    if match is None:
        return None  # reflog pruned past creation, or disabled
    start = match.group(1).strip()
    if not start or start == "HEAD":
        return None
    start = start.removeprefix("refs/heads/")
    if _try_git(["show-ref", "--verify", "--quiet",
                 f"refs/heads/{start}"], cwd) is not None:
        return start  # a local branch, named as-is
    # A remote-tracking start point ("origin/parent") names the branch
    # "parent"; the caller diffs against origin/<name> anyway.
    if _try_git(["show-ref", "--verify", "--quiet",
                 f"refs/remotes/{start}"], cwd) is not None:
        bare = start.split("/", 1)[1] if "/" in start else start
        return bare or None
    return None


def _merge_base(root: str, candidates: list[str],
                head_sha: str | None = None) -> tuple[str, str] | None:
    """First (sha, ref) whose merge-base with HEAD resolves, else None.

    Given *head_sha*, a candidate whose merge-base IS HEAD is skipped: that ref
    already contains the commit under review, so it is not a base — the diff
    against it is empty. `git checkout -b feature origin/feature` (the ordinary
    way to start on a branch that exists on the remote) records the branch's
    own upstream as its start point, so without this the FIRST candidate
    "succeeded" degenerately, the fallbacks below it never ran, and the review
    was silently empty.
    """
    for cand in candidates:
        merge_base = _try_git(["merge-base", "HEAD", cand], root)
        if not merge_base:
            continue
        merge_base = merge_base.strip()
        if merge_base == head_sha:
            log.info("base candidate %r already contains HEAD; not a base",
                     cand)
            continue
        return merge_base, cand
    return None


# Bound on the git calls that touch the network — fetching a base branch this
# clone has no ref for, and the D34 PR base. A stalled remote must not hang a
# review.
_FETCH_TIMEOUT = 30


def repo_info(cwd: str | None = None, base_ref: str | None = None) -> RepoInfo:
    """Discover repo root, branch, head/base SHAs and origin owner/repo.

    base_sha is the merge-base of HEAD and the branch this branch was created
    from — branch.<name>.cruxBase (set by the post-checkout hook), else the
    start point git recorded in the branch's own reflog (D34) — else
    origin/<default_branch>, else the local default branch, else the same base
    fetched from origin (single-branch clones have no local ref for it at all),
    else HEAD itself (empty diff). A candidate that already contains HEAD is
    not a base and does not end the search. An explicit base_ref overrides all
    of these, and once a PR exists its real base wins over the lot (see
    with_base).
    """
    root = run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    head_sha = run_git(["rev-parse", "HEAD"], cwd=root)

    origin_url = _try_git(["remote", "get-url", "origin"], root) or ""
    owner, repo = _parse_owner_repo(origin_url)

    default_branch = "main"
    ref = _try_git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], root)
    if ref:
        ref = ref.strip()
        prefix = "refs/remotes/origin/"
        default_branch = ref[len(prefix):] if ref.startswith(prefix) else ref.rsplit("/", 1)[-1]

    # The branch this branch was created from, recorded by the post-checkout
    # hook. It is the preferred diff/PR base; git's reflog answers the same
    # question for branches the hook never saw (made before it was installed,
    # or with `git branch x y`, which checks nothing out). The default branch
    # is the last resort. An explicit base_ref (caller override) still wins.
    crux_base = (_try_git(["config", "--get", f"branch.{branch}.cruxBase"],
                          root) or "").strip() or None
    if crux_base is None and branch != "HEAD":
        crux_base = branch_start_point(branch, root)
    if crux_base == branch:
        # `checkout -b feature origin/feature` leaves "Created from
        # origin/feature" in the reflog, which names THIS branch. A branch is
        # not its own parent (the post-checkout hook applies the same rule
        # before recording one), and keeping it would target a new PR at the
        # branch it is opened from as well as flattening the diff.
        crux_base = None

    candidates: list[str] = []
    if base_ref:
        # BOTH spellings, like every other candidate below. With only the bare
        # name, `--base release/3.0` lost to `origin/main` on an ORDINARY
        # clone whenever the branch existed solely as refs/remotes/origin/… —
        # the normal state of a release branch never checked out locally — and
        # the override was silently dropped for the default branch.
        candidates += [base_ref, f"origin/{base_ref}"]
    if crux_base:
        candidates += [f"origin/{crux_base}", crux_base]
    candidates += [f"origin/{default_branch}", default_branch]
    # crux_base is usually the default branch, and the same ref twice buys
    # nothing but a repeated merge-base and a doubled name in the warning
    # below (_fetch_base_sha dedupes its own list for the same reason).
    candidates = list(dict.fromkeys(candidates))

    base_sha, base_branch = head_sha, None
    resolved = _merge_base(root, candidates, head_sha)
    if resolved is None:
        # No candidate is a base. One that resolved to HEAD itself is a ref
        # that is right here and simply contains the work — no fetch can turn
        # it into a base, and repo_info runs inside git hooks, which must not
        # pay a network round trip to learn that. It still names the (empty)
        # diff, as it did before it was rejected as a base. Nothing resolving
        # AT ALL is the other case: a single-branch clone, which has no local
        # ref for the base, and there the remote is worth asking.
        resolved = _merge_base(root, candidates)
        if resolved is None:
            resolved = _fetch_base_sha(root, base_ref, crux_base,
                                       default_branch)
    if resolved is not None:
        base_sha, winner = resolved
        base_branch = winner.removeprefix("origin/") or None
    if base_sha == head_sha and branch != default_branch:
        # Every candidate is either absent here or already contains HEAD, so
        # the review ahead is empty — and an empty review reads exactly like
        # "nothing to flag", the worst thing a review tool can say by accident.
        # On the default branch itself there IS no base and an empty
        # diff is the right answer, so only a branch gets the warning.
        log.warning("no base for %s resolves to a commit it has moved past "
                    "(tried %s); the diff will be empty",
                    branch, ", ".join(candidates))

    return RepoInfo(
        root=root,
        branch=branch,
        head_sha=head_sha,
        base_sha=base_sha,
        owner=owner,
        repo=repo,
        default_branch=default_branch,
        crux_base=crux_base,
        base_branch=base_branch,
    )


def _fetch_base_sha(root: str, base_ref: str | None, crux_base: str | None,
                    default_branch: str) -> tuple[str, str] | None:
    """Last-resort base resolution: fetch the base branch from origin.

    Cloud/CI checkouts (claude.ai/code sessions among them) are often
    single-branch clones: the base branch exists on the remote but has no
    local ref at all, so every merge-base candidate fails and the diff
    silently collapses to zero lines. Fetch the base and merge-base against
    FETCH_HEAD — refspec-restricted clones don't grow origin/<base> from a
    plain fetch, but FETCH_HEAD always lands. Best-effort: None when offline
    or the branch doesn't exist on the remote either.

    Candidate order matches the local path in repo_info: an explicit base_ref
    (`--base`) first, then crux_base, then the default branch. Leaving
    base_ref out here made `--base` a no-op on exactly the checkouts this
    helper exists for, and the run then recorded the wrong
    RepoInfo.base_branch on the card. (The local path had its own version of
    the same bug — it tried only the bare `base_ref`, never `origin/<ref>` —
    so before both fixes an explicit override could be dropped on an ordinary
    clone too, not just here.)

    Returns (sha, branch name) like _merge_base, so the caller records which
    branch won as RepoInfo.base_branch (D34) on this path too.
    """
    names: list[str] = []
    for candidate in (base_ref, crux_base, default_branch):
        # `git fetch origin origin/main` is not a refspec git accepts; the
        # caller spells the winner without the prefix anyway.
        candidate = (candidate or "").removeprefix("origin/")
        if candidate and candidate not in names:
            names.append(candidate)
    for name in names:
        try:
            run_git(["fetch", "--quiet", "--no-tags", "origin", name],
                    cwd=root, timeout=_FETCH_TIMEOUT)
        except GitError:
            continue
        merge_base = _try_git(["merge-base", "HEAD", "FETCH_HEAD"], root)
        if merge_base:
            log.info("no local ref for base %r; fetched it from origin", name)
            return merge_base.strip(), name
    return None


def with_base(info: RepoInfo, base_branch: str | None) -> RepoInfo:
    """RepoInfo re-based on *base_branch* — the branch the PR merges into (D34).

    The local guess (cruxBase / reflog / default branch) is only ever a guess;
    the PR's own base is the truth, and the two diverge whenever a PR is
    retargeted on GitHub, opened by hand into another branch, or auto-retargeted
    by GitHub when its base branch is merged and deleted. Reviewing the guess
    then describes a diff nobody can see on the PR.

    Fetches the base once when this clone has never seen it (a teammate's
    branch, or one retargeted to somewhere never pulled). Returns *info*
    unchanged — never raises — when the base is empty, already in force, or
    cannot be resolved even after the fetch.
    """
    if not base_branch or base_branch == info.base_branch:
        return info
    candidates = [f"origin/{base_branch}", base_branch]
    resolved = _merge_base(info.root, candidates)
    if resolved is None:
        # Unknown locally: ask the remote for exactly that branch, once.
        try:
            run_git(["fetch", "--quiet", "--no-tags", "origin", base_branch],
                    cwd=info.root, timeout=_FETCH_TIMEOUT)
        except GitError as exc:
            log.warning("could not fetch PR base %r: %s", base_branch, exc)
        resolved = _merge_base(info.root, [*candidates, "FETCH_HEAD"])
    if resolved is None:
        log.warning("PR base %r does not resolve in this clone; reviewing "
                    "against %s instead", base_branch,
                    info.base_branch or info.default_branch)
        return info
    base_sha, _ = resolved
    log.info("diff base is the PR's own base %r (was %r)", base_branch,
             info.base_branch)
    return replace(info, base_sha=base_sha, base_branch=base_branch)


# ---------------------------------------------------------------------------
# diff_hunks
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

# Symbol-defining lines across the languages we care about:
# def/class (Python), function/class (JS/TS), fn (Rust), func (Go, incl.
# method receivers), impl (Rust). Optional leading visibility/async keywords.
_SYMBOL_RE = re.compile(
    r"^[ \t]*"
    r"(?:(?:export|default|public|private|protected|static|abstract|final|async|unsafe|const)\s+)*"
    r"(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:async\s+)?"
    r"(?:"
    r"(?:def|class|function|fn|func|impl)\b\s*(?:<[^>]*>)?\s*([A-Za-z_][A-Za-z0-9_]*)"
    r"|func\s*\([^)]*\)\s*([A-Za-z_][A-Za-z0-9_]*)"
    r")"
)


def _symbol_from_line(line: str) -> str | None:
    m = _SYMBOL_RE.match(line)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _unquote(path: str) -> str:
    """Undo git's C-style path quoting (octal escapes of UTF-8 bytes)."""
    if not (len(path) >= 2 and path.startswith('"') and path.endswith('"')):
        return path
    body = path[1:-1]
    out = bytearray()
    i = 0
    escapes = {"n": b"\n", "t": b"\t", "r": b"\r", "a": b"\a", "b": b"\b",
               "f": b"\f", "v": b"\v", "\\": b"\\", '"': b'"'}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567" and i + 3 < len(body):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            out.extend(escapes.get(nxt, nxt.encode("utf-8")))
            i += 2
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _strip_ab(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _parse_diff(text: str) -> list[Hunk]:
    """Parse `git diff` unified output into Hunks. Skips binary files."""
    hunks: list[Hunk] = []
    lines = text.split("\n")
    old_path: str | None = None
    new_path: str | None = None
    binary = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            old_path = new_path = None
            binary = False
            i += 1
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
            i += 1
            continue
        if line.startswith("rename to "):
            new_path = _unquote(line[len("rename to "):])
            i += 1
            continue
        if line.startswith("--- "):
            # Git appends a literal TAB to header paths containing spaces —
            # outside the closing quote for quoted paths — so split it off
            # before unquoting. Real tabs inside a path are always C-quoted
            # ("\t"), never literal, so the first literal TAB is git's.
            old_path = _strip_ab(_unquote(line[4:].split("\t", 1)[0]))
            i += 1
            continue
        if line.startswith("+++ "):
            new_path = _strip_ab(_unquote(line[4:].split("\t", 1)[0]))
            i += 1
            continue
        m = _HUNK_RE.match(line)
        if m and not binary:
            old_start = int(m.group(1))
            old_count = int(m.group(2) or "1")
            new_start = int(m.group(3))
            new_count = int(m.group(4) or "1")
            # renames use the new path; deletions keep the old path
            if new_path is not None and new_path != "/dev/null":
                file = new_path
            else:
                file = old_path or ""
            body = [line]
            old_rem, new_rem = old_count, new_count
            i += 1
            # Count lines against the @@ header so body lines that happen to
            # start with "---"/"+++"/"diff" are never mistaken for headers.
            while i < len(lines) and (old_rem > 0 or new_rem > 0):
                bl = lines[i]
                if bl.startswith("\\"):  # "\ No newline at end of file"
                    body.append(bl)
                    i += 1
                    continue
                if bl.startswith(" ") or bl == "":
                    old_rem -= 1
                    new_rem -= 1
                elif bl.startswith("+"):
                    new_rem -= 1
                elif bl.startswith("-"):
                    old_rem -= 1
                else:
                    break  # malformed input; end the hunk defensively
                body.append(bl)
                i += 1
            if i < len(lines) and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            hunks.append(Hunk(
                id=f"{file}:{new_start}",
                file=file,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                patch="\n".join(body),
            ))
            continue
        i += 1
    return hunks


def _symbol_from_hunk_body(body_lines: list[str]) -> str | None:
    """Nearest symbol at or above the hunk's first changed line."""
    found: str | None = None
    for line in body_lines:
        tag = line[:1]
        if tag not in (" ", "+", "-"):
            continue
        sym = _symbol_from_line(line[1:])
        if sym is not None:
            found = sym
        if tag in ("+", "-"):
            break  # symbols after the first change are below it, not enclosing
    return found


def _head_file_lines(info: RepoInfo, path: str,
                     cache: dict[str, list[str] | None]) -> list[str] | None:
    if path not in cache:
        text = _try_git(["show", f"{info.head_sha}:{path}"], info.root)
        cache[path] = text.split("\n") if text is not None else None
    return cache[path]


def _enclosing_symbol(hunk: Hunk, info: RepoInfo,
                      cache: dict[str, list[str] | None]) -> str | None:
    """Find the def/class/fn/func/impl symbol enclosing this hunk.

    Order: hunk body context, then the @@ function-context trailer, then the
    head-version file scanned upward from new_start.
    """
    patch_lines = hunk.patch.split("\n")
    header, body = patch_lines[0], patch_lines[1:]

    sym = _symbol_from_hunk_body(body)
    if sym:
        return sym

    m = _HUNK_RE.match(header)
    if m and m.group(5):
        sym = _symbol_from_line(m.group(5))
        if sym:
            return sym

    if hunk.new_count == 0:
        return None  # file deleted at head; nothing to scan
    file_lines = _head_file_lines(info, hunk.file, cache)
    if not file_lines:
        return None
    start = min(hunk.new_start, len(file_lines)) - 1
    for j in range(start, -1, -1):
        sym = _symbol_from_line(file_lines[j])
        if sym:
            return sym
    return None


def diff_hunks(info: RepoInfo) -> list[Hunk]:
    """Parse ``git diff -U3 base...head`` into Hunks with enclosing symbols."""
    out = run_git(
        [
            "diff", "-U3", "-M", "--no-color", "--no-ext-diff",
            # force prefixes so parsing survives diff.noprefix in user config
            "--src-prefix=a/", "--dst-prefix=b/",
            f"{info.base_sha}...{info.head_sha}",
        ],
        cwd=info.root,
    )
    if not out.strip():
        return []
    hunks = _parse_diff(out)
    cache: dict[str, list[str] | None] = {}
    for hunk in hunks:
        hunk.enclosing_symbol = _enclosing_symbol(hunk, info, cache)
    return hunks
