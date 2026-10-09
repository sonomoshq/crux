# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.slack. All HTTP is mocked: no network, no real Slack token."""
from __future__ import annotations

import json
import os
import unittest
import urllib.parse
from unittest import mock

from crux import slack
from crux.models import Config, RepoInfo


def make_info() -> RepoInfo:
    return RepoInfo(root="/r", branch="feat", head_sha="h" * 40, base_sha="b" * 40,
                    owner="o", repo="r")


class _Resp:
    def __init__(self, obj):
        self._data = json.dumps(obj).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


class EnabledTest(unittest.TestCase):
    def test_disabled_without_channel(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-1"}):
            self.assertFalse(slack.enabled(Config()))

    def test_disabled_without_token(self):
        # No env var AND no credentials file (isolate from a real one on the
        # dev's machine) => no token => disabled.
        env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("crux.credentials.get_slack_bot_token", return_value=""):
            self.assertFalse(slack.enabled(Config(slack_channel="pull-requests")))

    def test_enabled_with_channel_and_token(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-1"}):
            self.assertTrue(slack.enabled(Config(slack_channel="pull-requests")))

    def test_token_falls_back_to_credentials_file(self):
        # No env var, but the credentials file has one => enabled. This is the
        # shell-independent path (works under zsh/fish/PowerShell/IDE pushes).
        env = {k: v for k, v in os.environ.items() if k != "SLACK_BOT_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("crux.credentials.get_slack_bot_token",
                           return_value="xoxb-from-file"):
            self.assertTrue(slack.enabled(Config(slack_channel="pull-requests")))

    def test_env_var_wins_over_credentials_file(self):
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-env"}), \
                mock.patch("crux.credentials.get_slack_bot_token",
                           return_value="xoxb-file") as from_file:
            self.assertEqual(slack._token(Config()), "xoxb-env")
            from_file.assert_not_called()  # env short-circuits; file untouched


class AnnounceTest(unittest.TestCase):
    URL = "https://github.com/o/r/pull/7"

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.calls: list[tuple[str, dict | None]] = []

    def _run(self, history_messages, channel="C0123456", previous_ts="",
             author=""):
        def fake_urlopen(req, timeout=None):
            payload = json.loads(req.data) if req.data else None
            self.calls.append((req.full_url, payload))
            if "conversations.history" in req.full_url:
                return _Resp({"ok": True, "messages": history_messages})
            if "conversations.list" in req.full_url:
                return _Resp({"ok": True,
                              "channels": [{"name": "pull-requests", "id": "C777"}]})
            if "chat.postMessage" in req.full_url:
                return _Resp({"ok": True, "ts": "111.222"})
            return _Resp({"ok": True})

        cfg = Config(slack_channel=channel)
        with mock.patch("crux.slack.urllib.request.urlopen", side_effect=fake_urlopen):
            return slack.announce_pr(cfg, make_info(), 7, "My PR", self.URL,
                                     previous_ts, author=author)

    def _post_payload(self) -> dict:
        return next(p for u, p in self.calls if "chat.postMessage" in u)

    def test_posts_new_message_when_pr_not_linked(self):
        ch, ts = self._run([{"text": "unrelated chatter", "ts": "1.0"}])
        self.assertEqual((ch, ts), ("C0123456", "111.222"))
        payload = self._post_payload()
        self.assertNotIn("thread_ts", payload)     # a brand-new top-level message
        self.assertIn(self.URL, payload["text"])
        self.assertIn("New PR", payload["text"])

    def test_new_message_names_the_repo_linked_before_the_pr_link(self):
        # The channel carries several repos' PRs: the announcement links the
        # repo by name (owner/repo) ahead of the PR link.
        self._run([{"text": "unrelated chatter", "ts": "1.0"}])
        text = self._post_payload()["text"]
        repo_link = "<https://github.com/o/r|o/r>"
        self.assertIn(repo_link, text)
        self.assertLess(text.index(repo_link), text.index(self.URL))

    def test_author_is_credited_before_the_repo_and_the_links(self):
        # Who wrote it is the first thing a scanning reader needs; the repo
        # and the PR link follow.
        self._run([{"text": "x", "ts": "1.0"}], author="Fixture A.")
        text = self._post_payload()["text"]
        self.assertIn("New PR up for review by Fixture A. in", text)
        self.assertLess(text.index("Fixture A."), text.index("github.com"))

    def test_unknown_author_still_reads_as_a_sentence(self):
        self._run([{"text": "x", "ts": "1.0"}])
        self.assertIn("New PR up for review in", self._post_payload()["text"])

    def test_threads_when_pr_already_in_channel(self):
        ch, ts = self._run(
            [{"text": f"here it is <{self.URL}|#7>", "ts": "999.000"}])
        # returns the ROOT ts of the existing message, and replies in-thread
        self.assertEqual((ch, ts), ("C0123456", "999.000"))
        self.assertEqual(self._post_payload()["thread_ts"], "999.000")

    def test_channel_checked_before_posting(self):
        # conversations.history is called before chat.postMessage
        self._run([{"text": "x", "ts": "1"}])
        kinds = [u.split("/")[-1].split("?")[0] for u, _ in self.calls]
        self.assertLess(kinds.index("conversations.history"),
                        kinds.index("chat.postMessage"))

    def test_previous_ts_used_when_history_misses(self):
        ch, ts = self._run([{"text": "nothing here", "ts": "1"}],
                           previous_ts="555.000")
        self.assertEqual(ts, "555.000")
        self.assertEqual(self._post_payload()["thread_ts"], "555.000")

    def test_copy_never_claims_crux_reviewed_the_pr(self):
        # A person reviews; Crux only announces. Neither the first post nor
        # the thread reply may read as Crux having done a code review.
        self._run([{"text": "unrelated", "ts": "1.0"}])
        first = self._post_payload()["text"]
        self.calls.clear()
        self._run([{"text": f"here it is <{self.URL}|#7>", "ts": "999.000"}])
        reply = self._post_payload()["text"]
        self.assertIn("New commits pushed", reply)
        for text in (first, reply):
            self.assertNotIn("Crux", text)
            self.assertNotIn("reviewed", text)

    def test_channel_name_is_resolved_to_id(self):
        ch, ts = self._run([], channel="pull-requests")
        self.assertEqual(ch, "C777")

    def test_disabled_is_noop(self):
        with mock.patch("crux.slack.urllib.request.urlopen") as urlopen:
            result = slack.announce_pr(Config(), make_info(), 7, "t", self.URL)
        self.assertEqual(result, ("", ""))
        urlopen.assert_not_called()


class ErrorDiagnosticsTest(unittest.TestCase):
    """Slack failures must be debuggable from the log alone (field-tested:
    a missing channels:read scope was invisible without these)."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_missing_scope_error_names_the_needed_scope(self):
        resp = _Resp({"ok": False, "error": "missing_scope",
                      "needed": "channels:read"})
        with mock.patch("crux.slack.urllib.request.urlopen", return_value=resp):
            with self.assertLogs("crux.slack", level="WARNING") as logs:
                slack._call("conversations.list", "xoxb-test", params={"limit": 1})
        self.assertIn("missing_scope", logs.output[0])
        self.assertIn("channels:read", logs.output[0])

    def test_bad_token_error_is_flagged_as_an_auth_problem(self):
        # A revoked/expired token needs a fresh token, not a scope — the log
        # must say so, not just echo the bare error code.
        for error in ("invalid_auth", "token_revoked", "account_inactive"):
            resp = _Resp({"ok": False, "error": error})
            with mock.patch("crux.slack.urllib.request.urlopen", return_value=resp):
                with self.assertLogs("crux.slack", level="WARNING") as logs:
                    slack._call("chat.postMessage", "xoxb-bad", payload={"x": 1})
            joined = "\n".join(logs.output)
            self.assertIn(error, joined)
            self.assertIn("reinstall", joined.lower())

    def test_unresolvable_channel_name_logs_the_two_ways_out(self):
        resp = _Resp({"ok": False, "error": "missing_scope",
                      "needed": "channels:read"})
        cfg = Config(slack_channel="pull-requests")
        with mock.patch("crux.slack.urllib.request.urlopen", return_value=resp):
            with self.assertLogs("crux.slack", level="WARNING") as logs:
                result = slack._resolve_channel(cfg, "xoxb-test")
        self.assertIsNone(result)
        joined = "\n".join(logs.output)
        self.assertIn("channels:read", joined)     # way 1: add the scope
        self.assertIn("channel ID", joined)        # way 2: skip the lookup

    def test_channel_id_needs_no_lookup_at_all(self):
        cfg = Config(slack_channel="C0123456")
        with mock.patch("crux.slack.urllib.request.urlopen") as urlopen:
            self.assertEqual(slack._resolve_channel(cfg, "xoxb-test"), "C0123456")
        urlopen.assert_not_called()

    def _resolve_with(self, listings: dict):
        """listings: types param -> response object for conversations.list."""
        def fake_urlopen(req, timeout=None):
            assert "conversations.list" in req.full_url
            types = urllib.parse.parse_qs(
                urllib.parse.urlparse(req.full_url).query)["types"][0]
            return _Resp(listings[types])
        cfg = Config(slack_channel="pull-requests")
        with mock.patch("crux.slack.urllib.request.urlopen",
                        side_effect=fake_urlopen):
            return slack._resolve_channel(cfg, "xoxb-test")

    def test_public_and_private_listed_separately_never_combined(self):
        # A combined types request dies whole on missing_scope for tokens
        # with only the three documented scopes (field-tested). The public
        # listing alone must resolve a public channel even when the private
        # listing is unavailable.
        result = self._resolve_with({
            "public_channel": {"ok": True, "channels": [
                {"name": "pull-requests", "id": "C42"}]},
            "private_channel": {"ok": False, "error": "missing_scope",
                                "needed": "groups:read"},
        })
        self.assertEqual(result, "C42")

    def test_private_channel_found_when_groups_read_present(self):
        result = self._resolve_with({
            "public_channel": {"ok": True, "channels": [
                {"name": "general", "id": "C1"}]},
            "private_channel": {"ok": True, "channels": [
                {"name": "pull-requests", "id": "G99"}]},
        })
        self.assertEqual(result, "G99")

    def test_not_public_and_private_blocked_names_the_likely_cause(self):
        with self.assertLogs("crux.slack", level="WARNING") as logs:
            result = self._resolve_with({
                "public_channel": {"ok": True, "channels": [
                    {"name": "general", "id": "C1"}]},
                "private_channel": {"ok": False, "error": "missing_scope",
                                    "needed": "groups:read"},
            })
        self.assertIsNone(result)
        joined = "\n".join(logs.output)
        self.assertIn("groups:read", joined)
        self.assertIn("private", joined)
        self.assertIn("channel ID", joined)


if __name__ == "__main__":
    unittest.main()
