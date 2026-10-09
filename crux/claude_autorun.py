# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Claude Code PostToolUse-hook logic: auto-run the review when a PR ships.

The plugin's hooks/hooks.json registers ``_crux-hook claude-post-bash`` for
the PostToolUse event, so this runs after every Bash / GitHub-MCP tool call
in a Claude Code session. When that tool call was the moment a PR came into
being — a ``git push``, a ``gh pr create``, or one of the GitHub MCP
equivalents cloud sessions use instead of shelling out — it detaches the same
``crux run`` the git pre-push hook would have, so the review card appears
without anyone asking, on every surface where the plugin loads (terminal CLI,
desktop app, claude.ai/code sessions).

Which repositories Crux acts on is the same D13 scope check the git hooks
use: ``[scope] owners`` plus the optional ``[scope] repos`` allowlist in
crux.toml. And when this machine also has crux's git pre-push hook installed,
a plain ``git push`` is already covered there — this hook skips it rather
than reviewing the same push twice (PR creation still triggers, since no git
hook fires for that).

Must never crash or block the session: run() swallows every error, returns 0.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys

# Both triggers must fire on the command being *run*, not on a command merely
# named in an argument — `git commit -m "detach the run on gh pr create"` is a
# commit, not a PR creation, and `-m "fix the git push path"` is not a push.
# Both were live false positives; a commit message mentioning the trigger
# detached a review run off a plain commit. Two independent guards, because
# either alone leaks:
#
#   _unquoted() drops quoted spans, since arguments live in quotes. But shell
#   quoting is not reliably parseable by regex — an apostrophe in prose
#   ("the plugin's hook") unbalances the pairing, and heredoc bodies are not
#   quoted at all — so it cannot be the only guard.
#
#   The command-position anchor requires the trigger to *start* a pipeline
#   segment. That is what a real invocation looks like, and it holds even
#   when quote-stripping has been defeated: prose mentions sit mid-sentence,
#   preceded by a word or a backtick rather than a segment boundary.
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL)

# Start of string, or just past a `| ; & && || ( {` boundary or newline —
# then any number of things that legitimately precede a command: environment
# assignments and wrappers (`FOO=1 sudo -E gh …`), and the shell keywords that
# introduce a command inside a loop or conditional (`; do git push $r`).
_CMD_POS = (r"(?:^|[|;&\n(){}])\s*"
            r"(?:(?:\w+=\S*|sudo|env|time|command|nohup|exec|xargs|"
            r"do|then|else)\s+)*")

# --dry-run/-n pushes move nothing and are ignored.
_PUSH_RE = re.compile(_CMD_POS + r"git\b[^|;&\n]{0,200}?\bpush\b")
_DRY_RUN_RE = re.compile(r"(?:--dry-run|\s-n)\b")
_PR_CREATE_RE = re.compile(_CMD_POS + r"gh\s+pr\s+create\b")


def _unquoted(cmd: str) -> str:
    """*cmd* with single/double-quoted spans blanked out (see _QUOTED_RE)."""
    return _QUOTED_RE.sub(" ", cmd)

# GitHub MCP tools that ship commits / create the PR in cloud sessions,
# matched by suffix so any server-name prefix (mcp__github__…) works.
_MCP_TOOL_SUFFIXES = ("create_pull_request", "push_files")


def _trigger(data: dict) -> str | None:
    """``"push"`` / ``"pr-create"`` when this tool call shipped code, else None."""
    tool = str(data.get("tool_name") or "")
    if tool == "Bash":
        tool_input = data.get("tool_input")
        cmd = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
        cmd = _unquoted(cmd)
        if _PR_CREATE_RE.search(cmd):
            return "pr-create"
        if _PUSH_RE.search(cmd) and not _DRY_RUN_RE.search(cmd):
            return "push"
        return None
    if tool.startswith("mcp__") and tool.endswith(_MCP_TOOL_SUFFIXES):
        return "pr-create"
    return None


def _git_hook_covers_push(root: str) -> bool:
    """True when crux's git pre-push hook already reviews pushes from this
    repo (globally via core.hooksPath, or a repo-local shim), so a ``git
    push`` seen here would be reviewed twice. Fail-safe: any error means
    False — better a duplicate card upsert than a missed review."""

    def _has_crux_prepush(hooks_dir: str) -> bool:
        try:
            with open(os.path.join(hooks_dir, "pre-push"), encoding="utf-8",
                      errors="replace") as fh:
                return "crux" in fh.read(4096)
        except OSError:
            return False

    def _git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""

    try:
        hooks_path = _git("config", "--get", "core.hooksPath")
        if hooks_path:
            return _has_crux_prepush(os.path.expanduser(hooks_path))
        common = _git("rev-parse", "--git-common-dir")
        if common:
            if not os.path.isabs(common):
                common = os.path.join(root, common)
            return _has_crux_prepush(os.path.join(common, "hooks"))
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def _handle(data: dict, spawn) -> None:
    trigger = _trigger(data)
    if trigger is None:
        return
    # Scope resolution and the spawned run both read the repo from cwd; the
    # hook payload's cwd is the session's project dir.
    cwd = str(data.get("cwd") or "") or os.getcwd()
    try:
        os.chdir(cwd)
    except OSError:
        return
    import crux.cli as cli
    log = logging.getLogger(cli.LOG_NAME)
    scope = cli._hook_scope(log)
    if scope is None:
        return
    info, autorun_cfg = scope
    # D38: same reason as the git hook — Crux's buttons need the service up.
    cli._ensure_serving(autorun_cfg, log)
    if trigger == "push" and _git_hook_covers_push(info.root):
        log.info("claude-post-bash: git pre-push hook already covers %s; "
                 "skipping", info.root)
        return
    # D37: same rule as the git hook — a branch inside a super PR is reviewed
    # as part of that bundle, so the push re-briefs the bundle rather than
    # posting the per-PR card the super PR exists to replace.
    number = cli._bundle_number(info, log)
    if number is not None:
        log.info("claude-post-bash: %s is in super PR #%d; re-briefing it",
                 info.branch, number)
        spawn(["super", "refresh", str(number), "--delay", "15"], log)
        return
    log.info("claude-post-bash: %s detected in %s; detaching review",
             trigger, info.root)
    spawn(["run", "--delay", "15", "--yes"], log)


def run(stream=None, spawn=None) -> int:
    """Read the PostToolUse payload from *stream* (default stdin) and, when
    it marks a push / PR creation in an in-scope repo, detach a ``crux run``.

    *spawn* is injectable for tests; default is cli._spawn_detached. Never
    raises: any failure is swallowed so the session is never blocked or
    crashed. Always returns 0.
    """
    try:
        data = json.load(stream if stream is not None else sys.stdin)
        if isinstance(data, dict):
            if spawn is None:
                from crux.cli import _spawn_detached as spawn
            _handle(data, spawn)
    except BaseException:
        pass
    return 0
