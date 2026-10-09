# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.credentials: the shell-independent Slack token store."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crux import credentials


class CredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # credentials_path() resolves under $XDG_CONFIG_HOME/crux/
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.path = Path(tmp.name) / "crux" / "credentials.json"

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(credentials.load_credentials(), {})
        self.assertEqual(credentials.get_slack_bot_token(), "")

    def test_save_then_get_round_trips(self) -> None:
        path = credentials.save_slack_bot_token("xoxb-abc")
        self.assertEqual(path, self.path)
        self.assertTrue(self.path.is_file())
        self.assertEqual(credentials.get_slack_bot_token(), "xoxb-abc")
        self.assertEqual(json.loads(self.path.read_text())["slack_bot_token"],
                         "xoxb-abc")

    def test_get_strips_whitespace(self) -> None:
        credentials.save_slack_bot_token("  xoxb-x  ")
        self.assertEqual(credentials.get_slack_bot_token(), "xoxb-x")

    def test_save_preserves_unknown_keys(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"other_secret": "keep me"}),
                             encoding="utf-8")
        credentials.save_slack_bot_token("xoxb-new")
        data = json.loads(self.path.read_text())
        self.assertEqual(data["other_secret"], "keep me")  # not clobbered
        self.assertEqual(data["slack_bot_token"], "xoxb-new")

    def test_corrupt_file_reads_as_empty(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(credentials.load_credentials(), {})
        self.assertEqual(credentials.get_slack_bot_token(), "")

    def test_clear_removes_only_the_token(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(
            {"slack_bot_token": "xoxb-x", "other": "y"}), encoding="utf-8")
        self.assertTrue(credentials.clear_slack_bot_token())
        data = json.loads(self.path.read_text())
        self.assertNotIn("slack_bot_token", data)
        self.assertEqual(data["other"], "y")
        # clearing again is a no-op (nothing left to remove)
        self.assertFalse(credentials.clear_slack_bot_token())

    @unittest.skipIf(os.name == "nt", "POSIX file-mode bits only")
    def test_file_is_written_private(self) -> None:
        credentials.save_slack_bot_token("xoxb-secret")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)  # owner-only; not group/world readable


if __name__ == "__main__":
    unittest.main()
