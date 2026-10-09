# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.cli and the shipped hooks.

All sibling modules (gitio, config, gate, dag, ...) are replaced with
MagicMock modules injected into sys.modules — cli.py imports them lazily, so
these tests run with or without the real implementations present. No network,
no gh, no claude: external stages are mocked; the intent hook is exercised as
a local python subprocess.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crux.cli as cli
import crux.harvest  # noqa: F401  (parent package must be in sys.modules for
#                       `import crux.harvest.<mocked submodule>` to bind)
from crux.models import (
    Annotation,
    Badge,
    Config,
    CruxError,
    DagNode,
    GateDecision,
    Hunk,
    RepoInfo,
)

SIBLINGS = [
    "crux.gitio", "crux.config", "crux.gate", "crux.dag", "crux.analyze",
    "crux.tiers", "crux.render", "crux.post", "crux.cache", "crux.commitmsg",
    "crux.harvest.structural", "crux.harvest.defuse", "crux.harvest.blast",
    "crux.harvest.history", "crux.harvest.testprox",
]



# git invokes the pre-push hook with two args — the remote NAME and URL — and
# the ref lines on stdin. The hook must tolerate (ignore) them; a bare
# ["pre-push"] would miss the argparse "unrecognized arguments" regression.
PRE_PUSH_ARGV = ["pre-push", "origin", "git@github.com:example-org/demo.git"]


def make_hunk() -> Hunk:
    return Hunk(id="a.py:1", file="a.py", old_start=1, old_count=1,
                new_start=1, new_count=2, patch="@@ -1,1 +1,2 @@\n+x = 1\n x\n")


class CliTestBase(unittest.TestCase):
    """Shared fixture: temp HOME, mocked sibling modules, happy-path returns."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        # Keep ~/.cache/crux/crux.log and the global hooks dir (which
        # install-hooks derives from XDG_CONFIG_HOME) inside the temp dir.
        env = mock.patch.dict(os.environ, {
            "HOME": self.root,
            "XDG_CONFIG_HOME": os.path.join(self.root, ".config")})
        env.start()
        self.addCleanup(env.stop)

        self.mods = {name: mock.MagicMock(name=name) for name in SIBLINGS}
        mods = mock.patch.dict(sys.modules, self.mods)
        mods.start()
        self.addCleanup(mods.stop)
        # `import crux.X as X` binds via getattr on the parent package first
        # (sys.modules is only the fallback). If another test module already
        # imported the real submodule, the parent-package attribute would win
        # over the sys.modules mock, so patch the attribute as well.
        for name, module in self.mods.items():
            parent_name, _, attr = name.rpartition(".")
            attr_patch = mock.patch.object(
                sys.modules[parent_name], attr, module, create=True)
            attr_patch.start()
            self.addCleanup(attr_patch.stop)

        self.info = RepoInfo(root=self.root, branch="feat/x",
                             head_sha="a" * 40, base_sha="b" * 40,
                             owner="example-org", repo="demo")
        self.cfg = Config(scope_owners=["example-org"])
        m = self.mods
        m["crux.gitio"].repo_info.return_value = self.info
        m["crux.gitio"].diff_hunks.return_value = [make_hunk()]
        # D34 defaults: no reflog-recorded parent, and a PR whose base already
        # matches the local guess — so the base-alignment step is a no-op
        # unless a test opts into it.
        m["crux.gitio"].branch_start_point.return_value = None
        m["crux.gitio"].with_base.side_effect = lambda info, base: info
        m["crux.post"].pr_base.return_value = None
        m["crux.config"].load.return_value = self.cfg
        m["crux.harvest.structural"].mechanical_clusters.return_value = []
        m["crux.harvest.defuse"].extract_defs_uses.return_value = {}
        m["crux.gate"].decide.return_value = GateDecision(
            skip=False, reasons=[], stats={"total_lines": 10})
        m["crux.cache"].load.return_value = None
        self.node = DagNode(number=1, title="WriteBuffer class",
                            hunk_ids=["a.py:1"], badge=Badge.CODE_CHANGE)
        m["crux.dag"].build.return_value = ([self.node], [])
        m["crux.analyze"].annotate.return_value = Annotation(summary="did things")
        m["crux.tiers"].assign.return_value = []
        m["crux.render"].render_card.return_value = "<!-- crux:card -->\nTHE CARD"
        m["crux.render"].render_skip_note.return_value = "crux: skipped (trivial)"
        m["crux.post"].find_pr.return_value = None
        m["crux.post"].ensure_pr.return_value = None
        # Slack announce unpacks this pair (created_at, author); a bare
        # MagicMock would raise on unpacking and be swallowed as a failed
        # announce, silently breaking every Slack assertion below.
        m["crux.post"].pr_meta.return_value = ("2026-07-23T02:05:25Z", "Fixture Alpha")
        m["crux.post"].author_name.return_value = "Fixture Alpha"

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()


class TestVersion(CliTestBase):

    def test_version_flag_prints_version_and_exits_zero(self) -> None:
        import crux
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(crux.__version__, out.getvalue())


class TestScopeCheck(CliTestBase):

    def test_other_owner_blocks_before_any_analysis(self) -> None:
        self.info.owner = "evilcorp"
        code, _, err = self.run_cli(["run", "--dry-run"])
        self.assertEqual(code, 0)
        self.mods["crux.gitio"].diff_hunks.assert_not_called()
        self.mods["crux.post"].upsert_comment.assert_not_called()
        self.assertIn("scope", err)
        self.assertIn("D13", err)

    def test_in_scope_owner_proceeds(self) -> None:
        code, _, _ = self.run_cli(["run", "--dry-run", "--no-llm"])
        self.assertEqual(code, 0)
        self.mods["crux.gitio"].diff_hunks.assert_called_once()

    def test_ensure_pr_respects_scope(self) -> None:
        self.info.owner = "stranger"
        code, _, err = self.run_cli(["ensure-pr"])
        self.assertEqual(code, 0)
        self.mods["crux.post"].find_pr.assert_not_called()
        self.assertIn("D13", err)


class TestEnrichCommitCmd(CliTestBase):
    """The D27 subcommand: scope-checked dispatch into crux.commitmsg."""

    def test_respects_scope(self) -> None:
        self.info.owner = "stranger"
        code, _, err = self.run_cli(["enrich-commit", "--sha", "a" * 40])
        self.assertEqual(code, 0)
        self.mods["crux.commitmsg"].enrich.assert_not_called()
        self.assertIn("D13", err)

    def test_dispatches_and_notifies_on_amend(self) -> None:
        self.mods["crux.commitmsg"].enrich.return_value = "Better subject"
        code, _, _ = self.run_cli(["enrich-commit", "--sha", "a" * 40])
        self.assertEqual(code, 0)
        self.mods["crux.commitmsg"].enrich.assert_called_once_with(
            self.info, self.cfg, "a" * 40)
        notice = self.mods["crux.post"].notify_tty.call_args.args[0]
        self.assertIn("Better subject", notice)

    def test_quiet_when_nothing_was_amended(self) -> None:
        self.mods["crux.commitmsg"].enrich.return_value = None
        code, _, _ = self.run_cli(["enrich-commit", "--sha", "a" * 40])
        self.assertEqual(code, 0)
        self.mods["crux.post"].notify_tty.assert_not_called()

    def test_crux_error_still_exits_zero(self) -> None:
        self.mods["crux.commitmsg"].enrich.side_effect = CruxError("claude broke")
        code, _, err = self.run_cli(["enrich-commit", "--sha", "a" * 40])
        self.assertEqual(code, 0)
        self.assertIn("claude broke", err)
        # Detached post-commit run: the failure must still reach the terminal.
        notice = self.mods["crux.post"].notify_tty.call_args.args[0]
        self.assertIn("commit message", notice)
        self.assertIn("crux.log", notice)


class TestEnsureEnrichedCmd(CliTestBase):
    """The D28 subcommand: exit 1 is the deliberate stop-this-push signal."""

    def test_respects_scope(self) -> None:
        self.info.owner = "stranger"
        code, _, err = self.run_cli(["ensure-enriched"])
        self.assertEqual(code, 0)
        self.mods["crux.commitmsg"].ensure_enriched.assert_not_called()
        self.assertIn("D13", err)

    def test_exit_1_when_the_push_went_stale(self) -> None:
        self.mods["crux.commitmsg"].ensure_enriched.return_value = True
        code, _, _ = self.run_cli(["ensure-enriched"])
        self.assertEqual(code, 1)
        notice = self.mods["crux.post"].notify_tty.call_args.args[0]
        self.assertIn("push", notice)

    def test_exit_0_when_everything_is_enriched(self) -> None:
        self.mods["crux.commitmsg"].ensure_enriched.return_value = False
        code, _, _ = self.run_cli(["ensure-enriched"])
        self.assertEqual(code, 0)

    def test_crux_error_exits_zero_and_never_blocks_the_push(self) -> None:
        self.mods["crux.commitmsg"].ensure_enriched.side_effect = (
            CruxError("claude broke"))
        code, _, err = self.run_cli(["ensure-enriched"])
        self.assertEqual(code, 0)
        self.assertIn("claude broke", err)

    def test_hook_flag_passes_stdin_refs_through(self) -> None:
        self.mods["crux.commitmsg"].ensure_enriched.return_value = False
        lines = ("refs/heads/main aaa refs/heads/main bbb\n"
                 "refs/tags/v1 ccc refs/tags/v1 ddd\n")
        with mock.patch("sys.stdin", io.StringIO(lines)):
            code, _, _ = self.run_cli(["ensure-enriched", "--hook"])
        self.assertEqual(code, 0)
        kwargs = self.mods["crux.commitmsg"].ensure_enriched.call_args.kwargs
        self.assertEqual(kwargs["refs"],
                         [("refs/heads/main", "aaa"), ("refs/tags/v1", "ccc")])

    def test_read_push_refs_empty_stdin_means_current_branch(self) -> None:
        with mock.patch("sys.stdin", io.StringIO("")):
            self.assertIsNone(cli._read_push_refs())


class TestGateSkip(CliTestBase):

    def test_skip_saves_skipped_state_and_posts_nothing(self) -> None:
        # A PR must exist for the run to reach the gate at all (the review is
        # gated on a PR); a trivial diff then leaves that PR without a card.
        self.mods["crux.post"].find_pr.return_value = 7
        self.mods["crux.gate"].decide.return_value = GateDecision(
            skip=True, reasons=["trivial"], stats={})
        code, _, err = self.run_cli(["run"])
        self.assertEqual(code, 0)
        self.mods["crux.dag"].build.assert_not_called()
        self.mods["crux.analyze"].annotate.assert_not_called()
        self.mods["crux.post"].upsert_comment.assert_not_called()
        save = self.mods["crux.cache"].save
        save.assert_called_once()
        state = save.call_args.args[1]
        self.assertTrue(state.skipped)
        self.assertEqual(state.pr_number, 7)
        self.assertEqual(state.branch, "feat/x")
        # fingerprints are persisted even for skipped runs (D9)
        self.assertIn("a.py:1", state.fingerprints)
        self.assertRegex(state.fingerprints["a.py:1"], r"^[0-9a-f]{40}$")
        self.assertIn("skipped", err)


class TestDryRun(CliTestBase):

    def test_dry_run_prints_card_and_touches_nothing(self) -> None:
        code, out, _ = self.run_cli(["run", "--dry-run", "--no-llm"])
        self.assertEqual(code, 0)
        self.assertIn("THE CARD", out)
        self.mods["crux.post"].find_pr.assert_not_called()
        self.mods["crux.post"].ensure_pr.assert_not_called()
        self.mods["crux.post"].upsert_comment.assert_not_called()
        self.mods["crux.cache"].save.assert_not_called()

    def test_preview_is_alias_for_dry_run(self) -> None:
        code, out, _ = self.run_cli(["preview", "--no-llm"])
        self.assertEqual(code, 0)
        self.assertIn("THE CARD", out)
        self.mods["crux.post"].upsert_comment.assert_not_called()

    def test_no_llm_builds_annotation_from_node_titles(self) -> None:
        code, _, _ = self.run_cli(["run", "--dry-run", "--no-llm"])
        self.assertEqual(code, 0)
        self.mods["crux.analyze"].annotate.assert_not_called()
        # render_card(info, pr, items, annotation, gate_stats)
        annotation = self.mods["crux.render"].render_card.call_args.args[3]
        self.assertIsInstance(annotation, Annotation)
        self.assertEqual(annotation.claims, [])
        self.assertEqual(annotation.audit, [])
        self.assertEqual(annotation.nodes[1].title, "WriteBuffer class")


class TestDelay(CliTestBase):

    def test_delay_sleeps_before_running(self) -> None:
        with mock.patch("crux.cli.time.sleep") as sleep:
            code, out, _ = self.run_cli(
                ["run", "--dry-run", "--no-llm", "--delay", "3"])
        self.assertEqual(code, 0)
        sleep.assert_called_once_with(3)
        self.assertIn("THE CARD", out)

    def test_zero_delay_does_not_sleep(self) -> None:
        with mock.patch("crux.cli.time.sleep") as sleep:
            self.run_cli(["run", "--dry-run", "--no-llm"])
        sleep.assert_not_called()

    def test_non_integer_delay_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["run", "--delay", "soon"])
        self.assertEqual(ctx.exception.code, 2)


class TestPerPrState(CliTestBase):
    """A branch with two open PRs must not let one inherit the other's state."""

    def test_previous_state_is_looked_up_for_this_pr(self) -> None:
        code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        self.mods["crux.cache"].load.assert_called_once_with(self.info, 5)

    def test_a_preview_scopes_by_nothing(self) -> None:
        self.run_cli(["preview", "--no-llm"])
        self.mods["crux.cache"].load.assert_called_once_with(self.info, None)


class TestPrBaseAlignment(CliTestBase):
    """D34: the PR's own base branch drives the review, not the local guess."""

    def setUp(self) -> None:
        super().setUp()
        self.info.crux_base = "parent"
        self.info.base_branch = "parent"
        self.aligned = RepoInfo(root=self.root, branch="feat/x",
                                head_sha="a" * 40, base_sha="c" * 40,
                                owner="example-org", repo="demo",
                                crux_base="parent", base_branch="develop")
        self.mods["crux.gitio"].with_base.side_effect = None
        self.mods["crux.gitio"].with_base.return_value = self.aligned

    def test_retargeted_pr_rebases_the_whole_run(self) -> None:
        self.mods["crux.post"].pr_base.return_value = "develop"
        code, _, err = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        self.mods["crux.gitio"].with_base.assert_called_once_with(
            self.info, "develop")
        # every later stage sees the re-based info, not the stale guess
        self.assertIs(self.mods["crux.gitio"].diff_hunks.call_args.args[0],
                      self.aligned)
        state = self.mods["crux.cache"].save.call_args.args[1]
        self.assertEqual(state.base_sha, "c" * 40)
        self.assertIn("D34", err)

    def test_the_correction_is_remembered_as_the_branchs_parent(self) -> None:
        # Writing it back to the D17 store makes the next preview, and the next
        # run, agree with GitHub instead of re-deriving the stale guess.
        self.mods["crux.post"].pr_base.return_value = "develop"
        self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.mods["crux.gitio"].run_git.assert_called_once_with(
            ["config", "branch.feat/x.cruxBase", "develop"], cwd=self.root)

    def test_matching_base_changes_nothing(self) -> None:
        self.mods["crux.post"].pr_base.return_value = "parent"
        code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        self.mods["crux.gitio"].with_base.assert_not_called()
        self.mods["crux.gitio"].run_git.assert_not_called()

    def test_unreadable_pr_base_keeps_the_local_guess(self) -> None:
        self.mods["crux.post"].pr_base.side_effect = CruxError("gh is down")
        code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        self.mods["crux.gitio"].with_base.assert_not_called()
        self.assertIs(self.mods["crux.gitio"].diff_hunks.call_args.args[0],
                      self.info)

    def test_a_base_that_will_not_resolve_is_not_remembered(self) -> None:
        # with_base could not find the branch and handed back the guess: there
        # is nothing to announce and nothing worth recording.
        self.mods["crux.post"].pr_base.return_value = "develop"
        self.mods["crux.gitio"].with_base.return_value = self.info
        code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        self.mods["crux.gitio"].run_git.assert_not_called()

    def test_explicit_base_flag_wins_over_the_pr(self) -> None:
        code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5",
                                   "--base", "main"])
        self.assertEqual(code, 0)
        self.mods["crux.gitio"].repo_info.assert_called_once_with(base_ref="main")
        self.mods["crux.post"].pr_base.assert_not_called()
        self.mods["crux.gitio"].with_base.assert_not_called()

    def test_explicit_base_says_that_it_skipped_the_pr_base_check(self) -> None:
        """Skipping D34 for --base is deliberate, but it must not be silent.

        The user also forfeits the correction that would otherwise catch a
        base resolving to the wrong branch, so the run has to record which
        base is in force and what it skipped.
        """
        # assertLogs cannot see this: _setup_logging drops every handler on
        # the "crux" logger at the top of each main(). Its StreamHandler binds
        # the sys.stderr in force at that moment, which run_cli captures.
        code, _, err = self.run_cli(["run", "--no-llm", "--pr", "5",
                                     "--base", "release/3.0"])
        self.assertEqual(code, 0)
        self.assertIn("release/3.0", err)
        self.assertIn("skipping the D34 alignment", err)

    def test_no_base_flag_passes_none(self) -> None:
        self.run_cli(["run", "--no-llm", "--dry-run"])
        self.mods["crux.gitio"].repo_info.assert_called_once_with(base_ref=None)

    def test_preview_never_asks_github_for_a_base(self) -> None:
        self.run_cli(["preview", "--no-llm"])
        self.mods["crux.post"].pr_base.assert_not_called()


class TestPosting(CliTestBase):

    def test_explicit_pr_flag_posts_and_saves_state(self) -> None:
        code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        upsert = self.mods["crux.post"].upsert_comment
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.args[1], 5)
        self.assertIn("THE CARD", upsert.call_args.args[2])
        self.mods["crux.post"].find_pr.assert_not_called()
        self.mods["crux.post"].ensure_pr.assert_not_called()
        state = self.mods["crux.cache"].save.call_args.args[1]
        self.assertEqual(state.pr_number, 5)
        self.assertFalse(state.skipped)
        self.assertIn("THE CARD", state.card)

    def test_no_create_without_pr_skips_review_entirely(self) -> None:
        # --no-create + no existing PR: the review is PR-gated, so crux does no
        # diff, posts nothing, writes no fallback card, and saves no state.
        code, _, err = self.run_cli(["run", "--no-llm", "--no-create"])
        self.assertEqual(code, 0)
        self.mods["crux.post"].ensure_pr.assert_not_called()
        self.mods["crux.post"].upsert_comment.assert_not_called()
        self.mods["crux.gitio"].diff_hunks.assert_not_called()
        self.mods["crux.cache"].save.assert_not_called()
        fallback = Path(self.root) / ".crux" / "last-card.md"
        self.assertFalse(fallback.is_file())
        self.assertIn("skipping review", err)

    def test_status_and_terminal_notice_pending_then_success(self) -> None:
        code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        states = [c.args[2]
                  for c in self.mods["crux.post"].set_status.call_args_list]
        self.assertEqual(states, ["pending", "success"])
        # the terminal gets a writing-the-review notice and a done notice
        self.assertEqual(self.mods["crux.post"].notify_tty.call_count, 2)
        # title/description synced twice: before the review, and again after so
        # the description can link the freshly-posted comments
        self.assertEqual(self.mods["crux.post"].sync_pr_metadata.call_count, 2)

    def test_integration_test_steps_posted_as_second_comment(self) -> None:
        from crux.models import TEST_MARKER
        self.mods["crux.analyze"].annotate.return_value = Annotation(
            summary="s", integration_test=["do the thing", "see it work"])
        code, _, _ = self.run_cli(["run", "--pr", "5"])
        self.assertEqual(code, 0)
        markers = [c.kwargs.get("marker")
                   for c in self.mods["crux.post"].upsert_comment.call_args_list]
        self.assertIn(TEST_MARKER, markers)  # the card + the test comment

    def test_no_second_comment_when_no_integration_test(self) -> None:
        from crux.models import TEST_MARKER
        self.mods["crux.analyze"].annotate.return_value = Annotation(summary="s")
        code, _, _ = self.run_cli(["run", "--pr", "5"])
        self.assertEqual(code, 0)
        markers = [c.kwargs.get("marker")
                   for c in self.mods["crux.post"].upsert_comment.call_args_list]
        self.assertNotIn(TEST_MARKER, markers)  # no integration-test comment

    def test_slack_announce_saves_thread_ts(self) -> None:
        with mock.patch("crux.slack.enabled", return_value=True), \
             mock.patch("crux.slack.announce_pr",
                        return_value=("C1", "12.34")) as announce:
            code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        announce.assert_called_once()
        state = self.mods["crux.cache"].save.call_args.args[1]
        self.assertEqual(state.slack_channel, "C1")
        self.assertEqual(state.slack_ts, "12.34")

    def test_llm_pr_title_is_applied_after_review(self) -> None:
        self.mods["crux.analyze"].annotate.return_value = Annotation(
            summary="s", pr_title="Batch draft saves")
        code, _, _ = self.run_cli(["run", "--pr", "5"])  # LLM path (no --no-llm)
        self.assertEqual(code, 0)
        titles = [c.kwargs.get("title")
                  for c in self.mods["crux.post"].sync_pr_metadata.call_args_list]
        self.assertIn("Batch draft saves", titles)

    def test_slack_disabled_leaves_thread_ts_empty(self) -> None:
        code, _, _ = self.run_cli(["run", "--no-llm", "--pr", "5"])
        self.assertEqual(code, 0)
        state = self.mods["crux.cache"].save.call_args.args[1]
        self.assertEqual(state.slack_ts, "")

    def test_yes_makes_ensure_pr_non_interactive(self) -> None:
        self.mods["crux.post"].ensure_pr.return_value = 12
        code, _, _ = self.run_cli(["run", "--no-llm", "--yes"])
        self.assertEqual(code, 0)
        ensure = self.mods["crux.post"].ensure_pr
        ensure.assert_called_once()
        self.assertFalse(ensure.call_args.kwargs["interactive"])
        self.assertEqual(
            self.mods["crux.post"].upsert_comment.call_args.args[1], 12)


class TestErrors(CliTestBase):

    def test_crux_error_with_known_pr_posts_failure_card(self) -> None:
        self.mods["crux.gitio"].diff_hunks.side_effect = CruxError("boom")
        self.mods["crux.render"].render_failure_card.return_value = (
            "<!-- crux:card -->\nCrux run FAILED: boom")
        code, _, err = self.run_cli(["run", "--pr", "9"])
        self.assertEqual(code, 0)
        upsert = self.mods["crux.post"].upsert_comment
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.args[1], 9)
        self.assertIn("boom", upsert.call_args.args[2])
        self.assertIn("boom", err)

    def test_dry_run_failure_prints_card_and_never_posts(self) -> None:
        # A failed --dry-run/preview must not touch the PR: the failure card is
        # printed locally, never upserted over the real review card (regression:
        # a timed-out preview used to overwrite the card with a RUN FAILED note).
        self.mods["crux.gitio"].diff_hunks.side_effect = CruxError("boom")
        self.mods["crux.render"].render_failure_card.return_value = (
            "<!-- crux:card -->\nCrux run FAILED: boom")
        code, out, _ = self.run_cli(["run", "--pr", "9", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("boom", out)
        self.mods["crux.post"].upsert_comment.assert_not_called()
        self.mods["crux.post"].set_status.assert_not_called()

    def test_preview_failure_prints_card_and_never_posts(self) -> None:
        # Same guarantee via the `preview` alias (dry_run defaulted on).
        self.mods["crux.gitio"].diff_hunks.side_effect = CruxError("boom")
        self.mods["crux.render"].render_failure_card.return_value = (
            "<!-- crux:card -->\nCrux run FAILED: boom")
        code, out, _ = self.run_cli(["preview", "--pr", "9"])
        self.assertEqual(code, 0)
        self.assertIn("boom", out)
        self.mods["crux.post"].upsert_comment.assert_not_called()

    def test_crux_error_without_pr_still_exits_zero(self) -> None:
        # An error while ensuring the PR (before any diff) still exits 0 and
        # posts nothing — there is no PR to post a failure card to. But the
        # detached run must not die silently: the terminal gets a notice
        # pointing at the log.
        self.mods["crux.post"].find_pr.side_effect = CruxError("boom")
        code, _, err = self.run_cli(["run"])
        self.assertEqual(code, 0)
        self.mods["crux.post"].upsert_comment.assert_not_called()
        self.assertIn("boom", err)
        notice = self.mods["crux.post"].notify_tty.call_args.args[0]
        self.assertIn("failed", notice)
        self.assertIn("crux.log", notice)

    def test_unexpected_exception_exits_zero(self) -> None:
        # A crash before repo_info even resolves (info is None) still surfaces
        # a terminal notice — this is the class of silent failure that hid the
        # crux.harvest ModuleNotFoundError after a push.
        self.mods["crux.gitio"].repo_info.side_effect = RuntimeError("weird")
        code, _, err = self.run_cli(["run"])
        self.assertEqual(code, 0)
        self.assertIn("weird", err)
        notice = self.mods["crux.post"].notify_tty.call_args.args[0]
        self.assertIn("crash", notice)
        self.assertIn("crux.log", notice)


class TestSlackFailureNotice(CliTestBase):
    """A configured-but-failing Slack announce must not be silent (D16):
    the terminal gets a warning pointing at the log."""

    def _announce(self, fake_slack: mock.MagicMock) -> tuple[str, str]:
        self.cfg.slack_channel = "pull-requests"
        with mock.patch.dict(sys.modules, {"crux.slack": fake_slack}):
            with mock.patch.object(sys.modules["crux"], "slack", fake_slack,
                                   create=True):
                return cli._announce_slack(self.cfg, self.info, 7, None,
                                           mock.MagicMock())

    def test_failed_announce_warns_on_the_terminal(self) -> None:
        fake_slack = mock.MagicMock()
        fake_slack.enabled.return_value = True
        fake_slack.announce_pr.return_value = ("", "")
        self.assertEqual(self._announce(fake_slack), ("", ""))
        notice = self.mods["crux.post"].notify_tty.call_args.args[0]
        self.assertIn("Slack", notice)
        self.assertIn("pull-requests", notice)
        self.assertIn("crux.log", notice)

    def test_successful_announce_keeps_the_happy_notice(self) -> None:
        fake_slack = mock.MagicMock()
        fake_slack.enabled.return_value = True
        fake_slack.announce_pr.return_value = ("C777", "1.2")
        self.assertEqual(self._announce(fake_slack), ("C777", "1.2"))
        notice = self.mods["crux.post"].notify_tty.call_args.args[0]
        self.assertIn("posted PR #7 to Slack", notice)

    def test_disabled_slack_stays_silent(self) -> None:
        fake_slack = mock.MagicMock()
        fake_slack.enabled.return_value = False
        self.assertEqual(self._announce(fake_slack), ("", ""))
        self.mods["crux.post"].notify_tty.assert_not_called()


class TestInstallHooksLocal(CliTestBase):

    def _git_dir(self) -> Path:
        git_dir = Path(self.root) / ".git"
        (git_dir / "hooks").mkdir(parents=True, exist_ok=True)

        def run_git(cmd: list[str], cwd: str | None = None) -> str:
            # --local resolves the repo's own hooks dir via the common dir
            # (never --git-path, which honors core.hooksPath and would point
            # back at the global crux dir once that is installed).
            if cmd == ["rev-parse", "--git-common-dir"]:
                return str(git_dir) + "\n"
            if cmd == ["rev-parse", "--show-toplevel"]:
                return self.root + "\n"
            raise AssertionError(f"unexpected git call: {cmd}")

        self.mods["crux.gitio"].run_git.side_effect = run_git
        return git_dir

    def test_installs_hook_executable_and_claude_hook(self) -> None:
        git_dir = self._git_dir()
        code, out, _ = self.run_cli(["install-hooks", "--local"])
        self.assertEqual(code, 0)
        dest = git_dir / "hooks" / "pre-push"
        self.assertTrue(dest.is_file())
        self.assertIn("crux", dest.read_text(encoding="utf-8"))
        self.assertTrue(dest.stat().st_mode & stat.S_IXUSR)
        # --local puts the Claude Stop hook in the REPO's settings.json,
        # registered as the private _crux-hook entry point (no python3 path)
        settings = json.loads(
            (Path(self.root) / ".claude" / "settings.json").read_text(
                encoding="utf-8"))
        command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(command, "_crux-hook claude-stop")
        # D38: the SessionStart hook brings crux serve back after a reboot
        start = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(start, "_crux-hook claude-session-start")
        self.assertIn("settings.json", out)

    def test_local_never_touches_git_config(self) -> None:
        self._git_dir()
        code, _, _ = self.run_cli(["install-hooks", "--local"])
        self.assertEqual(code, 0)
        for call in self.mods["crux.gitio"].run_git.call_args_list:
            self.assertNotIn("config", call.args[0])

    def test_refuses_to_clobber_foreign_hook(self) -> None:
        git_dir = self._git_dir()
        dest = git_dir / "hooks" / "pre-push"
        dest.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
        code, out, _ = self.run_cli(["install-hooks", "--local"])
        self.assertEqual(code, 0)
        self.assertIn("refusing", out)
        self.assertEqual(dest.read_text(encoding="utf-8"),
                         "#!/bin/sh\necho custom\n")
        # the Claude hook still installs; only the git hook was refused
        self.assertTrue(
            (Path(self.root) / ".claude" / "settings.json").is_file())

    def test_force_overwrites_foreign_hook(self) -> None:
        git_dir = self._git_dir()
        dest = git_dir / "hooks" / "pre-push"
        dest.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
        code, _, _ = self.run_cli(["install-hooks", "--local", "--force"])
        self.assertEqual(code, 0)
        self.assertIn("crux", dest.read_text(encoding="utf-8"))

    def test_reinstalls_over_existing_crux_hook(self) -> None:
        git_dir = self._git_dir()
        dest = git_dir / "hooks" / "pre-push"
        dest.write_text("#!/bin/sh\n# old crux hook\n", encoding="utf-8")
        code, out, _ = self.run_cli(["install-hooks", "--local"])
        self.assertEqual(code, 0)
        self.assertIn("installed", out)
        self.assertIn("_crux-hook pre-push", dest.read_text(encoding="utf-8"))


class TestInstallHooksGlobal(CliTestBase):
    """Default (no --local) mode: core.hooksPath + pass-through shims."""

    def _wire_git(self, hooks_path: str = "") -> None:
        """Mock run_git: `config --global core.hooksPath` reads/writes."""
        self.config_writes: list[str] = []

        def run_git(cmd: list[str], cwd: str | None = None) -> str:
            if cmd[:3] == ["config", "--global", "core.hooksPath"]:
                if len(cmd) == 3:  # read
                    if hooks_path:
                        return hooks_path
                    raise CruxError("git config exited 1 (unset)")
                self.config_writes.append(cmd[3])  # write
                return ""
            raise AssertionError(f"unexpected git call: {cmd}")

        self.mods["crux.gitio"].run_git.side_effect = run_git

    def _default_dir(self) -> Path:
        return Path(self.root) / ".config" / "crux" / "git-hooks"

    def test_installs_globally_and_sets_hooks_path(self) -> None:
        self._wire_git()
        code, out, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        dest = self._default_dir() / "pre-push"
        self.assertTrue(dest.is_file())
        self.assertIn("crux", dest.read_text(encoding="utf-8"))
        self.assertTrue(dest.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(self.config_writes, [str(self._default_dir())])
        self.assertIn("core.hooksPath", out)
        # the Claude Stop hook lands in the USER settings.json
        settings = json.loads(
            (Path(self.root) / ".claude" / "settings.json").read_text(
                encoding="utf-8"))
        command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn("claude-stop", command)

    def test_installs_post_checkout_as_a_real_hook(self) -> None:
        self._wire_git()
        code, out, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        dest = self._default_dir() / "post-checkout"
        self.assertTrue(dest.is_file())
        text = dest.read_text(encoding="utf-8")
        self.assertIn("crux", text)      # a crux shim, not the generic pass-through
        self.assertIn("_crux-hook post-checkout", text)
        self.assertTrue(dest.stat().st_mode & stat.S_IXUSR)
        self.assertIn("post-checkout", out)
        # and it is NOT in the pass-through set (it would be overwritten by a shim)
        self.assertNotIn("post-checkout", cli._PASSTHROUGH_HOOKS)

    def test_installs_post_commit_as_a_real_hook(self) -> None:
        self._wire_git()
        code, out, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        dest = self._default_dir() / "post-commit"
        self.assertTrue(dest.is_file())
        text = dest.read_text(encoding="utf-8")
        self.assertIn("_crux-hook post-commit", text)  # a crux shim, not pass-through
        self.assertTrue(dest.stat().st_mode & stat.S_IXUSR)
        self.assertIn("post-commit", out)
        # and it is NOT in the pass-through set (a shim would overwrite it)
        self.assertNotIn("post-commit", cli._PASSTHROUGH_HOOKS)

    def test_upgrades_a_post_commit_shim_to_the_real_hook(self) -> None:
        # Older crux installs shipped post-commit as a pass-through shim; the
        # shim text contains "crux", so a re-run upgrades it without --force.
        self._wire_git()
        shim_dir = self._default_dir()
        shim_dir.mkdir(parents=True)
        (shim_dir / "post-commit").write_text(cli._PASSTHROUGH_SHIM,
                                              encoding="utf-8")
        code, _, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        self.assertIn("_crux-hook post-commit",
                      (shim_dir / "post-commit").read_text(encoding="utf-8"))

    def test_writes_passthrough_shims_for_other_hooks(self) -> None:
        self._wire_git()
        code, _, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        for name in cli._PASSTHROUGH_HOOKS:
            shim = self._default_dir() / name
            self.assertTrue(shim.is_file(), name)
            self.assertTrue(shim.stat().st_mode & stat.S_IXUSR, name)
            text = shim.read_text(encoding="utf-8")
            self.assertIn("--git-common-dir", text)
            self.assertIn('exec "$local_hook" "$@"', text)
        # exclusive-semantics hooks must NOT be shimmed
        self.assertFalse((self._default_dir() / "fsmonitor-watchman").exists())
        self.assertFalse((self._default_dir() / "push-to-checkout").exists())

    def test_respects_existing_hooks_path_and_leaves_config_alone(self) -> None:
        existing = Path(self.root) / "my-hooks"
        existing.mkdir()
        self._wire_git(hooks_path=str(existing))
        code, _, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        self.assertTrue((existing / "pre-push").is_file())
        self.assertEqual(self.config_writes, [])
        # a user-managed hooks dir gets no shims sprayed into it
        self.assertFalse((existing / "pre-commit").exists())

    def test_refuses_foreign_global_pre_push(self) -> None:
        existing = Path(self.root) / "my-hooks"
        existing.mkdir()
        (existing / "pre-push").write_text("#!/bin/sh\necho mine\n",
                                           encoding="utf-8")
        self._wire_git(hooks_path=str(existing))
        code, out, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        self.assertIn("refusing", out)
        self.assertEqual((existing / "pre-push").read_text(encoding="utf-8"),
                         "#!/bin/sh\necho mine\n")

    def test_relative_hooks_path_bails_with_hint(self) -> None:
        self._wire_git(hooks_path="relative/hooks")
        code, out, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        self.assertIn("relative", out)
        self.assertIn("--local", out)
        self.assertEqual(self.config_writes, [])

    def test_reinstall_is_idempotent(self) -> None:
        self._wire_git()
        self.run_cli(["install-hooks"])
        # second run: hooksPath now set to the crux dir
        self._wire_git(hooks_path=str(self._default_dir()))
        code, out, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        self.assertIn("installed", out)
        self.assertEqual(self.config_writes, [])
        # the Claude Stop hook is not duplicated either
        settings = json.loads(self._claude_settings().read_text(encoding="utf-8"))
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 1)

    def _claude_settings(self) -> Path:
        return Path(self.root) / ".claude" / "settings.json"

    def test_claude_hook_merges_into_existing_settings(self) -> None:
        self._wire_git()
        existing = {"model": "opus",
                    "hooks": {"Stop": [{"hooks": [
                        {"type": "command", "command": "echo mine"}]}]}}
        self._claude_settings().parent.mkdir(parents=True)
        self._claude_settings().write_text(json.dumps(existing),
                                           encoding="utf-8")
        code, _, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        settings = json.loads(self._claude_settings().read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "opus")  # untouched
        stop = settings["hooks"]["Stop"]
        self.assertEqual(len(stop), 2)
        self.assertEqual(stop[0]["hooks"][0]["command"], "echo mine")
        self.assertIn("claude-stop", stop[1]["hooks"][0]["command"])

    def test_claude_hook_adds_session_start_to_existing_stop_install(self) -> None:
        # A machine that ran install-hooks before the SessionStart hook existed
        # gets it on a re-run, without its Stop hook being duplicated.
        self._wire_git()
        older = {"hooks": {
            "Stop": [{"hooks": [{"type": "command",
                                 "command": "_crux-hook claude-stop"}]}],
            "SessionStart": [{"hooks": [{"type": "command",
                                         "command": "echo mine"}]}]}}
        self._claude_settings().parent.mkdir(parents=True)
        self._claude_settings().write_text(json.dumps(older), encoding="utf-8")
        code, _, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        settings = json.loads(self._claude_settings().read_text(encoding="utf-8"))
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)
        start = settings["hooks"]["SessionStart"]
        self.assertEqual(len(start), 2)
        self.assertEqual(start[0]["hooks"][0]["command"], "echo mine")
        self.assertEqual(start[1]["hooks"][0]["command"],
                         "_crux-hook claude-session-start")

    def test_claude_hook_refreshes_stale_path(self) -> None:
        self._wire_git()
        stale = {"hooks": {"Stop": [{"hooks": [
            {"type": "command",
             "command": "python3 /old/place/claude_intent_hook.py"}]}]}}
        self._claude_settings().parent.mkdir(parents=True)
        self._claude_settings().write_text(json.dumps(stale), encoding="utf-8")
        code, _, _ = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        settings = json.loads(self._claude_settings().read_text(encoding="utf-8"))
        stop = settings["hooks"]["Stop"]
        self.assertEqual(len(stop), 1)  # migrated in place, not appended
        command = stop[0]["hooks"][0]["command"]
        self.assertNotIn("/old/place/", command)   # legacy python3 path gone
        self.assertEqual(command, "_crux-hook claude-stop")

    def test_unparseable_settings_left_alone_with_instructions(self) -> None:
        self._wire_git()
        self._claude_settings().parent.mkdir(parents=True)
        self._claude_settings().write_text("{not json", encoding="utf-8")
        code, out, err = self.run_cli(["install-hooks"])
        self.assertEqual(code, 0)
        self.assertEqual(self._claude_settings().read_text(encoding="utf-8"),
                         "{not json")
        # falls back to the manual merge instructions
        self.assertIn("Merge this", out)
        self.assertIn("claude-stop", out)
        self.assertIn("cannot parse", err)


class TestIntentHook(unittest.TestCase):
    """Runs the Claude Stop hook (`_crux-hook claude-stop`) as a real
    subprocess. `_crux-hook` isn't installed in the test env, so drive its
    entry point, cli.hook_main, directly — the same code path the console
    script runs."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def _run_hook(self, stdin_text: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        return subprocess.run(
            [sys.executable, "-c",
             "import sys; from crux.cli import hook_main; "
             "sys.exit(hook_main(['claude-stop']))"],
            input=stdin_text, capture_output=True, text=True,
            cwd=self.root, env=env, timeout=30)

    def test_writes_intent_from_last_assistant_message(self) -> None:
        transcript = Path(self.root) / "t.jsonl"
        records = [
            {"type": "user",
             "message": {"role": "user", "content": "do the thing"}},
            {"type": "assistant",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "first answer"}]}},
            {"type": "assistant",
             "message": {"role": "assistant",
                         "content": [{"type": "text",
                                      "text": "Renamed the flag.\n"
                                              "TODO: check flush timing\n"
                                              "I am not sure about locking."}]}},
        ]
        transcript.write_text(
            "\n".join(json.dumps(r) for r in records), encoding="utf-8")
        payload = {"session_id": "s1", "transcript_path": str(transcript),
                   "cwd": self.root}
        proc = self._run_hook(json.dumps(payload))
        self.assertEqual(proc.returncode, 0)
        intent = json.loads(
            (Path(self.root) / ".crux" / "intent.json").read_text(encoding="utf-8"))
        self.assertEqual(intent["session_id"], "s1")
        self.assertTrue(intent["summary"].startswith("Renamed the flag."))
        self.assertNotIn("first answer", intent["summary"])
        self.assertEqual(intent["uncertainties"],
                         ["TODO: check flush timing",
                          "I am not sure about locking."])
        self.assertTrue(intent["ts"])

    def test_summary_capped_at_2000_chars(self) -> None:
        transcript = Path(self.root) / "t.jsonl"
        transcript.write_text(json.dumps(
            {"type": "assistant",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "x" * 5000}]}}),
            encoding="utf-8")
        payload = {"session_id": "s2", "transcript_path": str(transcript),
                   "cwd": self.root}
        proc = self._run_hook(json.dumps(payload))
        self.assertEqual(proc.returncode, 0)
        intent = json.loads(
            (Path(self.root) / ".crux" / "intent.json").read_text(encoding="utf-8"))
        self.assertEqual(len(intent["summary"]), 2000)

    def test_never_crashes_on_garbage_stdin(self) -> None:
        proc = self._run_hook("this is not json")
        self.assertEqual(proc.returncode, 0)

    def test_never_crashes_on_missing_transcript(self) -> None:
        payload = {"session_id": "s3",
                   "transcript_path": str(Path(self.root) / "missing.jsonl"),
                   "cwd": self.root}
        proc = self._run_hook(json.dumps(payload))
        self.assertEqual(proc.returncode, 0)


class TestHookShims(unittest.TestCase):
    """The installed hooks are thin POSIX-sh shims (cli._hook_shim): delegate
    to a repo-local hook, then `exec _crux-hook <name>`. No bash, no nohup, no
    /dev/tty — all the logic lives in the Python hook bodies below."""

    def test_all_shims_are_posix_sh_and_delegate_locally(self) -> None:
        for name in ("pre-push", "post-commit", "post-checkout"):
            text = cli._hook_shim(name)
            self.assertTrue(text.startswith("#!/bin/sh\n"), name)
            # delegate to the repo-local hook via the common dir (never
            # --git-path, which would resolve back to the global dir)
            self.assertIn("--git-common-dir", text)
            self.assertIn('-ef "$0"', text)
            # hand off to the private machinery entry point, output logged
            self.assertIn(f"exec _crux-hook {name}", text)
            self.assertIn("command -v _crux-hook", text)
            self.assertIn('>>"$HOME/.cache/crux/crux.log" 2>&1', text)
            # never a bash-only / POSIX-shell-only detachment primitive
            self.assertNotIn("nohup", text)
            self.assertNotIn("/dev/tty", text)

    def test_pre_push_shim_propagates_a_local_failure(self) -> None:
        # only pre-push must let a failing repo-local hook abort the push
        self.assertIn('"$local_hook" "$@" || exit $?', cli._hook_shim("pre-push"))

    def test_informational_shims_ignore_a_local_failure(self) -> None:
        for name in ("post-commit", "post-checkout"):
            self.assertIn('"$local_hook" "$@" || true', cli._hook_shim(name))

    def test_passthrough_shim_is_posix_sh_and_calls_no_crux(self) -> None:
        text = cli._PASSTHROUGH_SHIM
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn('exec "$local_hook" "$@"', text)
        self.assertIn('basename "$0"', text)
        self.assertNotIn("_crux-hook", text)  # pure delegation, no python startup


class TestSetupLoggingDedup(unittest.TestCase):
    """`_setup_logging` must not write each record twice. The hook shim runs
    `_crux-hook <name> >>crux.log 2>&1`, so under a hook stderr *is* the log
    file; a FileHandler on that same file would then duplicate every line."""

    def _fresh_log(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d / "crux.log"

    def test_single_write_when_stderr_is_the_log_file(self) -> None:
        logpath = self._fresh_log()
        logpath.parent.mkdir(parents=True, exist_ok=True)
        with open(logpath, "a", encoding="utf-8") as fh, \
                mock.patch("crux.cli._log_file", return_value=logpath), \
                mock.patch("sys.stderr", fh):
            log = cli._setup_logging()
            log.info("ONLY-ONCE")
            for handler in log.handlers:
                handler.flush()
        self.assertEqual(logpath.read_text(encoding="utf-8").count("ONLY-ONCE"),
                         1)

    def test_file_captured_when_stderr_is_the_console(self) -> None:
        logpath = self._fresh_log()
        # stderr is an in-memory stream, not the log file -> FileHandler added.
        with mock.patch("crux.cli._log_file", return_value=logpath), \
                mock.patch("sys.stderr", io.StringIO()):
            log = cli._setup_logging()
            log.info("TO-FILE")
            for handler in log.handlers:
                handler.flush()
        self.assertEqual(logpath.read_text(encoding="utf-8").count("TO-FILE"), 1)


class TestHookSubcommands(CliTestBase):
    """The Python hook bodies behind `_crux-hook <name>` (cli.hook_main),
    where the real logic now lives. Siblings are mocked; the detached spawn is
    replaced so nothing is actually launched."""

    def setUp(self) -> None:
        super().setUp()
        # not stale by default, so the pre-push body reaches the spawn step
        self.mods["crux.commitmsg"].ensure_enriched.return_value = False
        spawn = mock.patch("crux.cli._spawn_detached")
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)

    def _hook(self, argv: list[str], stdin: str = "") -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = cli.hook_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    # --- claude-session-start (D38 autostart) ------------------------------
    def test_session_start_ensures_serve_and_prints_nothing(self) -> None:
        # SessionStart stdout is injected into the session's context, so the
        # hook must stay silent; it only brings the service up.
        self.mods["crux.gitio"].run_git.return_value = self.root + "\n"
        with mock.patch("crux.serve.ensure_running") as ensure:
            code, out, _ = self._hook(["claude-session-start"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        ensure.assert_called_once()
        self.assertIs(ensure.call_args.args[0], self.cfg)
        self.mods["crux.config"].load.assert_called_once_with(self.root)

    def test_session_start_outside_a_repo_uses_cwd(self) -> None:
        self.mods["crux.gitio"].run_git.side_effect = CruxError("not a repo")
        with mock.patch("crux.serve.ensure_running") as ensure:
            code, out, _ = self._hook(["claude-session-start"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        ensure.assert_called_once()
        self.mods["crux.config"].load.assert_called_once_with(os.getcwd())

    def test_session_start_never_fails_the_session(self) -> None:
        self.mods["crux.gitio"].run_git.return_value = self.root + "\n"
        with mock.patch("crux.serve.ensure_running",
                        side_effect=RuntimeError("boom")):
            code, out, _ = self._hook(["claude-session-start"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    # --- pre-push -------------------------------------------------------
    def test_pre_push_in_scope_spawns_detached_review(self) -> None:
        code, _, _ = self._hook(PRE_PUSH_ARGV)
        self.assertEqual(code, 0)
        self.spawn.assert_called_once()
        self.assertEqual(self.spawn.call_args.args[0],
                         ["run", "--delay", "15", "--yes"])

    def test_unknown_hook_name_exits_0_silently(self) -> None:
        # A newer plugin (or hook shim) can invoke a hook this crux does not
        # have — claude-post-bash on a pre-plugin CLI. argparse would exit 2,
        # which Claude Code reports as a blocking error on every Bash call,
        # so the unknown name must be ignored instead.
        code, out, err = self._hook(["claude-not-a-real-hook"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        # the reason is logged, but argparse's usage dump is suppressed
        self.assertIn("claude-not-a-real-hook", err)
        self.assertNotIn("usage:", err)
        self.spawn.assert_not_called()

    def test_pre_push_out_of_scope_does_nothing(self) -> None:
        self.info.owner = "stranger"
        code, _, err = self._hook(PRE_PUSH_ARGV)
        self.assertEqual(code, 0)
        self.spawn.assert_not_called()
        self.mods["crux.commitmsg"].ensure_enriched.assert_not_called()
        self.assertIn("D13", err)

    def test_pre_push_stale_shas_returns_1_and_skips_review(self) -> None:
        # D28: enrichment happened now, so the push's shas are stale — exit 1
        # aborts the push (the shim propagates it) and no review is spawned.
        self.mods["crux.commitmsg"].ensure_enriched.return_value = True
        code, _, _ = self._hook(PRE_PUSH_ARGV)
        self.assertEqual(code, 1)
        self.spawn.assert_not_called()

    # --- post-commit ----------------------------------------------------
    def test_post_commit_human_commit_spawns_enrichment(self) -> None:
        self.mods["crux.gitio"].run_git.return_value = "fix stuff"
        code, _, _ = self._hook(["post-commit"])
        self.assertEqual(code, 0)
        self.spawn.assert_called_once()
        self.assertEqual(self.spawn.call_args.args[0],
                         ["enrich-commit", "--sha", "a" * 40])

    def test_post_commit_claude_commit_is_left_alone(self) -> None:
        self.mods["crux.gitio"].run_git.return_value = (
            "feat: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        code, _, _ = self._hook(["post-commit"])
        self.assertEqual(code, 0)
        self.spawn.assert_not_called()

    def test_post_commit_amended_commit_is_left_alone(self) -> None:
        self.mods["crux.gitio"].run_git.return_value = "msg\n\nAmended-by: Crux"
        code, _, _ = self._hook(["post-commit"])
        self.assertEqual(code, 0)
        self.spawn.assert_not_called()

    def test_post_commit_out_of_scope_does_not_spawn(self) -> None:
        self.info.owner = "stranger"
        self.mods["crux.gitio"].run_git.return_value = "fix stuff"
        code, _, _ = self._hook(["post-commit"])
        self.assertEqual(code, 0)
        self.spawn.assert_not_called()

    # --- post-checkout --------------------------------------------------
    def _wire_checkout_git(self, existing_base: bool = False) -> list[list[str]]:
        calls: list[list[str]] = []

        def run_git(cmd: list[str], cwd: str | None = None) -> str:
            calls.append(cmd)
            if cmd == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return "feature"
            if cmd == ["config", "--get", "branch.feature.cruxBase"]:
                if existing_base:
                    return "develop"
                raise CruxError("unset")           # git exits 1 when unset
            if cmd == ["rev-parse", "--abbrev-ref", "@{-1}"]:
                return "main"
            if cmd == ["show-ref", "--verify", "--quiet", "refs/heads/main"]:
                return ""
            if cmd[:2] == ["config", "branch.feature.cruxBase"]:
                return ""
            raise AssertionError(f"unexpected git call: {cmd}")

        self.mods["crux.gitio"].run_git.side_effect = run_git
        return calls

    @staticmethod
    def _recorded(calls: list[list[str]]) -> list[str]:
        return [c[2] for c in calls if c[:2] == ["config", "branch.feature.cruxBase"]]

    def test_post_checkout_records_parent_on_branch_creation(self) -> None:
        # `checkout -b feature` off main: git records "Created from HEAD", so
        # the start point is unusable and @{-1} — valid here, because HEAD did
        # not move — names the parent.
        calls = self._wire_checkout_git()
        code, _, _ = self._hook(["post-checkout", "abc", "abc", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(self._recorded(calls), ["main"])

    def test_post_checkout_records_start_point_when_head_moved(self) -> None:
        # D34: `git switch -c feature parent` moves HEAD and leaves @{-1}
        # pointing at whatever was checked out before — NOT the parent. Git's
        # own reflog start point is what gets this right.
        calls = self._wire_checkout_git()
        self.mods["crux.gitio"].branch_start_point.return_value = "parent"
        code, _, _ = self._hook(["post-checkout", "abc", "def", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(self._recorded(calls), ["parent"])
        self.assertNotIn(["rev-parse", "--abbrev-ref", "@{-1}"], calls)

    def test_post_checkout_asks_only_for_a_fresh_branch(self) -> None:
        self._wire_checkout_git()
        self._hook(["post-checkout", "abc", "abc", "1"])
        kwargs = self.mods["crux.gitio"].branch_start_point.call_args.kwargs
        self.assertTrue(kwargs.get("fresh_only"))

    def test_post_checkout_does_not_clobber_recorded_parent(self) -> None:
        calls = self._wire_checkout_git(existing_base=True)
        code, _, _ = self._hook(["post-checkout", "abc", "abc", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(self._recorded(calls), [])

    def test_post_checkout_ignores_plain_switch(self) -> None:
        # HEAD moved (prev != new) and the branch is not freshly created, so
        # branch_start_point answers None: a switch to an existing branch, not
        # a creation — nothing recorded.
        calls = self._wire_checkout_git()
        code, _, _ = self._hook(["post-checkout", "abc", "def", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(self._recorded(calls), [])

    def test_post_checkout_ignores_file_checkout(self) -> None:
        # flag != 1: a path checkout, not a branch checkout.
        self.mods["crux.gitio"].run_git.side_effect = AssertionError(
            "must not touch git on a file checkout")
        code, _, _ = self._hook(["post-checkout", "abc", "abc", "0"])
        self.assertEqual(code, 0)


class TestSpawnDetached(unittest.TestCase):
    """The one irreducibly OS-specific step: portable process detachment."""

    def test_uses_new_session_on_posix(self) -> None:
        with mock.patch("crux.cli.os.name", "posix"), \
             mock.patch("crux.cli.subprocess.Popen") as popen, \
             mock.patch("crux.cli.open", mock.mock_open()):
            cli._spawn_detached(["run"], mock.MagicMock())
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs.get("start_new_session"))
        self.assertNotIn("creationflags", kwargs)
        # invokes the current interpreter's -m crux, never a bare `crux`
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:3], [sys.executable, "-m", "crux"])

    def test_uses_detached_flags_on_windows(self) -> None:
        # emulate the Windows-only creationflags constants
        fake = mock.MagicMock()
        fake.DETACHED_PROCESS = 0x8
        fake.CREATE_NEW_PROCESS_GROUP = 0x200
        fake.DEVNULL = -3
        fake.STDOUT = -2
        with mock.patch("crux.cli.os.name", "nt"), \
             mock.patch("crux.cli.subprocess", fake), \
             mock.patch("crux.cli._log_file", return_value=mock.MagicMock()), \
             mock.patch("crux.cli.open", mock.mock_open()):
            cli._spawn_detached(["run"], mock.MagicMock())
        kwargs = fake.Popen.call_args.kwargs
        self.assertEqual(kwargs.get("creationflags"), 0x8 | 0x200)
        self.assertNotIn("start_new_session", kwargs)

    def test_never_raises_when_spawn_fails(self) -> None:
        with mock.patch("crux.cli.subprocess.Popen",
                        side_effect=OSError("boom")), \
             mock.patch("crux.cli.open", mock.mock_open()):
            cli._spawn_detached(["run"], mock.MagicMock())  # must not raise

    def test_hands_the_terminal_down_via_inherited_fd(self) -> None:
        # The detached child sheds its controlling terminal, so the parent opens
        # a fd to it and hands it down (pass_fds + CRUX_TTY_FD) so notify_tty
        # still lands. The parent then closes its own copy.
        with mock.patch("crux.post.open_terminal_fd", return_value=17), \
             mock.patch("crux.cli.os.close") as close, \
             mock.patch("crux.cli.subprocess.Popen") as popen, \
             mock.patch("crux.cli.open", mock.mock_open()):
            cli._spawn_detached(["run"], mock.MagicMock())
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs.get("pass_fds"), (17,))
        self.assertEqual(kwargs["env"]["CRUX_TTY_FD"], "17")
        close.assert_any_call(17)  # parent drops its copy after spawning

    def test_no_terminal_fd_when_no_terminal(self) -> None:
        # No terminal to hand down (CI, editor, agent): inherit the env as-is,
        # no pass_fds, and never close a fd we never opened.
        with mock.patch("crux.post.open_terminal_fd", return_value=None), \
             mock.patch("crux.cli.os.close") as close, \
             mock.patch("crux.cli.subprocess.Popen") as popen, \
             mock.patch("crux.cli.open", mock.mock_open()):
            cli._spawn_detached(["run"], mock.MagicMock())
        kwargs = popen.call_args.kwargs
        self.assertIsNone(kwargs.get("env"))
        self.assertNotIn("pass_fds", kwargs)
        close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
