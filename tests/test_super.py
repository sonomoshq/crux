# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for super PRs (D37): scope, candidates, bundle state, combined
diffs, and the one-screen render caps.

Real git repositories are built in temp dirs for the merge tests — the
combined-diff engine is the piece most likely to break silently, and mocking
git would only test the mock. Everything touching GitHub goes through
crux.post._run_gh, which is mocked; no network, no gh binary.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crux.bundle as bundle_store
import crux.candidates as candidates
import crux.clones as clones
import crux.config as config
import crux.superanalyze as superanalyze
import crux.superdiff as superdiff
import crux.superpost as superpost
import crux.superpr as superpr
import crux.superrender as superrender
from crux.models import (SUPER_ENV, Bundle, BundleMember, Candidate, ChangeMap,
                         Config, CruxError, MapArrow, MapStep, RepoInfo,
                         SuperAnnotation, SuperCheck)


def git(args: list[str], cwd: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def git_or_error(args: list[str], cwd: str) -> str:
    """`superact._git`'s contract against a real repo: output, or CruxError.

    Tests that turn on which flags reach git cannot use a fake for it, and a
    fake that raised CalledProcessError instead of CruxError would not exercise
    the handling either.
    """
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True)
    if proc.returncode != 0:
        raise CruxError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc.stdout.strip()


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(["init", "-q", "-b", "main"], str(path))
    git(["config", "user.email", "t@t.co"], str(path))
    git(["config", "user.name", "T"], str(path))
    (path / "f.txt").write_text("line1\nline2\nline3\n")
    git(["add", "-A"], str(path))
    git(["commit", "-qm", "base"], str(path))
    return git(["rev-parse", "HEAD"], str(path))


class TestSuperScope(unittest.TestCase):
    def _load(self, toml: str) -> Config:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "crux.toml").write_text(toml)
            with mock.patch.object(config, "global_config_path",
                                   return_value=Path(tmp) / "missing.toml"):
                return config.load(tmp)

    def test_home_comes_from_super_table(self) -> None:
        cfg = self._load('[super]\nhome = "o/Super"\n')
        self.assertEqual(superpr.home_repo(cfg), "o/Super")

    def test_missing_home_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(superpr.SuperError):
            superpr.home_repo(Config())

    def test_candidates_come_from_the_scope_allowlist(self) -> None:
        # The allowlist is taken as written — no second declaration of which
        # repos a bundle may draw from, and no repo listing call.
        cfg = Config(scope_owners=["acme"], scope_repos=["web", "other/Thing"])
        with mock.patch.object(candidates.prs, "_discover_all") as discover:
            repos, problems = candidates.scope_repos(cfg)
        discover.assert_not_called()
        self.assertEqual(repos, ["acme/web", "other/Thing"])
        self.assertEqual(problems, [])

    def test_no_allowlist_falls_back_to_every_repo_of_the_owners(self) -> None:
        cfg = Config(scope_owners=["acme"], scope_repos=[])
        with mock.patch.object(candidates.prs, "_discover_all",
                               return_value=["acme/A", "acme/B"]) as discover:
            repos, _ = candidates.scope_repos(cfg)
        discover.assert_called_once()
        self.assertEqual(repos, ["acme/A", "acme/B"])

    def test_qualify_uses_scope_owner_for_bare_names(self) -> None:
        cfg = Config(scope_owners=["acme"])
        self.assertEqual(clones.qualify(["web", "other/Thing"], cfg),
                         ["acme/web", "other/Thing"])


class TestClones(unittest.TestCase):
    def test_finds_clone_by_origin_not_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "some-local-name"
            make_repo(path)
            git(["remote", "add", "origin",
                 "git@github.com:acme/RealName.git"], str(path))
            found = clones.find_clones([tmp], ["acme/RealName"])
            self.assertIn("acme/realname", found)
            self.assertEqual(found["acme/realname"].repo, "RealName")

    def test_age_reads_in_plain_words(self) -> None:
        self.assertEqual(clones.age(1_000_000 - 300, now=1_000_000), "5 minutes ago")
        self.assertEqual(clones.age(1_000_000 - 3600, now=1_000_000), "1 hour ago")
        self.assertEqual(clones.age(0), "unknown")


class TestSelection(unittest.TestCase):
    def test_ranges_and_commas(self) -> None:
        picked, bad = candidates.parse_selection("1,3-5", 6)
        self.assertEqual(picked, [0, 2, 3, 4])
        self.assertEqual(bad, [])

    def test_out_of_range_is_a_problem_not_a_crash(self) -> None:
        picked, bad = candidates.parse_selection("1,99", 3)
        self.assertEqual(picked, [0])
        self.assertEqual(len(bad), 1)

    def test_duplicates_collapse(self) -> None:
        picked, _ = candidates.parse_selection("2,2,1-2", 4)
        self.assertEqual(picked, [1, 0])


class TestBundleStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            bundle_store, "store_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _bundle(self, number: int, prs: list[int]) -> Bundle:
        return Bundle(number=number, name="g", home="o/Super", members=[
            BundleMember(owner="o", repo="R", branch=f"b{p}", pr=p) for p in prs])

    def test_round_trip(self) -> None:
        bundle_store.save(self._bundle(1, [10, 11]))
        loaded = bundle_store.load(1)
        self.assertIsNotNone(loaded)
        self.assertEqual([m.pr for m in loaded.members], [10, 11])

    def test_numbers_are_never_reused(self) -> None:
        bundle_store.save(self._bundle(1, [10]))
        bundle_store.save(self._bundle(2, [11]))
        bundle_store.delete(2)
        # 2 is retired even though it is free again: old links must keep
        # meaning what they meant.
        self.assertEqual(bundle_store.next_number(), 2)
        bundle_store.save(self._bundle(5, [12]))
        self.assertEqual(bundle_store.next_number(), 6)

    @staticmethod
    def _issues(*titles: str) -> str:
        return json.dumps([{"number": i + 1, "title": t}
                           for i, t in enumerate(titles)])

    def test_a_number_taken_by_a_brief_is_not_minted_again(self) -> None:
        """The bug this guards: bundles 1 and 2 were filed from another laptop,
        this one holds only 1, so the local answer was 2 — the number of a
        bundle that already exists. `super new` saved over it, then the refresh
        that follows found that brief by tag, saw a revision far ahead of the
        one-revision-old bundle, and adopted its 13 members. The user was shown
        a review of PRs they never picked, under a number already in use."""
        bundle_store.save(self._bundle(1, [10]))
        with mock.patch.object(
                superpost.post, "_run_gh",
                return_value=self._issues(
                    "🦸 Super PR #1: frosting [super-pr-1]",
                    "🦸 Super PR #2: v1-round [super-pr-2]")) as gh:
            self.assertEqual(bundle_store.next_number("o/Super"), 3)
        # Closed briefs count too: gh must be asked for every state, not just
        # the open ones, or a merged bundle's number comes back around.
        self.assertIn("state=all", " ".join(gh.call_args[0][0]))

    def test_a_number_taken_only_locally_still_counts(self) -> None:
        bundle_store.save(self._bundle(4, [10]))
        with mock.patch.object(superpost.post, "_run_gh",
                               return_value=self._issues()):
            self.assertEqual(bundle_store.next_number("o/Super"), 5)

    def test_an_unreachable_home_falls_back_to_the_local_answer(self) -> None:
        """A create must not be blocked by a home repo it cannot read."""
        bundle_store.save(self._bundle(1, [10]))
        with mock.patch.object(superpost.post, "_run_gh",
                               side_effect=CruxError("gh: not authenticated")):
            self.assertEqual(bundle_store.next_number("o/Super"), 2)

    def test_untagged_issues_in_the_home_repo_are_ignored(self) -> None:
        """The home repo is a normal repo; only Crux's own tag names a bundle."""
        with mock.patch.object(
                superpost.post, "_run_gh",
                return_value=self._issues("flaky CI on main", "bump deps")):
            self.assertEqual(bundle_store.next_number("o/Super"), 1)

    def test_no_home_repo_asks_github_nothing(self) -> None:
        bundle_store.save(self._bundle(1, [10]))
        with mock.patch.object(superpost.post, "_run_gh") as gh:
            self.assertEqual(bundle_store.next_number(), 2)
            self.assertEqual(bundle_store.next_number("not-a-slug"), 2)
        gh.assert_not_called()

    def test_a_pr_belongs_to_one_bundle(self) -> None:
        bundle_store.save(self._bundle(1, [10, 11]))
        taken = bundle_store.bundled_prs()
        self.assertEqual(taken["o/R#10"], 1)
        self.assertEqual(taken["o/R#11"], 1)

    def test_editing_a_bundle_does_not_exclude_its_own_members(self) -> None:
        bundle_store.save(self._bundle(1, [10]))
        self.assertEqual(bundle_store.bundled_prs(exclude=1), {})

    def test_corrupt_file_does_not_hide_the_others(self) -> None:
        bundle_store.save(self._bundle(1, [10]))
        (Path(self.tmp.name) / "9.json").write_text("{not json")
        self.assertEqual([b.number for b in bundle_store.load_all()], [1])


class TestCombinedDiff(unittest.TestCase):
    """The combined diff is the heart of D37 — and the merge must never touch
    the working tree, since these are repos the user may be mid-edit in."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "repo"
        self.base = make_repo(self.path)
        self.root = str(self.path)

    def _branch(self, name: str, filename: str, content: str) -> str:
        git(["checkout", "-q", "-b", name, "main"], self.root)
        (self.path / filename).write_text(content)
        git(["add", "-A"], self.root)
        git(["commit", "-qm", name], self.root)
        sha = git(["rev-parse", "HEAD"], self.root)
        git(["checkout", "-q", "main"], self.root)
        return sha

    def test_independent_changes_combine(self) -> None:
        a = self._branch("a", "a.txt", "A\n")
        b = self._branch("b", "b.txt", "B\n")
        tree, conflicts = superdiff._merge_tree(self.root, a, b)
        self.assertEqual(conflicts, [])
        self.assertTrue(tree)

    def test_same_line_collision_is_reported_with_its_files(self) -> None:
        a = self._branch("a", "f.txt", "AAA\nline2\nline3\n")
        b = self._branch("b", "f.txt", "BBB\nline2\nline3\n")
        tree, conflicts = superdiff._merge_tree(self.root, a, b)
        self.assertEqual(tree, "")
        self.assertEqual(conflicts, ["f.txt"])

    def test_merge_leaves_the_working_tree_untouched(self) -> None:
        a = self._branch("a", "a.txt", "A\n")
        b = self._branch("b", "b.txt", "B\n")
        (self.path / "dirty.txt").write_text("uncommitted work\n")
        before = git(["status", "--porcelain"], self.root)
        superdiff._merge_tree(self.root, a, b)
        self.assertEqual(git(["status", "--porcelain"], self.root), before)
        self.assertEqual((self.path / "dirty.txt").read_text(),
                         "uncommitted work\n")

    def test_conflict_kinds_are_distinguished(self) -> None:
        with_base = superdiff.Conflict(owner="o", repo="R", pr=1, branch="b",
                                       files=["f.txt"], against=[])
        sibling = superdiff.Conflict(owner="o", repo="R", pr=2, branch="c",
                                     files=["f.txt"], against=[1])
        # These mean different work — rebase yours, versus reconcile with
        # someone else's — and the brief must not collapse them.
        self.assertTrue(with_base.with_base)
        self.assertFalse(sibling.with_base)
        self.assertIn("rebase", superrender._conflict_check(with_base))
        self.assertIn("#1", superrender._conflict_check(sibling))


class TestSuperCard(unittest.TestCase):
    def _bundle(self, n_prs: int) -> Bundle:
        return Bundle(number=7, name="g", home="o/Super", members=[
            BundleMember(owner="o", repo=f"R{i}", branch=f"b{i}", pr=i)
            for i in range(1, n_prs + 1)])

    def _diffs(self, bundle: Bundle) -> list[superdiff.RepoDiff]:
        return [superdiff.RepoDiff(owner=m.owner, repo=m.repo, root=".",
                                   base_sha="a" * 40, head_sha="b" * 40,
                                   members=[m])
                for m in bundle.members]

    def test_caps_hold_however_big_the_bundle(self) -> None:
        cfg = Config(super_ideas_max=5, super_checks_max=7)
        big = self._bundle(30)
        ann = SuperAnnotation(
            thesis="t",
            ideas=[f"idea {i}" for i in range(20)],
            checks=[SuperCheck(text=f"check {i}") for i in range(20)])
        card = superrender.render_card(big, ann, self._diffs(big), cfg)
        self.assertEqual(card.count("- idea "), 5)
        self.assertEqual(card.count("- [ ] check "), 7)

    def test_conflicts_are_never_dropped_to_fit_the_cap(self) -> None:
        # A change that cannot land is not an optional read: it must survive
        # the checks budget even when the model filled every slot.
        cfg = Config(super_checks_max=2)
        b = self._bundle(2)
        diffs = self._diffs(b)
        diffs[0].conflicts = [superdiff.Conflict(
            owner="o", repo="R1", pr=1, branch="b1", files=["x.py"], against=[2])]
        ann = SuperAnnotation(checks=[SuperCheck(text=f"c{i}") for i in range(5)])
        card = superrender.render_card(b, ann, diffs, cfg)
        self.assertIn("o/R1#1", card)
        self.assertEqual(card.count("- [ ] c"), 2)

    def test_anchor_becomes_a_link_across_repos(self) -> None:
        b = self._bundle(1)
        diffs = self._diffs(b)
        ann = SuperAnnotation(checks=[SuperCheck(
            text="check this", anchor="o/R1 src/a.py:10-20")])
        card = superrender.render_card(b, ann, diffs, Config())
        self.assertIn("https://github.com/o/R1/blob/" + "b" * 40 + "/src/a.py#L10-L20",
                      card)

    def test_unknown_repo_anchor_stays_plain_text(self) -> None:
        # A link to a repo not in the bundle would read as verified when it
        # is not; plain code is the honest fallback.
        b = self._bundle(1)
        ann = SuperAnnotation(checks=[SuperCheck(
            text="x", anchor="other/Nope src/a.py:1")])
        card = superrender.render_card(b, ann, self._diffs(b), Config())
        self.assertIn("`src/a.py:1`", card)
        self.assertNotIn("other/Nope/blob", card)

    def test_anchor_without_a_line_shows_the_path_only(self) -> None:
        # Nothing to link to, so the slug is dropped: "owner/repo some/file.py"
        # renders like a link that failed rather than a deliberate reference.
        b = self._bundle(1)
        ann = SuperAnnotation(checks=[SuperCheck(text="x", anchor="o/R1 src/a.py")])
        card = superrender.render_card(b, ann, self._diffs(b), Config())
        self.assertIn("`src/a.py`", card)
        self.assertNotIn("o/R1 src/a.py", card)

    def test_every_member_appears_in_the_landing_order(self) -> None:
        b = self._bundle(3)
        # The model listed only one PR; the other two must not vanish.
        ann = SuperAnnotation(order=["o/R2#2"])
        card = superrender.render_card(b, ann, self._diffs(b), Config())
        for ref in ("o/R1#1", "o/R2#2", "o/R3#3"):
            self.assertIn(ref, card)

    def test_map_needs_two_steps_and_an_arrow(self) -> None:
        b = self._bundle(1)
        lonely = SuperAnnotation(change_map=ChangeMap(
            steps=[MapStep(id="a", label="Only step")], arrows=[]))
        card = superrender.render_card(b, lonely, self._diffs(b), Config())
        self.assertNotIn("```mermaid", card)

    def test_map_ids_are_renumbered(self) -> None:
        b = self._bundle(1)
        ann = SuperAnnotation(change_map=ChangeMap(
            steps=[MapStep(id="weird id", label="User sends data"),
                   MapStep(id="other", label="System stores it")],
            arrows=[MapArrow(src="weird id", dst="other")]))
        card = superrender.render_card(b, ann, self._diffs(b), Config())
        self.assertIn("n1 --> n2", card)
        self.assertNotIn("weird id", card)


class TestMergeReport(unittest.TestCase):
    def test_blocked_prs_are_named_with_reasons(self) -> None:
        b = Bundle(number=1, name="g", home="o/S")
        results = [
            BundleMember(owner="o", repo="A", branch="x", pr=1, state="merged"),
            BundleMember(owner="o", repo="B", branch="y", pr=2, state="blocked",
                         error="is blocked — a required review has not passed"),
        ]
        text = superrender.render_merge_report(b, results)
        self.assertIn("1 of 2 landed", text)
        self.assertIn("o/B#2", text)
        self.assertIn("required review", text)
        self.assertIn("run the merge again", text)


class TestSuperThesis(unittest.TestCase):
    """A blank thesis has no fallback, so annotate() retries for it."""

    def _annotate(self, *replies: dict):
        with mock.patch.object(superanalyze, "build_prompt", return_value="P"), \
             mock.patch.object(superanalyze, "claude_json",
                               side_effect=list(replies)) as call:
            ann = superanalyze.annotate(mock.Mock(members=[]), [], [],
                                        Config())
        return ann, call

    def test_missing_thesis_is_retried(self) -> None:
        ann, call = self._annotate({}, {"thesis": "Billing moves to the API."})
        self.assertEqual(ann.thesis, "Billing moves to the API.")
        self.assertEqual(call.call_count, 2)
        self.assertIn("`thesis` empty", call.call_args.args[0])

    def test_present_thesis_costs_one_call(self) -> None:
        ann, call = self._annotate({"thesis": "Billing moves to the API."})
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class TestInstallConfig(unittest.TestCase):
    """install.py must add the [super] block to configs written before D37.

    Scaffolding only copies crux.toml.example when no config exists, so without
    this an upgrading user keeps a config with no [super] table and never
    discovers super PRs exist.
    """

    @staticmethod
    def _install():
        import importlib.util
        spec = importlib.util.spec_from_file_location("_inst", ROOT / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def setUp(self) -> None:
        self.inst = self._install()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "crux.toml"

    def _load(self) -> dict:
        import tomllib
        return tomllib.loads(self.path.read_text(encoding="utf-8"))

    def test_example_ships_the_super_block(self) -> None:
        block = self.inst._super_block()
        self.assertIn("[super]", block)
        self.assertIn("home", block)

    def test_pre_d34_config_gains_the_block(self) -> None:
        self.path.write_text('[scope]\nowners = ["acme"]\n\n[slack]\nchannel = ""\n')
        self.inst._write_super_home(self.path, "acme/pr-bundles")
        data = self._load()
        self.assertEqual(data["super"]["home"], "acme/pr-bundles")
        # The rest of the user's config must survive untouched.
        self.assertEqual(data["scope"]["owners"], ["acme"])
        self.assertIn("slack", data)
        # And they get the documented caps, same as a fresh install.
        self.assertEqual(data["super"]["checks_max"], 7)

    def test_existing_empty_home_is_filled_not_duplicated(self) -> None:
        self.path.write_text(
            '[scope]\nowners = ["acme"]\n\n[super]\nhome  = ""\nroots = []\n')
        self.inst._write_super_home(self.path, "acme/pr-bundles")
        text = self.path.read_text()
        self.assertEqual(self._load()["super"]["home"], "acme/pr-bundles")
        self.assertEqual(text.count("[super]"), 1)

    def test_home_key_of_another_table_is_not_hijacked(self) -> None:
        # A `home` key under some other table must not be mistaken for the
        # [super] one — the writer tracks which table it is inside.
        self.path.write_text(
            '[other]\nhome = "keep/me"\n\n[super]\nhome  = ""\n')
        self.inst._write_super_home(self.path, "acme/pr-bundles")
        data = self._load()
        self.assertEqual(data["other"]["home"], "keep/me")
        self.assertEqual(data["super"]["home"], "acme/pr-bundles")

    def test_scope_owner_defaults_the_home_repo(self) -> None:
        self.path.write_text('[scope]\nowners = ["acme", "other"]\n')
        self.assertEqual(self.inst._scope_owner(self.path), "acme")

    def test_appended_block_is_separated_and_parses(self) -> None:
        self.path.write_text('[scope]\nowners = ["acme"]\n')
        self.inst._write_super_home(self.path, "acme/pr-bundles")
        text = self.path.read_text()
        self.assertIn('owners = ["acme"]\n\n# --- Super PRs', text)
        self._load()  # must still be valid TOML

    def test_setup_super_survives_a_host_without_gh(self) -> None:
        """setup_super's `gh repo view` / `gh repo create` are NOT behind a
        shutil.which("gh") guard the way the prerequisite check is; run() is a
        bare subprocess.run and __main__ catches only KeyboardInterrupt, so on
        a gh-less host answering yes to "Set up super PRs?" ended the whole
        installer in an uncaught FileNotFoundError traceback. Cloud containers
        have no gh, and the README now prescribes the setup script as the
        reliable install route there, so this prompt is on the recommended
        path. It must record the choice and move on instead.
        """
        self.path.write_text('[scope]\nowners = ["acme"]\n')
        boom = mock.patch.object(
            self.inst, "run",
            side_effect=AssertionError("gh must not be invoked without a guard"))
        with mock.patch.object(self.inst, "shutil") as sh, \
             mock.patch.object(self.inst, "_existing_configs",
                               return_value=[self.path]), \
             mock.patch.object(self.inst, "ask", return_value=True), \
             mock.patch("builtins.input", return_value="acme/pr-bundles"), \
             contextlib.redirect_stdout(io.StringIO()), \
             boom:
            sh.which.return_value = None          # a host with no gh
            self.inst.setup_super()               # must not raise
        self.assertEqual(self._load()["super"]["home"], "acme/pr-bundles")


class TestSuperSlack(unittest.TestCase):
    """D16/D37: Slack announcements for a bundle, when Slack is configured.

    A bundle has no single repo to name and is re-briefed repeatedly, so the
    message is keyed on the bundle and every later update replies in ONE
    thread rather than posting to the channel again.
    """
    # Bundle 4 on this machine, but issue 17 on GitHub: the two numbering
    # spaces are deliberately different in these tests, because Slack must
    # show the one a reader can act on.
    URL = "https://github.com/o/pr-bundles/issues/17"

    def setUp(self) -> None:
        import os
        self._env = mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.calls: list[tuple[str, dict | None]] = []

    def _bundle(self) -> Bundle:
        return Bundle(number=4, name="g", home="o/pr-bundles", issue=17, members=[
            BundleMember(owner="o", repo="A", branch="x", pr=1),
            BundleMember(owner="o", repo="B", branch="y", pr=2),
            BundleMember(owner="o", repo="B", branch="z", pr=3)])

    def _fake(self, history):
        class Resp:
            def __init__(self, body): self._b = json.dumps(body).encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            payload = json.loads(req.data) if req.data else None
            self.calls.append((req.full_url, payload))
            if "conversations.history" in req.full_url:
                return Resp({"ok": True, "messages": history})
            if "chat.postMessage" in req.full_url:
                return Resp({"ok": True, "ts": "111.222"})
            return Resp({"ok": True})
        return urlopen

    def _post(self) -> dict:
        return next(p for u, p in self.calls if "chat.postMessage" in u)

    def _announce(self, history, previous_ts="", authors=None):
        import crux.slack as slack
        cfg = Config(slack_channel="C0123456")
        with mock.patch("crux.slack.urllib.request.urlopen",
                        side_effect=self._fake(history)):
            return slack.announce_super(cfg, self._bundle(), self.URL,
                                        "does one thing", previous_ts,
                                        authors=authors)

    def test_announces_the_pr_count_and_the_repos_by_name(self) -> None:
        ch, ts = self._announce([{"text": "chatter", "ts": "1.0"}])
        self.assertEqual((ch, ts), ("C0123456", "111.222"))
        text = self._post()["text"]
        # 3 PRs, but only 2 repos — named, not counted, and deduped.
        self.assertIn("3 PRs across A, B", text)

    def test_the_github_issue_number_is_the_link_to_the_brief(self) -> None:
        # GitHub's number, not Crux's local counter — it is the one that
        # matches the issue and means the same thing on anyone's machine.
        # And one thing to click: no second "read the brief" line under it.
        self._announce([{"text": "chatter", "ts": "1.0"}])
        text = self._post()["text"]
        self.assertIn(f"<{self.URL}|*Super PR #17*>", text)
        self.assertNotIn("#4", text)
        self.assertEqual(text.count(self.URL), 1)

    def test_authors_are_credited_before_the_repos(self) -> None:
        self._announce([{"text": "chatter", "ts": "1.0"}],
                       authors=["Fixture A.", "Fixture B."])
        text = self._post()["text"]
        self.assertIn("by Fixture A. and Fixture B.", text)
        self.assertLess(text.index("Fixture A."), text.index("3 PRs across"))

    def test_refresh_replies_in_thread_not_the_channel(self) -> None:
        # The brief is republished on every refresh; N channel posts for one
        # bundle is exactly the noise threading exists to prevent.
        ch, ts = self._announce([{"text": f"see {self.URL}", "ts": "9.9"}])
        self.assertEqual(ts, "9.9")
        self.assertEqual(self._post()["thread_ts"], "9.9")

    def test_saved_ts_threads_when_the_link_scrolled_away(self) -> None:
        _, ts = self._announce([{"text": "chatter", "ts": "1.0"}],
                               previous_ts="7.7")
        self.assertEqual(ts, "7.7")
        self.assertEqual(self._post()["thread_ts"], "7.7")

    def test_brief_link_is_not_unfurled(self) -> None:
        # The brief lives in a private repo; an unfurl would render its
        # contents into the channel.
        self._announce([{"text": "chatter", "ts": "1.0"}])
        self.assertFalse(self._post()["unfurl_links"])

    def test_disabled_slack_is_silent(self) -> None:
        import crux.slack as slack
        with mock.patch("crux.slack.urllib.request.urlopen") as opened:
            self.assertEqual(
                slack.announce_super(Config(slack_channel=""), self._bundle(),
                                     self.URL), ("", ""))
            opened.assert_not_called()

    def test_merge_report_names_blocked_prs(self) -> None:
        import crux.slack as slack
        results = [
            BundleMember(owner="o", repo="A", branch="x", pr=1, state="merged"),
            BundleMember(owner="o", repo="B", branch="y", pr=2, state="blocked",
                         error="required check failing"),
        ]
        cfg = Config(slack_channel="C0123456")
        with mock.patch("crux.slack.urllib.request.urlopen",
                        side_effect=self._fake([{"text": f"x {self.URL}", "ts": "5.5"}])):
            slack.announce_super_merge(cfg, self._bundle(), self.URL, results)
        payload = self._post()
        self.assertEqual(payload["thread_ts"], "5.5")
        self.assertIn("1 of 2 landed", payload["text"])
        self.assertIn("o/B#2", payload["text"])
        self.assertIn("required check failing", payload["text"])

    def test_merge_report_says_all_landed(self) -> None:
        import crux.slack as slack
        results = [BundleMember(owner="o", repo="A", branch="x", pr=1,
                                state="merged")]
        cfg = Config(slack_channel="C0123456")
        with mock.patch("crux.slack.urllib.request.urlopen",
                        side_effect=self._fake([])):
            slack.announce_super_merge(cfg, self._bundle(), self.URL, results)
        self.assertIn("landed", self._post()["text"])
        self.assertNotIn("blocked", self._post()["text"])


class TestBundleSlackState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            bundle_store, "store_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_thread_ts_survives_a_round_trip(self) -> None:
        # Without this the next refresh cannot find its thread and posts to
        # the channel again.
        b = Bundle(number=1, name="g", home="o/S", slack_channel="C1",
                   slack_ts="123.456")
        bundle_store.save(b)
        loaded = bundle_store.load(1)
        self.assertEqual(loaded.slack_channel, "C1")
        self.assertEqual(loaded.slack_ts, "123.456")


class TestSuperOwnsItsFlow(unittest.TestCase):
    """D37: a super PR is ONE change. Selecting five branches must not run the
    single-repo flow five times — no per-repo prompt, review card or Slack
    message — and the one question that IS asked must name the repos."""

    def _cand(self, repo: str, pr: int | None = None) -> Candidate:
        return Candidate(owner="o", repo=repo, branch="feat", pr=pr,
                         path=f"/clones/{repo}")

    def _info(self, repo: str) -> RepoInfo:
        return RepoInfo(root=f"/clones/{repo}", branch="feat", head_sha="a" * 40,
                        base_sha="b" * 40, owner="o", repo=repo)

    def test_the_push_tells_the_pre_push_hook_to_stand_down(self) -> None:
        # Without SUPER_ENV every branch pushed here drags the single-repo
        # flow behind it: a D11 prompt, a detached review, a Slack post.
        pushes: list[dict] = []

        def fake_git(args, cwd=None, env=None, **kw):
            pushes.append({"args": args, "env": env})
            return ""

        with mock.patch.object(superpr.gitio, "run_git", side_effect=fake_git), \
             mock.patch.object(superpr.gitio, "repo_info",
                               return_value=self._info("A")), \
             mock.patch.object(superpr.post, "_create_pr", return_value=7):
            number, base, error = superpr._ensure_pr(self._cand("A"), Config())

        self.assertEqual((number, error), (7, ""))
        self.assertEqual(base, "main")
        self.assertEqual(pushes[0]["env"], {SUPER_ENV: "1"})

    def test_the_hook_body_does_nothing_under_a_super_push(self) -> None:
        import crux.cli as cli
        with mock.patch.dict("os.environ", {SUPER_ENV: "1"}), \
             mock.patch.object(cli, "_cmd_ensure_enriched") as enriched, \
             mock.patch.object(cli, "_cmd_ensure_pr") as ask, \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            rc = cli._cmd_hook_prepush(mock.Mock())
        self.assertEqual(rc, 0)
        enriched.assert_not_called()   # no per-repo model call
        ask.assert_not_called()        # no repo-less "Create a PR?" prompt
        spawned.assert_not_called()    # no per-repo card, no per-repo Slack

    def test_without_it_the_hook_still_reviews_a_normal_push(self) -> None:
        import crux.cli as cli
        env = {k: v for k, v in os.environ.items() if k != SUPER_ENV}
        # Neutralize the D38 serve-autostart: the hook always calls it, and
        # whether it spawns depends on an ambient `crux serve` being up on the
        # configured port — so without this the spawn count below is decided by
        # the machine's live services and test order, not by the hook. This
        # test is about the one thing the review path owes a normal push: a
        # detached review run. serve-autostart has its own tests.
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(cli, "_hook_scope",
                               return_value=(self._info("A"), Config())), \
             mock.patch.object(cli, "_ensure_serving"), \
             mock.patch.object(cli, "_cmd_ensure_enriched", return_value=0), \
             mock.patch.object(cli, "_cmd_ensure_pr"), \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            cli._cmd_hook_prepush(mock.Mock())
        spawned.assert_called_once()

    def test_only_branches_without_a_pr_are_asked_about(self) -> None:
        picks = [self._cand("A"), self._cand("B", pr=4), self._cand("C")]
        self.assertEqual([c.repo for c in superpr.needs_pr(picks)], ["A", "C"])

    def test_the_ask_shows_the_base_each_pr_would_land_in(self) -> None:
        with mock.patch.object(superpr.gitio, "repo_info",
                               return_value=self._info("A")):
            plan = superpr.pr_plan([self._cand("A"), self._cand("B", pr=4)],
                                   Config(pr_default_base="trunk"))
        # Only the PR-less pick, and its base is the branch it came from.
        self.assertEqual([(c.repo, b) for c, b in plan], [("A", "trunk")])

    def test_a_typed_branch_redirects_every_new_pr(self) -> None:
        import crux.cli as cli
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch("builtins.input", return_value="release-2"), \
             mock.patch("sys.stdin.isatty", return_value=True):
            go, base = cli._confirm_super_prs([(self._cand("A"), "main"),
                                               (self._cand("B"), "main")])
        self.assertEqual((go, base), (True, "release-2"))

    def test_the_shown_base_stands_when_the_ask_is_accepted(self) -> None:
        import crux.cli as cli
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch("sys.stdin.isatty", return_value=True):
            self.assertEqual(cli._confirm_super_prs([(self._cand("A"), "main")]),
                             (True, ""))

    def test_the_chosen_base_reaches_the_pr_and_the_member(self) -> None:
        with mock.patch.object(superpr.gitio, "run_git", return_value=""), \
             mock.patch.object(superpr.gitio, "repo_info",
                               return_value=self._info("A")), \
             mock.patch.object(superpr.post, "_create_pr",
                               return_value=9) as created, \
             mock.patch.object(superpr.bundle_store, "next_number",
                               return_value=1), \
             mock.patch.object(superpr.bundle_store, "save"):
            bundle, problems = superpr.create(
                Config(super_home="o/S"), [self._cand("A")], base="release-2")
        self.assertEqual(problems, [])
        self.assertEqual(created.call_args[0][1], "release-2")
        self.assertEqual(bundle.members[0].base, "release-2")

    def test_the_ask_names_every_repo_not_just_the_branch(self) -> None:
        # The complaint the separate prompt exists to fix: one shared branch
        # name across four repos, and a prompt that showed only the branch.
        import crux.cli as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch("sys.stdin.isatty", return_value=True):
            ok, _base = cli._confirm_super_prs([(self._cand("Alpha"), "main"),
                                                (self._cand("Beta"), "dev")])
        self.assertTrue(ok)
        self.assertIn("o/Alpha", out.getvalue())
        self.assertIn("o/Beta", out.getvalue())

    def test_declining_the_ask_pushes_nothing(self) -> None:
        import crux.cli as cli
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch("builtins.input", return_value="n"), \
             mock.patch("sys.stdin.isatty", return_value=True):
            go, _base = cli._confirm_super_prs([(self._cand("Alpha"), "main")])
        self.assertFalse(go)

    def test_nothing_to_open_asks_nothing(self) -> None:
        import crux.cli as cli
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertEqual(cli._confirm_super_prs([]), (True, ""))


class TestSuperTestSteps(unittest.TestCase):
    """D37: the walkthrough that crosses repos is the one no member PR can
    give — and it stays as short as the per-PR card's."""

    def test_steps_are_parsed_and_capped(self) -> None:
        import crux.superanalyze as superanalyze
        ann = superanalyze._coerce(
            {"thesis": "t",
             "integration_test": [f"step {i}" for i in range(20)] + ["", "  "]},
            Config())
        self.assertEqual(len(ann.integration_test),
                         superanalyze._TEST_STEPS_MAX)
        self.assertEqual(ann.integration_test[0], "step 0")

    def test_no_steps_is_a_fine_answer(self) -> None:
        import crux.superanalyze as superanalyze
        ann = superanalyze._coerce({"thesis": "t"}, Config())
        self.assertEqual(ann.integration_test, [])

    def test_the_comment_is_numbered_sticky_and_not_the_card(self) -> None:
        from crux.models import SUPER_MARKER, TEST_MARKER
        body = superrender.render_test_comment(
            Bundle(number=3, name="feature", home="o/S"),
            ["run `crux super new`", "see the brief issue appear"])
        self.assertTrue(body.startswith(TEST_MARKER))   # sticky, upserted
        self.assertNotIn(SUPER_MARKER, body)            # never the brief itself
        self.assertIn("1. run `crux super new`", body)
        self.assertIn("2. see the brief issue appear", body)

    def test_the_prompt_asks_for_the_cross_repo_walkthrough(self) -> None:
        import crux.superanalyze as superanalyze
        prompt = superanalyze.PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn('"integration_test"', prompt)
        self.assertIn("{test_steps_max}", prompt)

    def test_a_dry_run_shows_the_steps_it_would_post(self) -> None:
        # Otherwise the preview hides them and they land unreviewed.
        from crux.models import SuperAnnotation
        bundle = Bundle(number=1, name="f", home="o/S",
                        members=[BundleMember(owner="o", repo="A",
                                              branch="x", pr=1)])
        ann = SuperAnnotation(thesis="t", integration_test=["start the bakery app"])
        with mock.patch.object(superpr, "_clone_paths", return_value=({}, {})), \
             mock.patch.object(superpr.superdiff, "build_all",
                               return_value=([mock.Mock()], [])), \
             mock.patch.object(superpr.superanalyze, "harvest", return_value=[]), \
             mock.patch.object(superpr.superanalyze, "annotate", return_value=ann), \
             mock.patch.object(superpr.superrender, "render_card",
                               return_value="CARD"), \
             mock.patch.object(superpr.superpost, "publish") as published:
            card, url, _problems = superpr.refresh(bundle, Config(),
                                                   publish=False)
        published.assert_not_called()
        self.assertIn("CARD", card)
        self.assertIn("start the bakery app", card)

    def test_publishing_stickies_them_under_the_brief(self) -> None:
        from crux.models import TEST_MARKER
        import crux.superpost as superpost
        bundle = Bundle(number=1, name="f", home="o/S", issue=12,
                        members=[BundleMember(owner="o", repo="A",
                                              branch="x", pr=1)])
        with mock.patch.object(superpost, "_upsert") as upserted:
            superpost.publish_test_steps(bundle, ["start the bakery app"])
        slug, number, body, marker = upserted.call_args[0]
        self.assertEqual((slug, number, marker), ("o/S", 12, TEST_MARKER))
        self.assertIn("start the bakery app", body)


class TestMemberPushRefreshesTheBundle(unittest.TestCase):
    """D37: a member branch is reviewed AS part of its bundle. A push to one
    re-briefs the super PR instead of posting the per-PR card."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            bundle_store, "store_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        bundle_store.save(Bundle(
            number=4, name="feature", home="o/S",
            members=[BundleMember(owner="o", repo="A", branch="feat", pr=1)]))
        import crux.cli as cli
        # The member's PR is open unless a test says otherwise — no real gh.
        state = mock.patch.object(cli, "_member_pr_state", return_value="open")
        self.pr_state = state.start()
        self.addCleanup(state.stop)

    def _info(self, repo: str, branch: str) -> RepoInfo:
        return RepoInfo(root=f"/clones/{repo}", branch=branch,
                        head_sha="a" * 40, base_sha="b" * 40, owner="o",
                        repo=repo)

    def test_the_branch_finds_its_bundle_without_a_pr_number(self) -> None:
        # A push knows its branch, not its PR — the lookup must not need one.
        found = bundle_store.find_by_branch("O", "a", "feat")
        self.assertEqual(found.number, 4)
        self.assertIsNone(bundle_store.find_by_branch("o", "A", "other"))

    def test_pre_push_rebriefs_the_bundle_instead_of_the_card(self) -> None:
        import crux.cli as cli
        env = {k: v for k, v in os.environ.items() if k != SUPER_ENV}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(cli, "_hook_scope",
                               return_value=(self._info("A", "feat"), Config())), \
             mock.patch.object(cli, "_cmd_ensure_enriched", return_value=0), \
             mock.patch.object(cli, "_cmd_ensure_pr") as ask, \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            cli._cmd_hook_prepush(mock.Mock())
        self.assertEqual(spawned.call_args[0][0],
                         ["super", "refresh", "4", "--delay", "15"])
        ask.assert_not_called()   # the member PR exists; nothing to ask

    def test_a_branch_outside_any_bundle_still_gets_its_card(self) -> None:
        import crux.cli as cli
        env = {k: v for k, v in os.environ.items() if k != SUPER_ENV}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(cli, "_hook_scope",
                               return_value=(self._info("A", "solo"), Config())), \
             mock.patch.object(cli, "_cmd_ensure_enriched", return_value=0), \
             mock.patch.object(cli, "_cmd_ensure_pr"), \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            cli._cmd_hook_prepush(mock.Mock())
        self.assertEqual(spawned.call_args[0][0],
                         ["run", "--delay", "15", "--yes"])

    def test_the_claude_hook_routes_a_member_push_the_same_way(self) -> None:
        import crux.claude_autorun as autorun
        import crux.cli as cli
        spawned: list[list[str]] = []
        payload = {"tool_name": "Bash", "cwd": ".",
                   "tool_input": {"command": "git push"}}
        with mock.patch.object(cli, "_hook_scope",
                               return_value=(self._info("A", "feat"), Config())), \
             mock.patch.object(autorun, "_git_hook_covers_push",
                               return_value=False):
            autorun._handle(payload, lambda argv, log: spawned.append(argv))
        self.assertEqual(spawned, [["super", "refresh", "4", "--delay", "15"]])

    def test_an_unreadable_bundle_store_falls_back_to_the_card(self) -> None:
        # Silence is the wrong failure: a broken store must not skip the review.
        import crux.cli as cli
        with mock.patch.object(bundle_store, "find_by_branch",
                               side_effect=OSError("boom")):
            self.assertIsNone(cli._bundle_number(self._info("A", "feat"),
                                                 logging.getLogger("t")))

    def _number(self) -> int | None:
        import crux.cli as cli
        return cli._bundle_number(self._info("A", "feat"), logging.getLogger("t"))

    def test_a_closed_bundle_no_longer_claims_its_branches(self) -> None:
        found = bundle_store.load(4)
        found.closed = True
        bundle_store.save(found)
        self.assertIsNone(self._number())
        self.pr_state.assert_not_called()

    def test_a_merged_pr_leaves_the_bundle_and_is_remembered(self) -> None:
        # Merged in the GitHub UI: the store never heard. New commits on the
        # branch are new work, not a refresh of a bundle that already landed.
        self.pr_state.return_value = "merged"
        self.assertIsNone(self._number())
        self.assertEqual(bundle_store.load(4).members[0].state, "merged")
        self.pr_state.reset_mock()
        self.assertIsNone(self._number())
        self.pr_state.assert_not_called()   # remembered: no second read

    def test_a_closed_pr_leaves_the_bundle_without_being_marked_merged(self) -> None:
        self.pr_state.return_value = "closed"
        self.assertIsNone(self._number())
        self.assertEqual(bundle_store.load(4).members[0].state, "")

    def test_an_unreadable_pr_state_keeps_the_bundle(self) -> None:
        # A network blip must not quietly split a live bundle.
        self.pr_state.return_value = ""
        self.assertEqual(self._number(), 4)


class TestBriefButtons(unittest.TestCase):
    """D38: the brief's Merge/Close buttons, and the rule that you cannot land
    a bundle you wrote."""

    def _bundle(self) -> Bundle:
        return Bundle(number=3, name="feature-x", home="o/S", issue=41,
                      members=[BundleMember(owner="o", repo="A", branch="x", pr=1),
                               BundleMember(owner="o", repo="B", branch="x", pr=2)])

    def test_the_card_carries_loopback_links(self) -> None:
        # The same URL for everyone: 127.0.0.1 resolves on the clicker's box.
        lines = superrender.render_actions(self._bundle(), Config())
        text = "\n".join(lines)
        self.assertIn("http://127.0.0.1:8787/super/3/merge", text)
        self.assertIn("http://127.0.0.1:8787/super/3/close", text)

    def test_no_port_means_no_buttons(self) -> None:
        self.assertEqual(
            superrender.render_actions(self._bundle(), Config(serve_port=0)), [])

    def test_writing_all_of_it_means_no_button_at_all(self) -> None:
        import crux.superact as superact
        bundle = self._bundle()
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "author_login", return_value="pat"), \
             mock.patch.object(superact, "_approve") as approved, \
             mock.patch.object(superact.superpr, "merge") as merged:
            with self.assertRaises(superact.ActError) as caught:
                superact.merge(bundle, Config())
        approved.assert_not_called()   # nothing half-approved on refusal
        merged.assert_not_called()
        self.assertIn("not yours to merge", str(caught.exception))

    def test_writing_one_of_them_skips_that_one_loudly_and_goes_on(self) -> None:
        import crux.superact as superact
        bundle = self._bundle()
        logins = {1: "pat", 2: "sam"}
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "author_login",
                               side_effect=lambda m: logins[m.pr]), \
             mock.patch.object(superact, "_approve", return_value="") as approved, \
             mock.patch.object(superact.superpr, "merge",
                               return_value=(bundle.members, "1 of 2 landed")) as merged:
            _results, line, problems = superact.merge(bundle, Config())
        # Only the other person's PR is approved…
        self.assertEqual([c.args[0].pr for c in approved.call_args_list], [2])
        # …and the presser's own is named, blocked, and skipped by the merge.
        self.assertEqual(len(problems), 1)
        self.assertIn("o/A#1", problems[0])
        self.assertEqual(merged.call_args.kwargs["skip"], {"o/A#1"})
        self.assertEqual(bundle.members[0].state, "blocked")
        self.assertIn("you opened it", bundle.members[0].error)
        self.assertEqual(line, "1 of 2 landed")

    def test_a_skipped_member_still_appears_in_the_merge_run(self) -> None:
        # A report that quietly omitted one would read as "everything landed".
        import crux.supermerge as supermerge
        bundle = self._bundle()
        with mock.patch.object(supermerge, "_merge_one",
                               return_value=(True, "")) as merged_one:
            results = supermerge.run(bundle, skip={"o/A#1"})
        self.assertEqual(len(results), 2)
        self.assertEqual([c.args[1] for c in merged_one.call_args_list], [2])

    def test_an_admin_merge_approves_nothing_and_says_so(self) -> None:
        import crux.superact as superact
        bundle = self._bundle()
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "admin_on", return_value=True), \
             mock.patch.object(superact, "_approve") as approved, \
             mock.patch.object(superact.superpr, "merge",
                               return_value=(bundle.members, "all landed")) as merged:
            _results, line, _ = superact.admin_merge(bundle, Config())
        approved.assert_not_called()
        note = merged.call_args.kwargs["note"]
        self.assertIn("@pat", note)
        self.assertIn("admin merge", note)
        self.assertIn("no approval", note)
        self.assertEqual(line, "all landed")

    def test_an_admin_merge_needs_admin_on_every_repo(self) -> None:
        import crux.superact as superact
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "admin_on",
                               side_effect=lambda slug: slug == "o/A"), \
             mock.patch.object(superact.superpr, "merge") as merged:
            with self.assertRaises(superact.ActError) as caught:
                superact.admin_merge(self._bundle(), Config())
        merged.assert_not_called()
        self.assertIn("o/B", str(caught.exception))

    def test_the_override_is_written_where_the_merge_is_read(self) -> None:
        note = "⚠️ Landed by @pat with an **admin merge**"
        report = superrender.render_merge_report(
            self._bundle(), self._bundle().members, note=note)
        self.assertIn(note, report)

    def test_the_author_page_still_offers_the_admin_override(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "author_login", return_value="pat"):
            page = serve.merge_page(self._bundle(), Config()).decode()
        self.assertIn("not yours to merge", page)
        self.assertIn("admin-merge", page)

    def test_a_part_author_is_warned_but_gets_the_button(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        logins = {1: "pat", 2: "sam"}
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "author_login",
                               side_effect=lambda m: logins[m.pr]):
            page = serve.merge_page(self._bundle(), Config()).decode()
        self.assertIn("Approve and merge all", page)
        self.assertIn("o/A#1", page)
        self.assertIn("skipped and reported", page)

    def test_an_ordinary_pr_card_carries_the_same_button(self) -> None:
        import crux.render as render
        from crux.models import RepoInfo
        info = RepoInfo(root="/r", branch="x", head_sha="a" * 40,
                        base_sha="b" * 40, owner="o", repo="A")
        text = "\n".join(render.render_actions(info, 12, Config()))
        self.assertIn("http://127.0.0.1:8787/pr/o/A/12/merge", text)
        self.assertEqual(render.render_actions(info, None, Config()), [])
        self.assertEqual(render.render_actions(info, 12, Config(serve_port=0)), [])

    def test_you_cannot_approve_your_own_ordinary_pr_either(self) -> None:
        import crux.superact as superact
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "pr_author", return_value="pat"), \
             mock.patch.object(superact, "_approve") as approved:
            with self.assertRaises(superact.ActError):
                superact.merge_pr("o/A", 12)
        approved.assert_not_called()

    def test_someone_else_approves_every_pr_then_merges(self) -> None:
        import crux.superact as superact
        bundle = self._bundle()
        with mock.patch.object(superact, "actor_login", return_value="sam"), \
             mock.patch.object(superact, "author_login", return_value="pat"), \
             mock.patch.object(superact, "_approve", return_value="") as approved, \
             mock.patch.object(superact.superpr, "merge",
                               return_value=(bundle.members, "all landed")):
            results, line, problems = superact.merge(bundle, Config())
        self.assertEqual(approved.call_count, 2)
        self.assertEqual((line, problems), ("all landed", []))
        self.assertEqual(len(results), 2)

    def test_the_merge_page_shows_the_refusal_not_a_button(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        bundle = self._bundle()
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "author_login", return_value="pat"):
            page = serve.merge_page(bundle, Config()).decode()
        self.assertIn("not yours to merge", page)
        self.assertIn("Ask in Slack", page)
        self.assertNotIn("Approve and merge all", page)

    def test_the_merge_page_offers_the_button_to_anyone_else(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        with mock.patch.object(superact, "actor_login", return_value="sam"), \
             mock.patch.object(superact, "author_login", return_value="pat"):
            page = serve.merge_page(self._bundle(), Config()).decode()
        self.assertIn("Approve and merge all", page)
        self.assertIn("o/A#1", page)

    def test_the_close_page_separates_the_destructive_choice(self) -> None:
        import crux.serve as serve
        page = serve.close_page(self._bundle()).decode()
        self.assertIn("Close the brief", page)
        self.assertIn("Close the brief and all 2 PRs", page)
        self.assertIn("danger", page)

    def test_closing_the_brief_leaves_the_prs_alone(self) -> None:
        import crux.superact as superact
        bundle = self._bundle()
        with mock.patch.object(superact.superpost, "close_issue") as closed, \
             mock.patch.object(superact.post, "_run_gh") as gh, \
             mock.patch.object(superact.bundle_store, "save"):
            problems = superact.close(bundle, Config(), with_prs=False)
        closed.assert_called_once()
        gh.assert_not_called()
        self.assertEqual(problems, [])
        self.assertTrue(bundle.closed)

    def test_a_closed_bundle_releases_its_prs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bundle_store, "store_dir",
                                   return_value=Path(tmp)):
                b = self._bundle()
                bundle_store.save(b)
                self.assertEqual(len(bundle_store.bundled_prs()), 2)
                b.closed = True
                bundle_store.save(b)
                self.assertEqual(bundle_store.bundled_prs(), {})

    def test_a_bare_get_never_acts(self) -> None:
        # Link scanners and prefetchers fetch URLs out of issue bodies.
        import crux.serve as serve
        source = Path(serve.__file__).read_text(encoding="utf-8")
        get_body = source.split("def do_GET")[1].split("def do_POST")[0]
        for acting in ("do_merge", "do_close", "superact.merge", "superact.close"):
            self.assertNotIn(acting, get_body)

    def test_a_post_without_this_process_token_is_refused(self) -> None:
        import crux.serve as serve
        self.assertIn("_TOKEN", Path(serve.__file__).read_text(encoding="utf-8"))
        self.assertTrue(serve._TOKEN)

    def test_the_superhero_marks_the_card_and_the_slack_post(self) -> None:
        lines = superrender.render_actions(self._bundle(), Config())
        self.assertIn("🦸", "\n".join(lines))


class TestCheckoutForTesting(unittest.TestCase):
    """D38: "set up to test" — every member repo on its branch at once, which
    is what the verification steps assume and what nobody wants to do by hand
    across four directories."""

    def _bundle(self) -> Bundle:
        return Bundle(number=3, name="feature-x", home="o/S", issue=41,
                      test_steps=["start the bakery app", "watch the oven log"],
                      members=[BundleMember(owner="o", repo="A", branch="feat", pr=1),
                               BundleMember(owner="o", repo="B", branch="feat", pr=2)])

    def _member(self, repo: str = "A") -> BundleMember:
        return BundleMember(owner="o", repo=repo, branch="feat", pr=1)

    def test_a_clean_repo_is_fetched_switched_and_fast_forwarded(self) -> None:
        import crux.superact as superact
        calls: list[list[str]] = []

        def fake_git(args, cwd):
            calls.append(args)
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return "main"
            if args[0] == "status":
                return ""
            return ""

        with mock.patch.object(superact, "_git", side_effect=fake_git):
            got = superact._checkout_one(self._member(), "/clones/A")
        self.assertEqual(got.state, "ready")
        self.assertEqual(got.detail, "main → feat")
        self.assertIn(["fetch", "origin", "feat"], calls)
        self.assertIn(["checkout", "feat"], calls)
        self.assertIn(["merge", "--ff-only", "origin/feat"], calls)

    def test_uncommitted_work_is_never_touched(self) -> None:
        import crux.superact as superact
        calls: list[list[str]] = []

        def fake_git(args, cwd):
            calls.append(args)
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return "main"
            if args[0] == "status":
                return " M crux/cli.py"
            return ""

        with mock.patch.object(superact, "_git", side_effect=fake_git):
            got = superact._checkout_one(self._member(), "/clones/A")
        self.assertEqual(got.state, "skipped")
        self.assertIn("uncommitted changes", got.detail)
        self.assertNotIn(["checkout", "feat"], calls)   # nothing switched
        self.assertNotIn(["fetch", "origin", "feat"], calls)

    def test_dirty_but_already_on_the_branch_still_updates(self) -> None:
        # Editing the branch you are testing is normal; only a SWITCH would
        # put those edits somewhere the person did not expect.
        import crux.superact as superact

        def fake_git(args, cwd):
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return "feat"
            if args[0] == "status":
                return " M a.py"
            return ""

        with mock.patch.object(superact, "_git", side_effect=fake_git):
            got = superact._checkout_one(self._member(), "/clones/A")
        self.assertEqual(got.state, "ready")
        self.assertIn("already on feat", got.detail)

    def test_untracked_files_are_not_uncommitted_work(self) -> None:
        # A build directory or a scratch file is the normal state of a working
        # clone, and refusing the checkout over one skipped repos with nothing
        # at stake. Asked of a REAL repo, because the whole bug was which git
        # flags were passed — a fake `_git` would only test the fake.
        import crux.superact as superact
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "A"
            make_repo(repo)
            git(["checkout", "-qb", "feat"], str(repo))
            (repo / "build").mkdir()
            (repo / "build" / "out.o").write_text("x")
            (repo / "scratch.txt").write_text("x")
            git(["checkout", "-q", "main"], str(repo))
            calls: list[list[str]] = []

            def real_git(args, cwd):
                calls.append(args)
                if args[0] in ("fetch", "merge"):    # no remote in a temp repo
                    return ""
                return git_or_error(args, cwd)

            with mock.patch.object(superact, "_git", side_effect=real_git):
                got = superact._checkout_one(self._member(), str(repo))
        self.assertEqual((got.state, got.detail), ("ready", "main → feat"))
        self.assertIn(["checkout", "feat"], calls)

    def test_a_tracked_edit_still_stops_a_switch(self) -> None:
        # The guard itself is unchanged: unsaved edits are never switched under.
        import crux.superact as superact
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "A"
            make_repo(repo)
            (repo / "f.txt").write_text("edited\n")
            with mock.patch.object(superact, "_git", side_effect=git_or_error):
                got = superact._checkout_one(self._member(), str(repo))
        self.assertEqual(got.state, "skipped")
        self.assertIn("uncommitted changes", got.detail)

    def test_git_refusing_to_clobber_an_untracked_file_is_reported(self) -> None:
        # What `-uno` hands off to git: the one untracked file that DOES matter
        # is the one the switch would overwrite, and git sees it coming.
        import crux.superact as superact
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "A"
            make_repo(repo)
            git(["checkout", "-qb", "feat"], str(repo))
            (repo / "new.txt").write_text("on the branch\n")
            git(["add", "-A"], str(repo))
            git(["commit", "-qm", "add new.txt"], str(repo))
            git(["checkout", "-q", "main"], str(repo))
            (repo / "new.txt").write_text("untracked, would be lost\n")

            def real_git(args, cwd):
                if args[0] in ("fetch", "merge"):
                    return ""
                return git_or_error(args, cwd)

            with mock.patch.object(superact, "_git", side_effect=real_git):
                got = superact._checkout_one(self._member(), str(repo))
            self.assertEqual((repo / "new.txt").read_text(),
                             "untracked, would be lost\n")  # left as it was
        self.assertEqual(got.state, "failed")
        # Named for what happened — not "could not be fast-forwarded" to a
        # branch the repo never reached.
        self.assertIn("could not be switched to feat", got.detail)

    def test_a_missing_clone_is_cloned_first(self) -> None:
        # A teammate pressing this usually lacks repos they have never touched;
        # a list of repos to go and clone by hand is not "set up to test".
        import crux.superact as superact

        def fake_git(args, cwd):
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return "feat"
            return ""

        with mock.patch.object(superact, "_clone_missing",
                               return_value=("/src/A", "")) as cloned, \
             mock.patch.object(superact, "_git", side_effect=fake_git):
            got = superact._checkout_one(self._member(), "", root="/src")
        cloned.assert_called_once_with("o/A", "/src")
        self.assertEqual((got.state, got.path), ("ready", "/src/A"))

    def test_a_missing_clone_with_nowhere_to_put_it_is_reported(self) -> None:
        import crux.superact as superact
        got = superact._checkout_one(self._member(), "", root="")
        self.assertEqual(got.state, "failed")
        self.assertIn("no clone of this repo", got.detail)

    def test_a_failed_clone_is_reported_not_raised(self) -> None:
        import crux.superact as superact
        with mock.patch.object(superact.post, "_run_gh",
                               side_effect=CruxError("no such repo")), \
             mock.patch("os.path.exists", return_value=False):
            got = superact._checkout_one(self._member(), "", root="/src")
        self.assertEqual(got.state, "failed")
        self.assertIn("could not be cloned", got.detail)

    def test_one_broken_repo_does_not_stop_the_others(self) -> None:
        import crux.superact as superact
        paths = {"o/A": "/clones/A"}   # B has no clone here
        with mock.patch.object(superact.superpr, "_clone_paths",
                               return_value=(paths, {})), \
             mock.patch.object(superact, "_clone_missing",
                               return_value=("", "could not be cloned (nope)")), \
             mock.patch.object(superact, "_git", return_value=""):
            results = superact.checkout(self._bundle(), Config())
        self.assertEqual([r.slug for r in results], ["o/A", "o/B"])
        self.assertEqual(results[1].state, "failed")

    def test_the_result_page_shows_the_steps_you_set_up_for(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        bundle = self._bundle()
        with mock.patch.object(superact, "checkout", return_value=[
                superact.Checkout(slug="o/A", branch="feat", path="/clones/A",
                                  state="ready", detail="main → feat")]):
            page = serve.do_checkout(bundle, Config()).decode()
        self.assertIn("1 of 1 repos ready", page)
        self.assertIn("start the bakery app", page)
        self.assertIn("watch the oven log", page)

    def test_the_button_sits_on_the_verification_comment(self) -> None:
        # It is the first step of those instructions, not a third merge button.
        bundle = self._bundle()
        comment = superrender.render_test_comment(bundle, bundle.test_steps,
                                                  Config())
        self.assertIn("http://127.0.0.1:8787/super/3/checkout", comment)
        self.assertIn("Set up to test", comment)
        actions = "\n".join(superrender.render_actions(bundle, Config()))
        self.assertNotIn("/checkout", actions)

    def test_without_the_service_it_is_a_prerequisites_line(self) -> None:
        bundle = self._bundle()
        comment = superrender.render_test_comment(bundle, bundle.test_steps,
                                                  Config(serve_port=0))
        self.assertNotIn("127.0.0.1", comment)
        self.assertIn("Prerequisites", comment)

    def test_the_steps_are_kept_on_the_bundle_for_it(self) -> None:
        # Otherwise the page that just checked 4 repos out has to go back to
        # GitHub to find what the person was setting up for.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bundle_store, "store_dir",
                                   return_value=Path(tmp)):
                bundle_store.save(self._bundle())
                self.assertEqual(bundle_store.load(3).test_steps,
                                 ["start the bakery app", "watch the oven log"])


class TestMergeCommand(unittest.TestCase):
    """D38: `crux merge` — the terminal twin of the card's Merge button, on the
    same code beneath so the two doors cannot drift into two policies."""

    def _info(self) -> RepoInfo:
        return RepoInfo(root="/r", branch="feat", head_sha="a" * 40,
                        base_sha="b" * 40, owner="o", repo="A")

    def _run(self, **overrides):
        import crux.cli as cli
        args = argparse.Namespace(pr=0, method="squash", admin=False, yes=True)
        for key, value in overrides.items():
            setattr(args, key, value)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli._cmd_merge(args)
        return code, out.getvalue()

    def test_it_approves_and_merges_this_branch_s_pr(self) -> None:
        import crux.gitio as gitio
        import crux.post as post
        import crux.superact as superact
        with mock.patch.object(gitio, "repo_info", return_value=self._info()), \
             mock.patch.object(post, "find_pr", return_value=12), \
             mock.patch.object(bundle_store, "find_by_branch", return_value=None), \
             mock.patch.object(superact, "merge_pr",
                               return_value=(True, "approved and merged o/A#12")) as merged:
            code, out = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(merged.call_args[0][:2], ("o/A", 12))
        self.assertIn("approved and merged o/A#12", out)

    def test_a_member_of_a_super_pr_is_sent_to_the_bundle(self) -> None:
        # Landing one member alone is the half-applied state a bundle prevents.
        import crux.gitio as gitio
        import crux.post as post
        import crux.superact as superact
        found = Bundle(number=4, home="o/S",
                       members=[BundleMember(owner="o", repo="A", branch="feat",
                                             pr=12)])
        with mock.patch.object(gitio, "repo_info", return_value=self._info()), \
             mock.patch.object(post, "find_pr", return_value=12), \
             mock.patch.object(bundle_store, "find_by_branch", return_value=found), \
             mock.patch.object(superact, "merge_pr") as merged:
            code, out = self._run()
        self.assertEqual(code, 1)
        merged.assert_not_called()
        self.assertIn("crux super merge 4", out)

    def test_admin_takes_the_override_path(self) -> None:
        import crux.gitio as gitio
        import crux.post as post
        import crux.superact as superact
        with mock.patch.object(gitio, "repo_info", return_value=self._info()), \
             mock.patch.object(post, "find_pr", return_value=12), \
             mock.patch.object(bundle_store, "find_by_branch", return_value=None), \
             mock.patch.object(superact, "merge_pr") as normal, \
             mock.patch.object(superact, "admin_merge_pr",
                               return_value=(True, "merged o/A#12 on admin rights, "
                                                   "with no approval")) as override:
            code, out = self._run(admin=True)
        self.assertEqual(code, 0)
        normal.assert_not_called()
        override.assert_called_once()
        self.assertIn("no approval", out)

    def test_your_own_pr_is_refused_with_the_same_sentence(self) -> None:
        import crux.gitio as gitio
        import crux.post as post
        import crux.superact as superact
        with mock.patch.object(gitio, "repo_info", return_value=self._info()), \
             mock.patch.object(post, "find_pr", return_value=12), \
             mock.patch.object(bundle_store, "find_by_branch", return_value=None), \
             mock.patch.object(superact, "merge_pr",
                               side_effect=superact.ActError(
                                   "You opened o/A#12, so it is not yours to merge")):
            code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("not yours to merge", out)

    def test_no_pr_is_a_plain_message_not_a_traceback(self) -> None:
        import crux.gitio as gitio
        import crux.post as post
        with mock.patch.object(gitio, "repo_info", return_value=self._info()), \
             mock.patch.object(post, "find_pr", return_value=None):
            code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("no open PR for feat", out)


class TestConfirmDialogs(unittest.TestCase):
    """D38: the last thing between a mis-click and a merge. Every button that
    acts asks again in the browser; the ones that only read do not."""

    def _bundle(self) -> Bundle:
        return Bundle(number=3, name="feature-x", home="o/S", issue=41,
                      members=[BundleMember(owner="o", repo="A", branch="x", pr=1),
                               BundleMember(owner="o", repo="B", branch="x", pr=2)])

    def _forms(self, page: str) -> list[tuple[str, str]]:
        """(action, data-confirm) per form, in page order. The two Close forms
        share an action, so this cannot be a dict."""
        out: list[tuple[str, str]] = []
        for form in re.findall(r"<form[^>]*>", page):
            action = re.search(r"action='([^']+)'", form)
            ask = re.search(r'data-confirm="([^"]*)"', form)
            if action:
                out.append((action.group(1), ask.group(1) if ask else ""))
        return out

    def _confirm(self, forms: list[tuple[str, str]], action: str) -> str:
        return next(text for act, text in forms if act == action)

    def test_every_merge_and_close_button_asks_again(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        with mock.patch.object(superact, "actor_login", return_value="sam"), \
             mock.patch.object(superact, "author_login", return_value="pat"):
            merge = self._forms(serve.merge_page(self._bundle(), Config()).decode())
        close = self._forms(serve.close_page(self._bundle()).decode())
        self.assertIn("cannot be undone", self._confirm(merge, "/super/3/merge"))
        self.assertIn("NO approval recorded",
                      self._confirm(merge, "/super/3/admin-merge"))
        # Both Close buttons ask, and the destructive one says what it destroys.
        self.assertEqual([bool(text) for _a, text in close], [True, True])
        self.assertIn("possibly other people's",
                      "".join(t for _a, t in close).replace("&#x27;", "'"))

    def test_the_single_pr_buttons_ask_too(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        with mock.patch.object(superact, "actor_login", return_value="sam"), \
             mock.patch.object(superact, "pr_author", return_value="pat"):
            forms = self._forms(serve.pr_page("o/A", 12).decode())
        self.assertIn("o/A#12", self._confirm(forms, "/pr/o/A/12/merge"))
        self.assertIn("cannot be undone",
                      self._confirm(forms, "/pr/o/A/12/merge"))
        self.assertIn("NO approval",
                      self._confirm(forms, "/pr/o/A/12/admin-merge"))

    def test_reading_and_asking_never_interrupt_you(self) -> None:
        # Checking repos out is reversible and asking in Slack harms nothing;
        # a dialog on either would teach people to click through dialogs.
        import crux.serve as serve
        import crux.superact as superact
        checkout = self._forms(serve.checkout_page(self._bundle()).decode())
        self.assertEqual(self._confirm(checkout, "/super/3/checkout"), "")
        with mock.patch.object(superact, "actor_login", return_value="pat"), \
             mock.patch.object(superact, "author_login", return_value="pat"):
            refusal = self._forms(serve.merge_page(self._bundle(), Config()).decode())
        self.assertEqual(self._confirm(refusal, "/super/3/ask"), "")

    def test_the_dialog_text_survives_an_apostrophe(self) -> None:
        # Wired through a data attribute for exactly this: inline JS would have
        # been broken open by the quote.
        import crux.serve as serve
        form = serve._form("/x", "Go", confirm="don't do it")
        self.assertIn('data-confirm="don&#x27;t do it"', form)
        self.assertIn("data-confirm", serve._CONFIRM_JS)

    def test_a_page_without_javascript_still_works(self) -> None:
        # The dialog is a guard, never the mechanism: the form posts regardless.
        import crux.serve as serve
        form = serve._form("/super/3/merge", "Approve and merge all",
                           confirm="sure?")
        self.assertIn("method=post", form)
        self.assertIn("name=token", form)


class TestAutostart(unittest.TestCase):
    """D38: the buttons are printed into every card and brief, so a service
    nobody remembered to start is a page of dead links — and the failure lands
    on the reader, who did nothing wrong."""

    def test_it_starts_the_service_when_the_port_is_free(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(serve, "probe", return_value="free"), \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            state = serve.ensure_running(Config(), logging.getLogger("t"))
        self.assertEqual(state, "free")
        # The port is passed explicitly: a detached child resolves config from
        # ITS cwd, and a service on another port is the same dead link.
        self.assertEqual(spawned.call_args[0][0], ["serve", "--port", "8787"])

    def test_it_does_nothing_when_already_up(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(serve, "probe", return_value="crux"), \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            self.assertEqual(serve.ensure_running(Config(), logging.getLogger("t")),
                             "crux")
        spawned.assert_not_called()

    def test_it_never_fights_a_foreign_listener(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(serve, "probe", return_value="other"), \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            self.assertEqual(serve.ensure_running(Config(), logging.getLogger("t")),
                             "other")
        spawned.assert_not_called()

    def test_port_zero_starts_nothing(self) -> None:
        # No port means no buttons in the cards, so nothing to keep alive.
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(cli, "_spawn_detached") as spawned:
            self.assertEqual(
                serve.ensure_running(Config(serve_port=0), logging.getLogger("t")),
                "disabled")
        spawned.assert_not_called()

    def test_a_push_brings_it_up(self) -> None:
        import crux.cli as cli
        info = RepoInfo(root="/r", branch="solo", head_sha="a" * 40,
                        base_sha="b" * 40, owner="o", repo="A")
        env = {k: v for k, v in os.environ.items() if k != SUPER_ENV}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(cli, "_hook_scope", return_value=(info, Config())), \
             mock.patch.object(cli, "_cmd_ensure_enriched", return_value=0), \
             mock.patch.object(cli, "_cmd_ensure_pr"), \
             mock.patch.object(cli, "_bundle_number", return_value=None), \
             mock.patch.object(cli, "_spawn_detached"), \
             mock.patch.object(cli, "_ensure_serving") as ensured:
            cli._cmd_hook_prepush(mock.Mock())
        ensured.assert_called_once()

    def test_a_broken_probe_never_breaks_the_push(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(serve, "ensure_running",
                               side_effect=OSError("boom")):
            cli._ensure_serving(Config(), logging.getLogger("t"))  # must not raise

    def test_probe_tells_crux_apart_from_anything_else(self) -> None:
        import crux.serve as serve
        httpd, port = serve.start_background(Config(), 0)
        try:
            self.assertEqual(serve.probe(port), "crux")
        finally:
            httpd.shutdown()
        # Nothing listening there now.
        self.assertEqual(serve.probe(port), "free")


class TestRestart(unittest.TestCase):
    """D38: the service is the one process that outlives the command that
    started it, so it keeps running the code that was on disk when it started.
    `crux serve --restart` is how an edit to Crux's own source reaches it."""

    def test_the_port_says_which_pid_is_serving(self) -> None:
        # Stopping by pid is only possible because /health returns one.
        import crux.serve as serve
        httpd, port = serve.start_background(Config(), 0)
        try:
            self.assertEqual(serve.service_pid(port), os.getpid())
        finally:
            httpd.shutdown()
        self.assertIsNone(serve.service_pid(port))

    def test_it_signals_only_the_pid_the_port_reported(self) -> None:
        import crux.serve as serve
        with mock.patch.object(serve.os, "kill") as killed, \
             mock.patch.object(serve, "probe", side_effect=["crux", "free"]), \
             mock.patch.object(serve, "service_pid", return_value=4242):
            self.assertEqual(serve.stop(1234), "stopped")
        killed.assert_called_once_with(4242, signal.SIGTERM)

    def test_it_escalates_when_the_port_stays_held(self) -> None:
        # A wedged process still holding the port is exactly what a restart is
        # for, so it gets killed rather than reported as a puzzle.
        import crux.serve as serve
        with mock.patch.object(serve.os, "kill") as killed, \
             mock.patch.object(serve, "probe", return_value="crux"), \
             mock.patch.object(serve, "service_pid", return_value=4242):
            self.assertEqual(serve.stop(1234, timeout=0.2), "failed")
        self.assertEqual([c[0][1] for c in killed.call_args_list],
                         [signal.SIGTERM,
                          getattr(signal, "SIGKILL", signal.SIGTERM)])

    def test_an_older_service_that_names_no_pid_says_so(self) -> None:
        # The one-time upgrade case: it answers as crux, but /health predates
        # the pid, so there is nothing that is certainly it to signal.
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(serve.os, "kill") as killed, \
             mock.patch.object(serve, "probe", return_value="crux"), \
             mock.patch.object(serve, "service_pid", return_value=None):
            self.assertEqual(serve.stop(1234), "unknown")
        killed.assert_not_called()
        args = argparse.Namespace(port=8787, stop=False, restart=True)
        with mock.patch.object(serve, "restart", return_value="unknown"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli._cmd_serve(args), 1)
        self.assertIn("does not report its pid", out.getvalue())

    def test_it_signals_nothing_it_cannot_identify(self) -> None:
        import crux.serve as serve
        for state in ("free", "other"):
            with self.subTest(state=state), \
                 mock.patch.object(serve.os, "kill") as killed, \
                 mock.patch.object(serve, "probe", return_value=state):
                self.assertEqual(serve.stop(1234), state)
            killed.assert_not_called()

    def test_restart_leaves_a_detached_service_on_the_same_port(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(serve, "stop", return_value="stopped"), \
             mock.patch.object(serve, "probe", return_value="crux"), \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            self.assertEqual(serve.restart(Config(), 0, logging.getLogger("t")),
                             "restarted")
        self.assertEqual(spawned.call_args[0][0], ["serve", "--port", "8787"])

    def test_restart_starts_one_when_none_was_running(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(serve, "stop", return_value="free"), \
             mock.patch.object(serve, "probe", return_value="crux"), \
             mock.patch.object(cli, "_spawn_detached"):
            self.assertEqual(serve.restart(Config(), 0, logging.getLogger("t")),
                             "started")

    def test_restart_never_fights_a_foreign_listener(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        with mock.patch.object(serve, "stop", return_value="other"), \
             mock.patch.object(cli, "_spawn_detached") as spawned:
            self.assertEqual(serve.restart(Config(), 0, logging.getLogger("t")),
                             "other")
        spawned.assert_not_called()

    def test_stop_reaches_the_service_without_ever_serving(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        args = argparse.Namespace(port=9999, stop=True, restart=False)
        with mock.patch.object(serve, "stop", return_value="stopped") as stopped, \
             mock.patch.object(serve, "serve") as served, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli._cmd_serve(args), 0)
        stopped.assert_called_once_with(9999)
        served.assert_not_called()

    def test_restart_reaches_the_service_without_ever_serving(self) -> None:
        # The foreground serve() would block the command forever; --restart
        # must hand off to the detached one and return.
        import crux.cli as cli
        import crux.serve as serve
        args = argparse.Namespace(port=9999, stop=False, restart=True)
        with mock.patch.object(serve, "restart", return_value="restarted") as re_, \
             mock.patch.object(serve, "service_pid", return_value=7), \
             mock.patch.object(serve, "serve") as served, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli._cmd_serve(args), 0)
        self.assertEqual(re_.call_args[0][1], 9999)
        served.assert_not_called()
        self.assertIn("pid 7", out.getvalue())

    def test_a_foreign_listener_is_an_error_not_a_silent_no_op(self) -> None:
        import crux.cli as cli
        import crux.serve as serve
        args = argparse.Namespace(port=9999, stop=False, restart=True)
        with mock.patch.object(serve, "restart", return_value="other"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli._cmd_serve(args), 1)
        self.assertIn("not crux", out.getvalue())

    def test_the_parser_takes_both_flags(self) -> None:
        import crux.cli as cli
        parsed = cli._build_parser().parse_args(["serve", "--restart"])
        self.assertTrue(parsed.restart)
        self.assertFalse(parsed.stop)


class TestBriefCarriesItsOwnState(unittest.TestCase):
    """D38: bundles are MADE on one machine and READ on everyone else's. The
    only person guaranteed to hold the local file is the author — the one
    person forbidden to merge — so local-only state made every button work for
    exactly the wrong person."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            bundle_store, "store_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _bundle(self) -> Bundle:
        return Bundle(number=1, name="feature-x", home="o/S", issue=41,
                      order=["o/B#2", "o/A#1"],
                      test_steps=["start the bakery app"],
                      members=[BundleMember(owner="o", repo="A", branch="feat",
                                            pr=1, base="main"),
                               BundleMember(owner="o", repo="B", branch="feat",
                                            pr=2, base="dev")])

    def test_the_published_brief_carries_the_bundle(self) -> None:
        card = superrender.render_card(self._bundle(), SuperAnnotation(), [],
                                       Config())
        rebuilt = bundle_store.decode_state(card, "o/S")
        self.assertEqual(rebuilt.number, 1)
        self.assertEqual([(m.repo, m.branch, m.pr) for m in rebuilt.members],
                         [("A", "feat", 1), ("B", "feat", 2)])
        self.assertEqual(rebuilt.order, ["o/B#2", "o/A#1"])
        self.assertEqual(rebuilt.test_steps, ["start the bakery app"])

    def test_a_teammate_rebuilds_it_from_the_brief(self) -> None:
        import crux.superpost as superpost
        card = superrender.render_card(self._bundle(), SuperAnnotation(), [],
                                       Config())
        # Nothing in this machine's store — the friend who pressed the button.
        self.assertIsNone(bundle_store.load(1))
        with mock.patch.object(superpost, "find_brief", return_value=41), \
             mock.patch.object(superpost, "issue_body", return_value=card):
            found = bundle_store.hydrate(1, Config(super_home="o/S"))
        self.assertEqual(found.number, 1)
        self.assertEqual(found.issue, 41)
        self.assertEqual(len(found.members), 2)
        # …and it is theirs now: the lookup is paid once.
        self.assertIsNotNone(bundle_store.load(1))

    def test_every_button_hydrates_not_just_the_test_one(self) -> None:
        # Merge and Close were broken the same way and are fixed the same way.
        import crux.serve as serve
        handler = serve.Handler.__new__(serve.Handler)
        handler.cfg = Config(super_home="o/S")
        with mock.patch.object(bundle_store, "hydrate",
                               return_value=self._bundle()) as hydrated:
            got = handler._bundle(1)
        hydrated.assert_called_once_with(1, handler.cfg)
        self.assertEqual(got.number, 1)

    def test_a_reader_sees_a_member_added_after_they_last_pressed(self) -> None:
        # The bug this exists for: the friend pressed "set up to test" when the
        # bundle held 7, an 8th was added, and every later press still showed 7
        # — their first press had cached the brief, and nothing ever looked at
        # it again.
        import crux.superpost as superpost
        cached = self._bundle()
        cached.rev = 3
        bundle_store._mirror(cached)              # what their first press left

        grown = self._bundle()
        grown.rev = 4
        grown.members.append(BundleMember(owner="o", repo="C", branch="feat",
                                          pr=3, base="main"))
        card = superrender.render_card(grown, SuperAnnotation(), [], Config())

        with mock.patch.object(superpost, "find_brief", return_value=41), \
             mock.patch.object(superpost, "issue_body", return_value=card):
            found = bundle_store.hydrate(1, Config(super_home="o/S"))
        self.assertEqual([m.repo for m in found.members], ["A", "B", "C"])
        # …and the cache moved with it, so `crux super show` agrees.
        self.assertEqual([m.repo for m in bundle_store.load(1).members],
                         ["A", "B", "C"])

    def test_unpublished_work_here_is_not_reverted_by_the_brief(self) -> None:
        # Between a --no-brief edit and the next refresh this machine holds
        # membership the brief has not been told about. A "refresh" that first
        # undid its own change would be a trap.
        import crux.superpost as superpost
        ahead = self._bundle()
        ahead.members.append(BundleMember(owner="o", repo="C", branch="feat",
                                          pr=3, base="main"))
        bundle_store.save(ahead)                  # rev 1, not published yet
        published = self._bundle()                # rev 0, what the brief holds
        card = superrender.render_card(published, SuperAnnotation(), [], Config())

        with mock.patch.object(superpost, "find_brief", return_value=41), \
             mock.patch.object(superpost, "issue_body", return_value=card):
            found = bundle_store.hydrate(1, Config(super_home="o/S"))
        self.assertEqual([m.repo for m in found.members], ["A", "B", "C"])

    def test_equal_revisions_take_the_brief(self) -> None:
        # Including the one window that cannot be numbered: a bundle saved
        # before revisions existed, where both sides read 0.
        import crux.superpost as superpost
        legacy = self._bundle()
        bundle_store._mirror(legacy)              # rev 0, pre-upgrade cache
        grown = self._bundle()
        grown.members.append(BundleMember(owner="o", repo="C", branch="feat",
                                          pr=3, base="main"))
        card = superrender.render_card(grown, SuperAnnotation(), [], Config())

        with mock.patch.object(superpost, "find_brief", return_value=41), \
             mock.patch.object(superpost, "issue_body", return_value=card):
            found = bundle_store.hydrate(1, Config(super_home="o/S"))
        self.assertEqual([m.repo for m in found.members], ["A", "B", "C"])

    def test_the_slack_thread_survives_taking_the_brief(self) -> None:
        # The brief carries membership and nothing else by design. Replacing
        # the local copy wholesale would drop slack_ts and turn one bundle's
        # conversation into a fresh channel post per re-brief.
        import crux.superpost as superpost
        local = self._bundle()
        local.slack_channel, local.slack_ts = "C123", "1712.45"
        bundle_store._mirror(local)
        grown = self._bundle()
        grown.rev = 9
        card = superrender.render_card(grown, SuperAnnotation(), [], Config())

        with mock.patch.object(superpost, "find_brief", return_value=41), \
             mock.patch.object(superpost, "issue_body", return_value=card):
            found = bundle_store.hydrate(1, Config(super_home="o/S"))
        self.assertEqual((found.slack_channel, found.slack_ts), ("C123", "1712.45"))
        self.assertEqual(found.rev, 9)

    def test_an_unreachable_brief_falls_back_to_what_is_here(self) -> None:
        # Offline, or `gh` unauthenticated: the buttons still work on what this
        # machine knows rather than reporting the bundle missing.
        import crux.superpost as superpost
        bundle_store._mirror(self._bundle())
        with mock.patch.object(superpost, "find_brief",
                               side_effect=CruxError("no network")):
            found = bundle_store.hydrate(1, Config(super_home="o/S"))
        self.assertEqual(len(found.members), 2)

    def test_a_known_issue_number_is_not_searched_for_again(self) -> None:
        import crux.superpost as superpost
        bundle_store._mirror(self._bundle())      # already knows issue 41
        card = superrender.render_card(self._bundle(), SuperAnnotation(), [],
                                       Config())
        with mock.patch.object(superpost, "find_brief") as searched, \
             mock.patch.object(superpost, "issue_body",
                               return_value=card) as read:
            bundle_store.hydrate(1, Config(super_home="o/S"))
        searched.assert_not_called()
        self.assertEqual(read.call_args[0][1], 41)

    def test_a_save_counts_as_a_revision_and_a_cache_write_does_not(self) -> None:
        # A cache that out-numbered the brief it came from would pin itself in
        # place forever — which is exactly the bug, one layer down.
        bundle = self._bundle()
        bundle_store.save(bundle)
        bundle_store.save(bundle)
        self.assertEqual(bundle.rev, 2)
        bundle_store._mirror(bundle)
        self.assertEqual(bundle.rev, 2)
        self.assertEqual(bundle_store.load(1).rev, 2)

    def test_the_brief_carries_the_revision(self) -> None:
        bundle = self._bundle()
        bundle.rev = 7
        rebuilt = bundle_store.decode_state(
            bundle_store.encode_state(bundle), "o/S")
        self.assertEqual(rebuilt.rev, 7)

    def test_the_state_block_survives_an_arrow_in_a_branch_name(self) -> None:
        # An unescaped `-->` would close the comment early and take the rest of
        # the brief with it.
        bundle = self._bundle()
        bundle.members[0].branch = "spike/a-->b"
        block = bundle_store.encode_state(bundle)
        self.assertEqual(block.count("-->"), 1)
        rebuilt = bundle_store.decode_state(block, "o/S")
        self.assertEqual(rebuilt.members[0].branch, "spike/a-->b")

    def test_the_home_repo_is_never_taken_from_the_payload(self) -> None:
        # A doctored block must not point Crux's next write somewhere else.
        block = bundle_store.encode_state(self._bundle()).replace(
            '"number":1', '"home":"attacker/evil","number":1')
        rebuilt = bundle_store.decode_state(block, "o/S")
        self.assertEqual(rebuilt.home, "o/S")

    def test_a_brief_without_a_block_is_simply_not_found(self) -> None:
        import crux.superpost as superpost
        with mock.patch.object(superpost, "find_brief", return_value=41), \
             mock.patch.object(superpost, "issue_body", return_value="## old brief"):
            self.assertIsNone(bundle_store.hydrate(1, Config(super_home="o/S")))

    def test_no_home_configured_is_not_a_crash(self) -> None:
        self.assertIsNone(bundle_store.hydrate(1, Config(super_home="")))

    def test_the_block_is_appended_after_the_length_cap(self) -> None:
        # Truncating a huge brief must never cost a teammate the bundle.
        bundle = self._bundle()
        ann = SuperAnnotation(thesis="x" * 400, ideas=["y" * 400] * 5)
        card = superrender.render_card(bundle, ann, [], Config(comment_char_cap=200))
        self.assertIn("_(truncated)_", card)
        self.assertIsNotNone(bundle_store.decode_state(card, "o/S"))


class TestAddToAnExistingSuperPR(unittest.TestCase):
    """D37: a cross-repo change does not always arrive knowing its own extent.
    The repo nobody expected to touch turns up on day three, and rebuilding the
    super PR to include it would cost its number, its brief and the Slack
    thread every refresh has been replying to."""

    def _cand(self, repo: str, pr: int | None = None) -> Candidate:
        return Candidate(owner="o", repo=repo, branch="feat", pr=pr,
                         path=f"/clones/{repo}")

    def _bundle(self, **kw) -> Bundle:
        return Bundle(number=7, name="feature-x", home="o/S", issue=41,
                      members=[BundleMember(owner="o", repo="A",
                                            branch="feat", pr=1)], **kw)

    def test_a_member_joins_and_the_number_is_kept(self) -> None:
        bundle = self._bundle()
        with mock.patch.object(superpr.bundle_store, "save") as saved:
            got, problems = superpr.add(Config(super_home="o/S"), bundle,
                                        [self._cand("B", pr=4)])
        self.assertEqual(problems, [])
        self.assertEqual(got.number, 7)                 # the link still works
        self.assertEqual([(m.repo, m.pr) for m in got.members],
                         [("A", 1), ("B", 4)])
        saved.assert_called_once()

    def test_an_existing_member_is_reported_not_duplicated(self) -> None:
        bundle = self._bundle()
        with mock.patch.object(superpr.bundle_store, "save"):
            got, problems = superpr.add(Config(super_home="o/S"), bundle,
                                        [self._cand("A", pr=1),
                                         self._cand("B", pr=4)])
        self.assertEqual([(m.repo, m.pr) for m in got.members],
                         [("A", 1), ("B", 4)])
        self.assertIn("already in super PR #7", problems[0])

    def test_adding_only_what_is_already_there_changes_nothing(self) -> None:
        bundle = self._bundle()
        with mock.patch.object(superpr.bundle_store, "save") as saved, \
             self.assertRaises(superpr.SuperError) as caught:
            superpr.add(Config(super_home="o/S"), bundle,
                        [self._cand("A", pr=1)])
        self.assertIn("nothing was added", str(caught.exception))
        saved.assert_not_called()
        self.assertEqual(len(bundle.members), 1)

    def test_a_closed_bundle_refuses_rather_than_reopening_itself(self) -> None:
        # Closing released its PRs back to the picker; growing it again would
        # quietly take them back.
        with mock.patch.object(superpr.bundle_store, "save") as saved, \
             self.assertRaises(superpr.SuperError) as caught:
            superpr.add(Config(super_home="o/S"), self._bundle(closed=True),
                        [self._cand("B", pr=4)])
        self.assertIn("is closed", str(caught.exception))
        saved.assert_not_called()

    def test_a_branch_without_a_pr_gets_one_opened_into_the_chosen_base(self) -> None:
        # Exactly what `new` does for the same pick: the two build members
        # through one function so a late arrival is not a second-class member.
        bundle = self._bundle()
        with mock.patch.object(superpr.gitio, "run_git", return_value=""), \
             mock.patch.object(superpr.gitio, "repo_info",
                               return_value=RepoInfo(
                                   root="/clones/B", branch="feat",
                                   head_sha="a" * 40, base_sha="b" * 40,
                                   owner="o", repo="B")), \
             mock.patch.object(superpr.post, "_create_pr",
                               return_value=9) as created, \
             mock.patch.object(superpr.bundle_store, "save"):
            got, problems = superpr.add(Config(super_home="o/S"), bundle,
                                        [self._cand("B")], base="release-2")
        self.assertEqual(problems, [])
        self.assertEqual(created.call_args[0][1], "release-2")
        self.assertEqual((got.members[-1].pr, got.members[-1].base),
                         (9, "release-2"))

    def test_the_membership_is_saved_before_the_brief_is_rebuilt(self) -> None:
        # A re-brief that fails (no network, a repo that will not diff) must
        # not lose the addition — `crux super refresh` then finishes the job.
        bundle = self._bundle()
        with mock.patch.object(superpr.bundle_store, "save") as saved:
            superpr.add(Config(super_home="o/S"), bundle,
                        [self._cand("B", pr=4)])
        stored = saved.call_args[0][0]
        self.assertEqual(len(stored.members), 2)

    def test_the_picker_never_offers_a_pr_another_bundle_holds(self) -> None:
        # `add` leans on this: gather() drops every PR an open bundle holds,
        # including the target's own, so the list IS the addable set.
        import crux.candidates as candidates
        rows = {"o/A": [{"headRefName": "feat", "number": 1, "title": "t"}],
                "o/B": [{"headRefName": "feat", "number": 4, "title": "t"}]}
        with mock.patch.object(candidates, "scope_repos",
                               return_value=(["o/A", "o/B"], [])), \
             mock.patch.object(candidates.clones_mod, "find_clones",
                               return_value={}), \
             mock.patch.object(candidates.prs, "fetch_open_prs",
                               return_value=rows), \
             mock.patch.object(candidates.bundle, "bundled_prs",
                               return_value={bundle_store.key("o", "A", 1): 7}):
            cands, _problems = candidates.gather(Config())
        self.assertEqual([(c.repo, c.pr) for c in cands], [("B", 4)])


class TestSuperAddCommand(unittest.TestCase):
    """The CLI verb: `crux super add <n> [picks]`, which re-briefs by falling
    through to refresh exactly as `super new` does."""

    def _args(self, **kw) -> argparse.Namespace:
        base = dict(saction="add", number=7, pick=["1"], base="", yes=True,
                    no_brief=True, no_llm=False, dry_run=False, delay=0)
        base.update(kw)
        return argparse.Namespace(**base)

    def _bundle(self) -> Bundle:
        return Bundle(number=7, name="feature-x", home="o/S", issue=41,
                      members=[BundleMember(owner="o", repo="A",
                                            branch="feat", pr=1)])

    def _run(self, args, cands, added=None):
        import crux.cli as cli
        import crux.candidates as candidates
        import crux.superpr as superpr_
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(cli, "_cmd_serve"), \
             mock.patch.object(candidates, "gather", return_value=(cands, [])), \
             mock.patch.object(bundle_store, "hydrate",
                               return_value=self._bundle()), \
             mock.patch.object(superpr_, "add",
                               return_value=(added or self._bundle(), [])) as add_:
            code = cli._cmd_super(args)
        return code, out.getvalue(), add_

    def test_picked_numbers_reach_add(self) -> None:
        cands = [Candidate(owner="o", repo="B", branch="feat", pr=4)]
        code, text, add_ = self._run(self._args(), cands)
        self.assertEqual(code, 0)
        self.assertEqual([c.repo for c in add_.call_args[0][2]], ["B"])
        self.assertIn("now holds", text)
        self.assertIn("run `crux super refresh 7`", text)   # --no-brief

    def test_an_unknown_super_pr_is_reported(self) -> None:
        import crux.cli as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(bundle_store, "hydrate", return_value=None):
            code = cli._cmd_super(self._args(number=99))
        self.assertEqual(code, 1)
        self.assertIn("no super PR #99", out.getvalue())

    def test_an_empty_picker_says_everything_is_already_bundled(self) -> None:
        # The likeliest reason the list is empty here, and the one a bare
        # "nothing to pick" would leave the user guessing about.
        code, text, _add = self._run(self._args(), [])
        self.assertEqual(code, 1)
        self.assertIn("already bundled", text)

    def test_a_blank_selection_adds_nothing(self) -> None:
        cands = [Candidate(owner="o", repo="B", branch="feat", pr=4)]
        with mock.patch("builtins.input", return_value=""):
            code, text, add_ = self._run(self._args(pick=[]), cands)
        self.assertEqual(code, 1)
        add_.assert_not_called()

    def test_the_parser_takes_a_number_and_picks(self) -> None:
        import crux.cli as cli
        parsed = cli._build_parser().parse_args(["super", "add", "7", "1", "3-5"])
        self.assertEqual((parsed.saction, parsed.number, parsed.pick),
                         ("add", 7, ["1", "3-5"]))


class TestRemoveFromASuperPR(unittest.TestCase):
    """D37: detaching a PR from a bundle. Membership only — the pull request
    is untouched on GitHub, and goes back to being an ordinary PR."""

    def _bundle(self, states=("", ""), **kw) -> Bundle:
        return Bundle(
            number=7, name="feature-x", home="o/S", issue=41,
            order=["o/A#1", "o/B#4"],
            members=[BundleMember(owner="o", repo="A", branch="feat", pr=1,
                                  state=states[0]),
                     BundleMember(owner="o", repo="B", branch="feat", pr=4,
                                  state=states[1])], **kw)

    def test_a_member_is_detached_and_the_rest_stay(self) -> None:
        bundle = self._bundle()
        with mock.patch.object(superpr.bundle_store, "save") as saved:
            got, problems = superpr.remove(bundle, [bundle.members[1]])
        self.assertEqual(problems, [])
        self.assertEqual([(m.repo, m.pr) for m in got.members], [("A", 1)])
        self.assertEqual(got.number, 7)
        saved.assert_called_once()

    def test_the_pull_request_itself_is_never_touched(self) -> None:
        # No close, no comment, no retarget: removal is bundle state only.
        bundle = self._bundle()
        with mock.patch.object(superpr.bundle_store, "save"), \
             mock.patch.object(superpr.post, "_run_gh") as gh:
            superpr.remove(bundle, [bundle.members[1]])
        gh.assert_not_called()

    def test_a_detached_pr_is_selectable_again(self) -> None:
        # Nothing has to announce this: bundled_prs reads the bundles, so a PR
        # stops being taken the moment it stops being a member.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(bundle_store, "store_dir",
                                   return_value=Path(tmp)):
                bundle = self._bundle()
                bundle_store.save(bundle)
                self.assertIn(bundle_store.key("o", "B", 4),
                              bundle_store.bundled_prs())
                superpr.remove(bundle, [bundle.members[1]])
                self.assertNotIn(bundle_store.key("o", "B", 4),
                                 bundle_store.bundled_prs())

    def test_a_merged_member_stays_and_says_why(self) -> None:
        # The brief is the record of what landed as one change; dropping a
        # piece of it would leave the record describing something else.
        bundle = self._bundle(states=("merged", ""))
        with mock.patch.object(superpr.bundle_store, "save"):
            got, problems = superpr.remove(
                bundle, [bundle.members[0], bundle.members[1]])
        self.assertEqual([(m.repo, m.pr) for m in got.members], [("A", 1)])
        self.assertIn("already merged", problems[0])

    def test_removing_only_a_merged_member_removes_nothing(self) -> None:
        bundle = self._bundle(states=("merged", ""))
        with mock.patch.object(superpr.bundle_store, "save") as saved, \
             self.assertRaises(superpr.SuperError) as caught:
            superpr.remove(bundle, [bundle.members[0]])
        self.assertIn("nothing was removed", str(caught.exception))
        saved.assert_not_called()
        self.assertEqual(len(bundle.members), 2)

    def test_emptying_a_bundle_points_at_close_instead(self) -> None:
        bundle = self._bundle()
        with mock.patch.object(superpr.bundle_store, "save") as saved, \
             self.assertRaises(superpr.SuperError) as caught:
            superpr.remove(bundle, list(bundle.members))
        self.assertIn("crux super close 7", str(caught.exception))
        saved.assert_not_called()
        self.assertEqual(len(bundle.members), 2)   # left exactly as it was

    def test_a_closed_bundle_has_nothing_to_detach(self) -> None:
        bundle = self._bundle(closed=True)
        with self.assertRaises(superpr.SuperError) as caught:
            superpr.remove(bundle, [bundle.members[1]])
        self.assertIn("is closed", str(caught.exception))

    def test_the_merge_order_drops_the_member_too(self) -> None:
        # --no-brief leaves the stored order in place; a dangling ref would
        # still be printed by `crux super show`.
        bundle = self._bundle()
        with mock.patch.object(superpr.bundle_store, "save"):
            got, _ = superpr.remove(bundle, [bundle.members[1]])
        self.assertEqual(got.order, ["o/A#1"])

    def test_the_picker_numbers_members_in_stored_order(self) -> None:
        # NOT merge order: the number typed has to mean what it meant when
        # read, and order is recomputed by every refresh.
        bundle = self._bundle()
        bundle.order = ["o/B#4", "o/A#1"]
        listing = superpr.render_members(bundle)
        self.assertIn("1.    o/A#1", listing)
        self.assertIn("2.    o/B#4", listing)


class TestSuperRemoveCommand(unittest.TestCase):
    """The CLI verb: `crux super remove <n> [picks]`."""

    def _args(self, **kw) -> argparse.Namespace:
        base = dict(saction="remove", number=7, pick=["2"], no_brief=True,
                    no_llm=False, dry_run=False, delay=0)
        base.update(kw)
        return argparse.Namespace(**base)

    def _bundle(self) -> Bundle:
        return Bundle(number=7, name="feature-x", home="o/S", issue=41,
                      members=[BundleMember(owner="o", repo="A", branch="feat",
                                            pr=1),
                               BundleMember(owner="o", repo="B", branch="feat",
                                            pr=4)])

    def _run(self, args):
        import crux.cli as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(bundle_store, "hydrate",
                               return_value=self._bundle()), \
             mock.patch.object(superpr.bundle_store, "save"):
            code = cli._cmd_super(args)
        return code, out.getvalue()

    def test_a_picked_number_detaches_that_member(self) -> None:
        code, text = self._run(self._args())
        self.assertEqual(code, 0)
        self.assertIn("o/B#4 detached", text)
        self.assertIn("the pull request itself is untouched", text)
        self.assertIn("now holds 1 pull request", text)
        self.assertNotIn("1 pull requests", text)

    def test_an_unknown_super_pr_is_reported(self) -> None:
        import crux.cli as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(bundle_store, "hydrate", return_value=None):
            code = cli._cmd_super(self._args(number=99))
        self.assertEqual(code, 1)
        self.assertIn("no super PR #99", out.getvalue())

    def test_a_blank_selection_detaches_nothing(self) -> None:
        import crux.cli as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(bundle_store, "hydrate",
                               return_value=self._bundle()), \
             mock.patch.object(superpr, "remove",
                               side_effect=AssertionError("removed")):
            code = cli._cmd_super(self._args(pick=[]))
        self.assertEqual(code, 1)
        self.assertIn("nothing selected", out.getvalue())

    def test_the_interactive_list_is_shown_when_no_numbers_are_given(self) -> None:
        import crux.cli as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch("builtins.input", return_value="1"), \
             mock.patch.object(bundle_store, "hydrate",
                               return_value=self._bundle()), \
             mock.patch.object(superpr.bundle_store, "save"):
            code = cli._cmd_super(self._args(pick=[]))
        self.assertEqual(code, 0)
        self.assertIn("o/A#1", out.getvalue())
        self.assertIn("o/B#4", out.getvalue())

    def test_the_parser_takes_a_number_and_picks(self) -> None:
        import crux.cli as cli
        parsed = cli._build_parser().parse_args(
            ["super", "remove", "7", "1", "3-5"])
        self.assertEqual((parsed.saction, parsed.number, parsed.pick),
                         ("remove", 7, ["1", "3-5"]))


class TestCloneRoot(unittest.TestCase):
    """D38: where a missing member repo lands. `[super] roots` IS the answer
    when it is set — nothing is built underneath it but the repo's own name."""

    def test_roots_is_used_verbatim(self) -> None:
        import crux.superact as superact
        self.assertEqual(
            superact.clone_root(Config(super_roots=["/src/acme"])),
            "/src/acme")

    def test_it_never_clones_into_the_repo_it_is_run_from(self) -> None:
        # The buttons' service starts wherever the user happened to be, usually
        # inside a repo. Cloning there buries a bundle's other repos INSIDE one
        # of its members.
        import crux.gitio as gitio
        import crux.superact as superact
        with mock.patch.object(gitio, "run_git",
                               return_value="/src/acme/Crux"):
            got = superact.clone_root(Config())
        self.assertEqual(got, "/src/acme")

    def test_the_first_configured_root_wins_over_the_cwd(self) -> None:
        import crux.gitio as gitio
        import crux.superact as superact
        with mock.patch.object(gitio, "run_git") as git:
            got = superact.clone_root(Config(super_roots=["~/elsewhere"]),
                                      "/src/acme/Crux")
        git.assert_not_called()
        self.assertTrue(got.endswith("/elsewhere"))
        self.assertNotIn("~", got)          # expanded, ready to join onto

    def test_a_repo_gets_its_own_directory_under_the_root(self) -> None:
        import crux.superact as superact
        with mock.patch.object(superact.post, "_run_gh") as gh, \
             mock.patch("os.path.exists", return_value=False), \
             mock.patch("os.makedirs"):
            path, error = superact._clone_missing("o/Widget", "/src/acme")
        self.assertEqual((path, error), ("/src/acme/Widget", ""))
        self.assertEqual(gh.call_args[0][0],
                         ["repo", "clone", "o/Widget", "/src/acme/Widget"])


class TestClonesAreFoundWhereTheyAreCloned(unittest.TestCase):
    """D38: "set up to test" reported every already-checked-out repo as both
    missing AND in the way — `0 of 7 repos ready`, each one "already exists but
    is not a clone of" itself. Discovery searched INSIDE the repo the service
    was started in (`_walk` stops at the first `.git`, so it found one repo:
    that one), while cloning targeted its parent, where the clones actually
    were. The two must resolve the same directory."""

    def _tree(self, tmp: str) -> Path:
        """`sibling clones under one org directory` — the layout roots is for."""
        org = Path(tmp) / "example-org"
        for name in ("Crux", "web"):
            make_repo(org / name)
            git(["remote", "add", "origin",
                 f"https://github.com/example-org/{name}.git"], str(org / name))
        return org

    def test_discovery_climbs_out_of_the_repo_the_service_runs_in(self) -> None:
        import crux.clones as clones
        with tempfile.TemporaryDirectory() as tmp:
            org = self._tree(tmp)
            with mock.patch("os.getcwd", return_value=str(org / "Crux")):
                roots = clones.search_roots(Config())
                found = clones.find_clones(roots, ["example-org/web"])
        self.assertEqual(Path(roots[0]).resolve(), org.resolve())
        self.assertIn("example-org/web", found)

    def test_where_clones_are_sought_is_where_a_missing_one_goes(self) -> None:
        # The divergence itself, asserted directly: one answer, two callers.
        import crux.clones as clones
        import crux.superact as superact
        with tempfile.TemporaryDirectory() as tmp:
            org = self._tree(tmp)
            with mock.patch("os.getcwd", return_value=str(org / "Crux")):
                self.assertEqual(clones.search_roots(Config()),
                                 [superact.clone_root(Config())])

    def test_a_clone_already_there_is_used_not_blamed(self) -> None:
        # Belt and braces for the same failure: even if discovery misses it,
        # the directory is asked what it is before being called an obstacle.
        import crux.superact as superact
        with tempfile.TemporaryDirectory() as tmp:
            org = self._tree(tmp)
            with mock.patch.object(superact.post, "_run_gh") as gh:
                path, error = superact._clone_missing("example-org/web", str(org))
        self.assertEqual((path, error), (str(org / "web"), ""))
        gh.assert_not_called()          # nothing to clone, it is already here

    def test_a_directory_of_something_else_is_still_reported(self) -> None:
        import crux.superact as superact
        with tempfile.TemporaryDirectory() as tmp:
            org = self._tree(tmp)
            # A directory in the way that is genuinely NOT what was asked for:
            # the right name over the wrong repo, and one that is no repo.
            make_repo(org / "api")
            git(["remote", "add", "origin",
                 "https://github.com/example-org/web.git"],
                str(org / "api"))
            (org / "Empty").mkdir()
            with mock.patch.object(superact.post, "_run_gh") as gh:
                _, wrong = superact._clone_missing("example-org/api",
                                                   str(org))
                _, plain = superact._clone_missing("example-org/Empty", str(org))
        self.assertIn("is a clone of example-org/web", wrong)
        self.assertIn("is not a git clone", plain)
        gh.assert_not_called()

    def test_a_detached_clone_is_still_that_repos_clone(self) -> None:
        # _read_clone rejects detached HEAD (no branch to nominate as a
        # candidate); "is this the repo I wanted?" is the narrower question.
        import crux.clones as clones
        with tempfile.TemporaryDirectory() as tmp:
            org = self._tree(tmp)
            git(["checkout", "-q", "--detach", "HEAD"], str(org / "web"))
            self.assertEqual(clones.clone_slug(str(org / "web")),
                             "example-org/web")
            self.assertIsNone(clones._read_clone(str(org / "web")))


# ---------------------------------------------------------------------------
# D41: a human-decided landing order and merge method
# ---------------------------------------------------------------------------

def _three(**kw) -> Bundle:
    """o/A#1, o/B#2, o/C#3 — enough members for an order to mean something."""
    return Bundle(number=7, name="feature-x", home="o/S", issue=41, members=[
        BundleMember(owner="o", repo=r, branch="feat", pr=n)
        for r, n in (("A", 1), ("B", 2), ("C", 3))], **kw)


def _order_line(card: str) -> str:
    """The arrow line under the "Landing order" heading — the plan itself,
    as distinct from any other place a ref happens to be mentioned."""
    lines = card.splitlines()
    head = next(i for i, line in enumerate(lines)
                if line.startswith("### Landing order"))
    return lines[head + 1]


class _TmpStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            bundle_store, "store_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


class TestPinningAnOrder(_TmpStore):
    """`set_landing` validates a whole order, or changes nothing."""

    def test_full_and_short_refs_both_name_a_member(self) -> None:
        self.assertEqual(superpr.resolve_refs(_three(), ["o/C#3", "b#2", "A#1"]),
                         ["o/C#3", "o/B#2", "o/A#1"])

    def test_an_unknown_ref_is_refused_naming_the_members(self) -> None:
        with self.assertRaises(superpr.SuperError) as caught:
            superpr.resolve_refs(_three(), ["o/C#3", "o/B#2", "o/A#1", "o/X#9"])
        self.assertIn("o/X#9 is not in super PR #7", str(caught.exception))
        self.assertIn("members: o/A#1, o/B#2, o/C#3", str(caught.exception))

    def test_a_prefix_is_refused_with_the_whole_command_to_paste(self) -> None:
        # A pin is a decision about the whole sequence; a trailing remainder
        # nobody chose must not be printed as "pinned".
        with self.assertRaises(superpr.SuperError) as caught:
            superpr.resolve_refs(_three(), ["o/B#2"])
        text = str(caught.exception)
        self.assertIn("missing o/A#1, o/C#3", text)
        self.assertIn("crux super order 7 o/B#2 o/A#1 o/C#3", text)

    def test_a_member_listed_twice_is_refused(self) -> None:
        with self.assertRaises(superpr.SuperError) as caught:
            superpr.resolve_refs(_three(), ["o/A#1", "A#1", "o/B#2", "o/C#3"])
        self.assertIn("o/A#1 is listed twice", str(caught.exception))

    def test_a_short_ref_two_owners_share_asks_for_the_owner(self) -> None:
        bundle = Bundle(number=7, home="o/S", members=[
            BundleMember(owner="x", repo="A", branch="f", pr=1),
            BundleMember(owner="y", repo="A", branch="f", pr=1)])
        with self.assertRaises(superpr.SuperError) as caught:
            superpr.resolve_refs(bundle, ["A#1", "y/A#1"])
        self.assertIn("ambiguous", str(caught.exception))
        self.assertEqual(superpr.resolve_refs(bundle, ["y/A#1", "x/A#1"]),
                         ["y/A#1", "x/A#1"])

    def test_a_merged_member_may_be_left_out_and_goes_first(self) -> None:
        bundle = _three()
        bundle.members[1].state = "merged"
        self.assertEqual(superpr.resolve_refs(bundle, ["o/C#3", "o/A#1"]),
                         ["o/B#2", "o/C#3", "o/A#1"])

    def test_pinning_saves_the_order_and_the_method(self) -> None:
        bundle = _three()
        superpr.set_landing(bundle, refs=["o/C#3", "o/A#1", "o/B#2"],
                            method="merge")
        stored = bundle_store.load(7)
        self.assertEqual(stored.order, ["o/C#3", "o/A#1", "o/B#2"])
        self.assertTrue(stored.order_pinned)
        self.assertEqual(stored.merge_method, "merge")
        self.assertEqual(stored.rev, 1)          # a change, so a revision

    def test_a_bad_order_changes_nothing_at_all(self) -> None:
        # Validated before anything moves: the method must not stick when the
        # order it came with was refused.
        bundle = _three(order=["o/A#1", "o/B#2", "o/C#3"])
        with self.assertRaises(superpr.SuperError):
            superpr.set_landing(bundle, refs=["o/C#3"], method="merge")
        self.assertEqual((bundle.order_pinned, bundle.merge_method), (False, ""))
        self.assertIsNone(bundle_store.load(7))

    def test_unpinning_hands_back_the_review_pass_order(self) -> None:
        bundle = _three(order=["o/C#3", "o/A#1", "o/B#2"], order_pinned=True,
                        suggested_order=["o/A#1", "o/B#2", "o/C#3"])
        superpr.set_landing(bundle, unpin=True)
        self.assertFalse(bundle.order_pinned)
        self.assertEqual(bundle.order, ["o/A#1", "o/B#2", "o/C#3"])

    def test_default_drops_the_bundle_s_own_method(self) -> None:
        bundle = _three(merge_method="rebase")
        superpr.set_landing(bundle, method="default")
        self.assertEqual(bundle.merge_method, "")

    def test_a_method_alone_leaves_the_order_as_it_was(self) -> None:
        bundle = _three(order=["o/B#2", "o/A#1", "o/C#3"])
        superpr.set_landing(bundle, method="merge")
        self.assertEqual((bundle.order, bundle.order_pinned),
                         (["o/B#2", "o/A#1", "o/C#3"], False))

    def test_pinning_and_unpinning_at_once_is_refused(self) -> None:
        with self.assertRaises(superpr.SuperError):
            superpr.set_landing(_three(), refs=["o/A#1", "o/B#2", "o/C#3"],
                                unpin=True)

    def test_a_closed_bundle_has_nothing_left_to_order(self) -> None:
        with self.assertRaises(superpr.SuperError) as caught:
            superpr.set_landing(_three(closed=True), method="merge")
        self.assertIn("is closed", str(caught.exception))


class TestThePinSurvivesARebrief(_TmpStore):
    """The bug D41 exists for: every refresh — including the automatic one a
    member push triggers — replaced the order with the model's."""

    def _refresh(self, bundle: Bundle, model_order: list[str],
                 why: str = "C needs the API A adds") -> str:
        ann = SuperAnnotation(thesis="t", order=model_order, order_why=why)
        diffs = [superdiff.RepoDiff(owner="o", repo=m.repo, root=".",
                                    base_sha="a" * 40, head_sha="b" * 40,
                                    members=[m]) for m in bundle.members]
        with mock.patch.object(superpr, "_clone_paths", return_value=({}, {})), \
             mock.patch.object(superpr.superdiff, "build_all",
                               return_value=(diffs, [])), \
             mock.patch.object(superpr.superanalyze, "harvest", return_value=[]), \
             mock.patch.object(superpr.superanalyze, "annotate",
                               return_value=ann):
            card, _url, _problems = superpr.refresh(bundle, Config(),
                                                    publish=False)
        return card

    def test_a_rebrief_keeps_the_pinned_order(self) -> None:
        bundle = _three(order=["o/C#3", "o/A#1", "o/B#2"], order_pinned=True)
        card = self._refresh(bundle, ["o/A#1", "o/B#2", "o/C#3"])
        self.assertEqual(bundle.order, ["o/C#3", "o/A#1", "o/B#2"])
        self.assertEqual(_order_line(card), "o/C#3 → o/A#1 → o/B#2")
        self.assertIn("📌 pinned", card)
        # The model's view is kept as reasoning, not as the plan.
        self.assertEqual(bundle.suggested_order, ["o/A#1", "o/B#2", "o/C#3"])
        self.assertIn("The review pass suggested o/A#1 → o/B#2 → o/C#3: "
                      "C needs the API A adds", card)

    def test_an_unpinned_rebrief_still_takes_the_model_s_order(self) -> None:
        bundle = _three(order=["o/C#3", "o/A#1", "o/B#2"])
        card = self._refresh(bundle, ["o/B#2", "o/A#1", "o/C#3"])
        self.assertEqual(bundle.order, ["o/B#2", "o/A#1", "o/C#3"])
        self.assertEqual(_order_line(card), "o/B#2 → o/A#1 → o/C#3")
        self.assertNotIn("📌", card)

    def test_a_member_added_later_goes_after_the_pinned_ones(self) -> None:
        bundle = _three(order=["o/C#3", "o/A#1", "o/B#2"], order_pinned=True)
        superpr.add(Config(super_home="o/S"), bundle,
                    [Candidate(owner="o", repo="D", branch="feat", pr=4)])
        self.assertEqual(bundle.order, ["o/C#3", "o/A#1", "o/B#2", "o/D#4"])
        # …and the re-brief that follows does not let the model move it up.
        self._refresh(bundle, ["o/D#4", "o/A#1", "o/B#2", "o/C#3"])
        self.assertEqual(bundle.order, ["o/C#3", "o/A#1", "o/B#2", "o/D#4"])

    def test_a_removed_member_drops_out_of_the_pin(self) -> None:
        bundle = _three(order=["o/C#3", "o/A#1", "o/B#2"], order_pinned=True)
        superpr.remove(bundle, [bundle.members[0]])
        self.assertEqual(bundle.order, ["o/C#3", "o/B#2"])
        self._refresh(bundle, ["o/B#2", "o/C#3"])
        self.assertEqual(bundle.order, ["o/C#3", "o/B#2"])
        self.assertTrue(bundle.order_pinned)

    def test_the_merge_follows_the_pin_not_the_model(self) -> None:
        import crux.supermerge as supermerge
        bundle = _three(order=["o/C#3", "o/A#1", "o/B#2"], order_pinned=True)
        self._refresh(bundle, ["o/A#1", "o/B#2", "o/C#3"])
        self.assertEqual([m.pr for m in supermerge.order_members(bundle)],
                         [3, 1, 2])

    def test_the_brief_shows_the_order_the_merge_will_follow(self) -> None:
        # Before D41 a member the model left out was appended alphabetically
        # on the brief but in member order by the merge — two different plans.
        import crux.supermerge as supermerge
        bundle = Bundle(number=7, home="o/S", members=[
            BundleMember(owner="o", repo="Z", branch="f", pr=1),
            BundleMember(owner="o", repo="A", branch="f", pr=2),
            BundleMember(owner="o", repo="M", branch="f", pr=3)])
        card = self._refresh(bundle, ["o/M#3"])
        merged = [f"o/{m.repo}#{m.pr}" for m in supermerge.order_members(bundle)]
        self.assertEqual(_order_line(card), " → ".join(merged))
        self.assertEqual(merged, ["o/M#3", "o/Z#1", "o/A#2"])


class TestHowItLandsTravelsInTheBrief(_TmpStore):
    """D38's rule, extended: the brief is how a teammate's Crux learns what to
    do, so a pin or a method that stayed on the author's laptop would merge
    the wrong way for exactly the people allowed to press Merge."""

    def _pinned(self) -> Bundle:
        return _three(order=["o/C#3", "o/A#1", "o/B#2"], order_pinned=True,
                      merge_method="merge",
                      suggested_order=["o/A#1", "o/B#2", "o/C#3"],
                      order_why="C needs A")

    def test_the_state_block_carries_the_pin_and_the_method(self) -> None:
        rebuilt = bundle_store.decode_state(
            bundle_store.encode_state(self._pinned()), "o/S")
        self.assertTrue(rebuilt.order_pinned)
        self.assertEqual(rebuilt.merge_method, "merge")
        self.assertEqual(rebuilt.order, ["o/C#3", "o/A#1", "o/B#2"])
        self.assertEqual(rebuilt.suggested_order, ["o/A#1", "o/B#2", "o/C#3"])
        self.assertEqual(rebuilt.order_why, "C needs A")

    def test_a_brief_from_before_d41_reads_as_unpinned_with_no_method(self) -> None:
        # Exactly the block an older Crux published: none of the new keys.
        old = {"number": 7, "name": "feature-x", "rev": 3,
               "order": ["o/B#2", "o/A#1"], "test_steps": [],
               "members": [{"owner": "o", "repo": "A", "branch": "f", "pr": 1,
                            "base": "main", "author": "", "state": ""},
                           {"owner": "o", "repo": "B", "branch": "f", "pr": 2,
                            "base": "main", "author": "", "state": ""}]}
        body = "## brief\n\n<!-- crux:super-state " + json.dumps(old) + " -->"
        rebuilt = bundle_store.decode_state(body, "o/S")
        self.assertEqual(rebuilt.order, ["o/B#2", "o/A#1"])
        self.assertFalse(rebuilt.order_pinned)
        self.assertEqual((rebuilt.merge_method, rebuilt.suggested_order,
                          rebuilt.order_why), ("", [], ""))

    def test_a_bundle_file_from_before_d41_still_loads(self) -> None:
        (Path(self.tmp.name) / "7.json").write_text(json.dumps(
            {"number": 7, "home": "o/S", "order": ["o/A#1"],
             "members": [{"owner": "o", "repo": "A", "branch": "f", "pr": 1}]}))
        found = bundle_store.load(7)
        self.assertEqual((found.order_pinned, found.merge_method), (False, ""))

    def test_the_local_store_round_trips_them_too(self) -> None:
        bundle_store.save(self._pinned())
        found = bundle_store.load(7)
        self.assertEqual((found.order_pinned, found.merge_method, found.order),
                         (True, "merge", ["o/C#3", "o/A#1", "o/B#2"]))

    def test_a_doctored_method_never_reaches_the_merge_call(self) -> None:
        # The state block is text anyone who can edit the issue can change.
        block = bundle_store.encode_state(self._pinned()).replace(
            '"merge_method":"merge"', '"merge_method":"squash\\" --admin"')
        self.assertEqual(bundle_store.decode_state(block, "o/S").merge_method, "")

    def test_a_teammate_adopts_a_pin_published_after_their_last_press(self) -> None:
        import crux.superpost as superpost
        cached = _three(order=["o/A#1", "o/B#2", "o/C#3"])
        cached.rev = 3
        bundle_store._mirror(cached)        # what their first press left
        pinned = self._pinned()
        pinned.rev = 4
        card = superrender.render_card(pinned, SuperAnnotation(), [], Config())
        with mock.patch.object(superpost, "find_brief", return_value=41), \
             mock.patch.object(superpost, "issue_body", return_value=card):
            found = bundle_store.hydrate(7, Config(super_home="o/S"))
        self.assertTrue(found.order_pinned)
        self.assertEqual(found.merge_method, "merge")
        self.assertEqual(found.order, ["o/C#3", "o/A#1", "o/B#2"])


class TestMergeMethodPrecedence(_TmpStore):
    """--method > the bundle's own > [super] merge_method > squash."""

    def test_each_answer_outranks_the_next(self) -> None:
        import crux.supermerge as supermerge
        cfg = Config(super_merge_method="rebase")
        self.assertEqual(supermerge.resolve_method(
            _three(merge_method="merge"), cfg, "squash"), "squash")
        self.assertEqual(supermerge.resolve_method(
            _three(merge_method="merge"), cfg), "merge")
        self.assertEqual(supermerge.resolve_method(_three(), cfg), "rebase")
        self.assertEqual(supermerge.resolve_method(_three(), Config()), "squash")

    def test_nothing_configured_is_squash_as_it_always_was(self) -> None:
        import crux.supermerge as supermerge
        self.assertEqual(supermerge.resolve_method(_three(), None), "squash")

    def test_a_config_typo_falls_back_to_squash(self) -> None:
        import crux.supermerge as supermerge
        self.assertEqual(supermerge.resolve_method(
            _three(), Config(super_merge_method="merg")), "squash")

    def test_the_config_key_is_read_from_the_super_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "crux.toml").write_text(
                '[super]\nmerge_method = "merge"\n')
            with mock.patch.object(config, "global_config_path",
                                   return_value=Path(tmp) / "missing.toml"):
                self.assertEqual(config.load(tmp).super_merge_method, "merge")

    def test_the_example_config_documents_it(self) -> None:
        import tomllib
        data = tomllib.loads((ROOT / "crux.toml.example").read_text(
            encoding="utf-8"))
        self.assertEqual(data["super"]["merge_method"], "squash")

    def _merged_with(self, bundle: Bundle, cfg: Config, method: str = "") -> str:
        with mock.patch.object(superpr.supermerge, "run",
                               return_value=bundle.members) as run:
            superpr.merge(bundle, cfg, method=method, report=False)
        return run.call_args[0][1]

    def test_the_merge_core_lands_with_the_resolved_method(self) -> None:
        cfg = Config(super_merge_method="rebase")
        self.assertEqual(self._merged_with(_three(merge_method="merge"), cfg),
                         "merge")
        self.assertEqual(self._merged_with(_three(merge_method="merge"), cfg,
                                           "squash"), "squash")
        self.assertEqual(self._merged_with(_three(), cfg), "rebase")
        self.assertEqual(self._merged_with(_three(), Config()), "squash")

    def _cli_merge(self, *extra: str) -> mock.Mock:
        import crux.cli as cli
        import crux.superact as superact
        args = cli._build_parser().parse_args(["super", "merge", "7", "--yes",
                                               *extra])
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(bundle_store, "hydrate", return_value=_three()), \
             mock.patch.object(cli, "_zen_report_closed"), \
             mock.patch.object(superact, "merge",
                               return_value=([], "all landed", [])) as merged:
            cli._cmd_super(args)
        return merged

    def test_the_terminal_defers_to_the_bundle_unless_told(self) -> None:
        # No --method must reach the core as "no override", or the default in
        # argparse would silently beat the bundle's own choice.
        self.assertEqual(self._cli_merge().call_args.kwargs["method"], "")
        self.assertEqual(self._cli_merge("--method", "rebase")
                         .call_args.kwargs["method"], "rebase")


class TestTheButtonMergesTheBundlesWay(_TmpStore):
    """The brief's Merge button runs on whoever clicked — a teammate with no
    local copy of the bundle and none of the author's config. It must still
    land the pinned order with the bundle's method."""

    def _fake_gh(self, merges: list[tuple[str, dict]]):
        def fake(args, cwd=None, stdin_text=None, timeout=0, rest=None):
            if args[:2] == ["api", "user"]:
                return "sam"
            if "--jq" in args and args[-1] == ".user.login":
                return "pat"
            if args[:3] == ["api", "-X", "PUT"]:
                merges.append((args[3], json.loads(stdin_text)))
                return "{}"
            if len(args) == 2 and "/pulls/" in args[1]:
                return json.dumps({"state": "open", "mergeable": True,
                                   "merged": False, "draft": False})
            return "{}"                 # approvals, the report, closing
        return fake

    def _published(self) -> str:
        author = _three(order=["o/C#3", "o/A#1", "o/B#2"], order_pinned=True,
                        merge_method="merge")
        return superrender.render_card(author, SuperAnnotation(), [], Config())

    def test_a_teammate_s_click_merges_in_the_pinned_order_with_its_method(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        import crux.superpost as superpost
        merges: list[tuple[str, dict]] = []
        handler = serve.Handler.__new__(serve.Handler)
        handler.cfg = Config(super_home="o/S")   # squash, as far as config goes
        self.assertIsNone(bundle_store.load(7))  # nothing local on this machine
        with mock.patch.object(superpost, "find_brief", return_value=41), \
             mock.patch.object(superpost, "issue_body",
                               return_value=self._published()), \
             mock.patch.object(superact.post, "_run_gh",
                               side_effect=self._fake_gh(merges)), \
             mock.patch.object(superact, "retire_tickets"):
            bundle = handler._bundle(7)
            page = serve.do_merge(bundle, handler.cfg).decode()
        self.assertEqual([path for path, _ in merges],
                         ["repos/o/C/pulls/3/merge", "repos/o/A/pulls/1/merge",
                          "repos/o/B/pulls/2/merge"])
        self.assertEqual({p["merge_method"] for _, p in merges}, {"merge"})
        self.assertIn("all 3 pull requests landed", page)

    def test_the_confirm_page_says_how_it_will_merge(self) -> None:
        import crux.serve as serve
        import crux.superact as superact
        with mock.patch.object(superact, "actor_login", return_value="sam"), \
             mock.patch.object(superact, "author_login", return_value="pat"):
            pinned = serve.merge_page(_three(merge_method="merge"),
                                      Config()).decode()
            plain = serve.merge_page(_three(), Config()).decode()
        self.assertIn("<b>merge commits</b> — set on this super PR", pinned)
        self.assertIn("<b>squash and merge</b> — the default on this machine",
                      plain)


class TestRestampingTheBrief(_TmpStore):
    """After `crux super order`, the brief is rewritten in place — the landing
    section and the state block, and nothing a model wrote."""

    def _card(self, bundle: Bundle) -> str:
        ann = SuperAnnotation(thesis="Adds the relay across three repos",
                              ideas=["one idea"],
                              order=["o/A#1", "o/B#2", "o/C#3"],
                              order_why="A adds what B and C call")
        bundle.suggested_order, bundle.order_why = ann.order, ann.order_why
        return superrender.render_card(bundle, ann, [], Config())

    def test_only_the_landing_section_and_the_state_change(self) -> None:
        bundle = _three(order=["o/A#1", "o/B#2", "o/C#3"])
        published = self._card(bundle)
        bundle.order, bundle.order_pinned = ["o/C#3", "o/A#1", "o/B#2"], True
        bundle.merge_method = "merge"
        body = superrender.restamp(published, bundle, Config())

        self.assertEqual(_order_line(body), "o/C#3 → o/A#1 → o/B#2")
        self.assertIn("Merge method: **merge commits**", body)
        self.assertIn("The review pass suggested o/A#1 → o/B#2 → o/C#3: A adds "
                      "what B and C call", body)
        # What the model wrote is untouched…
        self.assertIn("_Adds the relay across three repos_", body)
        self.assertIn("- one idea", body)
        self.assertIn("[Merge this super PR]", body)
        # …and exactly one state block, which agrees with what is shown.
        self.assertEqual(body.count("crux:super-state"), 1)
        rebuilt = bundle_store.decode_state(body, "o/S")
        self.assertEqual((rebuilt.order, rebuilt.order_pinned,
                          rebuilt.merge_method),
                         (["o/C#3", "o/A#1", "o/B#2"], True, "merge"))

    def test_a_brief_with_no_landing_section_gets_one_before_its_buttons(self) -> None:
        bundle = _three(order_pinned=True, order=["o/B#2", "o/A#1", "o/C#3"])
        body = superrender.restamp(
            "<!-- crux:super -->\n## 🦸 Super PR #7\n\n---\n🦸 **[Merge]**",
            bundle, Config())
        self.assertLess(body.index("### Landing order"), body.index("---"))
        self.assertEqual(_order_line(body), "o/B#2 → o/A#1 → o/C#3")

    def test_republishing_makes_no_model_call(self) -> None:
        bundle = _three(order=["o/A#1", "o/B#2", "o/C#3"])
        published = self._card(bundle)
        superpr.set_landing(bundle, refs=["o/C#3", "o/A#1", "o/B#2"],
                            method="merge")
        with mock.patch.object(superpr.superanalyze, "annotate",
                               side_effect=AssertionError("model called")), \
             mock.patch.object(superpr.superdiff, "build_all",
                               side_effect=AssertionError("re-diffed")), \
             mock.patch.object(superpr.superpost, "issue_body",
                               return_value=published) as read, \
             mock.patch.object(superpr.superpost, "publish",
                               return_value=(41, "https://x/41")) as published_:
            url, problems = superpr.republish(bundle, Config())
        self.assertEqual((url, problems), ("https://x/41", []))
        self.assertEqual(read.call_args[0], ("o/S", 41))
        body = published_.call_args[0][1]
        self.assertEqual(_order_line(body), "o/C#3 → o/A#1 → o/B#2")
        # The block carries the revision just saved, so a teammate's cache —
        # and this machine's own copy — agree with the brief.
        self.assertEqual(bundle_store.decode_state(body, "o/S").rev,
                         bundle_store.load(7).rev)

    def test_an_unreadable_brief_is_never_overwritten(self) -> None:
        bundle = _three(order_pinned=True, order=["o/C#3", "o/A#1", "o/B#2"])
        with mock.patch.object(superpr.superpost, "issue_body", return_value=""), \
             mock.patch.object(superpr.superpost, "publish") as published, \
             self.assertRaises(superpr.SuperError) as caught:
            superpr.republish(bundle, Config())
        published.assert_not_called()
        self.assertIn("crux super refresh 7", str(caught.exception))

    def test_no_brief_yet_is_a_note_not_a_failure(self) -> None:
        bundle = _three()
        bundle.issue = None
        with mock.patch.object(superpr.superpost, "find_brief",
                               return_value=None), \
             mock.patch.object(superpr.superpost, "publish") as published:
            url, problems = superpr.republish(bundle, Config())
        published.assert_not_called()
        self.assertEqual(url, "")
        self.assertIn("no brief yet", problems[0])


class TestSuperOrderCommand(_TmpStore):
    """`crux super order N [REF ...] [--unpin] [--method M] [--no-brief]`."""

    def _run(self, *argv: str, bundle: Bundle | None = None):
        import crux.cli as cli
        args = cli._build_parser().parse_args(["super", "order", "7", *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(bundle_store, "hydrate",
                               return_value=bundle or _three()), \
             mock.patch.object(superpr, "republish",
                               return_value=("https://x/41", [])) as republished:
            code = cli._cmd_super(args)
        return code, out.getvalue(), republished

    def test_the_parser_takes_refs_and_a_method(self) -> None:
        import crux.cli as cli
        parsed = cli._build_parser().parse_args(
            ["super", "order", "7", "o/C#3", "A#1", "--method", "merge"])
        self.assertEqual((parsed.saction, parsed.number, parsed.refs,
                          parsed.method, parsed.unpin),
                         ("order", 7, ["o/C#3", "A#1"], "merge", False))

    def test_nothing_to_change_shows_the_plan_and_changes_nothing(self) -> None:
        code, text, republished = self._run()
        self.assertEqual(code, 0)
        self.assertIn("1. o/A#1", text)
        self.assertIn("crux super order 7 o/A#1 o/B#2 o/C#3", text)
        self.assertIn("merge method: squash and merge", text)
        republished.assert_not_called()
        self.assertIsNone(bundle_store.load(7))

    def test_a_pin_is_saved_and_the_brief_restamped(self) -> None:
        code, text, republished = self._run("o/C#3", "o/A#1", "o/B#2",
                                            "--method", "merge")
        self.assertEqual(code, 0)
        republished.assert_called_once()
        self.assertIn("📌 pinned by hand", text)
        self.assertIn("merge commits (set on this super PR)", text)
        self.assertIn("Brief updated — https://x/41", text)
        self.assertEqual(bundle_store.load(7).order, ["o/C#3", "o/A#1", "o/B#2"])

    def test_no_brief_saves_without_publishing(self) -> None:
        code, text, republished = self._run("--method", "rebase", "--no-brief")
        self.assertEqual(code, 0)
        republished.assert_not_called()
        self.assertIn("crux super refresh 7", text)
        self.assertEqual(bundle_store.load(7).merge_method, "rebase")

    def test_a_partial_order_exits_1_and_says_what_is_missing(self) -> None:
        code, text, republished = self._run("o/C#3")
        self.assertEqual(code, 1)
        self.assertIn("missing o/A#1, o/B#2", text)
        republished.assert_not_called()
        self.assertIsNone(bundle_store.load(7))


class TestNoOneAtTheKeyboard(unittest.TestCase):
    """A closed stdin — `</dev/null`, a pipe, CI, an agent's tool call — is
    "nobody answered", not a crash. It reached main()'s catch-all as an
    EOFError and was reported as "`super` crashed"."""

    def _new_args(self, **kw) -> argparse.Namespace:
        base = dict(saction="new", pick=[], name="", base="", yes=False,
                    no_brief=False, no_llm=False, dry_run=False, delay=0)
        base.update(kw)
        return argparse.Namespace(**base)

    def _cands(self) -> list[Candidate]:
        return [Candidate(owner="o", repo="A", branch="feat", pr=1)]

    def _bundle(self) -> Bundle:
        return Bundle(number=7, name="f", home="o/S", issue=41, members=[
            BundleMember(owner="o", repo="A", branch="feat", pr=1),
            BundleMember(owner="o", repo="B", branch="feat", pr=2)])

    def _run(self, args) -> tuple[int, str]:
        import crux.cli as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch.object(candidates, "gather",
                               return_value=(self._cands(), [])), \
             mock.patch.object(bundle_store, "hydrate",
                               return_value=self._bundle()), \
             mock.patch.object(superpr, "create",
                               side_effect=AssertionError("created")), \
             mock.patch.object(superpr, "add",
                               side_effect=AssertionError("added")), \
             mock.patch.object(superpr, "remove",
                               side_effect=AssertionError("removed")):
            code = cli._cmd_super(args)
        return code, out.getvalue()

    def test_the_new_picker_reads_eof_as_cancel(self) -> None:
        code, text = self._run(self._new_args())
        self.assertEqual(code, 1)
        self.assertIn("nothing selected — no answer on stdin", text)
        self.assertIn("`crux super new 1 3-5`", text)

    def test_through_main_it_is_not_reported_as_a_crash(self) -> None:
        import crux.cli as cli
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch.object(cli, "_setup_logging",
                               return_value=logging.getLogger("t")), \
             mock.patch.object(cli, "_tty_failure") as crashed, \
             mock.patch.object(candidates, "gather",
                               return_value=(self._cands(), [])), \
             mock.patch.object(superpr, "create",
                               side_effect=AssertionError("created")):
            code = cli.main(["super", "new"])
        self.assertEqual(code, 1)
        crashed.assert_not_called()

    def test_the_add_picker_names_its_own_command(self) -> None:
        code, text = self._run(argparse.Namespace(
            saction="add", number=7, pick=[], base="", yes=False,
            no_brief=False, no_llm=False, dry_run=False, delay=0))
        self.assertEqual(code, 1)
        self.assertIn("`crux super add 7 1 3-5`", text)

    def test_the_remove_picker_too(self) -> None:
        code, text = self._run(argparse.Namespace(
            saction="remove", number=7, pick=[], no_brief=False,
            no_llm=False, dry_run=False, delay=0))
        self.assertEqual(code, 1)
        self.assertIn("`crux super remove 7 2`", text)

    def test_a_merge_nobody_confirmed_merges_nothing(self) -> None:
        import crux.cli as cli
        import crux.superact as superact
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch.object(bundle_store, "hydrate",
                               return_value=self._bundle()), \
             mock.patch.object(superact, "merge",
                               side_effect=AssertionError("merged")):
            code = cli._cmd_super(argparse.Namespace(
                saction="merge", number=7, method="squash", yes=False,
                admin=False))
        self.assertEqual(code, 1)
        self.assertIn("nothing merged — no answer on stdin", out.getvalue())
        self.assertIn("--yes", out.getvalue())

    def test_the_single_pr_merge_too(self) -> None:
        import crux.cli as cli
        import crux.gitio as gitio
        import crux.post as post
        import crux.superact as superact
        out = io.StringIO()
        info = RepoInfo(root="/r", branch="feat", head_sha="a" * 40,
                        base_sha="b" * 40, owner="o", repo="A")
        with contextlib.redirect_stdout(out), \
             mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch.object(gitio, "repo_info", return_value=info), \
             mock.patch.object(post, "find_pr", return_value=12), \
             mock.patch.object(bundle_store, "find_by_branch", return_value=None), \
             mock.patch.object(superact, "merge_pr",
                               side_effect=AssertionError("merged")):
            code = cli._cmd_merge(argparse.Namespace(
                pr=0, method="squash", admin=False, yes=False))
        self.assertEqual(code, 1)
        self.assertIn("nothing merged — no answer on stdin", out.getvalue())

    def test_silence_at_the_open_prs_ask_opens_nothing(self) -> None:
        # Enter means yes there, but end-of-input is not Enter: opening PRs is
        # outward-facing, so silence declines.
        import crux.cli as cli
        cand = Candidate(owner="o", repo="A", branch="feat", pr=None,
                         path="/clones/A")
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch("sys.stdin.isatty", return_value=True):
            self.assertEqual(cli._confirm_super_prs([(cand, "main")]),
                             (False, ""))


class TestADryRunLeavesNothingBehind(unittest.TestCase):
    """`--dry-run` saved the bundle, printed "Super PR #N created", and opened
    PRs for picked branches that had none. After it, `crux super list` showed
    a super PR nobody made, its PRs were barred from every other bundle, and a
    push to one of its branches re-briefed it for real."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            bundle_store, "store_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        # Nothing may be pushed or opened on a dry run, whatever path tries.
        def git(args, *a, **kw):
            if args and args[0] == "push":
                raise AssertionError("a dry run pushed")
            raise CruxError("not a repo")     # the CLI then uses the cwd
        for target, name, effect in (
                (superpr.gitio, "run_git", git),
                (superpr.post, "_create_pr", AssertionError("a PR was opened"))):
            p = mock.patch.object(target, name, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)

    def _args(self, **kw) -> argparse.Namespace:
        base = dict(saction="new", pick=["1", "2"], name="", base="", yes=True,
                    no_brief=False, no_llm=True, dry_run=True, delay=0)
        base.update(kw)
        return argparse.Namespace(**base)

    def _run(self, args, cands=()):
        import crux.cli as cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(config, "load",
                               return_value=Config(super_home="o/S")), \
             mock.patch.object(candidates, "gather",
                               return_value=(list(cands), [])), \
             mock.patch.object(superpost, "highest_brief", return_value=2), \
             mock.patch.object(superpost, "find_brief", return_value=None), \
             mock.patch.object(superpr, "refresh",
                               return_value=("THE CARD", "", [])) as refreshed:
            code = cli._cmd_super(args)
        return code, out.getvalue(), refreshed

    def test_super_new_dry_run_saves_nothing_and_creates_nothing(self) -> None:
        cands = [Candidate(owner="o", repo="A", branch="feat", pr=4),
                 Candidate(owner="o", repo="B", branch="feat", pr=None,
                           path="/clones/B")]
        code, text, refreshed = self._run(
            self._args(), cands)
        self.assertEqual(code, 0)
        self.assertEqual(bundle_store.load_all(), [])       # no bundle file
        self.assertEqual(bundle_store.bundled_prs(), {})    # nothing held
        self.assertNotIn("created", text)
        self.assertIn("Dry run — Super PR #3 would hold 1 pull request", text)
        # The branch with no PR is named, not silently dropped — and not opened.
        self.assertIn("o/B: 'feat' has no PR yet", text)
        # The preview briefs the unsaved bundle itself, and publishes nothing.
        previewed = refreshed.call_args[0][0]
        self.assertEqual([(m.repo, m.pr) for m in previewed.members], [("A", 4)])
        self.assertIs(refreshed.call_args.kwargs["publish"], False)
        self.assertIn("THE CARD", text)

    def test_the_number_is_not_used_up_by_a_dry_run(self) -> None:
        code, _text, _ = self._run(
            self._args(pick=["1"]),
            [Candidate(owner="o", repo="A", branch="feat", pr=4)])
        self.assertEqual(code, 0)
        with mock.patch.object(superpost, "highest_brief", return_value=2):
            self.assertEqual(bundle_store.next_number("o/S"), 3)

    def test_a_dry_run_offers_no_ticket_links(self) -> None:
        import crux.cli as cli
        for no_brief in (False, True):
            with mock.patch.object(cli, "_zen_offer_bundle") as offered:
                code, _text, _ = self._run(
                    self._args(pick=["1"], no_brief=no_brief),
                    [Candidate(owner="o", repo="A", branch="feat", pr=4)])
            self.assertEqual(code, 0)
            offered.assert_not_called()

    def _stored(self) -> Bundle:
        bundle = Bundle(number=7, name="f", home="o/S", members=[
            BundleMember(owner="o", repo="A", branch="feat", pr=1),
            BundleMember(owner="o", repo="B", branch="feat", pr=2)])
        bundle_store.save(bundle)
        return bundle_store.load(7)

    def test_super_add_dry_run_leaves_the_bundle_as_it_was(self) -> None:
        before = self._stored()
        code, text, refreshed = self._run(
            self._args(saction="add", number=7, pick=["1"]),
            [Candidate(owner="o", repo="C", branch="feat", pr=3)])
        self.assertEqual(code, 0)
        after = bundle_store.load(7)
        self.assertEqual((len(after.members), after.rev),
                         (len(before.members), before.rev))
        self.assertEqual(len(refreshed.call_args[0][0].members), 3)
        self.assertIn("would hold 3 pull requests", text)

    def test_super_remove_dry_run_leaves_the_bundle_as_it_was(self) -> None:
        before = self._stored()
        code, text, refreshed = self._run(
            self._args(saction="remove", number=7, pick=["2"]))
        self.assertEqual(code, 0)
        after = bundle_store.load(7)
        self.assertEqual((len(after.members), after.rev),
                         (len(before.members), before.rev))
        self.assertEqual(len(refreshed.call_args[0][0].members), 1)
        self.assertIn("o/B#2 would be detached", text)
        self.assertNotIn("detached — the pull request", text)

    def test_the_real_run_still_saves(self) -> None:
        # The flag, not the flow, is what stops the write.
        with mock.patch.object(superpost, "highest_brief", return_value=0):
            bundle, _ = superpr.create(
                Config(super_home="o/S"),
                [Candidate(owner="o", repo="A", branch="feat", pr=4)])
        self.assertIsNotNone(bundle_store.load(bundle.number))


class TestGitTooOldForSuperDiffs(unittest.TestCase):
    """`git merge-tree --write-tree` is git 2.38+. On Ubuntu / Pop!_OS 22.04
    (git 2.34) every repo failed with git's usage text, one per repo, and
    nothing said the fix was a newer git."""

    def test_an_old_git_is_refused_up_front_with_the_fix(self) -> None:
        with mock.patch.object(superdiff, "git_version",
                               return_value=(2, 34, 1)), \
             mock.patch.object(superdiff, "build") as built:
            with self.assertRaises(superdiff.SuperDiffError) as caught:
                superdiff.build_all([BundleMember(owner="o", repo="A",
                                                  branch="f", pr=1)])
        built.assert_not_called()                 # not once per repo
        text = str(caught.exception)
        self.assertIn("git 2.38 or newer", text)
        self.assertIn("this machine has git 2.34.1", text)
        self.assertIn("ppa:git-core/ppa", text)

    def test_new_enough_or_unknown_goes_ahead(self) -> None:
        for version in ((2, 38, 0), (3, 0, 0), None):
            with mock.patch.object(superdiff, "git_version",
                                   return_value=version):
                superdiff.require_git()           # does not raise

    def test_git_s_usage_text_is_translated_too(self) -> None:
        # The belt to require_git's braces: a git whose version could not be
        # read still gets the plain explanation, not the usage dump.
        usage = subprocess.CompletedProcess(
            ["git"], 129, stdout="",
            stderr="usage: git merge-tree <base-tree> <branch1> <branch2>")
        with mock.patch.object(superdiff.subprocess, "run", return_value=usage):
            with self.assertRaises(superdiff.SuperDiffError) as caught:
                superdiff._merge_tree("/r", "a" * 40, "b" * 40)
        self.assertIn("git 2.38 or newer", str(caught.exception))

    def test_the_terminal_gets_one_clear_line(self) -> None:
        import crux.cli as cli
        out = io.StringIO()
        bundle = Bundle(number=7, home="o/S", members=[
            BundleMember(owner="o", repo="A", branch="f", pr=1)])
        with contextlib.redirect_stdout(out), \
             mock.patch.object(bundle_store, "hydrate", return_value=bundle), \
             mock.patch.object(superpr, "_clone_paths", return_value=({}, {})), \
             mock.patch.object(superdiff, "git_version",
                               return_value=(2, 34, 1)):
            code = cli._cmd_super(argparse.Namespace(
                saction="refresh", number=7, no_llm=True, dry_run=True,
                delay=0))
        self.assertEqual(code, 1)
        self.assertIn("❌ crux super failed: super PRs need git 2.38",
                      out.getvalue())
        self.assertNotIn("usage:", out.getvalue())

    @staticmethod
    def _install():
        import importlib.util
        spec = importlib.util.spec_from_file_location("_inst", ROOT / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _setup_says(self, stdout: str, pkg: str = "apt-get") -> str:
        inst = self._install()
        out = io.StringIO()
        done = subprocess.CompletedProcess(["git"], 0, stdout=stdout)
        with contextlib.redirect_stdout(out), \
             mock.patch.object(inst, "run", return_value=done), \
             mock.patch.object(inst, "PKG_MGR", pkg):
            inst.check_git_version()
        return out.getvalue()

    def test_setup_warns_about_an_old_git_and_names_the_ppa(self) -> None:
        said = self._setup_says("git version 2.34.1\n")
        self.assertIn("git 2.34 is older than 2.38", said)
        self.assertIn("ppa:git-core/ppa", said)

    def test_setup_names_the_package_manager_s_own_upgrade_elsewhere(self) -> None:
        said = self._setup_says("git version 2.30.0\n", pkg="brew")
        self.assertIn("brew install git", said)

    def test_setup_is_quiet_about_a_current_or_unreadable_git(self) -> None:
        self.assertEqual(self._setup_says("git version 2.43.0\n"), "")
        self.assertEqual(self._setup_says("garbage"), "")
