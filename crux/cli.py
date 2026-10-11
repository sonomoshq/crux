# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Crux command-line interface.

Subcommands:
  run            analyze the pushed branch, post/update the sticky PR card
  preview        alias for `run --dry-run` (prints the card, posts nothing)
  ensure-pr      interactive D11 ask only (used by the pre-push hook foreground)
  enrich-commit  D27: rewrite a terse human commit message from its diff and
                 amend the commit in place (spawned by the post-commit hook)
  ensure-enriched  D28: pre-push foreground check that every outgoing human
                 commit already carries its Crux summary; enriches the rest
                 and exits 1 when the push's shas went stale (hook aborts,
                 user pushes again)
  install-hooks  install the pre-push hook + Claude Code Stop hook, globally
                 via core.hooksPath and ~/.claude/settings.json (once per
                 machine); --local scopes both to the current repo
  prs            list open PRs — the current repo (.), named repos, or (no
                 args) every repo of the scope owners — one parallel
                 `gh pr list` per repo, pool sized by [prs] jobs / --jobs
                 (0 = auto: max parallel tasks for the machine)
  memory         D31: show and manage what Crux remembers about this repo —
                 the durable facts read into every review (list/add/forget/
                 clear; local state only, never talks to GitHub)
  merge          D38: approve this branch's PR as you and merge it (--admin
                 merges on admin rights, approving nothing, and says so on the
                 PR). A branch inside a super PR is sent to `crux super merge`
  super          D37: cross-repo bundles — new/add/remove/refresh/merge/
                 order/checkout/ask/close/show/list (order, D41: pin the
                 landing order and the merge method)
  serve          D38: the loopback service (127.0.0.1) behind the Merge,
                 Set-up-to-test and Close buttons on Crux's cards and briefs
                 (--restart to pick up an edit to Crux itself, --stop to end it)

Git-hook machinery lives behind a SEPARATE private console script, `_crux-hook`
(entry point crux.cli:hook_main) — not a `crux` subcommand, so it stays out of
the user-facing surface. It runs the Python body of each git hook (pre-push,
post-commit, post-checkout) and the Claude Code Stop hook (claude-stop, D10),
invoked by the thin sh shims install-hooks writes. All hook logic lives in
Python so nothing depends on bash/nohup/dev-tty — Crux runs on any OS.

Every command exits 0 except on argparse usage errors and the deliberate
pre-push "shas went stale" exit 1: Crux runs from git hooks and must never
otherwise block a push (D7). The other exceptions are the interactive-only
commands that never run from a hook: `crux prs` exits 1 when it could list
nothing, `crux memory` on a bad forget id or an unconfirmed clear (so scripts
can rely on them). All output is logged to ~/.cache/crux/crux.log AND stderr.

Sibling modules are imported lazily inside each command so that `crux.cli`
itself imports (and is testable with mocks) independently of the rest of the
package.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from crux.models import (
    CARD_MARKER,
    STATE_VERSION,
    SUPER_ENV,
    TEST_MARKER,
    Annotation,
    Candidate,
    CruxError,
    GateDecision,
    NodeAnnotation,
    NotLoggedInError,
    RepoInfo,
    RunState,
    fingerprint,
)

LOG_NAME = "crux"


# The Claude Code Stop hook is registered as the private `_crux-hook` console
# script, not `python3 <path>`: an installed entry point runs under crux's own
# interpreter on every OS, so it needs no `python3` on PATH (which native
# Windows lacks). `_crux-hook` is machinery — deliberately NOT a `crux`
# subcommand — so it stays out of the user-facing command surface.
_CLAUDE_HOOK_COMMAND = "_crux-hook claude-stop"
_CLAUDE_SESSION_START_COMMAND = "_crux-hook claude-session-start"

# The Claude Code hooks install-hooks merges into settings.json:
# (event, command, substrings that identify an existing crux registration —
# the legacy python3-path form included, so a re-run migrates it in place).
_CLAUDE_HOOKS = (
    ("Stop", _CLAUDE_HOOK_COMMAND, ("claude-stop", "claude_intent_hook.py")),
    ("SessionStart", _CLAUDE_SESSION_START_COMMAND, ("claude-session-start",)),
)

# Fallback shown when settings.json cannot be updated in place (unparseable
# or unexpectedly shaped): the user merges the hooks by hand.
_CLAUDE_HOOK_INSTRUCTIONS = """\
To capture authoring intent (D10) and keep the Merge/Close buttons live (D38),
register the Claude Code hooks. Merge this into .claude/settings.json in each
repo you author from (or into ~/.claude/settings.json to enable it everywhere):

{{
  "hooks": {{
    "Stop": [
      {{
        "hooks": [
          {{"type": "command", "command": "{command}"}}
        ]
      }}
    ],
    "SessionStart": [
      {{
        "hooks": [
          {{"type": "command", "command": "{session_start}"}}
        ]
      }}
    ]
  }}
}}

The Stop hook snapshots the session's last assistant message into
.crux/intent.json; `crux run` cross-examines it during the claims audit. The
SessionStart hook starts `crux serve` if it is not running (e.g. after a reboot).
"""


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

def _out_of_scope(info, cfg, log) -> bool:
    """D13: True when this repo's owner is not in [scope] owners, so Crux must
    do nothing. There is no built-in owner: with the list empty, say how to
    turn Crux on rather than staying silent and looking broken."""
    if info.owner in cfg.scope_owners:
        return False
    if not cfg.scope_owners:
        import crux.config as config
        log.warning("crux: no GitHub owners configured yet, so doing nothing. "
                    "To turn it on for this repo, add  [scope] owners = [\"%s\"]  "
                    "to %s", info.owner, config.global_config_path())
    else:
        log.info("scope: owner %r not in %s; doing nothing (D13)",
                 info.owner, cfg.scope_owners)
    return True


def _log_file() -> Path:
    return Path.home() / ".cache" / "crux" / "crux.log"


def _setup_logging() -> logging.Logger:
    log = logging.getLogger(LOG_NAME)
    log.setLevel(logging.INFO)
    log.propagate = False
    # main() may run more than once per process (tests); rebuild handlers so
    # they bind the *current* sys.stderr and log file.
    for handler in list(log.handlers):
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        path = _log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The git-hook shim runs `_crux-hook <name> >>crux.log 2>&1`, so under a
        # hook our stderr *is* the log file; a FileHandler on the same file would
        # then write every record twice. Skip it when stderr already points there
        # (the StreamHandler covers the file); keep it otherwise, e.g. `crux` from
        # a terminal, where stderr is the console.
        if not _stderr_points_at(path):
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setFormatter(fmt)
            log.addHandler(file_handler)
    except OSError:
        pass  # stderr logging still works; never fail on log setup
    return log


def _stderr_points_at(path: Path) -> bool:
    """True when the process's stderr is already the file at *path*.

    Compares (device, inode) of fd 2 against the file, so an OS-level redirect
    (`2>>crux.log`) is detected even though Python's ``sys.stderr.name`` still
    reads ``<stderr>``. Returns False whenever the identity can't be established
    (no fileno, missing file, or a filesystem that reports a zero inode, e.g.
    some Windows volumes) — the safe default is to keep the FileHandler.
    """
    try:
        err_stat = os.fstat(sys.stderr.fileno())
    except (AttributeError, ValueError, OSError):
        return False
    try:
        file_stat = path.stat()
    except OSError:
        return False
    if not err_stat.st_ino:
        return False
    return (err_stat.st_dev, err_stat.st_ino) == (file_stat.st_dev,
                                                   file_stat.st_ino)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_intent(repo_root: str) -> dict | None:
    path = Path(repo_root) / ".crux" / "intent.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _skip_note(render_mod: object, decision: GateDecision) -> str:
    # render_skip_note is optional in the render contract; fall back to a
    # plain one-liner so the gate-skip path never depends on it.
    fn = getattr(render_mod, "render_skip_note", None)
    if fn is not None:
        try:
            return str(fn(decision.stats))
        except Exception:
            pass
    return "crux: skipped (" + "; ".join(decision.reasons) + ")"


def _tty_failure(what: str) -> None:
    """Best-effort one-line terminal notice for a failure whose details only
    reached the log file. Detached hook runs (the post-commit enrich, the
    pre-push `crux run`) send stderr to the log, so a log line alone is
    invisible — this /dev/tty write is the only thing the user sees. No-op when
    there is no controlling tty (CI, an editor, an agent)."""
    try:
        import crux.post as post
        post.notify_tty(f"❌ Crux: {what} — details in {_log_file()}")
    except Exception:
        pass


def _post_failure(info: RepoInfo | None, pr: int | None, exc: CruxError,
                  log: logging.Logger, discover: bool = False,
                  dry_run: bool = False) -> bool:
    """D8: a failed run posts a loud one-line failure update, never silence.

    Best-effort and never raises. When no PR number is known yet (failures in
    stages 0-6 happen before PR discovery) and *discover* is set, the open PR
    for the branch is looked up so an existing stale card is loudly replaced.

    Under *dry_run* the failure card is printed locally and nothing is posted,
    mirroring the success path's `print(card)`: a preview must never touch the
    PR, and a *failed* preview would otherwise overwrite the real card with a
    RUN FAILED notice even though the user only asked for a preview.

    Returns True when it surfaced the failure (posted a card and notified the
    tty, or — under dry_run — printed it locally), so callers can fall back to a
    generic "see the log" notice when it could not (no PR to post onto yet).
    """
    if info is None:
        return False
    try:
        body = ""
        try:
            import crux.render as render
            fn = getattr(render, "render_failure_card", None)
            if fn is not None:
                body = fn(info, str(exc))
        except Exception:
            body = ""
        if not body:
            body = (f"{CARD_MARKER}\n## Crux · `{info.head_sha[:7]}` · "
                    f"run FAILED: {exc}")

        # A preview never touches GitHub — surface the failure locally instead.
        if dry_run:
            print(body)
            return True

        import crux.post as post
        if pr is None and discover:
            try:
                pr = post.find_pr(info)
            except Exception:
                pr = None
        if pr is None:
            return False
        post.upsert_comment(info, pr, body)
        post.set_status(info, pr, "failure", "Review failed — see the card")
        post.notify_tty(f"❌ Crux review failed for PR #{pr} — see the card")
        log.info("posted failure card to PR #%d", pr)
        return True
    except Exception:
        log.warning("could not post failure card to PR #%s", pr)
        return False


# ---------------------------------------------------------------------------
# crux run  (and preview)
# ---------------------------------------------------------------------------

def _announce_slack(cfg, info: RepoInfo, pr: int, previous, log) -> tuple[str, str]:
    """D16: post the PR to the Slack channel, or thread an update under an
    existing announcement. Best-effort; returns ('', '') when Slack is disabled
    or the call fails. previous.slack_ts is the fallback thread ts when the PR
    link has scrolled out of the scanned channel history."""
    try:
        import crux.slack as slack
        if not slack.enabled(cfg):
            return "", ""
        import crux.post as post
        title = post.pr_title_from_commits(info)
        url = f"https://github.com/{info.owner}/{info.repo}/pull/{pr}"
        prev_ts = previous.slack_ts if previous else ""
        # Falls back to the local git identity when the lookup fails, so the
        # line still says who pushed rather than dropping the credit.
        _, author = post.pr_meta(info, pr)
        channel, ts = slack.announce_pr(
            cfg, info, pr, title, url, prev_ts,
            author=slack.short_name(author or post.author_name(info)))
        if ts:
            log.info("announced PR #%d to slack channel %s", pr, channel)
            post.notify_tty(f"💬 Crux: posted PR #{pr} to Slack")
        else:
            # Slack is CONFIGURED but the announce failed (bad scope, bot not
            # in the channel, unresolvable name…). Best-effort must not mean
            # silent: say so on the terminal and point at the log, which has
            # the exact Slack error. Field-tested: a missing channels:read
            # scope was invisible until the user came asking.
            post.notify_tty(
                f"⚠️ Crux: could not post PR #{pr} to Slack "
                f"(channel {cfg.slack_channel!r}) — details in "
                "~/.cache/crux/crux.log")
        return channel, ts
    except Exception as exc:
        log.warning("slack announce failed for PR #%d: %s", pr, exc)
        return "", ""


def _align_with_pr_base(info: RepoInfo, pr: int, post, gitio, log) -> RepoInfo:
    """Re-base *info* on the branch PR *pr* actually merges into (D34).

    Also writes the answer back to `branch.<name>.cruxBase`, the D17 store, so
    the correction sticks: the next `crux preview`, the next re-created PR and
    every later run agree with GitHub instead of re-deriving the stale local
    guess. Best-effort throughout — a run must never fail over this.
    """
    try:
        base = post.pr_base(info, pr)
    except Exception as exc:  # gh missing/broken: keep the local guess
        log.warning("could not read the base of PR #%d: %s", pr, exc)
        return info
    if not base or base == info.base_branch:
        return info
    aligned = gitio.with_base(info, base)
    if aligned.base_branch != base:
        return aligned  # with_base could not resolve it; it logged why
    log.info("PR #%d targets %s, not %s — reviewing against the PR's base (D34)",
             pr, base, info.base_branch or info.default_branch)
    post.notify_tty(f"↩️ Crux: PR #{pr} targets `{base}` — reviewing this "
                    f"branch against that, not `{info.base_branch or info.default_branch}`")
    if base != info.crux_base:
        try:
            gitio.run_git(["config", f"branch.{info.branch}.cruxBase", base],
                          cwd=info.root)
        except Exception as exc:  # a config write must never fail a review
            log.warning("could not record %s as the parent of %s: %s",
                        base, info.branch, exc)
    return aligned


def _cmd_run(args: argparse.Namespace) -> int:
    log = logging.getLogger(LOG_NAME)
    info: RepoInfo | None = None
    pr: int | None = args.pr
    scoped = False  # True once the D13 allowlist check has passed
    held = contextlib.ExitStack()  # the single-flight lock of a delayed run
    try:
        import crux.config as config
        import crux.gitio as gitio
        import crux.post as post

        # 0. repo_info + D13 scope check, before any analysis or gh call.
        info = gitio.repo_info(base_ref=args.base)
        cfg = config.load(info.root)
        if _out_of_scope(info, cfg, log):
            return 0
        scoped = True

        # Acknowledge the push in the terminal the moment the detached run
        # starts (right after the git push), BEFORE the ~delay s wait for the
        # push to land. This is the line the user sees after `git push`.
        if args.delay > 0 and not args.dry_run:
            pr = pr or post.find_pr(info)
            if pr:
                post.notify_tty(
                    f"✅ Crux: push received — refreshing PR #{pr} and re-reviewing "
                    f"it in the background (starts in ~{args.delay}s)…")
            else:
                post.notify_tty(
                    f"✅ Crux: push received — creating a PR for {info.branch} and "
                    f"reviewing it in the background (starts in ~{args.delay}s)…")
        if args.delay > 0:
            # Used by the detached pre-push run so the push lands first.
            # Every push and every `gh pr create` detaches one of these, so a
            # push-then-create burst would review and announce the PR once
            # per trigger. Same trailing-edge debounce as super refresh: the
            # newest request runs, the rest stand down.
            import crux.superdebounce as superdebounce
            key = superdebounce.run_key(info.root, info.branch)
            ticket = superdebounce.claim(key)
            time.sleep(args.delay)
            if superdebounce.superseded(key, ticket):
                log.info("run for %s superseded by a newer trigger; skipping "
                         "this one", info.branch)
                return 0
            held.enter_context(superdebounce.single_flight(key, log))
            if superdebounce.superseded(key, ticket):
                log.info("run for %s superseded while waiting for the run "
                         "lock; skipping this one", info.branch)
                return 0

        import crux.cache as cache
        import crux.dag as dag
        import crux.gate as gate
        import crux.harvest.blast as blast
        import crux.harvest.defuse as defuse
        import crux.harvest.history as history
        import crux.harvest.structural as structural
        import crux.harvest.testprox as testprox
        import crux.render as render
        import crux.tiers as tiers

        # 0.5. Ensure a PR FIRST — the review card is only ever posted onto a
        # PR, so with none we do no diff and no analysis. PR creation is a
        # convenience (crux_base target, prompted or pr_auto_create); the
        # change-size gate below decides whether the PR actually gets a card.
        # Skipped for --dry-run, which prints the card locally without a PR.
        if not args.dry_run:
            if pr is None:
                pr = post.find_pr(info)
            if pr is None and not args.no_create:
                pr = post.ensure_pr(info, cfg, interactive=not args.yes)
                if pr is not None:
                    _zen_note_if_off(cfg)
            if pr is None:
                log.info("no PR for %s; skipping review", info.branch)
                post.notify_tty(f"Crux: no PR for {info.branch}; nothing to review")
                return 0
            # D34: the PR's own base is the base for everything below — the
            # diff, the commit list in the description, the card. Until now
            # info carried a LOCAL guess (cruxBase / reflog / default branch),
            # which is wrong whenever the PR was retargeted, opened by hand
            # into another branch, or auto-retargeted by GitHub when its base
            # merged. An explicit --base is the user's call and still wins.
            if not args.base:
                info = _align_with_pr_base(info, pr, post, gitio, log)
            else:
                # Deliberate (the override is the user's call), but it must not
                # be SILENT: skipping D34 also forfeits the correction that
                # would otherwise catch a base that resolved to the wrong
                # branch, so say which base is in force and what was skipped.
                log.info("--base %s given: reviewing against it and skipping "
                         "the D34 alignment with PR #%d's own base",
                         args.base, pr)
            # Keep the PR title + description in step with the commits FIRST
            # (fast, no LLM) — before the gate, so even trivial pushes refresh
            # metadata. sync_pr_metadata prints its own line when it changes.
            post.sync_pr_metadata(info, pr)
            # Running indicators for the slow harvest + LLM ahead: a pending
            # check on the PR/commit, and a terminal line. The detached run
            # keeps the controlling tty, so /dev/tty still reaches it. Neither
            # ever blocks the run.
            pr_url = f"https://github.com/{info.owner}/{info.repo}/pull/{pr}"
            post.set_status(info, pr, "pending", "Reviewing this push…")
            post.notify_tty(f"⏳ Crux: writing the review for PR #{pr} "
                            f"(this can take a few minutes)… {pr_url}")

        # 1. harvest — deterministic, no LLM spend (D6).
        hunks = gitio.diff_hunks(info)
        structural.classify(hunks, cfg)
        clusters = structural.mechanical_clusters(hunks)
        signals = defuse.extract_defs_uses(hunks)
        blast.add_blast(signals, hunks, info.root)
        history.add_history(signals, hunks, info.root, cfg)
        # Sensitivity tags must exist before the last harvest stage finalizes
        # HunkSignals.score, so the sensitive weight can contribute to it
        # (DAG numbering tie-breaks). gate.decide re-tags idempotently.
        gate.tag_sensitivity(hunks, signals, cfg)
        testprox.add_test_proximity(signals, hunks, info.root, cfg)

        # D9: THE canonical fingerprint (crux.models.fingerprint) — the same
        # function analyze._split_reused compares against, or reuse never fires.
        fingerprints = {h.id: fingerprint(h) for h in hunks}
        # Previous run: D9 annotation reuse + the saved Slack thread ts. Loaded
        # before the gate so the skip path can announce/thread to Slack too.
        # Scoped to this PR: a sibling PR on the same head branch has its own
        # Slack thread and its own base, and neither carries over.
        previous = cache.load(info, pr)

        # 2. gate — trivial change: leave the PR without a review card (post
        # nothing). The PR itself already exists; the diff just decided it is
        # not worth a card.
        decision = gate.decide(hunks, signals, cfg)
        if decision.skip:
            note = _skip_note(render, decision)
            log.info("%s", note)
            slack_channel, slack_ts = "", ""
            if args.dry_run:
                print(note)
            else:
                post.set_status(info, pr, "success",
                                "No review needed — trivial change")
                post.notify_tty(
                    f"✅ Crux: no review needed for PR #{pr} (trivial change)")
                slack_channel, slack_ts = _announce_slack(cfg, info, pr, previous, log)
            state = RunState(
                version=STATE_VERSION, branch=info.branch,
                base_sha=info.base_sha, head_sha=info.head_sha,
                pr_number=pr, fingerprints=fingerprints,
                generated_at=_now_iso(), skipped=True,
                slack_channel=slack_channel, slack_ts=slack_ts,
            )
            cache.save(info, state)
            return 0

        # 2.5. Non-trivial change: show the PR that a review is coming BEFORE
        # the slow LLM annotate step. This sticky placeholder (CARD_MARKER) is
        # replaced in place by the finished card, so the PR is never silent for
        # minutes. --no-llm renders instantly, so skip the placeholder there.
        if not args.dry_run and not args.no_llm:
            post.upsert_comment(info, pr, post.progress_card(info))
            log.info("crux: analyzing PR #%d (base %s); review card to follow",
                     pr, info.base_branch or info.default_branch)

        # 3. DAG. DagNode.reused is set by annotate() itself for nodes whose
        # previous annotation was carried over (previous loaded above), so the
        # saved state can never claim reuse that did not actually happen.
        nodes, edges = dag.build(hunks, signals, clusters)

        # 4. annotate — skipped entirely with --no-llm.
        if args.no_llm:
            annotation = Annotation(
                summary="",
                nodes={n.number: NodeAnnotation(number=n.number, title=n.title, why="")
                       for n in nodes},
            )
        else:
            import crux.analyze as analyze
            import crux.memory as memory
            intent = _load_intent(info.root)
            # D31: what Crux remembers about this repo, read into the prompt.
            remembered = memory.load(info) if cfg.memory_enabled else []
            annotation = analyze.annotate(nodes, edges, hunks, signals,
                                          intent, previous, cfg, info=info,
                                          memories=remembered)

        # 5. tiers.
        items = tiers.assign(nodes, hunks, signals, annotation, cfg)

        # 6+7. render + post. A PR is guaranteed here for a real run (ensured
        # in step 0.5, else we returned early); --dry-run prints it locally.
        card = render.render_card(info, pr, items, annotation,
                                  decision.stats, cfg=cfg)
        if args.dry_run:
            print(card)
            return 0

        post.upsert_comment(info, pr, card)
        # D38: the card just published carries a Merge button. Make sure the
        # service behind it is up before anyone reads it.
        _ensure_serving(cfg, log)
        # Secondary sticky comment: minimal integration-test steps, only when
        # the model judged the PR ships functionality worth verifying by hand.
        if annotation.integration_test:
            post.upsert_comment(
                info, pr, render.render_test_comment(info, annotation.integration_test),
                marker=TEST_MARKER)
            log.info("published integration-test steps to PR #%d", pr)
            post.notify_tty(f"🧪 Crux: published test steps for PR #{pr}")
        # Both sticky comments now exist; upgrade the PR title to the LLM's
        # concise pr_title and rewrite the description from the review's
        # summary + overview — a curated account of the branch, replacing the
        # raw commit list (D19). --no-llm leaves the annotation empty, so the
        # commit-based placeholder title and description stay.
        post.sync_pr_metadata(info, pr, title=annotation.pr_title or None,
                              summary=annotation.summary,
                              overview=annotation.overview)
        post.set_status(info, pr, "success", "Review posted")
        post.notify_tty(
            f"✅ Crux posted a review to PR #{pr} — "
            f"https://github.com/{info.owner}/{info.repo}/pull/{pr}")
        log.info("card posted to PR #%d (head %s)", pr, info.head_sha[:12])

        # D40: link the Zenhub tickets this PR closes. Two routes in, because
        # only one of them can ask anything. On a push, the answer was given to
        # the pre-push foreground and is applied here now that the PR exists;
        # this run is detached and could not have asked. On a foreground
        # `crux run` there is a terminal, so the picker is offered directly.
        # Silent either way unless [zenhub] workspace is set.
        import crux.zenlink as zenlink
        if not _zen_apply_pending(cfg, info, pr):
            _zen_offer(cfg, f"{info.owner}/{info.repo}#{pr}",
                       zenlink.pr_key(info.owner, info.repo, pr),
                       [info.branch, annotation.pr_title or ""]
                       + post._commit_subjects(info),
                       [f"{info.owner}/{info.repo}#{pr}"],
                       [(f"{info.owner}/{info.repo}", pr)])

        # D31: absorb the review's durable repo facts into the memory store,
        # and retire the ones this PR proved wrong (D36) — only after the card
        # actually posted (--dry-run returned above, so a preview never mutates
        # the store; --no-llm proposes nothing either way).
        if cfg.memory_enabled and (annotation.memories or annotation.forget_memories):
            import crux.memory as memory
            _, notes = memory.absorb(info, annotation.memories, cfg,
                                     retract=annotation.forget_memories)
            for note in notes:
                log.info("memory (D31): %s", note)

        # D16: announce the PR to Slack (or thread an update under an existing
        # message), and remember the thread ts for the next push.
        slack_channel, slack_ts = _announce_slack(cfg, info, pr, previous, log)

        state = RunState(
            version=STATE_VERSION, branch=info.branch,
            base_sha=info.base_sha, head_sha=info.head_sha,
            pr_number=pr, fingerprints=fingerprints,
            claims=annotation.claims, nodes=nodes, edges=edges,
            annotation=annotation, items=items, card=card,
            generated_at=_now_iso(), skipped=False,
            slack_channel=slack_channel, slack_ts=slack_ts,
        )
        cache.save(info, state)
        return 0

    except CruxError as exc:
        log.error("crux run failed: %s", exc)
        if not _post_failure(info, pr, exc, log,
                             discover=scoped and not args.dry_run,
                             dry_run=args.dry_run):
            # No PR to post a card onto (failure before one existed): the
            # detached run would otherwise die with nothing on the terminal.
            _tty_failure("the review run failed")
        return 0
    except Exception as exc:
        log.exception("crux run crashed")
        # D8 applies to unexpected crashes too: never leave a stale card silent.
        if not _post_failure(info, pr,
                             CruxError(f"unexpected {type(exc).__name__}: {exc}"),
                             log, discover=scoped and not args.dry_run,
                             dry_run=args.dry_run):
            _tty_failure("the review run crashed")
        return 0
    finally:
        held.close()


# ---------------------------------------------------------------------------
# crux ensure-pr
# ---------------------------------------------------------------------------

def _cmd_ensure_pr(args: argparse.Namespace) -> int:
    """Foreground half of the pre-push hook: every question the push must ask.

    Runs BEFORE the push transfers any refs, so it must never call
    `gh pr create` (the head branch does not exist on the remote yet).
    It records the answers; the detached `crux run --yes` consumes them after
    the push lands and acts on them then.

    This is the ONLY point on the push path where anything can be asked. The
    review that follows is detached, and a detached process has no controlling
    terminal to open — so the D11 base ask, the D39 head ask and the D40
    Zenhub ticket ask all live here, in that order of importance.
    """
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.config as config
        import crux.gitio as gitio

        info = gitio.repo_info()
        cfg = config.load(info.root)
        if _out_of_scope(info, cfg, log):
            return 0

        import crux.post as post
        pr = post.find_pr(info)
        _record_pr_answers(info, cfg, pr, log)
        # D40: asked LAST, after the push-critical questions, because it is the
        # least important of the three and the only one that can be answered
        # later with a single command.
        _zen_ask_prepush(info, cfg, pr)
    except CruxError as exc:
        log.error("crux ensure-pr failed: %s", exc)
    return 0


def _record_pr_answers(info, cfg, pr: int | None, log) -> None:
    """The D11 base ask and the D39 head ask — the original ensure-pr body.

    Split out so `_cmd_ensure_pr` has one exit and the D40 ticket ask can sit
    after all of it, rather than being repeated at three early returns.
    """
    import crux.post as post
    # NB: no terminal notice here. This foreground half runs BEFORE git's
    # push output, so anything printed now is buried above it and the wait
    # looks dead. All terminal progress comes from the detached run, whose
    # lines land AFTER the push output. (log.info still records to the log.)
    if pr is not None:
        log.info("PR #%d already exists for %s", pr, info.branch)
        return
    if cfg.pr_auto_create:
        # No prompt: the detached post-push run creates the PR into the
        # crux_base target automatically (pr.auto_create = true).
        log.info("pr.auto_create set; the post-push run will create a PR "
                 "for %s into %s", info.branch, post.default_base(info, cfg))
        # Still ask about the head (D39): auto_create says the PR is
        # wanted, which makes a head branch that the push will not create
        # more of a problem here, not less.
        if post.record_push_head_intent(info, cfg):
            log.info("recorded head-push intent for %s", info.branch)
        return
    base = post.record_pr_intent(info, cfg)
    if base is not None:
        log.info("recorded PR intent for %s (base %s); the post-push "
                 "run will create it", info.branch, base)
        if post.record_push_head_intent(info, cfg):
            log.info("recorded head-push intent for %s", info.branch)
    else:
        log.info("no PR will be created for %s", info.branch)


# ---------------------------------------------------------------------------
# crux enrich-commit
# ---------------------------------------------------------------------------

def _cmd_enrich_commit(args: argparse.Namespace) -> int:
    """Detached half of the post-commit hook (D27): rewrite a terse human
    commit message from the commit's diff and amend it in place. All the
    safety guards (Claude-authored, already amended, HEAD moved, staged
    changes, in-progress rebase/merge, already pushed) live in
    crux.commitmsg.enrich; this wrapper adds the D13 scope check and the
    terminal notice, and never exits nonzero."""
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.config as config
        import crux.gitio as gitio

        info = gitio.repo_info()
        cfg = config.load(info.root)
        if _out_of_scope(info, cfg, log):
            return 0

        import crux.commitmsg as commitmsg
        sha = args.sha or gitio.run_git(["rev-parse", "HEAD"],
                                        cwd=info.root).strip()
        subject = commitmsg.enrich(info, cfg, sha)
        if subject:
            import crux.post as post
            post.notify_tty(f"✍️ Crux: expanded the commit message — {subject}")
    except NotLoggedInError as exc:
        # The post-commit run is detached (its stdout/stderr go to the log
        # file), so a plain log line is invisible. notify_tty writes straight
        # to /dev/tty, so the "not logged in" notice reaches the terminal.
        import crux.post as post
        post.notify_tty(f"⚠️  Crux: {exc}")
        log.error("crux enrich-commit failed: %s", exc)
    except CruxError as exc:
        log.error("crux enrich-commit failed: %s", exc)
        _tty_failure("could not expand the commit message")
    return 0


# ---------------------------------------------------------------------------
# crux ensure-enriched
# ---------------------------------------------------------------------------

def _read_push_refs() -> list[tuple[str, str]] | None:
    """Parse pre-push hook stdin: '<local ref> <local sha> <remote ref>
    <remote sha>' per line -> (local_ref, local_sha) pairs. None when stdin
    is empty/unusable (manual invocation) => check the current branch."""
    try:
        lines = sys.stdin.read().splitlines()
    except OSError:
        return None
    refs = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            refs.append((parts[0], parts[1]))
    return refs or None


def _cmd_ensure_enriched(args: argparse.Namespace) -> int:
    """Foreground half of the pre-push hook for D28.

    Exit 1 is a deliberate signal — the shas git resolved for this push are
    stale (messages were just enriched, or a detached enrich-commit landed
    mid-push) and the hook must stop the push; the user pushes again and
    everything goes up enriched. Every failure path exits 0: a broken claude
    must never trap pushes.
    """
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.config as config
        import crux.gitio as gitio

        info = gitio.repo_info()
        cfg = config.load(info.root)
        if _out_of_scope(info, cfg, log):
            return 0

        import crux.commitmsg as commitmsg
        import crux.post as post
        refs = _read_push_refs() if args.hook else None
        stale = commitmsg.ensure_enriched(info, cfg, refs=refs,
                                          notify=post.notify_tty)
        if stale:
            post.notify_tty("✍️ Crux: commit messages were expanded — this "
                            "push stops here; run `git push` again to send "
                            "the updated commits")
            return 1
    except CruxError as exc:
        log.error("crux ensure-enriched failed: %s", exc)
        _tty_failure("the pre-push commit-message check failed")
    return 0


# ---------------------------------------------------------------------------
# crux merge
# ---------------------------------------------------------------------------

def _cmd_merge(args: argparse.Namespace) -> int:
    """D38: approve this branch's PR as you, then merge it.

    The terminal twin of the card's Merge button, and deliberately the same
    code beneath (`superact.merge_pr`): the two doors must not drift into two
    policies. A branch inside a super PR is sent to `crux super merge` instead
    — landing one member of a cross-repo feature on its own is the half-applied
    state the bundle exists to prevent.
    """
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.config as config
        import crux.gitio as gitio
        import crux.post as post
        import crux.superact as superact

        info = gitio.repo_info()
        cfg = config.load(info.root)
        slug = f"{info.owner}/{info.repo}"

        number = args.pr or post.find_pr(info)
        if number is None:
            print(f"❌ no open PR for {info.branch} in {slug}")
            return 1

        import crux.bundle as bundle_store
        found = bundle_store.find_by_branch(info.owner, info.repo, info.branch)
        if found is not None and not found.closed:
            print(f"⛔ {slug}#{number} is part of Super PR #{found.number}, "
                  f"which lands as one change.")
            print(f"   Merge the whole bundle: `crux super merge {found.number}`")
            return 1

        if not args.yes:
            verb = "Merge" if args.admin else "Approve and merge"
            answer = _answer(f"{verb} {slug}#{number}? [y/N] ")
            if answer is None:
                _no_answer("nothing merged",
                           "Pass --yes to merge without asking.")
                return 1
            if answer.lower() not in ("y", "yes"):
                print("nothing merged")
                return 1

        action = superact.admin_merge_pr if args.admin else superact.merge_pr
        try:
            merged, detail = action(slug, number, method=args.method)
        except superact.ActError as exc:
            print(f"\n⛔ {exc}")
            return 1
        print(f"{'✅' if merged else '❌'} {detail}")
        if merged:
            import crux.zenlink as zenlink
            _zen_report_closed(cfg, zenlink.pr_key(info.owner, info.repo, number))
        return 0 if merged else 1
    except CruxError as exc:
        log.error("crux merge failed: %s", exc)
        print(f"❌ crux merge failed: {exc}")
        return 1


# ---------------------------------------------------------------------------
# crux serve
# ---------------------------------------------------------------------------

def _cmd_serve(args: argparse.Namespace) -> int:
    """D38: run the loopback service the brief's buttons call.

    Foreground on purpose. It is a thing you either have running or do not, and
    a daemon that silently died is worse than a link that plainly refuses to
    connect — the card already tells the reader it needs this.

    `--restart` and `--stop` are the two exceptions, and they exist for the one
    process that outlives the command that started it: an edit to Crux's own
    source is live everywhere else and invisible here, because the running
    service still answers with the code it started with.
    """
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.config as config
        import crux.serve as serve
        try:
            import crux.gitio as gitio
            root = gitio.run_git(["rev-parse", "--show-toplevel"])
        except CruxError:
            root = os.getcwd()
        cfg = config.load(root)
        port = args.port or cfg.serve_port or 8787
        if args.stop:
            return _serve_stop(serve, port)
        if args.restart:
            return _serve_restart(serve, cfg, port, log)
        print(f"🦸 Crux is listening on http://127.0.0.1:{port} — the Merge "
              f"and Close buttons on your super PR briefs now work.")
        print("   They act as you: each PR is approved in your name, then "
              "merged. Ctrl-C to stop.")
        serve.serve(cfg, port)
        return 0
    except OSError as exc:
        log.error("crux serve failed: %s", exc)
        print(f"❌ crux serve could not listen: {exc}")
        return 1
    except CruxError as exc:
        log.error("crux serve failed: %s", exc)
        print(f"❌ crux serve failed: {exc}")
        return 1


# Only ever true once per machine: the service running right now was started
# before /health carried a pid, so there is nothing to signal that is certainly
# it. Says so, and says what to do — the alternative is a flat "would not stop"
# on the one restart that has a specific, one-time cause.
_SERVE_TOO_OLD = (
    "⚠️  crux serve on 127.0.0.1:{port} is an older Crux and does not report "
    "its pid, so it cannot be stopped safely by port alone.\n"
    "   Stop it by hand this once — `pkill -f 'crux serve'` — then "
    "`crux serve --restart` works from here on.")


def _serve_stop(serve, port: int) -> int:
    """`crux serve --stop`: leave the port free. Says what it found either way,
    because "nothing was running" and "I stopped it" look identical afterwards
    and only one of them means the thing you just edited was ever live."""
    outcome = serve.stop(port)
    if outcome == "stopped":
        print(f"🛑 crux serve on 127.0.0.1:{port} stopped.")
        return 0
    if outcome == "free":
        print(f"crux serve was not running on 127.0.0.1:{port}.")
        return 0
    if outcome == "other":
        print(f"⚠️  port {port} is held by something that is not crux — "
              f"leaving it alone.")
        return 1
    if outcome == "unknown":
        print(_SERVE_TOO_OLD.format(port=port))
        return 1
    print(f"❌ crux serve on 127.0.0.1:{port} would not stop.")
    return 1


def _serve_restart(serve, cfg, port: int, log: logging.Logger) -> int:
    """`crux serve --restart`: stop the old service, leave a new detached one
    behind. The pid is printed because the whole point is that the new process
    is a different one from the code you were unknowingly still running."""
    outcome = serve.restart(cfg, port, log)
    if outcome in ("restarted", "started"):
        pid = serve.service_pid(port)
        was = "restarted" if outcome == "restarted" else "started"
        print(f"🦸 crux serve {was} on http://127.0.0.1:{port}"
              f"{f' (pid {pid})' if pid else ''} — detached, running the code "
              f"that is on disk now.")
        return 0
    if outcome == "other":
        print(f"⚠️  port {port} is held by something that is not crux — "
              f"leaving it alone.")
        return 1
    if outcome == "unknown":
        print(_SERVE_TOO_OLD.format(port=port))
        return 1
    print(f"❌ crux serve could not be restarted on 127.0.0.1:{port} — "
          f"see {_log_file()}")
    return 1


# ---------------------------------------------------------------------------
# crux prs
# ---------------------------------------------------------------------------

def _cmd_prs(args: argparse.Namespace) -> int:
    """List open PRs across one repo, selected repos, or all repos, one
    parallel `gh pr list` per repo. Read-only, interactive-only (never runs
    from a git hook), so unlike the hook-driven commands it exits 1 when it
    could list nothing — scripts can rely on the exit code."""
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.config as config
        import crux.prs as prs

        # Layered config as everywhere else; outside a repo only the global
        # file applies (a missing <cwd>/crux.toml is a no-op overlay).
        try:
            import crux.gitio as gitio
            root = gitio.run_git(["rev-parse", "--show-toplevel"])
        except CruxError:
            root = os.getcwd()
        cfg = config.load(root)

        jobs = args.jobs if args.jobs is not None else cfg.prs_jobs
        if jobs <= 0:
            jobs = prs.auto_jobs()  # default: max parallel tasks (hardware)

        repos, problems = prs.resolve_repos(args.repos, cfg)
        for problem in problems:
            print(f"⚠️  {problem}")
        if not repos:
            return 1
        log.info("prs: scanning %d repo(s) with %d parallel task(s)",
                 len(repos), min(jobs, len(repos)))
        results = prs.fetch_open_prs(repos, jobs)
        print(prs.render_prs(results))
        return 0 if any(not isinstance(v, str) for v in results.values()) else 1
    except CruxError as exc:
        log.error("crux prs failed: %s", exc)
        print(f"❌ crux prs failed: {exc}")
        return 1


# ---------------------------------------------------------------------------
# crux memory
# ---------------------------------------------------------------------------

def _confirm_super_prs(plan: list[tuple[Candidate, str]], yes: bool = False,
                       ) -> tuple[bool, str]:
    """Show where each new PR would land, and ask (D37). Returns (go, base).

    The super flow asks its OWN question here rather than letting each push
    fall through to the single-repo D11 ask ("Create a PR for <branch>?"),
    which names a branch but never the repo it lives in — unreadable when the
    same branch name exists in four repos, which is the normal case for a
    feature that spans them. One question, every repo and its base spelled out,
    before anything is pushed.

    The default base per repo is the branch that one was created from, and it
    is only ever a default: typing a branch name here redirects every new PR to
    it. The returned base is "" when the shown defaults stand.

    *yes*, or no terminal to ask on, prints the plan and proceeds with those
    defaults: the numbers were already chosen deliberately.
    """
    if not plan:
        return True, ""
    count = len(plan)
    prs = "PR" if count == 1 else "PRs"
    width = max(len(cand.slug) for cand, _ in plan)
    branch_width = max(len(cand.branch) for cand, _ in plan)
    print(f"\nNo PR yet for {count} of these — one will be opened in each "
          f"repo, into the branch it came from:\n")
    for cand, base in plan:
        target = base or "?"
        print(f"  {cand.slug.ljust(width)}  "
              f"{cand.branch.ljust(branch_width)} → {target}")
    print()
    if yes or not sys.stdin.isatty():
        return True, ""
    answer = _answer(f"Open {count} {prs}? [Y/n/other-base-branch] ")
    if answer is None:
        # A terminal that hit end-of-input (Ctrl-D) is not a yes. Opening PRs
        # is outward-facing, so silence declines rather than taking the default.
        _no_answer("nothing opened", "Re-run and answer, or pass --yes.")
        return False, ""
    if answer.lower() in ("n", "no"):
        return False, ""
    if answer.lower() in ("", "y", "yes"):
        return True, ""
    print(f"→ opening all {count} {prs} into {answer} instead")
    return True, answer


def _plural(count: int, noun: str) -> str:
    """"1 pull request", "2 pull requests"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _answer(prompt: str) -> str | None:
    """input(), except that a stdin with nothing more to give is None.

    Every question here is asked of whatever stdin the command was handed, and
    that is not always a person: `</dev/null`, a pipe, CI, an agent's tool call.
    There input() raises EOFError, which reached main()'s catch-all as
    "`super` crashed" — a crash report for what is really "nobody answered".
    None lets each caller say what silence means for it, which is always the
    cautious thing: nothing picked, nothing opened, nothing merged.
    """
    try:
        return input(prompt).strip()
    except EOFError:
        print()     # the prompt left the cursor mid-line
        return None


def _dry_run_note(bundle) -> None:
    """What a `--dry-run` of new/add/remove would have done, and did not."""
    print(f"\n🔎 Dry run — Super PR #{bundle.number} would hold "
          f"{_plural(len(bundle.members), 'pull request')}. Nothing was "
          f"saved, pushed or opened.")


def _no_answer(what: str, instead: str) -> None:
    """The one wording for a question nobody was there to answer."""
    print(f"{what} — no answer on stdin. {instead}")


def _pick_candidates(cands: list["Candidate"], pick: list[str], heading: str,
                     command: str = "crux super new",
                     ) -> tuple[list["Candidate"] | None, int]:
    """The numbered picker, shared by `super new` and `super add`.

    Returns (picked, exit_code) — picked is None when nothing was chosen, and
    the code is what the command should exit with. One implementation because
    adding to a bundle and creating one ask the same question of the same list;
    only the sentence above it differs. *command* is how to pass the numbers
    instead, named when there is no one at the keyboard to type them.
    """
    import crux.candidates as candidates
    if pick:
        selection = ",".join(pick)
    else:
        print(heading)
        print(candidates.render(cands))
        print()
        answer = _answer("Which ones? (e.g. 1,3-5, or blank to cancel): ")
        if answer is None:
            _no_answer("nothing selected",
                       f"Pass the numbers instead: `{command} 1 3-5`")
            return None, 1
        selection = answer
    if not selection:
        print("nothing selected")
        return None, 1

    indices, bad = candidates.parse_selection(selection, len(cands))
    for problem in bad:
        print(f"⚠️  {problem}")
    if not indices:
        return None, 1
    return [cands[i] for i in indices], 0


def _pick_members(bundle, pick: list[str]) -> tuple[list | None, int]:
    """The remove picker: the bundle's OWN members, numbered.

    A different list from `_pick_candidates` on purpose — removing chooses from
    what is in the bundle, adding from what is not, and one list showing both
    would make the numbers mean two things at once.
    """
    import crux.candidates as candidates
    import crux.superpr as superpr
    if pick:
        selection = ",".join(pick)
    else:
        print(f"\nPull requests in super PR #{bundle.number}:\n")
        print(superpr.render_members(bundle))
        print()
        answer = _answer(
            "Which ones to remove? (e.g. 1,3-5, or blank to cancel): ")
        if answer is None:
            _no_answer("nothing selected", f"Pass the numbers instead: "
                       f"`crux super remove {bundle.number} 2`")
            return None, 1
        selection = answer
    if not selection:
        print("nothing selected")
        return None, 1

    indices, bad = candidates.parse_selection(selection, len(bundle.members))
    for problem in bad:
        print(f"⚠️  {problem}")
    if not indices:
        return None, 1
    return [bundle.members[i] for i in indices], 0


def _cmd_super(args: argparse.Namespace) -> int:
    """D37: super PRs — one brief and one merge across a group of repos.

    Interactive-only like `crux prs` and `crux memory` (never runs from a
    hook), so it may exit 1: an unknown group, an empty selection, a bundle
    that would not merge. Every verb shares crux/superpr.py with the plugin,
    so the CLI here is argument parsing and printing, nothing more.
    """
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.bundle as bundle_store
        import crux.candidates as candidates
        import crux.config as config
        import crux.superpr as superpr

        try:
            import crux.gitio as gitio
            root = gitio.run_git(["rev-parse", "--show-toplevel"])
        except CruxError:
            root = os.getcwd()
        cfg = config.load(root)
        action = getattr(args, "saction", None) or "list"
        # D40: set when this run CREATED the bundle, so the Zenhub picker is
        # offered once the brief exists — the ticket note goes on the brief,
        # which `new` has not filed yet when it hands over to `refresh`.
        zen_new: int | None = None
        # A dry run of new/add/remove previews a bundle that was never saved,
        # so `refresh` must brief THIS copy — looking the number up would find
        # nothing (new) or the unchanged original (add/remove).
        dry_run = bool(getattr(args, "dry_run", False))
        preview = None

        if action == "list":
            print(superpr.render_list(bundle_store.load_all()))
            return 0

        if action == "show":
            found = bundle_store.hydrate(args.number, cfg)
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1
            print(superpr.render_show(found, cfg))
            return 0

        if action == "order":
            found = bundle_store.hydrate(args.number, cfg)
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1
            # D41. Nothing to change is a question, not an error: show the plan
            # and the command that would pin it, ready to edit.
            if not args.refs and not args.unpin and args.method is None:
                print(superpr.render_landing_plan(found, cfg))
                return 0
            found = superpr.set_landing(found, refs=args.refs, unpin=args.unpin,
                                        method=args.method)
            print(superpr.render_landing_plan(found, cfg))
            if args.no_brief:
                print(f"\n   brief not updated — run "
                      f"`crux super refresh {found.number}`")
                return 0
            url, problems = superpr.republish(found, cfg)
            for problem in problems:
                print(f"⚠️  {problem}")
            if url:
                print(f"\n✅ Brief updated — {url}")
            return 0

        if action == "new":
            cands, problems = candidates.gather(cfg, repo_root=root)
            for problem in problems:
                print(f"⚠️  {problem}")
            if not cands:
                print(candidates.render(cands))
                return 1

            picked, code = _pick_candidates(
                cands, args.pick, "\nOpen PRs and branches you can bundle:\n")
            if picked is None:
                return code

            # A dry run opens no PRs, so there is nothing to ask about.
            go, base = (True, "") if dry_run else _confirm_super_prs(
                superpr.pr_plan(picked, cfg), yes=args.yes or bool(args.base))
            if not go:
                print("nothing created")
                return 1
            new, issues = superpr.create(cfg, picked, name=args.name or "",
                                         progress=lambda line: print(f"  ✅ {line}"),
                                         base=args.base or base,
                                         dry_run=dry_run)
            for problem in issues:
                print(f"⚠️  {problem}")
            if dry_run:
                # Nothing exists to link tickets to, so no Zenhub offer either.
                _dry_run_note(new)
                if args.no_brief:
                    return 0
                preview = new
            else:
                print(f"\n✅ Super PR #{new.number} created with "
                      f"{len(new.members)} pull requests")
                if args.no_brief:
                    print(f"   brief not written — run "
                          f"`crux super refresh {new.number}`")
                    # No brief yet, so the ticket note can only go on the
                    # member PRs. Offering anyway beats making the user come
                    # back: the note lands on the brief on the next refresh.
                    _zen_offer_bundle(cfg, new)
                    return 0
                zen_new = new.number
                args.number = new.number
            action = "refresh"

        if action == "add":
            found = bundle_store.hydrate(args.number, cfg)
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1
            cands, problems = candidates.gather(cfg, repo_root=root)
            for problem in problems:
                print(f"⚠️  {problem}")
            if not cands:
                # gather already drops every PR an open bundle holds, so an
                # empty list here usually means "there is nothing left to add",
                # not "nothing exists" — say which.
                print(f"nothing left to add to super PR #{found.number} — "
                      f"every PR in scope is already bundled")
                return 1

            picked, code = _pick_candidates(
                cands, args.pick,
                f"\nOpen PRs and branches you can add to super PR "
                f"#{found.number}:\n",
                command=f"crux super add {found.number}")
            if picked is None:
                return code

            go, chosen_base = (True, "") if dry_run else _confirm_super_prs(
                superpr.pr_plan(picked, cfg), yes=args.yes or bool(args.base))
            if not go:
                print("nothing added")
                return 1
            found, issues = superpr.add(
                cfg, found, picked,
                progress=lambda line: print(f"  ✅ {line}"),
                base=args.base or chosen_base, dry_run=dry_run)
            for problem in issues:
                print(f"⚠️  {problem}")
            if dry_run:
                _dry_run_note(found)
                preview = found
            else:
                print(f"\n✅ Super PR #{found.number} now holds "
                      f"{_plural(len(found.members), 'pull request')}")
            if args.no_brief:
                if not dry_run:
                    print(f"   brief not updated — run "
                          f"`crux super refresh {found.number}`")
                return 0
            action = "refresh"

        if action == "remove":
            found = bundle_store.hydrate(args.number, cfg)
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1

            picked, code = _pick_members(found, args.pick)
            if picked is None:
                return code

            found, issues = superpr.remove(found, picked, dry_run=dry_run)
            for problem in issues:
                print(f"⚠️  {problem}")
            for member in picked:
                if any(m.pr == member.pr and m.repo == member.repo
                       for m in found.members):
                    continue
                # Said plainly, because "removed" reads like something happened
                # to the PR, and nothing did.
                ref = f"{member.owner}/{member.repo}#{member.pr}"
                if dry_run:
                    print(f"  🔎 {ref} would be detached")
                else:
                    print(f"  ✅ {ref} detached — the pull request itself is "
                          f"untouched")
            if dry_run:
                _dry_run_note(found)
                preview = found
            else:
                print(f"\n✅ Super PR #{found.number} now holds "
                      f"{_plural(len(found.members), 'pull request')}")
            if args.no_brief:
                if not dry_run:
                    print(f"   brief not updated — run "
                          f"`crux super refresh {found.number}`")
                return 0
            action = "refresh"

        if action == "refresh":
            found = (preview if preview is not None
                     else bundle_store.hydrate(args.number, cfg))
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1
            number = found.number
            delay = getattr(args, "delay", 0)

            def _run_refresh() -> int:
                print(f"⏳ Analyzing {len(found.members)} PRs as one change "
                      f"(a single review pass)…")
                card, url, problems = superpr.refresh(
                    found, cfg, repo_root=root, no_llm=args.no_llm,
                    publish=not args.dry_run)
                for problem in problems:
                    print(f"⚠️  {problem}")
                if args.dry_run:
                    print()
                    print(card)
                    return 0
                print(f"✅ Brief published — {url}")
                return 0

            # A manual `crux super refresh N` (no delay) runs now — the person
            # asked for it. Only the DETACHED, delayed run from the pre-push
            # hook is debounced: a super PR fans over N repos, so one
            # coordinated update fires this hook N times for the SAME super PR,
            # and without coalescing that is N concurrent LLM passes racing to
            # write one brief. Trailing-edge debounce + single-flight collapses
            # the burst to a single run on the final state (see superdebounce).
            if delay <= 0:
                code = _run_refresh()
                if code == 0 and zen_new is not None:
                    fresh = bundle_store.hydrate(zen_new, cfg) or found
                    _zen_offer_bundle(cfg, fresh)
                return code

            import crux.superdebounce as superdebounce
            import crux.post as post
            ticket = superdebounce.claim(number)
            post.notify_tty(
                f"✅ Crux: push received — re-briefing Super PR "
                f"#{number} in the background (starts in ~{delay}s)…")
            time.sleep(delay)
            # A newer push during the wait owns the trailing run; stand down.
            if superdebounce.superseded(number, ticket):
                log.info("super refresh #%d superseded by a newer push; "
                         "skipping this one", number)
                print(f"↩️  Super PR #{number}: a newer push is already "
                      f"refreshing it — skipping this one")
                return 0
            # Serialize against any in-progress refresh of the same super PR,
            # then re-check: a push may have landed while we waited for the
            # lock, and the newest state must be the one that runs.
            with superdebounce.single_flight(number, log):
                if superdebounce.superseded(number, ticket):
                    log.info("super refresh #%d superseded while waiting for "
                             "the run lock; skipping this one", number)
                    print(f"↩️  Super PR #{number}: superseded while waiting "
                          f"— skipping this one")
                    return 0
                return _run_refresh()

        if action == "merge":
            found = bundle_store.hydrate(args.number, cfg)
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1
            if not args.yes:
                print(superpr.render_show(found, cfg))
                verb = ("Merge all" if args.admin
                        else "Approve and merge all")
                answer = _answer(f"\n{verb} {len(found.members)} PRs? [y/N] ")
                if answer is None:
                    _no_answer("nothing merged",
                               "Pass --yes to merge without asking.")
                    return 1
                if answer.lower() not in ("y", "yes"):
                    print("nothing merged")
                    return 1
            # D38: the terminal lands a bundle the same way the brief's button
            # does — approving each PR as you first, and refusing outright when
            # any of it is yours. Two doors, one rule; the explanation is the
            # same sentence in both places.
            import crux.superact as superact
            try:
                # No --method means "" — the bundle's own, then
                # [super] merge_method, then squash (D41). Only a method typed
                # here outranks the one the bundle carries.
                method = args.method or ""
                if args.admin:
                    results, line, issues = superact.admin_merge(
                        found, cfg, method=method)
                else:
                    results, line, issues = superact.merge(found, cfg,
                                                           method=method)
            except superact.ActError as exc:
                print(f"\n⛔ {exc}")
                print("\n   Crux can ask in Slack for you: "
                      f"`crux super ask {found.number}`")
                return 1
            for problem in issues:
                print(f"⚠️  {problem}")
            print()
            for member in results:
                mark = "✅" if member.state == "merged" else "❌"
                tail = f" — {member.error}" if member.error else ""
                print(f"  {mark} {member.owner}/{member.repo}#{member.pr}{tail}")
            print(f"\n{line}")
            import crux.zenlink as zenlink
            _zen_report_closed(cfg, zenlink.bundle_key(found.number))
            return 0 if all(m.state == "merged" for m in results) else 1

        if action == "checkout":
            found = bundle_store.hydrate(args.number, cfg)
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1
            import crux.superact as superact
            print(f"⏳ Putting {len(found.members)} repos on their branches…\n")
            results = superact.checkout(found, cfg, repo_root=root)
            for item in results:
                print(f"  {item.mark} {item.slug} — {item.detail}")
            ready = sum(1 for r in results if r.state == "ready")
            print(f"\n{ready} of {len(results)} repos ready")
            if found.test_steps:
                print("\n🧪 How to verify this")
                for i, step in enumerate(found.test_steps, 1):
                    print(f"  {i}. {step}")
            return 0 if ready == len(results) else 1

        if action == "ask":
            found = bundle_store.hydrate(args.number, cfg)
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1
            import crux.superact as superact
            login = superact.actor_login()
            print(superact.ask_for_merge(found, cfg, login,
                                         superact.authored_by(found, login)))
            return 0

        if action == "close":
            found = bundle_store.hydrate(args.number, cfg)
            if found is None:
                print(f"❌ no super PR #{args.number} here, and no brief for "
                      f"it in {cfg.super_home or '[super] home (unset)'}")
                return 1
            import crux.superact as superact
            problems = superact.close(found, cfg, with_prs=args.prs)
            for problem in problems:
                print(f"⚠️  {problem}")
            extra = f" and {len(found.members)} PRs" if args.prs else ""
            print(f"✅ closed the brief for super PR #{found.number}{extra}")
            return 0

        print(f"❌ unknown action {action!r}")
        return 1

    except CruxError as exc:
        log.error("crux super failed: %s", exc)
        print(f"❌ crux super failed: {exc}")
        return 1


# ---------------------------------------------------------------------------
# crux zenhub (D40)
# ---------------------------------------------------------------------------

def _zen_note_targets(bundle) -> list[tuple[str, int]]:
    """Where a bundle's ticket note goes: the brief, then every member PR.

    The brief first because that is what a reader of a super PR opens; the
    member PRs because a reader who arrives at one repo's PR has no reason to
    know a brief exists.
    """
    targets: list[tuple[str, int]] = []
    if bundle.home and bundle.issue:
        targets.append((bundle.home, int(bundle.issue)))
    targets += [(f"{m.owner}/{m.repo}", m.pr) for m in bundle.members if m.pr]
    return targets


def _zen_offer_bundle(cfg, bundle) -> None:
    """Offer the ticket picker for a freshly created super PR.

    A bundle's tickets hang off the BUNDLE, not off any one member: they close
    once every member PR has landed, because a cross-repo ticket is not done
    while half its repos are unmerged.
    """
    import crux.zenlink as zenlink
    _zen_offer(
        cfg, f"super PR #{bundle.number}", zenlink.bundle_key(bundle.number),
        [bundle.name] + [m.branch for m in bundle.members],
        [f"{m.owner}/{m.repo}#{m.pr}" for m in bundle.members if m.pr],
        _zen_note_targets(bundle))


def _zen_pick(cfg, hints: list[str], what: str, explicit: list[str],
              default_slug: str, show_all: bool = False):
    """The tickets to link: the ones named with --issue, or the ones chosen."""
    import crux.zenhub as zenhub
    import crux.zenlink as zenlink

    if explicit:
        chosen = []
        for ref in explicit:
            slug, _, number = ref.rpartition("#")
            slug = slug or default_slug
            if not number.isdigit():
                print(f"⚠️  {ref!r} is not an issue reference "
                      f"(try 29, #29, or owner/repo#29)")
                continue
            ticket = zenhub.issue_by_ref(cfg, slug, int(number))
            if ticket is None:
                print(f"⚠️  no Zenhub ticket {slug}#{number} in this workspace")
                continue
            if ticket.planning:
                print(f"⚠️  {slug}#{number} is a {ticket.kind or 'planning item'}"
                      f" — Crux only links tasks, bugs and features")
                continue
            chosen.append(ticket)
        return chosen

    tickets = zenhub.open_issues(cfg)
    if not tickets:
        print("no open Zenhub tickets in this workspace to link")
        return []
    scored = zenlink.rank(tickets, *hints)
    limit = len(scored) if show_all else zenlink.PICK_LIMIT
    return zenlink.ask(cfg, scored, what, limit=limit)


def _zen_note_if_off(cfg) -> None:
    """D40: when Crux opens a PR and Zenhub is not configured, say so once.

    A note, not a warning — Zenhub is optional. Printed only at PR creation, so
    it appears once per PR rather than on every push, and `[zenhub] ask = false`
    silences it for anyone who does not use Zenhub.
    """
    import crux.post as post
    import crux.zenhub as zenhub
    if not cfg.zenhub_ask or zenhub.enabled(cfg):
        return
    post.notify_tty(f"🎫 Crux note: no Zenhub ticket linked — "
                    f"{zenhub.why_disabled(cfg)} (Optional; `[zenhub] ask = "
                    f"false` hides this note.)")


def _zen_ask_prepush(info, cfg, pr: int | None) -> None:
    """D40: ask which Zenhub tickets this push closes, while a terminal exists.

    The pre-push foreground is the only place on the push path a question can
    be asked at all: the review that follows runs detached, and a detached
    process cannot open /dev/tty by name, so a picker offered there would
    silently answer itself "none" on every push. Same shape as the D11 base ask
    and the D39 head ask — the answer is recorded here and applied by the
    detached run once the PR exists (it may not yet: on a first push the PR is
    created by that run, which is exactly why the choice is held rather than
    attached now).

    Gated hard, in the order that costs least: an ordinary push where Zenhub is
    off, the ask is disabled, a link is already recorded, a choice is already
    waiting, or there is no terminal all return BEFORE the ticket list is
    fetched — so nobody pays a network round trip for a question that was never
    going to be asked.
    """
    import crux.post as post
    import crux.zenhub as zenhub
    import crux.zenlink as zenlink
    log = logging.getLogger(LOG_NAME)
    if not zenhub.enabled(cfg) or not cfg.zenhub_ask:
        return
    if zenlink.pending(info):
        return  # answered on an earlier push and not yet applied
    slug = f"{info.owner}/{info.repo}"
    if pr is not None and zenlink.get(zenlink.pr_key(info.owner, info.repo, pr)):
        return
    if not post.tty_available():
        log.info("zenhub: no terminal on this push; link with "
                 "`crux zenhub link` when you have one")
        return
    what = f"{slug}#{pr}" if pr is not None else f"{slug} ({info.branch})"
    try:
        picked = _zen_pick(cfg, [info.branch] + post._commit_subjects(info),
                           what, [], slug)
    except CruxError as exc:
        # The push is the thing happening here. A ticket list that could not be
        # fetched must not stand between the user and it.
        log.warning("zenhub: could not offer tickets for %s (%s)",
                    info.branch, exc)
        return
    if picked:
        zenlink.record_pending(info, picked)
        log.info("zenhub: holding %d ticket(s) for %s until its PR exists",
                 len(picked), info.branch)


def _zen_apply_pending(cfg, info, pr: int) -> bool:
    """Attach the tickets answered for at pre-push. True when there were some.

    Runs in the detached review, where there is no terminal and nothing to ask
    — the asking already happened. Never raises: the review is posted by this
    point, and a link that failed is one command to redo.
    """
    import crux.post as post
    import crux.zenhub as zenhub
    import crux.zenlink as zenlink
    log = logging.getLogger(LOG_NAME)
    try:
        picked = zenlink.consume_pending(info)
        if not picked:
            return False
        if not zenhub.enabled(cfg):
            log.info("zenhub: a ticket choice was waiting for %s but Zenhub is "
                     "no longer configured; dropping it", info.branch)
            return True
        slug = f"{info.owner}/{info.repo}"
        for problem in zenlink.attach(cfg, zenlink.pr_key(info.owner, info.repo, pr),
                                      picked, [f"{slug}#{pr}"], [(slug, pr)]):
            log.warning("zenhub: %s", problem)
        names = ", ".join(zenhub.describe(t) for t in picked)
        post.notify_tty(f"🎫 Crux: {slug}#{pr} will close {names}")
        log.info("zenhub: linked %s#%d to %s", slug, pr, names)
        return True
    except Exception as exc:  # noqa: BLE001 — the review is already posted
        log.warning("zenhub: could not apply the pre-push ticket choice for "
                    "%s (%s)", info.branch, exc)
        return True


def _zen_offer(cfg, what: str, key: str, hints: list[str], prs: list[str],
               note_on: list[tuple[str, int]]) -> None:
    """Offer the picker after a review or a `super new`, and link what is picked.

    Silent and skipped unless Zenhub is configured AND `[zenhub] ask` is on AND
    this link does not already exist AND there is a terminal to ask on. Every
    one of those is a way to have the feature without it being in the way,
    which is the whole reason it is opt-in.
    """
    import crux.zenhub as zenhub
    import crux.zenlink as zenlink
    if not zenhub.enabled(cfg) or not cfg.zenhub_ask:
        return
    if zenlink.get(key) is not None:
        return
    try:
        default_slug = prs[0].split("#")[0] if prs else ""
        picked = _zen_pick(cfg, hints, what, [], default_slug)
        if not picked:
            return
        for problem in zenlink.attach(cfg, key, picked, prs, note_on):
            print(f"⚠️  {problem}")
        print(f"🎫 linked to {what}: "
              + ", ".join(zenhub.describe(t) for t in picked))
    except CruxError as exc:
        # A review that worked must not report failure because a ticket did not
        # link. The link is recoverable in one command; the review is not.
        logging.getLogger(LOG_NAME).warning("zenhub: could not link %s (%s)",
                                            key, exc)


def _zen_report_closed(cfg, key: str) -> None:
    """Say on the terminal which Zenhub tickets the merge just retired.

    Reporting only. The closing itself happens in `crux/superact.py`, which is
    the single authority every merge goes through — the terminal AND the
    brief's Merge button — so the two doors cannot drift into two policies.
    """
    import crux.zenhub as zenhub
    import crux.zenlink as zenlink
    if not zenhub.enabled(cfg):
        return
    link = zenlink.get(key)
    if link is None or not link.closed or not link.tickets:
        return
    print("🎫 closed in Zenhub: "
          + ", ".join(zenhub.describe(t) for t in link.tickets))


def _cmd_zenhub(args: argparse.Namespace) -> int:
    """D40: link Zenhub tickets to the PRs that close them, and close them.

    Interactive-only, like `crux super` and `crux memory` — never runs from a
    hook, so it may exit 1 and may ask questions.
    """
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.bundle as bundle_store
        import crux.config as config
        import crux.zenhub as zenhub
        import crux.zenlink as zenlink

        try:
            import crux.gitio as gitio
            root = gitio.run_git(["rev-parse", "--show-toplevel"])
        except CruxError:
            root = os.getcwd()
        cfg = config.load(root)
        action = getattr(args, "zaction", None) or "status"

        if action == "setup":
            return _zen_setup(cfg)

        if action == "status":
            if not zenhub.enabled(cfg):
                print(f"🎫 Zenhub is off — {zenhub.why_disabled(cfg)}")
                return 0
            wid = zenhub.workspace_id(cfg)
            print(f"🎫 Zenhub workspace {cfg.zenhub_workspace!r}"
                  + (f" (id {wid})" if wid else " — could not be resolved"))
            names = [name for _, name in zenhub.pipelines(cfg)]
            print(f"   pipelines: {', '.join(names) if names else 'none readable'}")
            done = cfg.zenhub_done_pipeline or "(none — close without moving)"
            verb = "close tickets" if cfg.zenhub_close_on_merge else "leave tickets open"
            print(f"   on merge:  {verb}, move to {done}")
            print(f"   ask after a review: {'yes' if cfg.zenhub_ask else 'no'}")
            links = zenlink.load()
            still_open = [k for k, v in links.items() if not v.closed]
            print(f"   links: {len(links)} ({len(still_open)} still open)")
            return 0

        if action == "doctor":
            if not zenhub.enabled(cfg):
                print(f"❌ {zenhub.why_disabled(cfg)}")
                return 1
            queries, mutations = zenhub.introspect(cfg)
            if not queries and not mutations:
                print("❌ could not introspect the Zenhub schema — the key may "
                      "be rejected, or the endpoint unreachable. Re-run as "
                      "`crux --log-level DEBUG zenhub doctor` for the detail.")
                return 1
            missing = 0
            print("🎫 Zenhub schema check")
            print()
            # searchWorkspaces lives under viewer, so viewer is the root field
            # the workspace-name lookup depends on.
            for name in ("viewer", "workspace", "issueByInfo"):
                here = name in queries
                missing += 0 if here else 1
                print(f"  {'✅' if here else '❌'} query {name}")
            for name in ("closeIssues", "moveIssue"):
                here = name in mutations
                missing += 0 if here else 1
                print(f"  {'✅' if here else '❌'} mutation {name}")
            # The connection mutation is cosmetic: it draws the issue↔PR line
            # on the Zenhub card. Linking and closing both work without it, so
            # its absence is a note, not a failure.
            connect = "createIssuePrConnection" in mutations
            print(f"  {'✅' if connect else '⚠️ '} mutation createIssuePrConnection"
                  + ("" if connect else "  (optional — links and closing still "
                     "work; only the connection drawn on the Zenhub card is lost)"))
            print()
            if missing:
                print("Zenhub's schema has moved under Crux. Every query it "
                      "uses lives in one block at the top of crux/zenhub.py.")
            else:
                print("everything Crux needs is present")
            return 1 if missing else 0

        if action == "list":
            links = zenlink.load()
            if not links:
                print("no Zenhub links yet — `crux zenhub link` makes one")
                return 0
            for key, link in sorted(links.items()):
                print(f"{'✅' if link.closed else '🎫'} {key}")
                for ticket in link.tickets:
                    print(f"     {zenhub.describe(ticket)} · {ticket.title}")
                if link.prs:
                    print(f"     lands with: {', '.join(link.prs)}")
            return 0

        if not zenhub.enabled(cfg):
            print(f"❌ {zenhub.why_disabled(cfg)}")
            return 1

        if action == "sync":
            done, problems = zenlink.sync(cfg, dry_run=args.dry_run)
            for problem in problems:
                print(f"⚠️  {problem}")
            for line in done:
                print(f"{'📋' if args.dry_run else '✅'} {line}")
            if not done and not problems:
                # Two different quiet outcomes, and a reader deserves to know
                # which: nothing left to do, versus work still in flight.
                links = zenlink.load()
                waiting = [k for k, v in links.items() if not v.closed]
                if not links:
                    print("no Zenhub links yet — `crux zenhub link` makes one")
                elif waiting:
                    print(f"nothing to close — "
                          f"{_plural(len(waiting), 'link')} still waiting on "
                          f"open pull requests")
                else:
                    print("nothing to close — every linked ticket is already "
                          "closed")
            return 0

        # link / unlink both need a target, and the two shapes reach Zenhub
        # differently: a bundle's tickets hang off the BUNDLE (they close once
        # every member lands), a PR's off that one PR.
        target = _zen_target(args, cfg, bundle_store)
        if target is None:
            return 1
        key, what, hints, prs, note_on, default_slug = target

        if action == "unlink":
            if zenlink.detach(cfg, key, note_on):
                print(f"✅ {what} no longer closes any Zenhub ticket")
                return 0
            print(f"no Zenhub tickets were linked to {what}")
            return 0

        picked = _zen_pick(cfg, hints, what, args.issue or [], default_slug,
                           show_all=args.all)
        if not picked:
            print("nothing linked")
            return 1
        for problem in zenlink.attach(cfg, key, picked, prs, note_on):
            print(f"⚠️  {problem}")
        print()
        print(f"✅ {what} now closes "
              + ", ".join(zenhub.describe(t) for t in picked))
        return 0

    except CruxError as exc:
        log.error("crux zenhub failed: %s", exc)
        print(f"❌ crux zenhub failed: {exc}")
        return 1


def _zen_target(args, cfg, bundle_store):
    """Resolve what `crux zenhub link` is talking about.

    `--super N` is a bundle. Otherwise it is a pull request — `--pr N`, or the
    one for the branch you are on — EXCEPT when that branch turns out to be a
    member of an open bundle, which is the trap `crux merge` already guards: a
    ticket closed by one repo's PR would retire while the rest of the feature
    is still unmerged. Returns None (having said why) when it cannot tell.
    """
    import crux.gitio as gitio
    import crux.post as post
    import crux.zenlink as zenlink

    if getattr(args, "super", None):
        found = bundle_store.hydrate(args.super, cfg)
        if found is None:
            print(f"❌ no super PR #{args.super} here, and no brief for it in "
                  f"{cfg.super_home or '[super] home (unset)'}")
            return None
        hints = [found.name] + [m.branch for m in found.members]
        prs = [f"{m.owner}/{m.repo}#{m.pr}" for m in found.members if m.pr]
        return (zenlink.bundle_key(found.number), f"super PR #{found.number}",
                hints, prs, _zen_note_targets(found), found.home or "")

    info = gitio.repo_info()
    slug = f"{info.owner}/{info.repo}"
    number = getattr(args, "pr", None) or post.find_pr(info)
    if number is None:
        print(f"❌ no open PR for {info.branch} in {slug} — pass --pr N, or "
              f"--super N for a bundle")
        return None

    found = bundle_store.find_by_branch(info.owner, info.repo, info.branch)
    if found is not None and not found.closed:
        print(f"⛔ {slug}#{number} is part of Super PR #{found.number}, which "
              f"lands as one change.")
        print(f"   Link the bundle instead: "
              f"`crux zenhub link --super {found.number}`")
        return None

    title, _ = post.pr_meta(info, number)
    hints = [info.branch, title] + post._commit_subjects(info)
    return (zenlink.pr_key(info.owner, info.repo, number), f"{slug}#{number}",
            hints, [f"{slug}#{number}"], [(slug, number)], slug)


def _zen_setup(cfg) -> int:
    """Store a Zenhub API key, and say what still has to go in crux.toml.

    The key goes in the credentials file (0600) rather than a shell export, for
    the same reason the Slack token does: pushes come from IDEs, GUIs and
    agents that never loaded a shell profile.
    """
    import crux.config as config
    import crux.credentials as credentials
    import crux.zenhub as zenhub

    print("🎫 Zenhub setup")
    print()
    print("  Make a personal API key at https://app.zenhub.com/settings/tokens")
    print("  (it is shown once — copy it before closing the dialog)")
    print()
    try:
        import getpass
        key = getpass.getpass("  Zenhub API key (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("nothing saved")
        return 1
    if not key:
        print("no key given — nothing saved")
        return 1
    path = credentials.save_zenhub_api_key(key)
    print(f"✅ saved the key to {path}")

    if cfg.zenhub_workspace:
        wid = zenhub.workspace_id(cfg)
        if wid:
            print(f"✅ workspace {cfg.zenhub_workspace!r} resolves (id {wid})")
            print()
            print("Zenhub is on. `crux zenhub status` shows the settings.")
            return 0
        print(f"⚠️  the key works, but workspace {cfg.zenhub_workspace!r} could "
              f"not be resolved — check the name, or use its ID")
        return 1

    print()
    print(f"One line left. Add this to {config.global_config_path()}:")
    print()
    print('  [zenhub]')
    print('  workspace = "Your Workspace"   # name, or the ID from the app URL')
    print('  done_pipeline = "Closed"       # optional: where closed cards go')
    print()
    print("Until `workspace` is set, Crux makes no Zenhub calls at all.")
    return 0


def _cmd_memory(args: argparse.Namespace) -> int:
    """Show and manage the repo's long-term memory store (D31). Local state
    only — never talks to GitHub. Interactive-only like `crux prs` (never runs
    from a hook), so it may exit 1: missing forget ids, unconfirmed clear."""
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.gitio as gitio
        import crux.memory as memory

        info = gitio.repo_info()
        action = getattr(args, "maction", None) or "list"

        if action == "add":
            # A dead anchor would be silently forgotten by the next review's
            # pruning pass — refuse the typo now instead.
            if not memory.anchor_exists(args.anchor, info.root):
                print(f"⚠️  {args.anchor} does not exist in this repo — "
                      "fix the --anchor (or omit it for a repo-wide fact)")
                return 1
            new = memory.add(info, args.text, args.anchor)
            if new is None:
                print("already remembered — nothing added")
            else:
                print(f"remembered [{new.id}] {new.text}")
            return 0

        if action == "forget":
            removed, missing = memory.forget(info, args.ids)
            print(f"forgot {removed} memor{'y' if removed == 1 else 'ies'}")
            for mid in missing:
                print(f"⚠️  no memory with id {mid}")
            return 1 if missing else 0

        if action == "clear":
            if not args.yes:
                print("this forgets EVERYTHING Crux knows about "
                      f"{info.owner}/{info.repo} — re-run with --yes to confirm")
                return 1
            removed = memory.clear(info)
            print(f"forgot all {removed} memor{'y' if removed == 1 else 'ies'}")
            return 0

        memories = memory.load(info)
        if not memories:
            print(f"Crux has no memories of {info.owner}/{info.repo} yet — "
                  "they accumulate as it reviews PRs, or add one by hand:\n"
                  '  crux memory add "fact about this repo" --anchor path/file.py')
            return 0
        print(f"What Crux remembers about {info.owner}/{info.repo}:")
        for entry in memories:
            anchor = f"  ({entry.anchor})" if entry.anchor else ""
            source = "added by hand" if entry.source == "human" else "from a review"
            created = f", {entry.created}" if entry.created else ""
            print(f"  [{entry.id}] {entry.text}{anchor}  — {source}{created}")
        return 0
    except CruxError as exc:
        log.error("crux memory failed: %s", exc)
        print(f"❌ crux memory failed: {exc}")
        return 1


# ---------------------------------------------------------------------------
# _crux-hook <name>  — the Python body of each installed git hook
#
# The installed hooks are thin sh shims (see _hook_shim) that delegate to any
# repo-local hook and then `exec _crux-hook <name>`. `_crux-hook` is a private
# console script (entry point hook_main), NOT a `crux` subcommand, so this
# machinery stays out of the user-facing command surface. Every bit of real
# logic lives here, in Python, so nothing depends on bash, /dev/tty, nohup, or
# disown — that is what makes Crux run on any OS and from any shell.
# ---------------------------------------------------------------------------

# A commit whose message already carries a thorough account is left alone by
# the post-commit enrichment (D27): a Claude co-authored commit, and a commit
# Crux already amended (whose trailer is also the recursion guard).
_CLAUDE_TRAILER_RE = re.compile(r"^co-authored-by:.*claude",
                                re.IGNORECASE | re.MULTILINE)
_AMENDED_TRAILER_RE = re.compile(r"^amended-by:\s*crux",
                                 re.IGNORECASE | re.MULTILINE)


def _ensure_serving(cfg, log: logging.Logger) -> None:
    """D38 autostart: make sure `crux serve` is up. Never raises."""
    try:
        import crux.serve as serve
        serve.ensure_running(cfg, log)
    except Exception as exc:  # noqa: BLE001 — a hook never raises at the user
        log.info("could not check the crux serve port: %s", exc)


def _spawn_detached(crux_args: list[str], log: logging.Logger) -> None:
    """Run ``crux <crux_args>`` fully detached, output appended to the log.

    This is what lets a push (or commit) return instantly while the
    multi-minute review — or the commit-message enrichment — runs to
    completion in the background, surviving both this process exiting and the
    controlling terminal closing.

    Detachment is the one irreducibly OS-specific step: POSIX detaches via a
    new session (setsid); Windows via DETACHED_PROCESS + a new process group.
    There is no unified primitive, so this small branch is the whole of it.
    Invokes ``sys.executable -m crux`` so the child never depends on `crux`
    being resolvable on PATH a second time. Never raises.

    Detaching severs the child's controlling terminal, so it can no longer open
    /dev/tty by name and its `notify_tty` progress notices would vanish into the
    log. We still have the terminal here (the hook runs interactively), so open
    a fd to it now and hand it down (CRUX_TTY_FD + pass_fds): a fd opened before
    the sever keeps writing to that terminal, so the background review's notices
    still reach the user (see post.open_terminal_fd / post.notify_tty).
    """
    import crux.post as post
    logpath = _log_file()
    out: object = subprocess.DEVNULL
    try:
        logpath.parent.mkdir(parents=True, exist_ok=True)
        out = open(logpath, "a", encoding="utf-8")
    except OSError:
        out = subprocess.DEVNULL
    tty_fd = post.open_terminal_fd()
    env = None
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=out,
                        stderr=subprocess.STDOUT, close_fds=True, env=env)
    if tty_fd is not None:
        # close_fds=True would shut tty_fd in the child; pass_fds keeps it open
        # at the same number, and the child reads that number from the env.
        kwargs["pass_fds"] = (tty_fd,)
        kwargs["env"] = env = {**os.environ, "CRUX_TTY_FD": str(tty_fd)}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True  # setsid(): sever the terminal
    try:
        subprocess.Popen([sys.executable, "-m", "crux", *crux_args], **kwargs)
    except OSError as exc:
        log.error("could not spawn detached crux %s: %s", crux_args, exc)
    finally:
        if out is not subprocess.DEVNULL:
            try:
                out.close()  # the child inherited the fd; drop our copy
            except OSError:
                pass
        if tty_fd is not None:
            try:
                os.close(tty_fd)  # child kept its own copy via pass_fds
            except OSError:
                pass


def _hook_scope(log: logging.Logger):
    """``(info, cfg)`` when this repo is in the D13 owner allowlist, else None.

    Fail-safe: any error resolving the repo or config returns None, so a hook
    firing outside a git repo (or with a broken config) does nothing rather
    than blocking git.
    """
    try:
        import crux.config as config
        import crux.gitio as gitio
        info = gitio.repo_info()
        cfg = config.load(info.root)
    except Exception:
        return None
    if _out_of_scope(info, cfg, log):
        return None
    if cfg.scope_repos:
        full = f"{info.owner}/{info.repo}".lower()
        if full not in (r.lower() for r in cfg.scope_repos):
            log.info("scope: repo %r not in [scope] repos %s; hook doing "
                     "nothing (D13)", full, cfg.scope_repos)
            return None
    return info, cfg


def _cmd_hook_prepush(args: argparse.Namespace) -> int:
    """pre-push hook body. Returns 1 only for the deliberate D28 stale-shas
    signal (stop the push; the user pushes again); 0 otherwise."""
    log = logging.getLogger(LOG_NAME)
    # D37: this push came from `crux super`, which reviews the whole bundle in
    # one pass and announces it once. Everything below is the single-repo flow —
    # the D11 "create a PR?" ask, the D28 foreground check, the detached review
    # and its Slack message — and running it here would give a super PR over 5
    # repos five prompts, five cards and five announcements. Super owns its own
    # prompts; the hook stands down.
    if os.environ.get(SUPER_ENV):
        log.info("pre-push: CRUX_SUPER is set; the super PR flow reviews and "
                 "announces this push as part of its bundle (D37)")
        return 0
    scope = _hook_scope(log)
    if scope is None:
        return 0
    info, hook_cfg = scope
    # D38: the buttons Crux prints into every card and brief only work while
    # the loopback service is up, so bring it up here rather than leaving a
    # reader to discover a dead link. Costs one connect to localhost.
    _ensure_serving(hook_cfg, log)
    # D28: every outgoing human commit must carry its Crux summary BEFORE any
    # refs move. Reuse the standalone command's tested body; exit 1 = the shas
    # git resolved for this push went stale, so stop it (the shim propagates 1).
    if _cmd_ensure_enriched(argparse.Namespace(hook=True)) == 1:
        return 1
    # D37: a branch that belongs to a super PR is reviewed AS part of that
    # bundle. Its brief is what a reader of this work actually looks at, and it
    # goes stale the moment a member moves — so the push refreshes the brief
    # instead of posting a per-PR card the bundle exists to replace.
    number = _bundle_number(info, log)
    if number is not None:
        log.info("pre-push: %s is in super PR #%d; refreshing its brief "
                 "instead of the per-PR card (D37)", info.branch, number)
        _spawn_detached(["super", "refresh", str(number), "--delay", "15"], log)
        return 0
    # D11: offer to create a missing PR. This only RECORDS the answer (the PR
    # is created by the detached run after the push lands); the prompt reaches
    # the terminal because crux opens the tty device directly.
    _cmd_ensure_pr(argparse.Namespace())
    # The review runs after the push lands (--delay) and honors any recorded
    # base. Detached so `git push` returns immediately (D7).
    _spawn_detached(["run", "--delay", "15", "--yes"], log)
    return 0


def _bundle_number(info: RepoInfo, log: logging.Logger) -> int | None:
    """The OPEN super PR this branch still belongs to, or None (D37).

    A branch leaves its bundle once the bundle is closed or the branch's own
    PR is no longer open: new commits on a merged branch are new work, and
    re-briefing a bundle that already landed would bury them. The local
    record alone cannot say so when the PRs were merged outside `crux super
    merge` (the GitHub UI, a teammate), so a branch the store still lists as
    an open member costs one PR-state read. Every other push pays nothing.

    Never fatal: a hook that cannot read the bundle store falls through to the
    normal per-PR review rather than skipping the review altogether, and a PR
    state GitHub could not report keeps the bundle, as before.
    """
    try:
        import crux.bundle as bundle_store
        found = bundle_store.find_by_branch(info.owner, info.repo, info.branch)
        if found is None or found.closed:
            return None
        member = next(m for m in found.members
                      if m.repo.lower() == info.repo.lower()
                      and m.branch == info.branch)
        if member.state == "merged":
            return None
        state = _member_pr_state(info, member.pr, log)
        if state in ("merged", "closed"):
            log.info("%s#%d is %s; %s has left super PR #%d",
                     info.repo, member.pr, state, info.branch, found.number)
            if state == "merged":
                # Remember it, so the next push on this branch asks nothing.
                member.state = "merged"
                bundle_store.save(found)
            return None
    except Exception as exc:  # noqa: BLE001 — a hook never raises at the user
        log.info("could not check super PR membership: %s", exc)
        return None
    return found.number


def _member_pr_state(info: RepoInfo, pr: int, log: logging.Logger) -> str:
    """"open", "merged" or "closed" for a bundle member's PR; "" when GitHub
    could not say — which callers read as "unchanged", never as "gone"."""
    import crux.post as post
    try:
        out = post._run_gh(["api", f"repos/{info.owner}/{info.repo}/pulls/{pr}"],
                           cwd=info.root, timeout=post._GH_FOREGROUND_TIMEOUT)
        data = json.loads(out or "{}")
    except (CruxError, ValueError) as exc:
        log.info("could not read the state of %s#%d (%s)", info.repo, pr, exc)
        return ""
    if not isinstance(data, dict) or data.get("state") not in ("open", "closed"):
        return ""
    if data.get("merged"):
        return "merged"
    return data["state"]


def _cmd_hook_postcommit(args: argparse.Namespace) -> int:
    """post-commit hook body (D27): detach a message-enriching run for a terse
    HUMAN commit; leave Claude-authored and already-amended commits alone."""
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.gitio as gitio
        msg = gitio.run_git(["log", "-1", "--format=%B"])
    except Exception:
        return 0
    # Claude-authored: message already thorough. Crux-amended: recursion guard.
    if _CLAUDE_TRAILER_RE.search(msg) or _AMENDED_TRAILER_RE.search(msg):
        return 0
    scope = _hook_scope(log)
    if scope is None:
        return 0
    info, _cfg = scope
    _spawn_detached(["enrich-commit", "--sha", info.head_sha], log)
    return 0


def _cmd_hook_postcheckout(args: argparse.Namespace) -> int:
    """post-checkout hook body: record branch.<name>.cruxBase as the branch a
    new branch was created from, so crux diffs against (and opens the PR into)
    that parent. Only tags a fresh `checkout -b`/`switch -c`.

    The parent comes from git's own reflog ("branch: Created from <start>"),
    which names the real start point for every creation form. @{-1} is only
    consulted for the one form the reflog records as "HEAD" — a plain
    `checkout -b child` off the current branch — where it IS the parent.
    Reading @{-1} in the other forms is what used to get this wrong: `git
    switch -c child parent` leaves @{-1} pointing at whatever was checked out
    before, which is not the parent at all (D34).
    """
    log = logging.getLogger(LOG_NAME)
    try:
        import crux.gitio as gitio
        # Git's post-checkout args: prev HEAD, new HEAD, 1 for a branch
        # checkout. Both a creation and a plain switch look the same here, so
        # the reflog below is what tells them apart.
        if args.branch_flag != "1":
            return 0
        branch = gitio.run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if not branch or branch == "HEAD":
            return 0
        # Record only once, at creation; never clobber an existing parent or
        # one the user set by hand.
        try:
            if gitio.run_git(["config", "--get", f"branch.{branch}.cruxBase"]):
                return 0
        except CruxError:
            pass  # unset (git exits 1) — go on to record it
        parent = gitio.branch_start_point(branch, os.getcwd(), fresh_only=True)
        if parent is None:
            # "Created from HEAD" (or no reflog): only a creation that left
            # HEAD where it was can safely read the parent off @{-1}. A plain
            # switch to an existing branch lands here and is left alone.
            if args.prev_head != args.new_head:
                return 0
            parent = gitio.run_git(["rev-parse", "--abbrev-ref", "@{-1}"])
            # Only a real local branch is a valid parent (skip detached shas).
            try:
                gitio.run_git(["show-ref", "--verify", "--quiet",
                               f"refs/heads/{parent}"])
            except CruxError:
                return 0
        if not parent or parent == branch:
            return 0
        gitio.run_git(["config", f"branch.{branch}.cruxBase", parent])
        log.info("recorded %s as the parent of %s (D17)", parent, branch)
    except Exception:
        return 0
    return 0


# ---------------------------------------------------------------------------
# _crux-hook claude-stop  — Claude Code Stop hook (D10 intent capture)
# ---------------------------------------------------------------------------

def _cmd_claude_stop_hook(args: argparse.Namespace) -> int:
    import crux.claude_intent as claude_intent
    return claude_intent.run()


# ---------------------------------------------------------------------------
# _crux-hook claude-post-bash — Claude Code PostToolUse hook (plugin auto-run)
# ---------------------------------------------------------------------------

def _cmd_claude_post_bash_hook(args: argparse.Namespace) -> int:
    import crux.claude_autorun as claude_autorun
    return claude_autorun.run()


# ---------------------------------------------------------------------------
# _crux-hook claude-session-start — Claude Code SessionStart hook (D38 autostart)
# ---------------------------------------------------------------------------

def _cmd_claude_session_start_hook(args: argparse.Namespace) -> int:
    """Bring `crux serve` up when a Claude Code session starts.

    The push and publish autostarts leave a gap: after a reboot the service
    stays down until the next push, and every Merge/Close button clicked in
    between is a dead link. A session start is the next thing that happens on
    a machine using Crux, on every OS Claude Code runs on — so the gap closes
    without a system service. Prints nothing: SessionStart stdout is injected
    into the session's context.
    """
    log = logging.getLogger(LOG_NAME)
    import crux.config as config
    try:
        import crux.gitio as gitio
        root = gitio.run_git(["rev-parse", "--show-toplevel"]).strip()
    except CruxError:
        root = os.getcwd()  # not a repo: the global config still names the port
    _ensure_serving(config.load(root), log)
    return 0


# ---------------------------------------------------------------------------
# crux install-hooks
# ---------------------------------------------------------------------------

# crux's own hooks. Each is installed as a thin POSIX-sh shim (_hook_shim)
# that delegates to any repo-local hook of the same name, then hands off to
# `crux hook <name>`, where all the logic lives. pre-push posts the review;
# post-checkout records branch.<name>.cruxBase; post-commit expands terse
# human commit messages via enrich-commit (D27).
_CRUX_HOOKS = ("pre-push", "post-checkout", "post-commit")

# Client-side hooks that get a pass-through shim in the global hooks dir.
# A global core.hooksPath makes git ignore per-repo .git/hooks entirely, so
# each shim delegates back to the repo-local hook of the same name (the crux
# hooks do the same before their own work). Hooks whose absence means
# something different from "present and exit 0" (fsmonitor-watchman,
# push-to-checkout, reference-transaction) are deliberately not shimmed.
_PASSTHROUGH_HOOKS = (
    "applypatch-msg", "pre-applypatch", "post-applypatch",
    "pre-commit", "pre-merge-commit", "prepare-commit-msg", "commit-msg",
    "pre-rebase", "post-merge",
    "post-rewrite", "pre-auto-gc", "post-index-change",
)

# git runs hooks through its own bundled sh on every platform it supports —
# including Git for Windows — so /bin/sh is the most portable interpreter for
# the shims (more portable than a python shebang, which relies on the kernel
# or file associations). The shims carry no OS-specific logic themselves; the
# non-portable parts (detach, terminal I/O) all live in the `crux hook` Python.
_PASSTHROUGH_SHIM = """\
#!/bin/sh
# crux pass-through shim — installed by `crux install-hooks`. A global
# core.hooksPath points every repo here, which would otherwise disable
# per-repo hooks; delegate back to the repo-local hook of the same name.
local_hook="$(git rev-parse --git-common-dir 2>/dev/null)/hooks/$(basename "$0")"
if [ -x "$local_hook" ] && ! [ "$local_hook" -ef "$0" ]; then
    exec "$local_hook" "$@"
fi
exit 0
"""


def _hook_shim(name: str) -> str:
    """The POSIX-sh shim installed for git hook *name*.

    Delegates to any repo-local hook of the same name (a global core.hooksPath
    would otherwise disable per-repo hooks), then `exec _crux-hook <name>` with
    all output routed to the log file — the interactive D11 prompt still
    reaches the user because crux opens the terminal device directly. Only
    pre-push must still let a failing repo-local hook abort the push; the
    informational hooks ignore a local hook's exit status.
    """
    on_local_fail = "exit $?" if name == "pre-push" else "true"
    return f"""\
#!/bin/sh
# crux {name} hook — installed by `crux install-hooks`. Thin shim: delegate to
# any repo-local {name}, then run `_crux-hook {name}` (all logic lives in crux).
local_hook="$(git rev-parse --git-common-dir 2>/dev/null)/hooks/{name}"
if [ -x "$local_hook" ] && ! [ "$local_hook" -ef "$0" ]; then
    if ! grep -q crux "$local_hook" 2>/dev/null; then
        "$local_hook" "$@" || {on_local_fail}
    fi
fi
command -v _crux-hook >/dev/null 2>&1 || exit 0
mkdir -p "$HOME/.cache/crux" 2>/dev/null
exec _crux-hook {name} "$@" >>"$HOME/.cache/crux/crux.log" 2>&1
"""


def _install_claude_hook(settings_path: Path, log: logging.Logger) -> bool:
    """Merge crux's Claude Code hooks (_CLAUDE_HOOKS) into a settings.json.

    Registers each as a private `_crux-hook` entry point, which runs under
    crux's own interpreter on any OS (no python3-on-PATH assumption).
    Idempotent: an existing crux entry is left alone, and an older
    `python3 .../claude_intent_hook.py` registration is refreshed in place to
    the new command. Returns False — leaving the file untouched — when the
    existing file cannot be parsed or has an unexpected shape; the caller falls
    back to printing manual instructions.
    """
    settings: dict = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("install-hooks: cannot parse %s: %s", settings_path, exc)
            return False
        if not isinstance(loaded, dict):
            log.error("install-hooks: %s is not a JSON object", settings_path)
            return False
        settings = loaded
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        log.error("install-hooks: unexpected 'hooks' shape in %s", settings_path)
        return False
    changed = False
    for event, command, markers in _CLAUDE_HOOKS:
        matchers = hooks.setdefault(event, [])
        if not isinstance(matchers, list):
            log.error("install-hooks: unexpected 'hooks.%s' shape in %s",
                      event, settings_path)
            return False
        entry = None
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            for hook in matcher.get("hooks", []):
                cmd = str(hook.get("command", "")) if isinstance(hook, dict) else ""
                if any(m in cmd for m in markers):
                    entry = hook
                    break
            if entry is not None:
                break
        if entry is None:
            matchers.append({"hooks": [{"type": "command", "command": command}]})
            changed = True
        elif entry.get("command") != command:
            entry["command"] = command  # migrate a legacy/moved registration
            changed = True
    if not changed:
        return True  # already installed; don't rewrite the file
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n",
                                 encoding="utf-8")
    except OSError as exc:
        log.error("install-hooks: cannot write %s: %s", settings_path, exc)
        return False
    return True


def _global_hooks_dir(gitio) -> tuple[Path | None, bool]:
    """Resolve the global hooks dir: (dir, whether core.hooksPath was set).

    Returns (None, True) when core.hooksPath is set to a relative path —
    git resolves that per-repo, so no single directory reaches every repo.
    """
    try:
        current = gitio.run_git(["config", "--global", "core.hooksPath"]).strip()
    except CruxError:
        current = ""  # unset (git exits 1)
    if current:
        path = Path(current).expanduser()
        return (path, True) if path.is_absolute() else (None, True)
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "crux" / "git-hooks", False


def _cmd_install_hooks(args: argparse.Namespace) -> int:
    log = logging.getLogger(LOG_NAME)
    import crux.gitio as gitio
    repo_root: Path | None = None
    if args.local:
        try:
            # The repo's own hooks dir, NOT `--git-path hooks`: that honors
            # core.hooksPath, so with a crux global install in place --local
            # would resolve right back to the global dir. The common dir is
            # where git looks without hooksPath, where the global crux hook
            # and shims delegate to, and — unlike --git-dir, which points at
            # .git/worktrees/<name> — is correct in linked worktrees too.
            hooks_dir = Path(gitio.run_git(
                ["rev-parse", "--git-common-dir"]).strip()) / "hooks"
            repo_root = Path(gitio.run_git(
                ["rev-parse", "--show-toplevel"]).strip())
        except CruxError as exc:
            log.error("install-hooks: %s (not inside a git repo?)", exc)
            return 0
        if not hooks_dir.is_absolute():
            hooks_dir = (Path.cwd() / hooks_dir).resolve()
        had_hooks_path = True  # --local never touches git config
    else:
        hooks_dir, had_hooks_path = _global_hooks_dir(gitio)
        if hooks_dir is None:
            print("core.hooksPath is set to a relative path, which git "
                  "resolves per-repo; crux cannot install one global hook.\n"
                  "unset it or use `crux install-hooks --local` in each repo")
            return 0

    def _install_hook(name: str) -> bool:
        """Write a crux hook shim into hooks_dir. Returns False if an existing
        non-crux hook of that name blocked it (needs --force)."""
        dest = hooks_dir / name
        if dest.exists() and not args.force:
            existing = dest.read_text(encoding="utf-8", errors="replace")
            if "crux" not in existing:
                # Skip this git-hook only; the Claude hook below still installs
                # so a re-run with --force is the single missing step.
                print(f"refusing to overwrite existing non-crux hook: {dest}\n"
                      f"re-run with --force to replace it")
                return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_hook_shim(name), encoding="utf-8")
        os.chmod(dest, 0o755)  # a no-op on Windows; git runs it via its sh
        print(f"installed {name} hook -> {dest}")
        return True

    # pre-push is the primary hook: if it is refused, do not take over
    # core.hooksPath. post-checkout (records the diff/PR base) and post-commit
    # (D27 message enrichment) are best-effort. Any earlier crux hook of the
    # same name contains "crux", so _install_hook overwrites it without --force.
    refused = not _install_hook("pre-push")
    _install_hook("post-checkout")
    _install_hook("post-commit")

    if not refused and not args.local and not had_hooks_path:
        # Fresh global hooks dir: shim the other client-side hooks so
        # per-repo .git/hooks keep running, then point git at the dir.
        for name in _PASSTHROUGH_HOOKS:
            shim = hooks_dir / name
            if shim.exists():
                continue
            shim.write_text(_PASSTHROUGH_SHIM, encoding="utf-8")
            os.chmod(shim, 0o755)
        try:
            gitio.run_git(["config", "--global", "core.hooksPath",
                           str(hooks_dir)])
        except CruxError as exc:
            log.error("install-hooks: could not set core.hooksPath: %s", exc)
            return 0
        print(f"set git config --global core.hooksPath {hooks_dir}")
        print("per-repo .git/hooks still run: every hook there is reached "
              "via a pass-through shim")

    # D10 intent capture + D38 autostart: merge the Claude Code Stop and
    # SessionStart hooks into settings.json at the same scope as the git hook
    # (repo for --local, user otherwise).
    if args.local:
        claude_settings = repo_root / ".claude" / "settings.json"
    else:
        claude_settings = Path.home() / ".claude" / "settings.json"
    if _install_claude_hook(claude_settings, log):
        print(f"installed Claude Code hooks (Stop, SessionStart) -> "
              f"{claude_settings}")
    else:
        print()
        print(_CLAUDE_HOOK_INSTRUCTIONS.format(
            command=_CLAUDE_HOOK_COMMAND,
            session_start=_CLAUDE_SESSION_START_COMMAND))
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def _add_run_flags(sp: argparse.ArgumentParser, *, dry_run_flag: bool) -> None:
    sp.add_argument("--no-llm", action="store_true",
                    help="skip the LLM pass; annotate from DAG node titles only")
    if dry_run_flag:
        sp.add_argument("--dry-run", action="store_true",
                        help="print the card to stdout instead of posting")
    sp.add_argument("--delay", type=int, default=0, metavar="N",
                    help="sleep N seconds before running (detached hook use)")
    sp.add_argument("--yes", action="store_true",
                    help="skip the D11 tty ask (detached hook use); a PR is "
                         "created only from a base recorded by the foreground "
                         "pre-push ask, never invented")
    sp.add_argument("--no-create", action="store_true",
                    help="never create a PR; fall back to .crux/last-card.md")
    sp.add_argument("--pr", type=int, default=None, metavar="N",
                    help="post to this PR number instead of discovering one")
    sp.add_argument("--base", default=None, metavar="REF",
                    help="diff against REF instead of the branch's parent; "
                         "overrides the PR's own base (D34)")


def _build_parser() -> argparse.ArgumentParser:
    import crux
    parser = argparse.ArgumentParser(
        prog="crux",
        description="Crux finds the crux of a PR and posts one sticky review card.",
    )
    parser.add_argument("--version", action="version",
                        version=f"crux {crux.__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="analyze the branch and post/update the card")
    _add_run_flags(run, dry_run_flag=True)
    run.set_defaults(func=_cmd_run)

    preview = sub.add_parser("preview",
                             help="alias for `run --dry-run`: print the card")
    _add_run_flags(preview, dry_run_flag=False)
    preview.set_defaults(func=_cmd_run, dry_run=True)

    ensure = sub.add_parser("ensure-pr",
                            help="interactively offer to create a missing PR (D11)")
    ensure.set_defaults(func=_cmd_ensure_pr)

    enrich = sub.add_parser(
        "enrich-commit",
        help="expand a terse human commit message from its diff and amend "
             "the commit in place (D27; spawned by the post-commit hook)")
    enrich.add_argument("--sha", default=None, metavar="SHA",
                        help="the commit to amend (default: HEAD); if HEAD "
                             "has moved past it by amend time, nothing happens")
    enrich.set_defaults(func=_cmd_enrich_commit)

    ensure_enriched = sub.add_parser(
        "ensure-enriched",
        help="make sure every outgoing human commit carries its Crux summary "
             "before a push (D28; pre-push hook foreground). Exits 1 when the "
             "push's shas went stale and the push must be re-run")
    ensure_enriched.add_argument(
        "--hook", action="store_true",
        help="read the pre-push ref lines ('<local ref> <local sha> "
             "<remote ref> <remote sha>') from stdin")
    ensure_enriched.set_defaults(func=_cmd_ensure_enriched)

    prs = sub.add_parser(
        "prs",
        help="list open PRs — the current/named repos or every repo of the "
             "scope owners — fetched in parallel")
    prs.add_argument("repos", nargs="*", metavar="REPO",
                     help="repos to scan: owner/name, a bare name resolved "
                          "against the [scope] owners, or '.' for the current "
                          "repo; no REPOs = every repo of the scope owners")
    prs.add_argument("--jobs", type=int, default=None, metavar="N",
                     help="max parallel gh calls (default: [prs] jobs from "
                          "crux.toml; 0 or unset = auto: CPU count + 4, "
                          "capped at 32)")
    prs.set_defaults(func=_cmd_prs)

    merge_p = sub.add_parser(
        "merge",
        help="approve this branch's pull request as you, then merge it (D38)")
    merge_p.add_argument("--pr", type=int, default=0,
                         help="PR number (default: the one for this branch)")
    merge_p.add_argument("--method", default="squash",
                         choices=["merge", "squash", "rebase"],
                         help="merge method (default: squash)")
    merge_p.add_argument("--admin", action="store_true",
                         help="merge on admin rights without approving it "
                              "(recorded as a comment on the PR)")
    merge_p.add_argument("--yes", action="store_true",
                         help="merge without the confirmation prompt")
    merge_p.set_defaults(func=_cmd_merge)

    serve_p = sub.add_parser(
        "serve",
        help="run the loopback service behind the super PR brief's Merge and "
             "Close buttons (D38)")
    serve_p.add_argument("--port", type=int, default=0,
                         help="port on 127.0.0.1 (default: [serve] port, 8787)")
    serve_p.add_argument("--restart", action="store_true",
                         help="stop the running service and start a fresh "
                              "detached one — how you pick up an edit to "
                              "Crux's own source")
    serve_p.add_argument("--stop", action="store_true",
                         help="stop the running service and leave the port free")
    serve_p.set_defaults(func=_cmd_serve)

    super_p = sub.add_parser(
        "super",
        help="super PRs (D37): brief and merge a set of related PRs across "
             "repos as ONE change")
    super_p.set_defaults(func=_cmd_super, saction="list")
    ssub = super_p.add_subparsers(dest="saction")

    snew = ssub.add_parser("new", help="pick branches/PRs and create a super PR")
    snew.add_argument("pick", nargs="*", metavar="N",
                      help="picker numbers (e.g. 1 3-5); omit to choose "
                           "interactively")
    snew.add_argument("--name", default="",
                      help="label for the super PR (default: the first "
                           "selected branch)")
    snew.add_argument("--base", default="",
                      help="branch every newly opened PR targets (default: "
                           "the branch each one was created from)")
    snew.add_argument("--yes", action="store_true",
                      help="open the missing PRs without the confirmation "
                           "prompt")
    snew.add_argument("--no-brief", action="store_true",
                      help="create the bundle without analyzing it yet")
    snew.add_argument("--no-llm", action="store_true",
                      help="skip the review pass; render from rules alone")
    snew.add_argument("--dry-run", action="store_true",
                      help="print the brief instead of filing the issue")
    snew.set_defaults(func=_cmd_super, saction="new")

    sadd = ssub.add_parser(
        "add", help="add more branches/PRs to an existing super PR")
    sadd.add_argument("number", type=int, help="super PR number")
    sadd.add_argument("pick", nargs="*", metavar="N",
                      help="picker numbers (e.g. 1 3-5); omit to choose "
                           "interactively")
    sadd.add_argument("--base", default="",
                      help="branch every newly opened PR targets (default: "
                           "the branch each one was created from)")
    sadd.add_argument("--yes", action="store_true",
                      help="open the missing PRs without the confirmation "
                           "prompt")
    sadd.add_argument("--no-brief", action="store_true",
                      help="add the members without re-briefing yet")
    sadd.add_argument("--no-llm", action="store_true",
                      help="skip the review pass; render from rules alone")
    sadd.add_argument("--dry-run", action="store_true",
                      help="print the brief instead of filing the issue")
    sadd.set_defaults(func=_cmd_super, saction="add")

    sremove = ssub.add_parser(
        "remove", help="detach pull requests from a super PR (the PRs "
                       "themselves are untouched)")
    sremove.add_argument("number", type=int, help="super PR number")
    sremove.add_argument("pick", nargs="*", metavar="N",
                         help="picker numbers (e.g. 1 3-5); omit to choose "
                              "interactively")
    sremove.add_argument("--no-brief", action="store_true",
                         help="detach the members without re-briefing yet")
    sremove.add_argument("--no-llm", action="store_true",
                         help="skip the review pass; render from rules alone")
    sremove.add_argument("--dry-run", action="store_true",
                         help="print the brief instead of filing the issue")
    sremove.set_defaults(func=_cmd_super, saction="remove")

    srefresh = ssub.add_parser(
        "refresh", help="re-analyze a super PR and update its brief")
    srefresh.add_argument("number", type=int, help="super PR number")
    srefresh.add_argument("--delay", type=int, default=0,
                          help="wait N seconds first (used by the pre-push "
                               "hook so the push lands before the re-brief)")
    srefresh.add_argument("--no-llm", action="store_true",
                          help="skip the review pass; render from rules alone")
    srefresh.add_argument("--dry-run", action="store_true",
                          help="print the brief instead of filing the issue")
    srefresh.set_defaults(func=_cmd_super, saction="refresh")

    smerge = ssub.add_parser(
        "merge", help="merge every PR in a super PR, reporting what blocked")
    smerge.add_argument("number", type=int, help="super PR number")
    smerge.add_argument("--method", default=None,
                        choices=["merge", "squash", "rebase"],
                        help="merge method for this run (default: the super "
                             "PR's own — `crux super order N --method` — else "
                             "[super] merge_method, else squash)")
    smerge.add_argument("--yes", action="store_true",
                        help="merge without the confirmation prompt")
    smerge.add_argument("--admin", action="store_true",
                        help="merge on admin rights without approving anything "
                             "(recorded on the brief and in Slack)")
    smerge.set_defaults(func=_cmd_super, saction="merge")

    sorder = ssub.add_parser(
        "order",
        help="pin the landing order and/or the merge method of a super PR "
             "(D41); with nothing else, show them")
    sorder.add_argument("number", type=int, help="super PR number")
    sorder.add_argument("refs", nargs="*", metavar="REF",
                        help="every member still to land, in landing order: "
                             "owner/repo#N or repo#N. Re-briefs keep the "
                             "order; members added later go last")
    sorder.add_argument("--unpin", action="store_true",
                        help="hand the order back to the review pass")
    sorder.add_argument("--method", default=None,
                        choices=["merge", "squash", "rebase", "default"],
                        help="how this super PR merges, for everyone who "
                             "presses Merge; `default` drops it back to "
                             "[super] merge_method")
    sorder.add_argument("--no-brief", action="store_true",
                        help="save the change without updating the brief")
    sorder.set_defaults(func=_cmd_super, saction="order")

    scheckout = ssub.add_parser(
        "checkout",
        help="put every member repo on its branch, ready to test the bundle")
    scheckout.add_argument("number", type=int, help="super PR number")
    scheckout.set_defaults(func=_cmd_super, saction="checkout")

    sask = ssub.add_parser(
        "ask", help="ask in Slack for someone else to merge this super PR")
    sask.add_argument("number", type=int, help="super PR number")
    sask.set_defaults(func=_cmd_super, saction="ask")

    sclose = ssub.add_parser("close", help="close a super PR's brief")
    sclose.add_argument("number", type=int, help="super PR number")
    sclose.add_argument("--prs", action="store_true",
                        help="also close every member pull request")
    sclose.set_defaults(func=_cmd_super, saction="close")

    sshow = ssub.add_parser("show", help="show one super PR and its members")
    sshow.add_argument("number", type=int, help="super PR number")
    sshow.set_defaults(func=_cmd_super, saction="show")

    slist = ssub.add_parser("list", help="list super PRs (the default)")
    slist.set_defaults(func=_cmd_super, saction="list")

    # --- crux zenhub (D40) -------------------------------------------------
    zen_p = sub.add_parser(
        "zenhub",
        help="link Zenhub tickets to PRs and close them when the PRs land")
    zsub = zen_p.add_subparsers(dest="zaction")
    zen_p.set_defaults(func=_cmd_zenhub, zaction="status")

    zstatus = zsub.add_parser("status", help="show the Zenhub setup (the default)")
    zstatus.set_defaults(func=_cmd_zenhub, zaction="status")

    zsetup = zsub.add_parser("setup", help="store a Zenhub API key")
    zsetup.set_defaults(func=_cmd_zenhub, zaction="setup")

    zdoctor = zsub.add_parser(
        "doctor", help="check Crux's queries against the live Zenhub schema")
    zdoctor.set_defaults(func=_cmd_zenhub, zaction="doctor")

    zlink = zsub.add_parser(
        "link", help="link tickets to this branch's PR, or to a super PR")
    zlink.add_argument("--pr", type=int, default=None,
                       help="the PR number (default: this branch's)")
    zlink.add_argument("--super", type=int, default=None,
                       help="link a super PR instead — its tickets close when "
                            "EVERY member PR has landed")
    zlink.add_argument("--issue", action="append", default=[],
                       metavar="REF",
                       help="link this ticket outright (29, #29 or "
                            "owner/repo#29) instead of picking from a list; "
                            "repeatable")
    zlink.add_argument("--all", action="store_true",
                       help="show every open ticket, not just the likely ones")
    zlink.set_defaults(func=_cmd_zenhub, zaction="link")

    zunlink = zsub.add_parser("unlink", help="forget a PR's or bundle's tickets")
    zunlink.add_argument("--pr", type=int, default=None)
    zunlink.add_argument("--super", type=int, default=None)
    zunlink.set_defaults(func=_cmd_zenhub, zaction="unlink", issue=[], all=False)

    zlist = zsub.add_parser("list", help="show every link Crux is holding")
    zlist.set_defaults(func=_cmd_zenhub, zaction="list")

    zsync = zsub.add_parser(
        "sync",
        help="close tickets whose PRs landed elsewhere (the GitHub UI, a "
             "teammate, automerge)")
    zsync.add_argument("--dry-run", action="store_true",
                       help="say what would close, close nothing")
    zsync.set_defaults(func=_cmd_zenhub, zaction="sync")

    memory_p = sub.add_parser(
        "memory",
        help="show and manage what Crux remembers about this repo (D31): "
             "durable facts read into every review")
    memory_p.set_defaults(func=_cmd_memory, maction="list")
    msub = memory_p.add_subparsers(dest="maction")
    mlist = msub.add_parser("list", help="list this repo's memories (the default)")
    mlist.set_defaults(func=_cmd_memory, maction="list")
    madd = msub.add_parser("add", help="remember a fact by hand")
    madd.add_argument("text", help="the fact, one plain sentence")
    madd.add_argument("--anchor", default="", metavar="PATH[:LINE]",
                      help="file (optionally :line) inside the repo that "
                           "proves the fact")
    madd.set_defaults(func=_cmd_memory, maction="add")
    mforget = msub.add_parser("forget", help="forget memories by id")
    mforget.add_argument("ids", nargs="+", metavar="ID",
                         help="ids as shown by `crux memory`")
    mforget.set_defaults(func=_cmd_memory, maction="forget")
    mclear = msub.add_parser("clear", help="forget everything about this repo")
    mclear.add_argument("--yes", action="store_true",
                        help="confirm; without it nothing is cleared")
    mclear.set_defaults(func=_cmd_memory, maction="clear")

    install = sub.add_parser("install-hooks",
                             help="install the git pre-push hook globally "
                                  "(core.hooksPath; once per machine) and "
                                  "print Claude Code hook instructions")
    install.add_argument("--local", action="store_true",
                         help="install into this repo's .git/hooks instead "
                              "of globally")
    install.add_argument("--force", action="store_true",
                         help="overwrite an existing non-crux pre-push hook")
    install.set_defaults(func=_cmd_install_hooks)

    return parser


def _build_hook_parser() -> argparse.ArgumentParser:
    """Parser for the private `_crux-hook` entry point (git-hook machinery).

    Kept entirely separate from the `crux` command surface: these are invoked
    only by the installed sh shims and the Claude Code Stop hook, never meant
    to be run by hand.
    """
    parser = argparse.ArgumentParser(
        prog="_crux-hook",
        description="Internal crux git-hook machinery (invoked by the hook "
                    "shims install-hooks writes; not a user command).",
    )
    sub = parser.add_subparsers(dest="hook_name", required=True)

    hp = sub.add_parser("pre-push")
    hp.set_defaults(func=_cmd_hook_prepush)

    hc = sub.add_parser("post-commit")
    hc.set_defaults(func=_cmd_hook_postcommit)

    hco = sub.add_parser("post-checkout")
    # git's post-checkout args: <prev HEAD> <new HEAD> <1 for branch checkout>.
    hco.add_argument("prev_head", nargs="?", default="")
    hco.add_argument("new_head", nargs="?", default="")
    hco.add_argument("branch_flag", nargs="?", default="")
    hco.set_defaults(func=_cmd_hook_postcheckout)

    claude = sub.add_parser("claude-stop")
    claude.set_defaults(func=_cmd_claude_stop_hook)

    post_bash = sub.add_parser("claude-post-bash")
    post_bash.set_defaults(func=_cmd_claude_post_bash_hook)

    session_start = sub.add_parser("claude-session-start")
    session_start.set_defaults(func=_cmd_claude_session_start_hook)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)  # usage errors: SystemExit(2)
    log = _setup_logging()
    try:
        return int(args.func(args))
    except CruxError as exc:
        log.error("crux %s failed: %s", args.command, exc)
        _tty_failure(f"`{args.command}` failed")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Hooks must never see a nonzero exit from an internal crash.
        log.exception("crux %s crashed", args.command)
        _tty_failure(f"`{args.command}` crashed")
        return 0


def hook_main(argv: list[str] | None = None) -> int:
    """Entry point for the private `_crux-hook` console script.

    Dispatches the git-hook bodies (pre-push, post-commit, post-checkout) and
    the Claude Code Stop hook. Returns the handler's exit code — 0 for every
    hook except the deliberate pre-push exit 1 (D28 stale shas). Any internal
    crash is swallowed to 0: hook machinery must never block git.
    """
    # parse_known_args, not parse_args: git passes hook-specific trailing args
    # we don't consume — pre-push gets `<remote-name> <remote-url>`, and other
    # git versions may add more. Ignoring extras keeps a hook from ever failing
    # a push over an unrecognized argument (which argparse would exit 2 on).
    #
    # An unknown *hook name* still exits 2, and that one is reachable in the
    # field: the Claude Code plugin registers `_crux-hook claude-post-bash`,
    # so a machine whose plugin is newer than its installed crux CLI would
    # exit 2 on every hook call — which Claude Code reports as a blocking
    # error on every Bash tool use. Swallow it to 0 like every other failure.
    log = _setup_logging()
    try:
        # redirect_stderr: argparse prints its usage error before exiting, and
        # a hook has no business writing to git's / the session's stderr.
        with contextlib.redirect_stderr(io.StringIO()):
            args, _extra = _build_hook_parser().parse_known_args(argv)
    except SystemExit:
        log.info("_crux-hook: unknown hook %r; ignoring (this crux is older "
                 "than whatever invoked it — upgrade the CLI)",
                 " ".join(argv if argv is not None else sys.argv[1:]))
        return 0
    try:
        return int(args.func(args))
    except CruxError as exc:
        log.error("_crux-hook %s failed: %s", args.hook_name, exc)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.exception("_crux-hook %s crashed", args.hook_name)
        return 0


if __name__ == "__main__":
    sys.exit(main())
