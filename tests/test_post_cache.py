# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.post and crux.cache. All subprocess and tty use is mocked."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crux import cache, post
from crux.models import (
    CARD_MARKER,
    STATE_VERSION,
    TEST_MARKER,
    Annotation,
    AuditRow,
    Badge,
    ChangeMap,
    Claim,
    Config,
    DagEdge,
    DagNode,
    Item,
    MapArrow,
    MapStep,
    NodeAnnotation,
    PostError,
    RepoInfo,
    RunState,
    Tier,
    Verdict,
)


def make_info(branch: str = "feat/buffering", base_sha: str = "b" * 40,
              crux_base: str | None = None) -> RepoInfo:
    return RepoInfo(
        root="/repo",
        branch=branch,
        head_sha="h" * 40,
        base_sha=base_sha,
        owner="example-org",
        repo="crux",
        crux_base=crux_base,
    )


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeTty:
    """Duck-types the open('/dev/tty') handles used by _prompt_tty.

    _prompt_tty opens two separate handles (write-only + read-only); the same
    instance stands in for both here, receiving the write and serving the read.
    """

    def __init__(self, reply: str):
        self.reply = reply
        self.written = ""

    def write(self, text: str) -> None:
        self.written += text

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        return self.reply

    def __enter__(self) -> "FakeTty":
        return self

    def __exit__(self, *exc) -> bool:
        return False


# ---------------------------------------------------------------------------
# post.find_pr
# ---------------------------------------------------------------------------

def pr_rows(*pairs: tuple[int, str]) -> str:
    return json.dumps([{"number": n, "baseRefName": b} for n, b in pairs])


class FindPrTests(unittest.TestCase):
    def test_returns_the_open_pr_number(self) -> None:
        with mock.patch("crux.post.subprocess.run") as run:
            run.return_value = completed(stdout=pr_rows((42, "main")))
            self.assertEqual(post.find_pr(make_info()), 42)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            ["gh", "pr", "list", "--head", "feat/buffering",
             "--json", "number,baseRefName", "--state", "open"],
        )
        self.assertEqual(run.call_args.kwargs.get("cwd"), "/repo")

    def test_returns_none_when_no_pr(self) -> None:
        with mock.patch("crux.post.subprocess.run", return_value=completed(stdout="[]")):
            self.assertIsNone(post.find_pr(make_info()))

    def test_failure_raises_posterror_with_stderr_excerpt(self) -> None:
        with mock.patch(
            "crux.post.subprocess.run",
            return_value=completed(returncode=1, stderr="HTTP 401: Bad credentials"),
        ):
            with self.assertRaises(PostError) as ctx:
                post.find_pr(make_info())
        self.assertIn("Bad credentials", str(ctx.exception))

    def test_missing_gh_binary_falls_back_to_rest(self) -> None:
        """No gh (cloud session): the lookup degrades to the REST fallback —
        with no token configured either, the error says how to fix it."""
        with mock.patch("crux.post.subprocess.run", side_effect=FileNotFoundError("gh")), \
             mock.patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}), \
             mock.patch("crux.ghrest.load_credentials", return_value={}):
            with self.assertRaises(PostError) as ctx:
                post.find_pr(make_info())
        self.assertIn("GH_TOKEN", str(ctx.exception))

    def test_unparseable_json_raises_posterror(self) -> None:
        with mock.patch("crux.post.subprocess.run", return_value=completed(stdout="oops")):
            with self.assertRaises(PostError):
                post.find_pr(make_info())

    def test_non_list_json_raises_posterror(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout='{"message": "nope"}')):
            with self.assertRaises(PostError):
                post.find_pr(make_info())


class ChoosePrTests(unittest.TestCase):
    """One head branch can carry several open PRs (stacked work). Which one
    Crux reviews must be the same on every push, not whatever gh listed first."""

    def choose(self, rows: str, **kw) -> int | None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout=rows)):
            return post.find_pr(make_info(**kw))

    def test_prefers_the_pr_whose_base_we_are_reviewing_against(self) -> None:
        info = make_info()
        info.base_branch = "parent"
        with mock.patch("crux.post.subprocess.run", return_value=completed(
                stdout=pr_rows((9, "main"), (12, "parent")))):
            self.assertEqual(post.find_pr(info), 12)

    def test_falls_back_to_the_recorded_parent(self) -> None:
        self.assertEqual(
            self.choose(pr_rows((9, "main"), (12, "develop")),
                        crux_base="develop"), 12)

    def test_then_to_the_default_branch(self) -> None:
        self.assertEqual(
            self.choose(pr_rows((12, "release/2.0"), (9, "main"))), 9)

    def test_no_base_matches_takes_the_oldest(self) -> None:
        # Stable as new PRs are opened: a later PR never steals the card.
        self.assertEqual(
            self.choose(pr_rows((12, "release/2.0"), (9, "topic"))), 9)

    def test_order_from_gh_does_not_matter(self) -> None:
        forward = self.choose(pr_rows((9, "topic"), (12, "release/2.0")))
        reverse = self.choose(pr_rows((12, "release/2.0"), (9, "topic")))
        self.assertEqual(forward, reverse)

    def test_ambiguity_is_logged_with_every_candidate(self) -> None:
        with self.assertLogs("crux.post", level="WARNING") as caught:
            self.choose(pr_rows((9, "topic"), (12, "main")))
        message = "\n".join(caught.output)
        self.assertIn("#9 -> topic", message)
        self.assertIn("#12 -> main", message)
        self.assertIn("--pr", message)  # how to override it

    def test_a_single_pr_is_not_reported_as_ambiguous(self) -> None:
        with mock.patch("crux.post.log") as log:
            self.assertEqual(self.choose(pr_rows((9, "topic"))), 9)
        log.warning.assert_not_called()

    def test_unreadable_rows_are_skipped_not_fatal(self) -> None:
        rows = json.dumps([{"baseRefName": "main"}, "junk",
                           {"number": "x"}, {"number": 5, "baseRefName": "main"}])
        self.assertEqual(self.choose(rows), 5)

    def test_rows_without_a_usable_number_mean_no_pr(self) -> None:
        self.assertIsNone(self.choose(json.dumps([{"baseRefName": "main"}])))

    def test_missing_base_ref_still_selectable(self) -> None:
        self.assertEqual(self.choose(json.dumps([{"number": 5}])), 5)


# ---------------------------------------------------------------------------
# post.ensure_pr
# ---------------------------------------------------------------------------

def dispatching_run(pr_list_stdout: str = "[]",
                    create_stdout: str = "https://github.com/example-org/crux/pull/55\n",
                    missing_branches: tuple[str, ...] = ()):
    """subprocess.run side_effect handling gh pr list / gh pr create / git log.

    `gh api repos/.../branches/<b>` is the base-existence probe (_resolve_base):
    branches in *missing_branches* answer 404, everything else exists.
    """
    def fake_run(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "list"]:
            return completed(stdout=pr_list_stdout)
        if argv[:2] == ["gh", "api"] and "/branches/" in argv[2]:
            branch = argv[2].rsplit("/branches/", 1)[1]
            if branch in missing_branches:
                return completed(returncode=1, stderr="gh: Not Found (HTTP 404)")
            return completed(stdout=json.dumps({"name": branch}))
        if argv[:3] == ["gh", "pr", "create"]:
            return completed(stdout=create_stdout)
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return completed()  # the recorded PR intent is still this branch's
        if argv[0] == "git":
            return completed(stdout="feat: add write buffering\n")
        raise AssertionError(f"unexpected argv: {argv}")
    return fake_run


class PrBaseTests(unittest.TestCase):
    """D34: the PR's own base branch, the authority for the review's diff."""

    def test_returns_the_base_ref(self) -> None:
        with mock.patch("crux.post.subprocess.run") as run:
            run.return_value = completed(stdout="release/2.0\n")
            self.assertEqual(post.pr_base(make_info(), 13), "release/2.0")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["gh", "api", "repos/example-org/crux/pulls/13",
                                "--jq", ".base.ref"])
        self.assertEqual(run.call_args.kwargs.get("cwd"), "/repo")
        # runs on the foreground path too: it must not hang a push
        self.assertEqual(run.call_args.kwargs.get("timeout"),
                         post._GH_FOREGROUND_TIMEOUT)

    def test_best_effort_none_on_failure(self) -> None:
        # A failed lookup must leave the local guess in place, never raise.
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(returncode=1, stderr="boom")):
            self.assertIsNone(post.pr_base(make_info(), 13))
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="  \n")):
            self.assertIsNone(post.pr_base(make_info(), 13))

    def test_missing_gh_is_not_fatal(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        side_effect=FileNotFoundError("gh")):
            self.assertIsNone(post.pr_base(make_info(), 13))


class RemoteBranchProbeTests(unittest.TestCase):
    """D34: only a real 404 means a base branch is gone."""

    def test_404_is_missing(self) -> None:
        with mock.patch("crux.post.subprocess.run", return_value=completed(
                returncode=1, stderr="gh: Branch not found (HTTP 404)")):
            self.assertTrue(post._remote_branch_missing(make_info(), "develop"))

    def test_existing_branch_is_not_missing(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout='{"name": "develop"}')):
            self.assertFalse(post._remote_branch_missing(make_info(), "develop"))

    def test_transient_failures_are_not_missing(self) -> None:
        # Bad credentials, a 5xx or a timeout say nothing about the branch —
        # reading them as "deleted" silently retargets a stacked PR to main.
        for stderr in ("HTTP 401: Bad credentials",
                       "HTTP 502: Bad gateway",
                       "dial tcp: lookup api.github.com: no such host"):
            with self.subTest(stderr=stderr):
                with mock.patch("crux.post.subprocess.run", return_value=completed(
                        returncode=1, stderr=stderr)):
                    self.assertFalse(
                        post._remote_branch_missing(make_info(), "develop"))

    def test_a_transient_failure_keeps_the_intended_base(self) -> None:
        info = make_info(crux_base="develop")
        calls: list[list[str]] = []

        def recording(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["gh", "api"] and "/branches/" in argv[2]:
                return completed(returncode=1, stderr="HTTP 401: Bad credentials")
            return dispatching_run()(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            self.assertEqual(post.ensure_pr(info, Config(pr_auto_create=True)), 55)
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "develop")


class ProgressCardTests(unittest.TestCase):
    def test_names_the_base_the_diff_is_actually_against(self) -> None:
        # D34: base_branch (the PR's own base once aligned), not crux_base —
        # telling a reviewer the review is "against parent" when it is against
        # main is exactly the confusion this comment is meant to remove.
        info = make_info(crux_base="parent")
        info.base_branch = "main"
        self.assertIn("`main`", post.progress_card(info))
        self.assertNotIn("`parent`", post.progress_card(info))

    def test_falls_back_to_the_recorded_parent_then_the_default(self) -> None:
        self.assertIn("`parent`", post.progress_card(make_info(crux_base="parent")))
        self.assertIn("`main`", post.progress_card(make_info()))

    def test_carries_the_card_marker_so_the_review_replaces_it(self) -> None:
        self.assertIn(CARD_MARKER, post.progress_card(make_info()))


class PrMetaTests(unittest.TestCase):
    """Both Slack facts come from ONE `gh pr view` (D37): the REST pulls
    endpoint returns a simple-user object with no `name`, so the author's real
    name is only reachable through the GraphQL-backed command."""

    def test_returns_created_at_and_author_name(self) -> None:
        with mock.patch("crux.post.subprocess.run") as run:
            run.return_value = completed(stdout=json.dumps({
                "createdAt": "2026-07-23T02:05:25Z",
                "author": {"login": "fixture-alpha", "name": "Fixture Alpha"}}))
            self.assertEqual(post.pr_meta(make_info(), 13),
                             ("2026-07-23T02:05:25Z", "Fixture Alpha"))
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["gh", "pr", "view", "13", "--repo",
                                "example-org/crux", "--json", "author,createdAt"])

    def test_login_is_the_fallback_when_no_display_name(self) -> None:
        # Real case: an account with no profile name set. A login beats "".
        with mock.patch("crux.post.subprocess.run") as run:
            run.return_value = completed(stdout=json.dumps({
                "createdAt": "x", "author": {"login": "nonamedev", "name": ""}}))
            self.assertEqual(post.pr_meta(make_info(), 13)[1], "nonamedev")

    def test_best_effort_empty_on_any_failure(self) -> None:
        # gh error and unparseable JSON both degrade to ("", "") — the caller
        # only loses the credit and the wording, never the announce itself.
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(returncode=1, stderr="boom")):
            self.assertEqual(post.pr_meta(make_info(), 13), ("", ""))
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="not json")):
            self.assertEqual(post.pr_meta(make_info(), 13), ("", ""))


class PrMetadataTests(unittest.TestCase):
    def test_title_humanizes_branch_for_several_commits(self) -> None:
        # git log --format=%B%x00: full messages, NUL-separated (D27).
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(
                            stdout="latest\n\x00middle\n\x00original\n\x00")):
            title = post.pr_title_from_commits(
                make_info(branch="feat/123-add-write-buffer"))
        self.assertEqual(title, "Add write buffer")

    def test_title_single_commit_uses_its_subject(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="just one\n\x00")):
            self.assertEqual(post.pr_title_from_commits(make_info()), "just one")

    def test_title_single_amended_commit_uses_the_crux_subject(self) -> None:
        # D27: the Crux-written subject outranks the terse human line above it.
        amended = ("fix\n\nCrux: Buffer writes so a crash loses at most 5s\n\n"
                   "Amended-by: Crux\n\x00")
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout=amended)):
            self.assertEqual(post.pr_title_from_commits(make_info()),
                             "Buffer writes so a crash loses at most 5s")

    def test_title_generic_branch_falls_back_to_newest_commit(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="newest\n\x00older\n\x00")):
            self.assertEqual(
                post.pr_title_from_commits(make_info(branch="wip")), "newest")

    def test_body_carries_marker_and_commits(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="add a\n\x00fix b\n\x00")):
            body = post.pr_body_from_commits(make_info())
        self.assertIn(post._PR_BODY_MARKER, body)
        self.assertIn("- add a", body)

    def test_body_prefers_crux_subjects_for_amended_commits(self) -> None:
        amended = ("add a\n\x00"
                   "wip\n\nCrux: Retry failed flushes with backoff\n\n"
                   "- details here\n\nAmended-by: Crux\n\x00")
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout=amended)):
            body = post.pr_body_from_commits(make_info())
        self.assertIn("- Retry failed flushes with backoff", body)
        self.assertNotIn("- wip", body)

    def test_body_links_review_and_test_comments(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="add a\n\x00")):
            body = post.pr_body_from_commits(
                make_info(), review_url="https://gh/pull/7#c1",
                test_url="https://gh/pull/7#c2")
        self.assertIn("[Crux review](https://gh/pull/7#c1)", body)
        self.assertIn("[How to test this](https://gh/pull/7#c2)", body)

    def test_body_from_annotation_renders_summary_and_bullets(self) -> None:
        body = post.pr_body_from_annotation(
            "Makes the review fully automatic.",
            ["Creates the PR itself — `crux/post.py:76`",
             "⚠️ Hooks now apply to every repo — `crux/cli.py:462`"],
            review_url="https://gh/pull/7#c1")
        self.assertIn(post._PR_BODY_LLM_MARKER, body)
        self.assertIn("Makes the review fully automatic.", body)
        self.assertIn("- ⚠️ Hooks now apply to every repo", body)
        self.assertIn("[Crux review](https://gh/pull/7#c1)", body)
        self.assertNotIn(post._PR_BODY_MARKER + "\n", body)

    def test_body_from_annotation_empty_when_no_content(self) -> None:
        self.assertEqual(post.pr_body_from_annotation("", ["  ", ""]), "")

    def test_body_from_annotation_links_code_pointers(self) -> None:
        # D32: the `path:line` ending each bullet is a link in the description
        # too, as it already is on the card — unlinked it is only clutter.
        info = make_info()
        body = post.pr_body_from_annotation(
            "Buffers writes.", ["Writes batch through a buffer — `a/buffer.py:42`"],
            info=info)
        self.assertIn(
            f"[`a/buffer.py:42`](https://github.com/{info.owner}/{info.repo}"
            f"/blob/{info.head_sha}/a/buffer.py#L42)", body)

    def test_body_footer_is_one_line(self) -> None:
        # D32: the keep-hint already says Crux wrote the body, so the separate
        # provenance line it used to sit under is gone from both body styles.
        annotated = post.pr_body_from_annotation("Buffers writes.", ["idea"])
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="add a\n\x00")):
            from_commits = post.pr_body_from_commits(make_info())
        for body in (annotated, from_commits):
            self.assertNotIn("_Description written by Crux", body)
            self.assertNotIn("_Description generated by Crux", body)
            self.assertTrue(body.endswith(post._PR_KEEP_HINT))
            self.assertEqual(body.count("<sub>"), 1)  # one small-print footer

    def test_human_opted_out_only_on_comment_form(self) -> None:
        # The real opt-out: the HTML comment, case- and whitespace-tolerant.
        self.assertTrue(post._human_opted_out("keep this\n<!-- crux:keep -->"))
        self.assertTrue(post._human_opted_out("<!--crux:keep-->"))
        self.assertTrue(post._human_opted_out("<!--  CRUX:KEEP  -->"))
        # A bare mention (commit subject, prose) is NOT an opt-out.
        self.assertFalse(post._human_opted_out("Add crux:keep opt-out (D26)"))
        self.assertFalse(post._human_opted_out("we support crux:keep now"))
        self.assertFalse(post._human_opted_out(""))

    def test_crux_own_hint_is_not_an_opt_out(self) -> None:
        # Crux appends the hint (which shows the comment) to every body it
        # writes; that must never read as the human opting out.
        self.assertFalse(post._human_opted_out(
            f"## Summary\n\n- did a thing\n\n{post._PR_KEEP_HINT}"))
        # …but a human comment ADDED alongside the hint still opts out.
        self.assertTrue(post._human_opted_out(
            f"{post._PR_KEEP_HINT}\n<!-- crux:keep -->"))

    def test_find_comment_urls_maps_each_marker(self) -> None:
        comments = json.dumps([
            {"body": f"{CARD_MARKER}\nthe card", "html_url": "url-card"},
            {"body": f"{TEST_MARKER}\nsteps", "html_url": "url-test"},
            {"body": "a human comment", "html_url": "url-human"},
        ])
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout=comments)):
            urls = post.find_comment_urls(make_info(), 7,
                                          [CARD_MARKER, TEST_MARKER])
        self.assertEqual(urls[CARD_MARKER], "url-card")
        self.assertEqual(urls[TEST_MARKER], "url-test")

    def _dispatch(self, current: dict, calls: list):
        def run(argv, **kw):
            calls.append((argv, kw))
            if argv[0] == "git":
                return completed(stdout="new title\n")
            if argv[:2] == ["gh", "api"] and "-X" not in argv:  # GET the PR
                return completed(stdout=json.dumps(current))
            return completed(stdout="{}")                         # PATCH
        return run

    def test_sync_refreshes_description_but_not_title_by_default(self) -> None:
        # title=None (the default / pre-review sync): body updates, title is
        # left alone so a good LLM title is never downgraded.
        calls: list = []
        current = {"title": "old title",
                   "body": f"{post._PR_BODY_MARKER}\nold body"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7)
        patch = next((a, k) for a, k in calls if "PATCH" in a)
        payload = json.loads(patch[1]["input"])
        self.assertNotIn("title", payload)
        self.assertIn("new title", payload["body"])

    def test_sync_sets_title_when_given(self) -> None:
        calls: list = []
        current = {"title": "old title",
                   "body": f"{post._PR_BODY_MARKER}\nold body"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7, title="A concise real title")
        payload = json.loads(next(k for a, k in calls if "PATCH" in a)["input"])
        self.assertEqual(payload["title"], "A concise real title")

    def test_sync_updates_legacy_crux_body_without_marker(self) -> None:
        # PRs opened before the marker existed still carry a Crux signature and
        # must remain manageable.
        calls: list = []
        current = {"title": "old",
                   "body": "## Summary\n\n- x\n\n_Description generated by Crux "
                           "from the branch's commits._"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7)
        self.assertTrue(any("PATCH" in a for a, _ in calls))

    def test_sync_overwrites_human_description_by_default(self) -> None:
        # D26: a human rewrite no longer stops the sync — the description is
        # re-synced anyway (opting out takes the explicit crux:keep token).
        calls: list = []
        current = {"title": "human title", "body": "I wrote this myself"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7)
        payload = json.loads(next(k for a, k in calls if "PATCH" in a)["input"])
        self.assertIn("new title", payload["body"])  # commit-based body wins

    def test_sync_overwrites_human_title_with_llm_title(self) -> None:
        calls: list = []
        current = {"title": "human title", "body": "my own words"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7, title="LLM title",
                                  summary="What the branch does.",
                                  overview=["The one big idea — `a.py:1`"])
        payload = json.loads(next(k for a, k in calls if "PATCH" in a)["input"])
        self.assertEqual(payload["title"], "LLM title")
        self.assertIn("What the branch does.", payload["body"])

    def test_sync_keep_comment_blocks_title_and_body(self) -> None:
        # D26 escape hatch: the <!-- crux:keep --> comment => no PATCH at all,
        # even when an LLM title and review annotation are on offer.
        calls: list = []
        current = {"title": "human title",
                   "body": "My description.\n\n<!-- crux:keep -->"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7, title="LLM title",
                                  summary="s", overview=["o — `a.py:1`"])
        self.assertFalse(any("PATCH" in a for a, _ in calls))

    def test_sync_does_not_self_lock_on_generated_body(self) -> None:
        # D26 self-lockout guard: Crux's OWN body — commit subject mentioning
        # the token, plus the always-appended discoverability hint — must NOT
        # read as an opt-out, so the sync still refreshes it.
        calls: list = []
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="Add crux:keep opt-out (D26)\n")):
            generated = post.pr_body_from_commits(make_info())
        self.assertIn(post._PR_KEEP_HINT, generated)      # hint present
        self.assertIn("crux:keep", generated)             # token present in text
        self.assertFalse(post._human_opted_out(generated))  # but not an opt-out
        current = {"title": "old", "body": generated}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7, title="A real LLM title")
        payload = json.loads(next(k for a, k in calls if "PATCH" in a)["input"])
        self.assertEqual(payload["title"], "A real LLM title")

    def test_sync_bare_token_mention_does_not_block(self) -> None:
        # A human PR body that merely mentions the token in prose is overwritten
        # (only the comment form opts out) — proves no accidental lockout.
        calls: list = []
        current = {"title": "t", "body": "this PR adds the crux:keep feature"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7)
        self.assertTrue(any("PATCH" in a for a, _ in calls))

    def test_sync_upgrades_description_from_review(self) -> None:
        # Post-review sync: the annotation's summary + overview replace the
        # commit list as the description.
        calls: list = []
        current = {"title": "old title",
                   "body": f"{post._PR_BODY_MARKER}\n## Summary\n\n- test crux"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7,
                                  summary="Automates the review flow.",
                                  overview=["The PR is created for you — `a.py:1`"])
        payload = json.loads(next(k for a, k in calls if "PATCH" in a)["input"])
        self.assertIn(post._PR_BODY_LLM_MARKER, payload["body"])
        self.assertIn("Automates the review flow.", payload["body"])
        self.assertNotIn("test crux", payload["body"])

    def test_sync_never_downgrades_llm_description(self) -> None:
        # A pre-review commit sync must leave a review-written body alone.
        calls: list = []
        current = {"title": "t",
                   "body": f"{post._PR_BODY_LLM_MARKER}\ncurated description"}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7)
        self.assertFalse(any("PATCH" in a for a, _ in calls))

    def test_sync_noop_when_unchanged(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="new title\n")):
            body = post.pr_body_from_commits(make_info())
        calls: list = []
        current = {"title": "new title", "body": body}
        with mock.patch("crux.post.subprocess.run",
                        side_effect=self._dispatch(current, calls)):
            post.sync_pr_metadata(make_info(), 7)
        self.assertFalse(any("PATCH" in a for a, _ in calls))


class NotifyTtyTests(unittest.TestCase):
    def test_writes_a_line_to_dev_tty(self) -> None:
        opener = mock.mock_open()
        with mock.patch("builtins.open", opener):
            post.notify_tty("hello there")
        opener.assert_called_once_with("/dev/tty", "w", encoding="utf-8",
                                       errors="replace")
        written = "".join(c.args[0] for c in opener().write.call_args_list)
        self.assertIn("hello there", written)

    def test_no_tty_is_silent(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("no tty")):
            post.notify_tty("hello")  # must not raise

    def test_inherited_fd_env_bypasses_dev_tty(self) -> None:
        # A detached run can't open /dev/tty by name, so it writes to the
        # inherited CRUX_TTY_FD instead of opening the terminal by path.
        with mock.patch.dict(os.environ, {"CRUX_TTY_FD": "7"}), \
             mock.patch("crux.post.os.write") as osw, \
             mock.patch("builtins.open", side_effect=AssertionError("no open")):
            post.notify_tty("hello there")
        fd, data = osw.call_args.args
        self.assertEqual(fd, 7)
        self.assertIn(b"hello there", data)

    def test_falls_back_to_tty_when_inherited_fd_stale(self) -> None:
        # A closed/stale inherited fd falls back to opening the terminal by name.
        opener = mock.mock_open()
        with mock.patch.dict(os.environ, {"CRUX_TTY_FD": "7"}), \
             mock.patch("crux.post.os.write", side_effect=OSError("EBADF")), \
             mock.patch("builtins.open", opener):
            post.notify_tty("hello")
        opener.assert_called_once_with("/dev/tty", "w", encoding="utf-8",
                                       errors="replace")


class OpenTerminalFdTests(unittest.TestCase):
    def test_opens_write_fd_to_terminal(self) -> None:
        with mock.patch("crux.post.os.name", "posix"), \
             mock.patch("crux.post.os.open", return_value=9) as opn:
            self.assertEqual(post.open_terminal_fd(), 9)
        dev, flags = opn.call_args.args
        self.assertEqual(dev, "/dev/tty")
        self.assertTrue(flags & os.O_WRONLY)
        self.assertTrue(flags & os.O_NOCTTY)

    def test_none_when_no_controlling_terminal(self) -> None:
        with mock.patch("crux.post.os.name", "posix"), \
             mock.patch("crux.post.os.open", side_effect=OSError("no ctty")):
            self.assertIsNone(post.open_terminal_fd())

    def test_none_on_windows(self) -> None:
        with mock.patch("crux.post.os.name", "nt"):
            self.assertIsNone(post.open_terminal_fd())


class SetStatusTests(unittest.TestCase):
    def test_posts_status_to_head_sha(self) -> None:
        calls: list[tuple] = []

        def rec(argv, **kw):
            calls.append((argv, kw))
            return completed(stdout="{}")

        with mock.patch("crux.post.subprocess.run", side_effect=rec):
            post.set_status(make_info(), 7, "pending", "Reviewing this push…")
        argv, kw = calls[0]
        self.assertEqual(argv[:4], ["gh", "api", "-X", "POST"])
        self.assertIn(f"repos/example-org/crux/statuses/{'h' * 40}", argv)
        payload = json.loads(kw["input"])
        self.assertEqual(payload["state"], "pending")
        self.assertEqual(payload["context"], "Crux review")
        self.assertIn("/pull/7", payload["target_url"])

    def test_failure_is_swallowed(self) -> None:
        # A missing/failed status must never break the run.
        with mock.patch("crux.post._run_gh",
                        side_effect=post.PostError("no status scope")):
            post.set_status(make_info(), 7, "success", "done")  # no raise


class EnsurePrTests(unittest.TestCase):
    def test_existing_pr_returned_without_prompt(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout='[{"number": 9}]')):
            with mock.patch("crux.post._prompt_tty") as prompt:
                self.assertEqual(post.ensure_pr(make_info(), Config()), 9)
        prompt.assert_not_called()

    def test_non_interactive_skips_prompt_and_returns_none(self) -> None:
        with mock.patch("crux.post.subprocess.run", return_value=completed(stdout="[]")):
            with mock.patch("crux.post._prompt_tty") as prompt:
                self.assertIsNone(post.ensure_pr(make_info(), Config(), interactive=False))
        prompt.assert_not_called()

    def test_no_tty_returns_none(self) -> None:
        with mock.patch("crux.post.subprocess.run", return_value=completed(stdout="[]")):
            with mock.patch("crux.post._prompt_tty", return_value=None):
                self.assertIsNone(post.ensure_pr(make_info(), Config()))

    def test_enter_creates_pr_into_default_base(self) -> None:
        calls: list[list[str]] = []
        fake = dispatching_run()

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            with mock.patch("crux.post._prompt_tty", return_value="") as prompt:
                number = post.ensure_pr(make_info(), Config())
        self.assertEqual(number, 55)
        message = prompt.call_args.args[0]
        self.assertIn("feat/buffering", message)
        self.assertIn("main", message)
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(
            create[:9],
            ["gh", "pr", "create", "--base", "main", "--head", "feat/buffering",
             "--title", "feat: add write buffering"],
        )
        self.assertEqual(create[9], "--body")
        # body is a quick commit-based summary, not a static string
        body = create[10]
        self.assertIn("feat: add write buffering", body)
        self.assertIn("Summary", body)

    def test_y_answer_creates_pr(self) -> None:
        with mock.patch("crux.post.subprocess.run", side_effect=dispatching_run()):
            with mock.patch("crux.post._prompt_tty", return_value="Y"):
                self.assertEqual(post.ensure_pr(make_info(), Config()), 55)

    def test_n_answer_returns_none_without_create(self) -> None:
        calls: list[list[str]] = []
        fake = dispatching_run()

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            with mock.patch("crux.post._prompt_tty", return_value="n"):
                self.assertIsNone(post.ensure_pr(make_info(), Config()))
        self.assertFalse(any(argv[:3] == ["gh", "pr", "create"] for argv in calls))

    def test_other_text_used_as_base_branch(self) -> None:
        calls: list[list[str]] = []
        fake = dispatching_run()

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            with mock.patch("crux.post._prompt_tty", return_value="release/2.0"):
                self.assertEqual(post.ensure_pr(make_info(), Config()), 55)
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "release/2.0")

    def test_default_base_prefers_crux_base(self) -> None:
        self.assertEqual(post.default_base(make_info(crux_base="develop"),
                                          Config()), "develop")
        # falls back to the configured default when the parent is unknown
        self.assertEqual(post.default_base(make_info(), Config()), "main")

    def test_prompt_and_pr_target_the_crux_base(self) -> None:
        calls: list[list[str]] = []
        fake = dispatching_run()

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        info = make_info(crux_base="develop")
        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            with mock.patch("crux.post._prompt_tty", return_value="") as prompt:
                self.assertEqual(post.ensure_pr(info, Config()), 55)
        # the prompt names the recorded parent, and enter targets it
        self.assertIn("develop", prompt.call_args.args[0])
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "develop")

    def test_auto_create_skips_prompt_and_targets_crux_base(self) -> None:
        cfg = Config(pr_auto_create=True)
        info = make_info(crux_base="develop")
        with mock.patch("crux.post.subprocess.run", side_effect=dispatching_run()):
            with mock.patch("crux.post._prompt_tty",
                            side_effect=AssertionError("must not prompt")) as p:
                self.assertEqual(post.ensure_pr(info, cfg), 55)
        p.assert_not_called()

    def test_auto_create_non_interactive_without_intent_creates(self) -> None:
        cfg = Config(pr_auto_create=True)
        info = make_info(crux_base="develop")
        calls: list[list[str]] = []
        fake = dispatching_run()

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            self.assertEqual(post.ensure_pr(info, cfg, interactive=False), 55)
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "develop")

    def test_no_auto_create_non_interactive_without_intent_returns_none(self) -> None:
        info = make_info(crux_base="develop")
        with mock.patch("crux.post.subprocess.run",
                        side_effect=dispatching_run()) as run:
            self.assertIsNone(post.ensure_pr(info, Config(), interactive=False))
        self.assertFalse(any(c.args[0][:3] == ["gh", "pr", "create"]
                             for c in run.call_args_list))

    def test_unparseable_create_output_raises(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        side_effect=dispatching_run(create_stdout="no url here")):
            with mock.patch("crux.post._prompt_tty", return_value=""):
                with self.assertRaises(PostError):
                    post.ensure_pr(make_info(), Config())

    def test_deleted_crux_base_falls_back_to_default(self) -> None:
        # The recorded parent branch was merged and deleted; creating against it
        # would fail, so crux must fall back to the configured default (main).
        info = make_info(crux_base="design-color-picker")
        calls: list[list[str]] = []
        fake = dispatching_run(missing_branches=("design-color-picker",))

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            self.assertEqual(
                post.ensure_pr(info, Config(pr_auto_create=True)), 55)
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "main")

    def test_existing_base_is_used_unchanged(self) -> None:
        # When the recorded base still exists, no fallback: it is used as-is.
        info = make_info(crux_base="develop")
        calls: list[list[str]] = []
        fake = dispatching_run()

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            self.assertEqual(
                post.ensure_pr(info, Config(pr_auto_create=True)), 55)
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "develop")

    def test_base_gone_and_no_better_fallback_still_attempts(self) -> None:
        # Both the recorded base AND the default are gone: nothing better to
        # offer, so crux attempts the create with the original base (letting gh
        # surface the real error) rather than silently doing nothing.
        info = make_info(crux_base="design-color-picker")
        calls: list[list[str]] = []
        fake = dispatching_run(
            missing_branches=("design-color-picker", "main"))

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            self.assertEqual(
                post.ensure_pr(info, Config(pr_auto_create=True)), 55)
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(
            create[create.index("--base") + 1], "design-color-picker")


class PrIntentTests(unittest.TestCase):
    """D11 split: the pre-push foreground records the answer; the detached
    post-push run (`crux run --yes` => interactive=False) consumes it and
    only then creates the PR (the branch exists on the remote by then)."""

    def setUp(self) -> None:
        home = tempfile.mkdtemp(prefix="crux-intent-test-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"HOME": home})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _still_ours(yes: bool = True):
        """Stand in for `git merge-base --is-ancestor <recorded head> HEAD`."""
        return mock.patch("crux.post.subprocess.run",
                          return_value=completed(returncode=0 if yes else 1))

    def test_record_then_consume_roundtrip(self) -> None:
        info = make_info()
        with mock.patch("crux.post._prompt_tty", return_value="release/2.0"):
            self.assertEqual(post.record_pr_intent(info, Config()), "release/2.0")
        with self._still_ours():
            self.assertEqual(post.consume_pr_intent(info), "release/2.0")
            self.assertIsNone(post.consume_pr_intent(info))  # consumed once

    def test_record_declined_writes_nothing(self) -> None:
        info = make_info()
        with mock.patch("crux.post._prompt_tty", return_value="n"):
            self.assertIsNone(post.record_pr_intent(info, Config()))
        with self._still_ours():
            self.assertIsNone(post.consume_pr_intent(info))

    def test_an_intent_for_work_the_branch_no_longer_has_is_dropped(self) -> None:
        # The recorded head is not an ancestor of HEAD: the branch was reset or
        # force-pushed elsewhere since, so that answer was for other work.
        info = make_info()
        with mock.patch("crux.post._prompt_tty", return_value="release/2.0"):
            post.record_pr_intent(info, Config())
        with self._still_ours(False):
            self.assertIsNone(post.consume_pr_intent(info))
        # ...and it is still consumed, so it cannot fire on a later push either
        with self._still_ours():
            self.assertIsNone(post.consume_pr_intent(info))

    def test_a_commit_made_during_the_delay_keeps_the_answer(self) -> None:
        # The detached run starts ~15s after the ask; a commit landing in that
        # window moves HEAD forward but must not throw the answer away.
        info = make_info()
        with mock.patch("crux.post._prompt_tty", return_value="release/2.0"):
            post.record_pr_intent(info, Config())
        with self._still_ours() as run:
            self.assertEqual(post.consume_pr_intent(info), "release/2.0")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["git", "merge-base", "--is-ancestor"])
        self.assertEqual(argv[4], info.head_sha)

    def test_unreadable_git_discards_rather_than_trusts(self) -> None:
        info = make_info()
        with mock.patch("crux.post._prompt_tty", return_value="release/2.0"):
            post.record_pr_intent(info, Config())
        with mock.patch("crux.post.subprocess.run", side_effect=OSError("no git")):
            self.assertIsNone(post.consume_pr_intent(info))

    def test_an_existing_pr_clears_the_pending_answer(self) -> None:
        # ensure_pr found a PR, so the "should I open one?" answer is moot; it
        # must not survive to fire against some later, unrelated push.
        info = make_info()
        with mock.patch("crux.post._prompt_tty", return_value="release/2.0"):
            post.record_pr_intent(info, Config())
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout=pr_rows((7, "main")))):
            self.assertEqual(post.ensure_pr(info, Config(), interactive=False), 7)
        with self._still_ours():
            self.assertIsNone(post.consume_pr_intent(info))

    def test_record_never_calls_gh_create(self) -> None:
        info = make_info()
        with mock.patch("crux.post.subprocess.run") as run:
            with mock.patch("crux.post._prompt_tty", return_value=""):
                post.record_pr_intent(info, Config())
        run.assert_not_called()

    def test_non_interactive_ensure_pr_creates_from_recorded_intent(self) -> None:
        info = make_info()
        with mock.patch("crux.post._prompt_tty", return_value=""):
            post.record_pr_intent(info, Config())
        calls: list[list[str]] = []
        fake = dispatching_run()

        def recording(argv, **kwargs):
            calls.append(argv)
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            self.assertEqual(post.ensure_pr(info, Config(), interactive=False), 55)
        create = next(argv for argv in calls if argv[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "main")

    def test_non_interactive_without_intent_creates_nothing(self) -> None:
        calls: list[list[str]] = []

        def recording(argv, **kwargs):
            calls.append(argv)
            return completed(stdout="[]")

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            self.assertIsNone(post.ensure_pr(make_info(), Config(), interactive=False))
        self.assertFalse(any(argv[:3] == ["gh", "pr", "create"] for argv in calls))


class GhTimeoutTests(unittest.TestCase):
    """The pre-push foreground path must never hang the push on a stalled gh."""

    def test_timeout_maps_to_posterror(self) -> None:
        exc = subprocess.TimeoutExpired(cmd="gh", timeout=15)
        with mock.patch("crux.post.subprocess.run", side_effect=exc):
            with self.assertRaises(PostError) as ctx:
                post.find_pr(make_info())
        self.assertIn("timed out", str(ctx.exception))

    def test_find_pr_passes_short_timeout(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(stdout="[]")) as run:
            post.find_pr(make_info())
        self.assertEqual(run.call_args.kwargs.get("timeout"),
                         post._GH_FOREGROUND_TIMEOUT)

    def test_upsert_comment_has_a_timeout(self) -> None:
        def fake_run(argv, **kwargs):
            self.assertIsNotNone(kwargs.get("timeout"))
            if "--paginate" in argv:
                return completed(stdout="[]")
            return completed(stdout="{}")

        with mock.patch("crux.post.subprocess.run", side_effect=fake_run):
            post.upsert_comment(make_info(), 12, "body")


class PromptTtyTests(unittest.TestCase):
    def test_no_tty_returns_none(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("no tty")):
            self.assertIsNone(post._prompt_tty("create? "))

    def test_reads_stripped_reply_from_dev_tty(self) -> None:
        tty = FakeTty("  develop \n")
        with mock.patch("builtins.open", return_value=tty) as opener:
            self.assertEqual(post._prompt_tty("create? "), "develop")
        # Two separate handles — write-only for the prompt, read-only for the
        # reply — because a single "r+" tty handle is not seekable on some
        # platforms (io.UnsupportedOperation) and would be miscaught as no-tty.
        self.assertTrue(all(c.args[0] == "/dev/tty"
                            for c in opener.call_args_list))
        self.assertEqual([c.args[1] for c in opener.call_args_list], ["w", "r"])
        self.assertIn("create? ", tty.written)

    def test_eof_returns_none(self) -> None:
        with mock.patch("builtins.open", return_value=FakeTty("")):
            self.assertIsNone(post._prompt_tty("create? "))


# ---------------------------------------------------------------------------
# post.upsert_comment
# ---------------------------------------------------------------------------

class UpsertCommentTests(unittest.TestCase):
    def run_upsert(self, list_stdout: str, body: str = "new card") -> list[tuple[list[str], dict]]:
        calls: list[tuple[list[str], dict]] = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if "--paginate" in argv:
                return completed(stdout=list_stdout)
            return completed(stdout="{}")

        with mock.patch("crux.post.subprocess.run", side_effect=fake_run):
            post.upsert_comment(make_info(), 12, body)
        return calls

    def test_patches_existing_marker_comment_with_stdin_payload(self) -> None:
        listing = json.dumps([
            {"id": 1, "body": "unrelated"},
            {"id": 900, "body": f"{CARD_MARKER}\nold card"},
        ])
        calls = self.run_upsert(listing, body="new card")
        argv, kwargs = calls[-1]
        self.assertEqual(
            argv,
            ["gh", "api", "-X", "PATCH",
             "repos/example-org/crux/issues/comments/900", "--input", "-"],
        )
        self.assertEqual(json.loads(kwargs["input"]), {"body": "new card"})

    def test_posts_new_comment_when_marker_absent(self) -> None:
        listing = json.dumps([{"id": 1, "body": "unrelated"}])
        calls = self.run_upsert(listing, body="fresh card")
        argv, kwargs = calls[-1]
        self.assertEqual(
            argv,
            ["gh", "api", "-X", "POST",
             "repos/example-org/crux/issues/12/comments", "--input", "-"],
        )
        self.assertEqual(json.loads(kwargs["input"]), {"body": "fresh card"})

    def test_list_endpoint_and_pagination_flag(self) -> None:
        calls = self.run_upsert("[]")
        argv, _ = calls[0]
        self.assertEqual(
            argv,
            ["gh", "api", "repos/example-org/crux/issues/12/comments", "--paginate"],
        )

    def test_finds_marker_across_concatenated_pages(self) -> None:
        # gh api --paginate emits one array per page, back to back.
        page1 = json.dumps([{"id": 1, "body": "x"}])
        page2 = json.dumps([{"id": 2, "body": f"prefix {CARD_MARKER} suffix"}])
        calls = self.run_upsert(page1 + page2)
        argv, _ = calls[-1]
        self.assertIn("PATCH", argv)
        self.assertIn("repos/example-org/crux/issues/comments/2", argv)

    def test_gh_failure_raises_posterror(self) -> None:
        with mock.patch(
            "crux.post.subprocess.run",
            return_value=completed(returncode=1, stderr="HTTP 404: Not Found"),
        ):
            with self.assertRaises(PostError) as ctx:
                post.upsert_comment(make_info(), 12, "body")
        self.assertIn("404", str(ctx.exception))

    def test_unparseable_listing_raises_posterror(self) -> None:
        with self.assertRaises(PostError):
            self.run_upsert("{broken")

    def test_null_body_comment_is_skipped(self) -> None:
        listing = json.dumps([{"id": 3, "body": None}])
        calls = self.run_upsert(listing)
        argv, _ = calls[-1]
        self.assertIn("POST", argv)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def make_state(**overrides) -> RunState:
    state = RunState(
        version=STATE_VERSION,
        branch="feat/buffering",
        base_sha="b" * 40,
        head_sha="h" * 40,
        pr_number=12,
        fingerprints={"a.py:10": "deadbeef"},
        claims=[Claim(id="C1", text="no events lost during flush", hunk_ids=["a.py:10"])],
        nodes=[DagNode(number=1, title="WriteBuffer class", hunk_ids=["a.py:10"],
                       badge=Badge.CODE_CHANGE)],
        edges=[DagEdge(src=1, dst=2, reason="WriteBuffer")],
        annotation=Annotation(
            summary="adds write buffering",
            overview=["Writes are batched through a buffer — `a.py:10`"],
            change_map=ChangeMap(
                steps=[MapStep(id="in", label="Editor takes a keystroke"),
                       MapStep(id="out", label="Batch written to the database")],
                arrows=[MapArrow(src="in", dst="out", label="every 5 seconds")]),
            integration_test=["Run the app and confirm drafts autosave every 5s"],
            claims=[Claim(id="C1", text="no events lost during flush", hunk_ids=["a.py:10"])],
            nodes={1: NodeAnnotation(number=1, title="WriteBuffer drops on flush failure",
                                     why="root of the change", questions=["thread-safe add()?"],
                                     chips=["blast: 12"], minutes=6)},
            audit=[AuditRow(claim="no events lost", verdict=Verdict.CONTRADICTED,
                            evidence="flush failure path drops batch — buffer.py:61")],
        ),
        items=[Item(number=1, tier=Tier.RED, badge=Badge.CODE_CHANGE,
                    title="WriteBuffer drops on flush failure", file="a.py",
                    line_start=10, line_end=88, minutes=6)],
        card=f"{CARD_MARKER}\n## Crux",
        generated_at="2026-07-04T12:00:00Z",
        slack_channel="C0123456",
        slack_ts="1720012345.000100",
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="crux-cache-test-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def expected_path(self, info: RepoInfo) -> str:
        return os.path.join(
            self.home, ".cache", "crux",
            f"{info.owner}__{info.repo}",
            info.branch.replace("/", "__") + ".json",
        )

    def test_load_missing_returns_none(self) -> None:
        self.assertIsNone(cache.load(make_info()))

    def test_save_then_load_roundtrip(self) -> None:
        info = make_info()
        state = make_state()
        cache.save(info, state)
        loaded = cache.load(info)
        self.assertEqual(loaded, state)

    def test_state_from_another_pr_on_the_same_branch_is_ignored(self) -> None:
        # A branch can carry several open PRs; this file is keyed by branch
        # alone. Handing PR #12's state to a run about PR #13 would thread
        # #13's Slack update under #12's announcement and reuse annotations
        # written against a different base.
        info = make_info()
        cache.save(info, make_state(pr_number=12, slack_ts="1700.5"))
        self.assertIsNone(cache.load(info, 13))
        self.assertEqual(cache.load(info, 12).pr_number, 12)

    def test_no_pr_given_loads_whatever_is_there(self) -> None:
        # A preview has no PR to scope by, and must still get its reuse.
        info = make_info()
        cache.save(info, make_state(pr_number=12))
        self.assertIsNotNone(cache.load(info))
        self.assertIsNotNone(cache.load(info, None))

    def test_state_saved_before_pr_numbers_were_recorded_still_loads(self) -> None:
        info = make_info()
        cache.save(info, make_state(pr_number=None))
        self.assertIsNotNone(cache.load(info, 13))

    def test_path_layout_escapes_branch_slashes(self) -> None:
        info = make_info(branch="feat/deep/nesting")
        cache.save(info, make_state(branch=info.branch))
        expected = self.expected_path(info)
        self.assertTrue(os.path.isfile(expected), expected)
        self.assertIn("example-org__crux", expected)
        self.assertTrue(expected.endswith("feat__deep__nesting.json"))

    def test_state_cached_before_the_change_map_still_loads(self) -> None:
        # D33 added Annotation.change_map; a state saved by an older Crux has
        # no such key and must load with no map rather than blowing up.
        info = make_info()
        cache.save(info, make_state())
        path = self.expected_path(info)
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        del raw["annotation"]["change_map"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        loaded = cache.load(info)
        self.assertIsNone(loaded.annotation.change_map)

    def test_version_mismatch_returns_none(self) -> None:
        info = make_info()
        cache.save(info, make_state(version=STATE_VERSION + 1))
        self.assertIsNone(cache.load(info))

    def test_base_sha_mismatch_returns_none(self) -> None:
        cache.save(make_info(), make_state())
        rebased = make_info(base_sha="c" * 40)
        self.assertIsNone(cache.load(rebased))

    def test_corrupt_json_returns_none(self) -> None:
        info = make_info()
        path = self.expected_path(info)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(cache.load(info))

    def test_save_overwrites_and_leaves_no_temp_files(self) -> None:
        info = make_info()
        cache.save(info, make_state(head_sha="1" * 40))
        cache.save(info, make_state(head_sha="2" * 40))
        loaded = cache.load(info)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.head_sha, "2" * 40)
        entries = os.listdir(os.path.dirname(self.expected_path(info)))
        self.assertEqual(entries, ["feat__buffering.json"])


# ---------------------------------------------------------------------------
# D39: the head branch a renamed branch never pushed
# ---------------------------------------------------------------------------

def tracking(remote: str | None, merge: str | None):
    """A crux.gitio._try_git fake answering the branch.<name>.* config reads."""
    def fake(argv, cwd=None):
        if argv[:2] == ["config", "branch.feat/buffering.remote"]:
            return remote
        if argv[:2] == ["config", "branch.feat/buffering.merge"]:
            return merge
        return None
    return fake


class TrackedBranchMismatchTests(unittest.TestCase):
    """The rename signature, decided from git config alone (no network)."""

    def mismatch(self, remote, merge):
        with mock.patch("crux.gitio._try_git", side_effect=tracking(remote, merge)):
            return post.tracked_branch_mismatch(make_info())

    def test_upstream_naming_another_branch_is_a_mismatch(self) -> None:
        self.assertEqual(
            self.mismatch("origin", "refs/heads/feat/sourdough"), "feat/sourdough")

    def test_upstream_naming_itself_is_not(self) -> None:
        self.assertIsNone(self.mismatch("origin", "refs/heads/feat/buffering"))

    def test_no_upstream_is_not(self) -> None:
        # A branch tracking nothing gets its own remote branch from `git push`.
        self.assertIsNone(self.mismatch(None, None))
        self.assertIsNone(self.mismatch("origin", None))

    def test_a_remote_containing_a_slash_is_not_mis_split(self) -> None:
        # Why branch.<name>.merge is read instead of parsing @{u}: "team/fork"
        # + "feat/buffering" is unsplittable from "team/fork/feat/buffering".
        self.assertIsNone(self.mismatch("team/fork", "refs/heads/feat/buffering"))


class PushHeadTests(unittest.TestCase):
    def test_pushes_with_upstream_tracking(self) -> None:
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed()) as run:
            self.assertTrue(post.push_head(make_info(), Config()))
        self.assertEqual(run.call_args.args[0],
                         ["git", "push", "-u", "origin", "feat/buffering"])
        self.assertEqual(run.call_args.kwargs.get("cwd"), "/repo")

    def test_honors_the_configured_remote(self) -> None:
        cfg = Config(pr_push_remote="upstream")
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed()) as run:
            post.push_head(make_info(), cfg)
        self.assertEqual(run.call_args.args[0][:5],
                         ["git", "push", "-u", "upstream", "feat/buffering"])

    def test_a_failed_push_is_reported_not_raised(self) -> None:
        # gh pr create then fails with its own accurate error; inventing one
        # here would bury it.
        with mock.patch("crux.post.subprocess.run",
                        return_value=completed(returncode=1, stderr="denied")):
            self.assertFalse(post.push_head(make_info(), Config()))
        with mock.patch("crux.post.subprocess.run",
                        side_effect=FileNotFoundError("git")):
            self.assertFalse(post.push_head(make_info(), Config()))


class EnsureHeadOnRemoteTests(unittest.TestCase):
    """The head push inside ensure_pr: only on a definite 404, only if agreed."""

    def run_ensure(self, cfg, *, interactive=True, missing=(), tty=None,
                   intent=None):
        calls: list[list[str]] = []
        fake = dispatching_run(missing_branches=missing)

        def recording(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["git", "push"]:
                return completed()
            return fake(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            with mock.patch("crux.post.consume_intent",
                            return_value=dict(intent or {})):
                with mock.patch("crux.post._load_intent",
                                return_value=dict(intent or {})):
                    with mock.patch("crux.post._prompt_tty", return_value=tty):
                        post.ensure_pr(make_info(), cfg, interactive=interactive)
        return calls

    def pushed(self, calls) -> bool:
        return any(argv[:2] == ["git", "push"] for argv in calls)

    def test_missing_head_is_pushed_when_the_user_agrees(self) -> None:
        calls = self.run_ensure(Config(pr_auto_create=True),
                                missing=("feat/buffering",), tty="")
        self.assertTrue(self.pushed(calls))
        # and the PR is still created, now that it has a head
        self.assertTrue(any(a[:3] == ["gh", "pr", "create"] for a in calls))

    def test_a_present_head_is_never_pushed(self) -> None:
        calls = self.run_ensure(Config(pr_auto_create=True), tty="")
        self.assertFalse(self.pushed(calls))

    def test_declining_leaves_the_branch_alone(self) -> None:
        calls = self.run_ensure(Config(pr_auto_create=True),
                                missing=("feat/buffering",), tty="n")
        self.assertFalse(self.pushed(calls))

    def test_no_tty_leaves_the_branch_alone(self) -> None:
        calls = self.run_ensure(Config(pr_auto_create=True),
                                missing=("feat/buffering",), tty=None)
        self.assertFalse(self.pushed(calls))

    def test_never_does_not_even_probe(self) -> None:
        cfg = Config(pr_auto_create=True, pr_push_head="never")
        calls = self.run_ensure(cfg, missing=("feat/buffering",), tty="")
        self.assertFalse(self.pushed(calls))
        self.assertFalse(any(argv[:2] == ["gh", "api"]
                             and argv[2].endswith("/branches/feat/buffering")
                             for argv in calls))

    def test_always_pushes_without_asking(self) -> None:
        cfg = Config(pr_auto_create=True, pr_push_head="always")
        with mock.patch("crux.post._prompt_tty",
                        side_effect=AssertionError("must not prompt")):
            calls = self.run_ensure(cfg, missing=("feat/buffering",))
        self.assertTrue(self.pushed(calls))

    def test_the_detached_run_never_prompts(self) -> None:
        # It has no terminal of its own: with nothing recorded it must leave
        # the branch alone rather than block on /dev/tty.
        cfg = Config(pr_auto_create=True)
        with mock.patch("crux.post._prompt_tty",
                        side_effect=AssertionError("must not prompt")):
            calls = self.run_ensure(cfg, interactive=False,
                                    missing=("feat/buffering",))
        self.assertFalse(self.pushed(calls))

    def test_the_detached_run_honors_a_recorded_yes(self) -> None:
        cfg = Config(pr_auto_create=True)
        with mock.patch("crux.post._prompt_tty",
                        side_effect=AssertionError("must not prompt")):
            calls = self.run_ensure(cfg, interactive=False,
                                    missing=("feat/buffering",),
                                    intent={"push_head": True, "base": "main"})
        self.assertTrue(self.pushed(calls))

    def test_a_recorded_no_is_honored_over_a_tty(self) -> None:
        cfg = Config(pr_auto_create=True)
        calls = self.run_ensure(cfg, missing=("feat/buffering",), tty="",
                                intent={"push_head": False})
        self.assertFalse(self.pushed(calls))

    def test_a_transient_probe_failure_pushes_nothing(self) -> None:
        # D34 again: only a definite 404 may trigger a push. A 502 must not
        # push a branch nobody asked about.
        def flaky(argv, **kwargs):
            if argv[:2] == ["gh", "api"] and "/branches/" in argv[2]:
                return completed(returncode=1, stderr="HTTP 502: Bad gateway")
            return dispatching_run()(argv, **kwargs)

        calls: list[list[str]] = []

        def recording(argv, **kwargs):
            calls.append(argv)
            return flaky(argv, **kwargs)

        with mock.patch("crux.post.subprocess.run", side_effect=recording):
            with mock.patch("crux.post._prompt_tty", return_value=""):
                post.ensure_pr(make_info(), Config(pr_auto_create=True))
        self.assertFalse(self.pushed(calls))


class RecordPushHeadIntentTests(unittest.TestCase):
    """The pre-push half: ask only on the rename signature, record the answer."""

    def record(self, cfg, *, remote="origin", merge="refs/heads/old-name",
               tty=""):
        stored: dict = {}
        with mock.patch("crux.gitio._try_git", side_effect=tracking(remote, merge)):
            with mock.patch("crux.post._store_intent",
                            side_effect=lambda info, **kw: stored.update(kw)):
                with mock.patch("crux.post._prompt_tty", return_value=tty) as p:
                    agreed = post.record_push_head_intent(make_info(), cfg)
        return agreed, stored, p

    def test_records_the_agreement(self) -> None:
        agreed, stored, prompt = self.record(Config())
        self.assertTrue(agreed)
        self.assertEqual(stored, {"push_head": True})
        # the question says which branch the push would otherwise move
        self.assertIn("old-name", prompt.call_args.args[0])

    def test_records_a_refusal_too(self) -> None:
        # Recorded, not merely absent: the detached run must not re-decide it.
        agreed, stored, _ = self.record(Config(), tty="n")
        self.assertFalse(agreed)
        self.assertEqual(stored, {"push_head": False})

    def test_no_mismatch_asks_nothing(self) -> None:
        # A first push of an untracked branch creates the branch by itself.
        agreed, stored, prompt = self.record(Config(), remote=None, merge=None)
        self.assertFalse(agreed)
        self.assertEqual(stored, {})
        prompt.assert_not_called()

    def test_never_asks_nothing(self) -> None:
        agreed, stored, prompt = self.record(Config(pr_push_head="never"))
        self.assertFalse(agreed)
        self.assertEqual(stored, {})
        prompt.assert_not_called()

    def test_always_records_without_asking(self) -> None:
        agreed, stored, prompt = self.record(Config(pr_push_head="always"))
        self.assertTrue(agreed)
        self.assertEqual(stored, {"push_head": True})
        prompt.assert_not_called()


class IntentMergeTests(unittest.TestCase):
    """One intent file, two independently answered questions."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch("crux.post.Path.home",
                             return_value=Path(self.home.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_later_answer_does_not_erase_an_earlier_one(self) -> None:
        info = make_info()
        post._store_intent(info, base="develop")
        post._store_intent(info, push_head=True)
        with mock.patch("crux.post._is_ancestor", return_value=True):
            intent = post.consume_intent(info)
        self.assertEqual(intent.get("base"), "develop")
        self.assertIs(intent.get("push_head"), True)

    def test_consuming_deletes_the_file(self) -> None:
        info = make_info()
        post._store_intent(info, base="develop")
        with mock.patch("crux.post._is_ancestor", return_value=True):
            post.consume_intent(info)
            self.assertEqual(post.consume_intent(info), {})

    def test_a_stale_intent_is_discarded_whole(self) -> None:
        # Recorded for work this branch no longer contains: the head push is
        # no more answered-for than the base is.
        info = make_info()
        post._store_intent(info, base="develop", push_head=True)
        with mock.patch("crux.post._is_ancestor", return_value=False):
            self.assertEqual(post.consume_intent(info), {})


if __name__ == "__main__":
    unittest.main()
