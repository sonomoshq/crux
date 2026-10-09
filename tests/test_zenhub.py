# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Zenhub integration (D40): crux.zenhub and crux.zenlink.

All HTTP is mocked and every `gh` call is stubbed — no network, no real Zenhub
key, no GitHub. Fixture repos and people are invented.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from crux import zenhub, zenlink
from crux.models import Bundle, BundleMember, Config, CruxError, ZenLink, ZenTicket


def cfg_on(**over) -> Config:
    """A Config with Zenhub switched on (the key comes from the env stub)."""
    cfg = Config()
    cfg.zenhub_workspace = "Engineering"
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


class _Resp:
    def __init__(self, obj):
        self._data = json.dumps(obj).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


def ticket(number: int, title: str = "A ticket", **over) -> ZenTicket:
    base = dict(id=f"Z{number}", number=number, owner="example-org",
                repo="Widget", title=title, body="", url="", pipeline="")
    base.update(over)
    return ZenTicket(**base)


class _KeyEnv:
    """Put a Zenhub key in the environment for the duration of a test."""

    def __enter__(self):
        self._patch = mock.patch.dict(os.environ, {"ZENHUB_API_KEY": "zh-test"})
        self._patch.start()
        zenhub._ws_cache.clear()
        zenhub._ghid_cache.clear()
        return self

    def __exit__(self, *a):
        self._patch.stop()
        return False


# ---------------------------------------------------------------------------
# The switch: off unless BOTH halves are configured
# ---------------------------------------------------------------------------

class EnabledTest(unittest.TestCase):
    def test_off_by_default(self):
        """The whole feature is opt-in: a stock Config makes no Zenhub calls."""
        with mock.patch.dict(os.environ, {"ZENHUB_API_KEY": "zh-test"}):
            self.assertFalse(zenhub.enabled(Config()))

    def test_workspace_without_key_is_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("crux.credentials.get_zenhub_api_key", return_value=""):
                self.assertFalse(zenhub.enabled(cfg_on()))

    def test_both_halves_present(self):
        with _KeyEnv():
            self.assertTrue(zenhub.enabled(cfg_on()))

    def test_key_falls_back_to_the_credentials_file(self):
        """Same shell-independence as the Slack token: no env var needed."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("crux.credentials.get_zenhub_api_key",
                            return_value="zh-stored"):
                self.assertEqual(zenhub._token(cfg_on()), "zh-stored")

    def test_env_var_wins_over_the_file(self):
        with mock.patch.dict(os.environ, {"ZENHUB_API_KEY": "zh-env"}):
            with mock.patch("crux.credentials.get_zenhub_api_key",
                            return_value="zh-stored"):
                self.assertEqual(zenhub._token(cfg_on()), "zh-env")

    def test_unreadable_credentials_read_as_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("crux.credentials.get_zenhub_api_key",
                            side_effect=OSError("boom")):
                self.assertEqual(zenhub._token(cfg_on()), "")

    def test_why_disabled_names_the_missing_half(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("crux.credentials.get_zenhub_api_key", return_value=""):
                self.assertIn("no API key", zenhub.why_disabled(cfg_on()))
            with mock.patch("crux.credentials.get_zenhub_api_key",
                            return_value="zh-1"):
                self.assertIn("no workspace", zenhub.why_disabled(Config()))


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

class CallTest(unittest.TestCase):
    def test_posts_a_bearer_authenticated_graphql_document(self):
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen",
                            return_value=_Resp({"data": {"ok": 1}})) as opened:
                out = zenhub._call(cfg_on(), "query Q { x }", {"a": 1}, op="Q")
        self.assertEqual(out, {"ok": 1})
        req = opened.call_args[0][0]
        self.assertEqual(req.full_url, zenhub.ENDPOINT)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer zh-test")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["query"], "query Q { x }")
        self.assertEqual(body["variables"], {"a": 1})

    def test_no_key_makes_no_request(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("crux.credentials.get_zenhub_api_key", return_value=""):
                with mock.patch("urllib.request.urlopen") as opened:
                    self.assertIsNone(zenhub._call(cfg_on(), "query Q { x }"))
        opened.assert_not_called()

    def test_graphql_errors_arrive_as_a_200_and_still_read_as_failure(self):
        """GraphQL answers 200 with an `errors` array — the failure has to be
        read out of the body, not caught as an exception."""
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen",
                            return_value=_Resp({"errors": [{"message": "nope"}]})):
                self.assertIsNone(zenhub._call(cfg_on(), "query Q { x }"))

    def test_schema_drift_is_called_out_by_name(self):
        with _KeyEnv():
            errors = {"errors": [{"message": "Cannot query field 'htmlUrl'"}]}
            with mock.patch("urllib.request.urlopen", return_value=_Resp(errors)):
                with self.assertLogs("crux.zenhub", level="WARNING") as logs:
                    self.assertIsNone(zenhub._call(cfg_on(), "query Q { x }"))
        self.assertIn("zenhub doctor", "\n".join(logs.output))

    def test_a_rejected_key_says_so(self):
        with _KeyEnv():
            error = urllib.error.HTTPError(zenhub.ENDPOINT, 401, "no", {}, None)
            with mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertLogs("crux.zenhub", level="WARNING") as logs:
                    self.assertIsNone(zenhub._call(cfg_on(), "query Q { x }"))
        self.assertIn("rejected", "\n".join(logs.output))

    def test_a_network_failure_is_swallowed(self):
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen",
                            side_effect=urllib.error.URLError("down")):
                self.assertIsNone(zenhub._call(cfg_on(), "query Q { x }"))


# ---------------------------------------------------------------------------
# Workspace and pipelines
# ---------------------------------------------------------------------------

class WorkspaceTest(unittest.TestCase):
    def test_an_id_needs_no_lookup(self):
        wid = "a" * 24
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen") as opened:
                self.assertEqual(zenhub.workspace_id(cfg_on(zenhub_workspace=wid)),
                                 wid)
        opened.assert_not_called()

    def test_a_name_is_looked_up_and_cached(self):
        found = {"data": {"viewer": {"searchWorkspaces": {
            "nodes": [{"id": "w1", "name": "Engineering"}]}}}}
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen",
                            return_value=_Resp(found)) as opened:
                self.assertEqual(zenhub.workspace_id(cfg_on()), "w1")
                self.assertEqual(zenhub.workspace_id(cfg_on()), "w1")
        self.assertEqual(opened.call_count, 1, "the second call should be cached")

    def test_an_exact_name_beats_a_prefix_match(self):
        found = {"data": {"viewer": {"searchWorkspaces": {"nodes": [
            {"id": "w-other", "name": "Engineering Archive"},
            {"id": "w-exact", "name": "Engineering"}]}}}}
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen", return_value=_Resp(found)):
                self.assertEqual(zenhub.workspace_id(cfg_on()), "w-exact")

    def test_an_unknown_name_resolves_to_nothing(self):
        with _KeyEnv():
            empty = {"data": {"viewer": {"searchWorkspaces": {"nodes": []}}}}
            with mock.patch("urllib.request.urlopen", return_value=_Resp(empty)):
                with self.assertLogs("crux.zenhub", level="WARNING"):
                    self.assertEqual(zenhub.workspace_id(cfg_on()), "")

    def test_done_pipeline_matches_case_insensitively(self):
        pipes = {"data": {"workspace": {"pipelinesConnection": {"nodes": [
            {"id": "p1", "name": "In Progress"}, {"id": "p2", "name": "Closed"}]}}}}
        with _KeyEnv():
            cfg = cfg_on(zenhub_workspace="b" * 24, zenhub_done_pipeline="closed")
            with mock.patch("urllib.request.urlopen", return_value=_Resp(pipes)):
                self.assertEqual(zenhub.done_pipeline_id(cfg), "p2")

    def test_no_done_pipeline_configured_means_no_lookup(self):
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen") as opened:
                self.assertEqual(zenhub.done_pipeline_id(cfg_on()), "")
        opened.assert_not_called()

    def test_a_misspelled_pipeline_says_so_and_still_closes(self):
        pipes = {"data": {"workspace": {"pipelinesConnection": {"nodes": [
            {"id": "p1", "name": "Done"}]}}}}
        with _KeyEnv():
            cfg = cfg_on(zenhub_workspace="b" * 24, zenhub_done_pipeline="Dnoe")
            with mock.patch("urllib.request.urlopen", return_value=_Resp(pipes)):
                with self.assertLogs("crux.zenhub", level="WARNING") as logs:
                    self.assertEqual(zenhub.done_pipeline_id(cfg), "")
        self.assertIn("closed but not moved", "\n".join(logs.output))


# ---------------------------------------------------------------------------
# Reading issues
# ---------------------------------------------------------------------------

def _issue(number, *, title="T", pr=False, state="OPEN", body="", pipeline="New",
           kind="Task", disposition="BOARD"):
    return {"id": f"Z{number}", "number": number, "title": title, "body": body,
            "issueType": {"name": kind, "disposition": disposition},
            "state": state, "pullRequest": pr, "htmlUrl": f"http://x/{number}",
            "repository": {"ghId": 7, "name": "Widget", "ownerName": "example-org"},
            "pipelineIssue": {"pipeline": {"name": pipeline}}}


def _page(nodes, more=False, cursor=""):
    return {"data": {"workspace": {"issues": {
        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
        "nodes": nodes}}}}


class OpenIssuesTest(unittest.TestCase):
    def test_pull_requests_and_closed_issues_are_not_tickets(self):
        page = _page([_issue(1), _issue(2, pr=True), _issue(3, state="CLOSED")])
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen", return_value=_Resp(page)):
                out = zenhub.open_issues(cfg_on(zenhub_workspace="c" * 24))
        self.assertEqual([t.number for t in out], [1])

    def test_epics_and_projects_are_not_tickets(self):
        page = _page([_issue(1, kind="Bug"), _issue(2, kind="Feature"),
                      _issue(3, kind="Epic", disposition="PLANNING_PANEL"),
                      _issue(4, kind="Project", disposition="PLANNING_PANEL"),
                      {**_issue(5), "issueType": None}])
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen", return_value=_Resp(page)):
                out = zenhub.open_issues(cfg_on(zenhub_workspace="c" * 24))
        self.assertEqual([(t.number, t.kind) for t in out],
                         [(1, "Bug"), (2, "Feature"), (5, "")])

    def test_it_follows_the_cursor(self):
        pages = [_Resp(_page([_issue(1)], more=True, cursor="cur")),
                 _Resp(_page([_issue(2)]))]
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen", side_effect=pages) as opened:
                out = zenhub.open_issues(cfg_on(zenhub_workspace="c" * 24))
        self.assertEqual([t.number for t in out], [1, 2])
        second = json.loads(opened.call_args_list[1][0][0].data.decode())
        self.assertEqual(second["variables"]["after"], "cur")

    def test_paging_is_bounded(self):
        """A picker that has to page a thousand tickets is the wrong tool."""
        forever = _Resp(_page([_issue(1)], more=True, cursor="cur"))
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen",
                            side_effect=[forever] * 50) as opened:
                zenhub.open_issues(cfg_on(zenhub_workspace="c" * 24))
        self.assertEqual(opened.call_count, zenhub._MAX_PAGES)

    def test_fields_land_on_the_ticket(self):
        page = _page([_issue(9, title="Photo uploads", body="Long story",
                             pipeline="In Progress")])
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen", return_value=_Resp(page)):
                found = zenhub.open_issues(cfg_on(zenhub_workspace="c" * 24))[0]
        self.assertEqual(
            (found.id, found.number, found.owner, found.repo, found.title,
             found.body, found.pipeline),
            ("Z9", 9, "example-org", "Widget", "Photo uploads", "Long story",
             "In Progress"))

    def test_no_workspace_reads_nothing(self):
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen") as opened:
                self.assertEqual(zenhub.open_issues(Config()), [])
        opened.assert_not_called()


class IssueByRefTest(unittest.TestCase):
    def test_a_pull_request_is_refused_as_a_ticket(self):
        answer = {"data": {"issueByInfo": _issue(4, pr=True)}}
        with _KeyEnv():
            with mock.patch("crux.post._run_gh", return_value="7"):
                with mock.patch("urllib.request.urlopen", return_value=_Resp(answer)):
                    with self.assertLogs("crux.zenhub", level="WARNING"):
                        self.assertIsNone(
                            zenhub.issue_by_ref(cfg_on(), "example-org/Widget", 4))

    def test_an_unreadable_repo_id_stops_the_lookup(self):
        with _KeyEnv():
            with mock.patch("crux.post._run_gh", side_effect=CruxError("no gh")):
                with mock.patch("urllib.request.urlopen") as opened:
                    with self.assertLogs("crux.zenhub", level="WARNING"):
                        self.assertIsNone(
                            zenhub.issue_by_ref(cfg_on(), "example-org/Widget", 4))
        opened.assert_not_called()


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------

class CloseTicketsTest(unittest.TestCase):
    def _run(self, cfg, responses):
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen",
                            side_effect=responses) as opened:
                problems = zenhub.close_tickets(cfg, [ticket(1), ticket(2)])
        return problems, opened

    def test_it_closes_in_one_batch_and_moves_one_by_one(self):
        cfg = cfg_on(zenhub_workspace="d" * 24, zenhub_done_pipeline="Closed")
        pipes = _Resp({"data": {"workspace": {"pipelinesConnection": {
            "nodes": [{"id": "p2", "name": "Closed"}]}}}})
        responses = [_Resp({"data": {"closeIssues": {}}}), pipes,
                     _Resp({"data": {"moveIssue": {}}}),
                     _Resp({"data": {"moveIssue": {}}})]
        problems, opened = self._run(cfg, responses)
        self.assertEqual(problems, [])
        first = json.loads(opened.call_args_list[0][0][0].data.decode())
        self.assertEqual(first["variables"]["input"]["issueIds"], ["Z1", "Z2"])
        moves = [json.loads(c[0][0].data.decode()) for c in opened.call_args_list[2:]]
        self.assertEqual([m["variables"]["input"]["pipelineId"] for m in moves],
                         ["p2", "p2"])

    def test_no_pipeline_configured_closes_without_moving(self):
        problems, opened = self._run(
            cfg_on(zenhub_workspace="d" * 24),
            [_Resp({"data": {"closeIssues": {}}})])
        self.assertEqual(problems, [])
        self.assertEqual(opened.call_count, 1)

    def test_a_failed_close_names_every_ticket(self):
        problems, _ = self._run(cfg_on(zenhub_workspace="d" * 24),
                                [_Resp({"errors": [{"message": "nope"}]})])
        self.assertEqual(len(problems), 2)
        self.assertIn("example-org/Widget#1", problems[0])

    def test_a_close_that_worked_but_did_not_move_says_exactly_that(self):
        cfg = cfg_on(zenhub_workspace="d" * 24, zenhub_done_pipeline="Closed")
        pipes = _Resp({"data": {"workspace": {"pipelinesConnection": {
            "nodes": [{"id": "p2", "name": "Closed"}]}}}})
        responses = [_Resp({"data": {"closeIssues": {}}}), pipes,
                     _Resp({"errors": [{"message": "no"}]}),
                     _Resp({"data": {"moveIssue": {}}})]
        problems, _ = self._run(cfg, responses)
        self.assertEqual(len(problems), 1)
        self.assertIn("was closed but could not be moved", problems[0])

    def test_planning_items_are_never_closed(self):
        epic = ticket(3, kind="Epic", planning=True)
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen",
                            return_value=_Resp({"data": {"closeIssues": {}}})) as opened:
                with self.assertLogs("crux.zenhub", level="WARNING") as logs:
                    zenhub.close_tickets(cfg_on(zenhub_workspace="d" * 24),
                                         [ticket(1), epic])
        sent = json.loads(opened.call_args_list[0][0][0].data.decode())
        self.assertEqual(sent["variables"]["input"]["issueIds"], ["Z1"])
        self.assertIn("Epic is a planning item", "\n".join(logs.output))

    def test_tickets_without_an_id_are_not_sent(self):
        with _KeyEnv():
            with mock.patch("urllib.request.urlopen") as opened:
                self.assertEqual(
                    zenhub.close_tickets(cfg_on(), [ticket(1, id="")]), [])
        opened.assert_not_called()


class ConnectPrTest(unittest.TestCase):
    def test_a_missing_pr_node_is_not_an_error(self):
        """The connection is cosmetic — the link Crux relies on is its own."""
        with _KeyEnv():
            with mock.patch("crux.post._run_gh", return_value="7"):
                with mock.patch("urllib.request.urlopen",
                                return_value=_Resp({"data": {"issueByInfo": None}})):
                    self.assertFalse(
                        zenhub.connect_pr(cfg_on(), ticket(1),
                                          "example-org/Widget", 12))


# ---------------------------------------------------------------------------
# The link store
# ---------------------------------------------------------------------------

class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_round_trip(self):
        link = ZenLink(key="pr:example-org/Widget#12", tickets=[ticket(29)],
                       prs=["example-org/Widget#12"])
        zenlink.put(link)
        back = zenlink.get("pr:example-org/Widget#12")
        self.assertIsNotNone(back)
        self.assertEqual(back.tickets[0].number, 29)
        self.assertEqual(back.prs, ["example-org/Widget#12"])
        self.assertTrue(back.created_at, "put should stamp created_at")

    def test_a_missing_store_reads_as_empty(self):
        self.assertEqual(zenlink.load(), {})

    def test_a_corrupt_store_reads_as_empty_and_never_raises(self):
        path = zenlink.store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("crux.zenlink", level="WARNING"):
            self.assertEqual(zenlink.load(), {})

    def test_the_two_shapes_get_distinct_keys(self):
        self.assertEqual(zenlink.pr_key("example-org", "Widget", 12),
                         "pr:example-org/Widget#12")
        self.assertEqual(zenlink.bundle_key(7), "super:7")

    def test_drop(self):
        zenlink.put(ZenLink(key="super:7", tickets=[ticket(1)]))
        self.assertTrue(zenlink.drop("super:7"))
        self.assertFalse(zenlink.drop("super:7"))


# ---------------------------------------------------------------------------
# Guessing which ticket a branch is about
# ---------------------------------------------------------------------------

class RankTest(unittest.TestCase):
    def test_branch_reference_forms(self):
        self.assertEqual(zenlink.referenced_numbers("fix/issue-29-zero-diff"), {29})
        self.assertEqual(zenlink.referenced_numbers("29-fix-the-thing"), {29})
        self.assertEqual(zenlink.referenced_numbers("closes #4 and #5"), {4, 5})
        self.assertEqual(zenlink.referenced_numbers("ZH-88"), {88})

    def test_a_named_ticket_outranks_a_wordy_one(self):
        named = ticket(29, "Something else entirely")
        wordy = ticket(30, "Photo uploads fail on slow networks")
        scored = zenlink.rank([wordy, named], "fix/issue-29-retry-budget-cold-start")
        self.assertEqual(scored[0][1].number, 29)
        self.assertGreaterEqual(scored[0][0], 100)

    def test_shared_words_order_the_rest(self):
        close = ticket(30, "Photo uploads fail on slow networks")
        far = ticket(31, "Rename the dashboard tabs")
        scored = zenlink.rank([far, close], "feat/retry-budget-cold-start")
        self.assertEqual(scored[0][1].number, 30)

    def test_stopwords_do_not_create_a_match(self):
        noise = ticket(40, "Fix the thing and update the other")
        scored = zenlink.rank([noise], "fix/and-the-update")
        self.assertEqual(scored[0][0], 0)


class ExcerptTest(unittest.TestCase):
    def test_it_strips_the_template_and_keeps_the_point(self):
        body = ("<!-- please fill this in -->\n"
                "## Summary\n\n"
                "The uploader gives up after three attempts.\n")
        self.assertEqual(zenlink.excerpt(body),
                         "Summary The uploader gives up after three attempts.")

    def test_it_caps_and_ellipsizes(self):
        out = zenlink.excerpt("x" * 500, limit=20)
        self.assertEqual(len(out), 21)
        self.assertTrue(out.endswith("…"))

    def test_an_empty_body_is_an_empty_excerpt(self):
        self.assertEqual(zenlink.excerpt(""), "")
        self.assertEqual(zenlink.excerpt("### \n---\n"), "")


class ChoiceTest(unittest.TestCase):
    def setUp(self):
        self.scored = [(100, ticket(1)), (8, ticket(2)), (0, ticket(3))]

    def test_multiple_numbers(self):
        picked = zenlink.parse_choice("1,3", self.scored, 12)
        self.assertEqual([t.number for t in picked], [1, 3])

    def test_empty_means_none(self):
        self.assertEqual(zenlink.parse_choice("", self.scored, 12), [])

    def test_out_of_range_and_junk_are_ignored(self):
        self.assertEqual(zenlink.parse_choice("9 banana", self.scored, 12), [])

    def test_beyond_the_shown_limit_is_ignored(self):
        """Only what was actually printed can be chosen."""
        self.assertEqual(zenlink.parse_choice("3", self.scored, 2), [])

    def test_duplicates_collapse(self):
        picked = zenlink.parse_choice("2 2", self.scored, 12)
        self.assertEqual([t.number for t in picked], [2])

    def test_no_terminal_means_no_link_and_no_blocking(self):
        with mock.patch("crux.post._prompt_tty", return_value=None):
            with self.assertLogs("crux.zenlink", level="INFO"):
                self.assertEqual(
                    zenlink.ask(Config(), self.scored, "PR #1"), [])

    def test_the_prompt_shows_number_title_and_description(self):
        scored = [(100, ticket(29, "Photo uploads", body="It gives up early."))]
        seen = {}
        with mock.patch("crux.post._prompt_tty",
                        side_effect=lambda msg: seen.setdefault("msg", msg) and ""):
            zenlink.ask(Config(), scored, "PR #1")
        self.assertIn("example-org/Widget#29", seen["msg"])
        self.assertIn("Photo uploads", seen["msg"])
        self.assertIn("It gives up early.", seen["msg"])


# ---------------------------------------------------------------------------
# Attaching and closing
# ---------------------------------------------------------------------------

class AttachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_link_is_recorded_even_when_zenhub_refuses_the_connection(self):
        """The store write is what matters: a link Crux forgot is a ticket left
        open forever, where a missing connection is cosmetic."""
        with mock.patch("crux.zenhub.connect_pr", return_value=False):
            with mock.patch("crux.superpost._upsert"):
                problems = zenlink.attach(
                    Config(), "pr:example-org/Widget#12", [ticket(29)],
                    ["example-org/Widget#12"], [("example-org/Widget", 12)])
        self.assertEqual(problems, [])
        self.assertEqual(
            zenlink.get("pr:example-org/Widget#12").tickets[0].number, 29)

    def test_a_failed_note_is_reported_but_the_link_stands(self):
        with mock.patch("crux.zenhub.connect_pr", return_value=True):
            with mock.patch("crux.superpost._upsert",
                            side_effect=CruxError("no such repo")):
                problems = zenlink.attach(
                    Config(), "super:7", [ticket(29)], ["example-org/Widget#12"],
                    [("example-org/pr-bundles", 3)])
        self.assertEqual(len(problems), 1)
        self.assertIn("could not post the ticket note", problems[0])
        self.assertIsNotNone(zenlink.get("super:7"))

    def test_attaching_twice_does_not_duplicate_a_ticket(self):
        with mock.patch("crux.zenhub.connect_pr", return_value=True):
            with mock.patch("crux.superpost._upsert"):
                for _ in range(2):
                    zenlink.attach(Config(), "super:7", [ticket(29)],
                                   ["example-org/Widget#12"], [])
        self.assertEqual(len(zenlink.get("super:7").tickets), 1)

    def test_the_note_says_what_landing_closes(self):
        note = zenlink.render_note([ticket(29, "Photo uploads", url="http://x/29")])
        self.assertIn("Closes [example-org/Widget#29](http://x/29)", note)
        self.assertTrue(note.startswith("<!-- crux:zenhub -->"))


class CloseForTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = cfg_on(zenhub_workspace="e" * 24)

    def _link(self, key, prs):
        zenlink.put(ZenLink(key=key, tickets=[ticket(29)], prs=prs))

    def test_it_waits_for_every_pr_in_a_bundle(self):
        """A cross-repo ticket is not done while half its repos are unmerged."""
        self._link("super:7", ["example-org/Widget#1", "example-org/Gadget#2"])
        landed = {"example-org/Widget#1": True, "example-org/Gadget#2": False}
        with _KeyEnv():
            with mock.patch("crux.zenlink.pr_landed",
                            side_effect=lambda s, n: landed[f"{s}#{n}"]):
                with mock.patch("crux.zenhub.close_tickets") as closed:
                    self.assertEqual(zenlink.close_for(self.cfg, "super:7"), [])
        closed.assert_not_called()

    def test_it_closes_once_they_have_all_landed(self):
        self._link("super:7", ["example-org/Widget#1", "example-org/Gadget#2"])
        with _KeyEnv():
            with mock.patch("crux.zenlink.pr_landed", return_value=True):
                with mock.patch("crux.zenhub.close_tickets",
                                return_value=[]) as closed:
                    self.assertEqual(zenlink.close_for(self.cfg, "super:7"), [])
        closed.assert_called_once()
        self.assertTrue(zenlink.get("super:7").closed)

    def test_an_unreadable_pr_state_is_not_read_as_landed(self):
        """None is not True: a network blip must never close a live ticket."""
        self._link("super:7", ["example-org/Widget#1"])
        with _KeyEnv():
            with mock.patch("crux.zenlink.pr_landed", return_value=None):
                with mock.patch("crux.zenhub.close_tickets") as closed:
                    zenlink.close_for(self.cfg, "super:7")
        closed.assert_not_called()

    def test_force_skips_the_check(self):
        self._link("pr:example-org/Widget#1", ["example-org/Widget#1"])
        with _KeyEnv():
            with mock.patch("crux.zenlink.pr_landed") as asked:
                with mock.patch("crux.zenhub.close_tickets", return_value=[]):
                    zenlink.close_for(self.cfg, "pr:example-org/Widget#1",
                                      force=True)
        asked.assert_not_called()

    def test_it_is_idempotent(self):
        self._link("super:7", ["example-org/Widget#1"])
        with _KeyEnv():
            with mock.patch("crux.zenhub.close_tickets", return_value=[]) as closed:
                zenlink.close_for(self.cfg, "super:7", force=True)
                zenlink.close_for(self.cfg, "super:7", force=True)
        self.assertEqual(closed.call_count, 1)

    def test_a_failed_close_leaves_the_link_open_to_retry(self):
        self._link("super:7", ["example-org/Widget#1"])
        with _KeyEnv():
            with mock.patch("crux.zenhub.close_tickets", return_value=["bad"]):
                self.assertEqual(
                    zenlink.close_for(self.cfg, "super:7", force=True), ["bad"])
        self.assertFalse(zenlink.get("super:7").closed)

    def test_close_on_merge_off_closes_nothing(self):
        self._link("super:7", ["example-org/Widget#1"])
        cfg = cfg_on(zenhub_workspace="e" * 24, zenhub_close_on_merge=False)
        with _KeyEnv():
            with mock.patch("crux.zenhub.close_tickets") as closed:
                self.assertEqual(zenlink.close_for(cfg, "super:7", force=True), [])
        closed.assert_not_called()

    def test_zenhub_off_closes_nothing(self):
        self._link("super:7", ["example-org/Widget#1"])
        with mock.patch("crux.zenhub.close_tickets") as closed:
            self.assertEqual(zenlink.close_for(Config(), "super:7", force=True), [])
        closed.assert_not_called()


class PrLandedTest(unittest.TestCase):
    def test_merged_open_and_unreadable(self):
        with mock.patch("crux.post._run_gh",
                        return_value=json.dumps({"state": "closed", "merged": True})):
            self.assertIs(zenlink.pr_landed("example-org/Widget", 1), True)
        with mock.patch("crux.post._run_gh",
                        return_value=json.dumps({"state": "open", "merged": False})):
            self.assertIs(zenlink.pr_landed("example-org/Widget", 1), False)
        with mock.patch("crux.post._run_gh", side_effect=CruxError("offline")):
            self.assertIsNone(zenlink.pr_landed("example-org/Widget", 1))
        with mock.patch("crux.post._run_gh", return_value="{}"):
            self.assertIsNone(zenlink.pr_landed("example-org/Widget", 1))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = cfg_on(zenhub_workspace="f" * 24)
        zenlink.put(ZenLink(key="pr:example-org/Widget#1", tickets=[ticket(11)],
                            prs=["example-org/Widget#1"]))
        zenlink.put(ZenLink(key="pr:example-org/Gadget#2", tickets=[ticket(12)],
                            prs=["example-org/Gadget#2"]))

    def test_only_landed_links_close(self):
        landed = {"example-org/Widget#1": True, "example-org/Gadget#2": False}
        with _KeyEnv():
            with mock.patch("crux.zenlink.pr_landed",
                            side_effect=lambda s, n: landed[f"{s}#{n}"]):
                with mock.patch("crux.zenhub.close_tickets", return_value=[]):
                    done, problems = zenlink.sync(self.cfg)
        self.assertEqual(problems, [])
        self.assertEqual(len(done), 1)
        self.assertIn("pr:example-org/Widget#1", done[0])

    def test_dry_run_closes_nothing(self):
        with _KeyEnv():
            with mock.patch("crux.zenlink.pr_landed", return_value=True):
                with mock.patch("crux.zenhub.close_tickets") as closed:
                    done, _ = zenlink.sync(self.cfg, dry_run=True)
        closed.assert_not_called()
        self.assertEqual(len(done), 2)
        self.assertTrue(all("would close" in line for line in done))

    def test_already_closed_links_are_skipped(self):
        for key in ("pr:example-org/Widget#1", "pr:example-org/Gadget#2"):
            link = zenlink.get(key)
            link.closed = True
            zenlink.put(link)
        with _KeyEnv():
            with mock.patch("crux.zenlink.pr_landed") as asked:
                done, _ = zenlink.sync(self.cfg)
        asked.assert_not_called()
        self.assertEqual(done, [])

    def test_sync_says_why_when_zenhub_is_off(self):
        done, problems = zenlink.sync(Config())
        self.assertEqual(done, [])
        self.assertEqual(len(problems), 1)


# ---------------------------------------------------------------------------
# Wiring: config, credentials, and the merge path
# ---------------------------------------------------------------------------

class ConfigTest(unittest.TestCase):
    def test_the_zenhub_table_loads(self):
        import crux.config as config
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "crux.toml"), "w") as fh:
                fh.write('[zenhub]\nworkspace = "Engineering"\n'
                         'done_pipeline = "Closed"\nask = false\n'
                         'close_on_merge = false\ntoken_env = "ZH"\n')
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
                cfg = config.load(root)
        self.assertEqual(cfg.zenhub_workspace, "Engineering")
        self.assertEqual(cfg.zenhub_done_pipeline, "Closed")
        self.assertEqual(cfg.zenhub_token_env, "ZH")
        self.assertFalse(cfg.zenhub_ask)
        self.assertFalse(cfg.zenhub_close_on_merge)

    def test_defaults_leave_it_off(self):
        cfg = Config()
        self.assertEqual(cfg.zenhub_workspace, "")
        self.assertTrue(cfg.zenhub_ask)
        self.assertTrue(cfg.zenhub_close_on_merge)


class CredentialsTest(unittest.TestCase):
    def test_round_trip_preserves_other_secrets(self):
        import crux.credentials as credentials
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}):
                credentials.save_slack_bot_token("xoxb-1")
                credentials.save_zenhub_api_key("zh-1")
                self.assertEqual(credentials.get_zenhub_api_key(), "zh-1")
                self.assertEqual(credentials.get_slack_bot_token(), "xoxb-1")
                self.assertTrue(credentials.clear_zenhub_api_key())
                self.assertFalse(credentials.clear_zenhub_api_key())
                self.assertEqual(credentials.get_zenhub_api_key(), "")
                self.assertEqual(credentials.get_slack_bot_token(), "xoxb-1")


class MergePathTest(unittest.TestCase):
    """The close hangs off superact, so the terminal and the brief's Merge
    button cannot drift into two policies."""

    def _bundle(self, states):
        return Bundle(number=7, name="feature", home="example-org/pr-bundles",
                      issue=3, members=[
                          BundleMember(owner="example-org", repo=f"R{i}",
                                       branch="b", pr=i, state=state)
                          for i, state in enumerate(states, start=1)])

    def test_a_fully_landed_bundle_retires_its_tickets(self):
        import crux.superact as superact
        bundle = self._bundle(["merged", "merged"])
        with mock.patch("crux.superact.authored_by", return_value=[]):
            with mock.patch("crux.superact._approve", return_value=""):
                with mock.patch("crux.superpr.merge",
                                return_value=(bundle.members, "all landed")):
                    with mock.patch("crux.superact.retire_tickets") as retired:
                        superact.merge(bundle, Config(), login="reviewer")
        retired.assert_called_once_with("super:7", mock.ANY)

    def test_a_half_landed_bundle_retires_nothing(self):
        import crux.superact as superact
        bundle = self._bundle(["merged", "blocked"])
        with mock.patch("crux.superact.authored_by", return_value=[]):
            with mock.patch("crux.superact._approve", return_value=""):
                with mock.patch("crux.superpr.merge",
                                return_value=(bundle.members, "one blocked")):
                    with mock.patch("crux.superact.retire_tickets") as retired:
                        superact.merge(bundle, Config(), login="reviewer")
        retired.assert_not_called()

    def test_a_single_pr_merge_retires_its_own_key(self):
        import crux.superact as superact
        with mock.patch("crux.superact.pr_author", return_value="someone-else"):
            with mock.patch("crux.superact._approve", return_value=""):
                with mock.patch("crux.supermerge._merge_one",
                                return_value=(True, "merged")):
                    with mock.patch("crux.superact.retire_tickets") as retired:
                        superact.merge_pr("example-org/Widget", 12,
                                          login="reviewer")
        retired.assert_called_once_with("pr:example-org/Widget#12")

    def test_a_refused_merge_retires_nothing(self):
        import crux.superact as superact
        with mock.patch("crux.superact.pr_author", return_value="someone-else"):
            with mock.patch("crux.superact._approve", return_value=""):
                with mock.patch("crux.supermerge._merge_one",
                                return_value=(False, "is blocked")):
                    with mock.patch("crux.superact.retire_tickets") as retired:
                        superact.merge_pr("example-org/Widget", 12,
                                          login="reviewer")
        retired.assert_not_called()

    def test_retire_never_raises_after_a_merge_has_happened(self):
        import crux.superact as superact
        with mock.patch("crux.zenlink.close_for", side_effect=RuntimeError("boom")):
            with self.assertLogs("crux.superact", level="WARNING"):
                superact.retire_tickets("super:7", Config())


def make_info(branch="fix/issue-29-retry", head="a" * 40):
    from crux.models import RepoInfo
    return RepoInfo(root="/r", branch=branch, head_sha=head, base_sha="b" * 40,
                    owner="example-org", repo="Widget")


class PendingTest(unittest.TestCase):
    """The pre-push answer, held until the PR it belongs to exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch("pathlib.Path.home",
                           return_value=__import__("pathlib").Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.info = make_info()

    def test_round_trip(self):
        self.assertFalse(zenlink.pending(self.info))
        zenlink.record_pending(self.info, [ticket(29, "Photo uploads")])
        self.assertTrue(zenlink.pending(self.info))
        with mock.patch("crux.post._is_ancestor", return_value=True):
            back = zenlink.consume_pending(self.info)
        self.assertEqual([t.number for t in back], [29])

    def test_it_is_consumed_exactly_once(self):
        zenlink.record_pending(self.info, [ticket(29)])
        with mock.patch("crux.post._is_ancestor", return_value=True):
            self.assertEqual(len(zenlink.consume_pending(self.info)), 1)
            self.assertEqual(zenlink.consume_pending(self.info), [])
        self.assertFalse(zenlink.pending(self.info))

    def test_a_choice_recorded_for_work_this_branch_lost_is_discarded(self):
        zenlink.record_pending(self.info, [ticket(29)])
        with mock.patch("crux.post._is_ancestor", return_value=False):
            with self.assertLogs("crux.zenlink", level="INFO"):
                self.assertEqual(zenlink.consume_pending(self.info), [])

    def test_a_stale_choice_is_still_consumed(self):
        """It must not sit around waiting to surprise a later push."""
        zenlink.record_pending(self.info, [ticket(29)])
        with mock.patch("crux.post._is_ancestor", return_value=False):
            zenlink.consume_pending(self.info)
        self.assertFalse(zenlink.pending(self.info))

    def test_no_choice_reads_as_empty(self):
        self.assertEqual(zenlink.consume_pending(self.info), [])

    def test_branches_do_not_collide(self):
        other = make_info(branch="feat/other")
        zenlink.record_pending(self.info, [ticket(29)])
        self.assertFalse(zenlink.pending(other))

    def test_a_slash_in_the_branch_name_is_safe(self):
        path = zenlink.pending_path("example-org", "Widget", "fix/issue-29")
        self.assertNotIn("/", path.name)
        self.assertIn("fix__issue-29", path.name)

    def test_it_is_a_different_file_from_the_pr_intent(self):
        """post.consume_intent empties the intent file even when a PR already
        exists; a ticket choice sharing it would be thrown away."""
        import crux.post as post
        self.assertNotEqual(zenlink.pending_path("example-org", "Widget", "b"),
                            post._pr_intent_path(make_info(branch="b")))


class PrePushAskTest(unittest.TestCase):
    """The ask happens at pre-push, which is the only place a terminal exists
    on the push path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target in ("pathlib.Path.home",):
            patch = mock.patch(target,
                               return_value=__import__("pathlib").Path(self.tmp.name))
            patch.start()
            self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        import crux.cli as cli
        self.cli = cli
        self.info = make_info()

    def _ask(self, cfg, pr=None, tty=True):
        with mock.patch("crux.post.tty_available", return_value=tty):
            with mock.patch("crux.post._commit_subjects", return_value=[]):
                with mock.patch("crux.zenhub.open_issues") as listed:
                    listed.return_value = [ticket(29, "Photo uploads")]
                    with mock.patch("crux.post._prompt_tty", return_value="1"):
                        self.cli._zen_ask_prepush(self.info, cfg, pr)
        return listed

    def test_it_records_the_choice_for_the_detached_run(self):
        with _KeyEnv():
            self._ask(cfg_on())
        self.assertTrue(zenlink.pending(self.info))

    def test_zenhub_off_asks_nothing(self):
        self._ask(Config()).assert_not_called()

    def test_ask_false_asks_nothing(self):
        with _KeyEnv():
            self._ask(cfg_on(zenhub_ask=False)).assert_not_called()

    def test_no_terminal_costs_no_network_round_trip(self):
        """The tty is probed BEFORE the ticket list is fetched."""
        with _KeyEnv():
            with self.assertLogs("crux", level="INFO"):
                listed = self._ask(cfg_on(), tty=False)
        listed.assert_not_called()
        self.assertFalse(zenlink.pending(self.info))

    def test_an_existing_link_is_not_re_asked(self):
        zenlink.put(ZenLink(key="pr:example-org/Widget#42", tickets=[ticket(1)]))
        with _KeyEnv():
            self._ask(cfg_on(), pr=42).assert_not_called()

    def test_a_waiting_choice_is_not_re_asked_on_the_next_push(self):
        zenlink.record_pending(self.info, [ticket(29)])
        with _KeyEnv():
            self._ask(cfg_on()).assert_not_called()

    def test_a_zenhub_failure_never_blocks_the_push(self):
        with _KeyEnv():
            with mock.patch("crux.post.tty_available", return_value=True):
                with mock.patch("crux.post._commit_subjects", return_value=[]):
                    with mock.patch("crux.zenhub.open_issues",
                                    side_effect=CruxError("unreachable")):
                        with self.assertLogs("crux", level="WARNING"):
                            self.cli._zen_ask_prepush(self.info, cfg_on(), None)
        self.assertFalse(zenlink.pending(self.info))


class ApplyPendingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch("pathlib.Path.home",
                           return_value=__import__("pathlib").Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        import crux.cli as cli
        self.cli = cli
        self.info = make_info()

    def test_it_attaches_what_the_push_answered(self):
        zenlink.record_pending(self.info, [ticket(29, "Photo uploads")])
        with _KeyEnv():
            with mock.patch("crux.post._is_ancestor", return_value=True):
                with mock.patch("crux.zenhub.connect_pr", return_value=True):
                    with mock.patch("crux.superpost._upsert"):
                        with mock.patch("crux.post.notify_tty") as told:
                            handled = self.cli._zen_apply_pending(
                                cfg_on(), self.info, 42)
        self.assertTrue(handled)
        link = zenlink.get("pr:example-org/Widget#42")
        self.assertEqual([t.number for t in link.tickets], [29])
        self.assertIn("will close", told.call_args[0][0])

    def test_no_pending_choice_hands_back_to_the_picker(self):
        self.assertFalse(self.cli._zen_apply_pending(cfg_on(), self.info, 42))

    def test_a_choice_stranded_by_zenhub_being_turned_off_is_dropped(self):
        zenlink.record_pending(self.info, [ticket(29)])
        with mock.patch("crux.post._is_ancestor", return_value=True):
            with self.assertLogs("crux", level="INFO"):
                self.assertTrue(
                    self.cli._zen_apply_pending(Config(), self.info, 42))
        self.assertIsNone(zenlink.get("pr:example-org/Widget#42"))

    def test_it_never_raises_after_the_review_is_posted(self):
        zenlink.record_pending(self.info, [ticket(29)])
        with _KeyEnv():
            with mock.patch("crux.post._is_ancestor", return_value=True):
                with mock.patch("crux.zenlink.attach",
                                side_effect=RuntimeError("boom")):
                    with self.assertLogs("crux", level="WARNING"):
                        self.assertTrue(self.cli._zen_apply_pending(
                            cfg_on(), self.info, 42))


class NoteIfOffTest(unittest.TestCase):
    """When Crux opens a PR with Zenhub unconfigured, it says so once — as a
    note, since Zenhub is optional."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # No key anywhere: an empty credentials dir and no env var.
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ZENHUB_API_KEY", None)
        import crux.cli as cli
        self.cli = cli

    def _note(self, cfg):
        with mock.patch("crux.post.notify_tty") as told:
            self.cli._zen_note_if_off(cfg)
        return told

    def test_off_prints_a_note_naming_what_is_missing(self):
        told = self._note(Config())
        told.assert_called_once()
        text = told.call_args[0][0]
        self.assertIn("note", text)
        self.assertIn("[zenhub] workspace", text)
        self.assertIn("Optional", text)

    def test_ask_false_silences_it(self):
        self._note(Config(zenhub_ask=False)).assert_not_called()

    def test_configured_says_nothing(self):
        with _KeyEnv():
            self._note(cfg_on()).assert_not_called()


class TtyProbeTest(unittest.TestCase):
    def test_no_terminal_reads_as_unavailable(self):
        import crux.post as post
        with mock.patch("builtins.open", side_effect=OSError("no tty")):
            self.assertFalse(post.tty_available())

    def test_a_terminal_reads_as_available(self):
        import crux.post as post
        with mock.patch("builtins.open", mock.mock_open()):
            self.assertTrue(post.tty_available())


class OfferTest(unittest.TestCase):
    """The picker is offered only when every "stay out of the way" condition
    holds — the four ways this feature can be on without being in the way."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        import crux.cli as cli
        self.cli = cli

    def _offer(self, cfg):
        with mock.patch("crux.zenhub.open_issues") as listed:
            self.cli._zen_offer(cfg, "example-org/Widget#1",
                                "pr:example-org/Widget#1", ["branch"],
                                ["example-org/Widget#1"],
                                [("example-org/Widget", 1)])
        return listed

    def test_zenhub_off_asks_nothing(self):
        self._offer(Config()).assert_not_called()

    def test_ask_false_asks_nothing(self):
        with _KeyEnv():
            self._offer(cfg_on(zenhub_ask=False)).assert_not_called()

    def test_an_existing_link_is_not_re_asked(self):
        zenlink.put(ZenLink(key="pr:example-org/Widget#1", tickets=[ticket(1)]))
        with _KeyEnv():
            self._offer(cfg_on()).assert_not_called()

    def test_a_failure_while_linking_never_sinks_the_run(self):
        """A review that worked must not report failure because a ticket did
        not link."""
        with _KeyEnv():
            with mock.patch("crux.zenhub.open_issues",
                            side_effect=CruxError("zenhub is unwell")):
                with self.assertLogs("crux", level="WARNING"):
                    self.cli._zen_offer(cfg_on(), "example-org/Widget#1",
                                        "pr:example-org/Widget#1", ["b"],
                                        ["example-org/Widget#1"], [])

    def test_declining_the_picker_records_nothing(self):
        with _KeyEnv():
            with mock.patch("crux.zenhub.open_issues", return_value=[ticket(1)]):
                with mock.patch("crux.post._prompt_tty", return_value=""):
                    self.cli._zen_offer(cfg_on(), "example-org/Widget#1",
                                        "pr:example-org/Widget#1", ["b"],
                                        ["example-org/Widget#1"], [])
        self.assertIsNone(zenlink.get("pr:example-org/Widget#1"))

    def test_a_bundle_offer_notes_the_brief_and_every_member(self):
        bundle = Bundle(number=7, home="example-org/pr-bundles", issue=3, members=[
            BundleMember(owner="example-org", repo="Widget", branch="b", pr=101),
            BundleMember(owner="example-org", repo="Gadget", branch="b", pr=102)])
        self.assertEqual(
            self.cli._zen_note_targets(bundle),
            [("example-org/pr-bundles", 3), ("example-org/Widget", 101),
             ("example-org/Gadget", 102)])

    def test_a_bundle_with_no_brief_yet_notes_only_its_members(self):
        bundle = Bundle(number=7, home="example-org/pr-bundles", issue=None,
                        members=[BundleMember(owner="example-org", repo="Widget",
                                              branch="b", pr=101)])
        self.assertEqual(self.cli._zen_note_targets(bundle),
                         [("example-org/Widget", 101)])


if __name__ == "__main__":
    unittest.main()
