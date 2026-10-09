# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Shape checks for the Claude Code plugin files that make cloud sessions work.

These files are executed by Claude Code, not by crux itself, so nothing else
in the suite would catch a typo: hooks/hooks.json (the plugin's hooks) and
.claude/settings.json (the declarative install route, and the working example
the README points cloud users at — the README pairs it with two setup-script
lines, since a session can start with this file present but the plugin not
installed).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TestHooksJson(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads((REPO / "hooks" / "hooks.json").read_text())
        self.hooks = self.data["hooks"]

    def _commands(self, event: str) -> list[str]:
        return [h["command"]
                for entry in self.hooks[event] for h in entry["hooks"]]

    def test_crux_hook_commands_guard_missing_cli(self) -> None:
        """A session without the CLI (fresh cloud container, stale PATH) must
        get a silent no-op from Stop/PostToolUse, not a hook error."""
        for event in ("Stop", "PostToolUse"):
            for command in self._commands(event):
                self.assertIn("command -v _crux-hook", command, event)
                self.assertIn("_crux-hook claude-", command, event)

    def test_session_start_bootstraps_only_in_cloud(self) -> None:
        commands = [c for c in self._commands("SessionStart")
                    if "CLAUDE_PLUGIN_ROOT" in c]
        self.assertEqual(len(commands), 1)
        command = commands[0]
        # Skips entirely when crux is already installed…
        self.assertIn("command -v crux", command)
        # …installs only in a claude.ai/code cloud container…
        self.assertIn("CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE", command)
        # …from the plugin's own checkout, and never fails the session.
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", command)
        self.assertTrue(command.endswith("exit 0"))

    def test_session_start_ensures_crux_serve(self) -> None:
        """D38: a session start brings `crux serve` back after a reboot, so the
        Merge/Close buttons aren't dead links until the next push. Guarded
        like the other crux hooks: no CLI on PATH is a silent no-op."""
        commands = [c for c in self._commands("SessionStart")
                    if "claude-session-start" in c]
        self.assertEqual(len(commands), 1)
        self.assertIn("command -v _crux-hook", commands[0])
        self.assertIn("_crux-hook claude-session-start", commands[0])

    def test_post_tool_use_watches_bash_and_github_mcp(self) -> None:
        matcher = self.hooks["PostToolUse"][0]["matcher"]
        for tool in ("Bash", "mcp__github__create_pull_request",
                     "mcp__github__push_files"):
            self.assertIn(tool, matcher)


class TestClaudeSettings(unittest.TestCase):
    """The committed .claude/settings.json is the README's working example for
    the declarative half of installing the plugin in a target repo's cloud
    sessions. It is not on its own sufficient — live testing found sessions
    starting with it present and the plugin absent — so the README prescribes
    the setup-script lines alongside it. This file still has to be correct."""

    def setUp(self) -> None:
        self.data = json.loads(
            (REPO / ".claude" / "settings.json").read_text())

    def test_declares_this_repo_as_marketplace(self) -> None:
        source = self.data["extraKnownMarketplaces"]["crux"]["source"]
        self.assertEqual(source, {"source": "github", "repo": "sonomoshq/crux"})

    def test_enables_the_plugin(self) -> None:
        self.assertIs(self.data["enabledPlugins"]["crux@crux"], True)


if __name__ == "__main__":
    unittest.main()
