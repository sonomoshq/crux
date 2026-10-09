# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.prs (the `crux prs` command) and its CLI wiring.

All `gh` traffic goes through crux.post._run_gh, which is mocked here — no
network, no gh binary. The CLI tests mock crux.prs's public functions to
verify wiring (jobs resolution, exit codes) without any real fetching.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crux.cli as cli
import crux.config as config
import crux.prs as prs
from crux.models import Config, PostError


def gh_json(rows: list[dict]) -> str:
    return json.dumps(rows)


def pr_row(number: int = 1, title: str = "A change", branch: str = "feat/x",
           login: str = "fixture-user", draft: bool = False,
           updated: str = "2026-07-15T00:00:00Z") -> dict:
    return {"number": number, "title": title, "headRefName": branch,
            "author": {"login": login}, "isDraft": draft,
            "updatedAt": updated}


class ResolveReposTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config(scope_owners=["example-org"])

    def test_explicit_owner_repo_needs_no_discovery(self) -> None:
        with mock.patch("crux.post._run_gh") as run_gh:
            repos, problems = prs.resolve_repos(
                ["example-org/crux", "acme/Widgets"], self.cfg)
        self.assertEqual(repos, ["example-org/crux", "acme/Widgets"])
        self.assertEqual(problems, [])
        run_gh.assert_not_called()

    def test_no_args_discovers_all_owner_repos_sorted(self) -> None:
        with mock.patch("crux.post._run_gh", return_value=gh_json([
                {"nameWithOwner": "example-org/Gadget"},
                {"nameWithOwner": "example-org/crux"}])) as run_gh:
            repos, problems = prs.resolve_repos([], self.cfg)
        self.assertEqual(repos, ["example-org/crux", "example-org/Gadget"])
        self.assertEqual(problems, [])
        args = run_gh.call_args[0][0]
        self.assertEqual(args[:3], ["repo", "list", "example-org"])

    def test_bare_name_matches_case_insensitively(self) -> None:
        with mock.patch("crux.post._run_gh", return_value=gh_json(
                [{"nameWithOwner": "example-org/crux"}])):
            repos, problems = prs.resolve_repos(["crux", "nosuch"], self.cfg)
        self.assertEqual(repos, ["example-org/crux"])
        self.assertEqual(len(problems), 1)
        self.assertIn("nosuch", problems[0])

    def test_dot_resolves_current_repo_origin(self) -> None:
        with mock.patch("crux.gitio.run_git",
                        return_value="git@github.com:example-org/crux.git"):
            repos, problems = prs.resolve_repos(["."], self.cfg)
        self.assertEqual(repos, ["example-org/crux"])
        self.assertEqual(problems, [])

    def test_unreachable_owner_degrades_to_problem(self) -> None:
        with mock.patch("crux.post._run_gh",
                        side_effect=PostError("gh not installed")):
            repos, problems = prs.resolve_repos([], self.cfg)
        self.assertEqual(repos, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("example-org", problems[0])

    def test_duplicates_are_removed_in_order(self) -> None:
        repos, _ = prs.resolve_repos(
            ["a/one", "a/two", "A/ONE"], self.cfg)
        self.assertEqual(repos, ["a/one", "a/two"])


class FetchOpenPrsTest(unittest.TestCase):
    def test_fans_out_and_captures_per_repo_errors(self) -> None:
        def fake_gh(args, cwd=None, stdin_text=None, timeout=None):
            repo = args[args.index("--repo") + 1]
            if repo == "o/bad":
                raise PostError("gh pr list failed: boom")
            return gh_json([pr_row(number=7)])

        with mock.patch("crux.post._run_gh", side_effect=fake_gh):
            results = prs.fetch_open_prs(["o/good", "o/bad"], jobs=8)
        self.assertEqual(list(results), ["o/good", "o/bad"])  # input order
        self.assertEqual(results["o/good"][0]["number"], 7)
        self.assertIn("boom", results["o/bad"])

    def test_unparseable_json_is_an_error_not_a_crash(self) -> None:
        with mock.patch("crux.post._run_gh", return_value="not json"):
            results = prs.fetch_open_prs(["o/r"], jobs=1)
        self.assertIsInstance(results["o/r"], str)
        self.assertIn("unparseable", results["o/r"])

    def test_auto_jobs_is_hardware_bounded(self) -> None:
        self.assertGreaterEqual(prs.auto_jobs(), 1)
        self.assertLessEqual(prs.auto_jobs(), 32)


class RenderPrsTest(unittest.TestCase):
    def test_blocks_counts_and_failures(self) -> None:
        two_days = (datetime.now(timezone.utc)
                    - timedelta(days=2)).isoformat()
        out = prs.render_prs({
            "o/busy": [pr_row(number=12, title="Add prs command",
                              updated=two_days),
                       pr_row(number=9, title="Fix logs", branch="fix/logs",
                              login="alice", draft=True)],
            "o/quiet": [],
            "o/broken": "gh pr list failed: 404",
        })
        self.assertIn("o/busy — 2 open", out)
        self.assertIn("#12", out)
        self.assertIn("Add prs command", out)
        self.assertIn("2d", out)
        self.assertIn("[draft]", out)
        self.assertIn("o/quiet — no open PRs", out)
        self.assertIn("o/broken — FAILED: gh pr list failed: 404", out)
        self.assertIn("2 open PRs across 3 repos (1 failed)", out)

    def test_long_titles_and_branches_are_ellipsized(self) -> None:
        out = prs.render_prs({"o/r": [pr_row(title="x" * 200,
                                             branch="b" * 200)]})
        self.assertIn("…", out)
        self.assertNotIn("x" * 61, out)
        self.assertNotIn("b" * 41, out)


class PrsConfigTest(unittest.TestCase):
    def test_jobs_key_maps_and_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ,
                                 {"XDG_CONFIG_HOME": os.path.join(tmp, "xdg")}):
                self.assertEqual(config.load(tmp).prs_jobs, 0)  # default
                Path(tmp, "crux.toml").write_text(
                    "[prs]\njobs = 3\n", encoding="utf-8")
                self.assertEqual(config.load(tmp).prs_jobs, 3)
                # wrong type degrades to the default instead of crashing
                Path(tmp, "crux.toml").write_text(
                    '[prs]\njobs = "many"\n', encoding="utf-8")
                self.assertEqual(config.load(tmp).prs_jobs, 0)


class CliPrsTest(unittest.TestCase):
    """Wiring: jobs resolution and exit codes, with crux.prs mocked."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "HOME": tmp.name,
            "XDG_CONFIG_HOME": os.path.join(tmp.name, ".config")})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = Config()
        for target, kwargs in [
            ("crux.config.load", {"return_value": self.cfg}),
            ("crux.gitio.run_git", {"return_value": tmp.name}),
            ("crux.prs.resolve_repos", {"return_value": (["o/r"], [])}),
            ("crux.prs.fetch_open_prs", {"return_value": {"o/r": []}}),
            ("crux.prs.render_prs", {"return_value": "rendered"}),
            ("crux.prs.auto_jobs", {"return_value": 7}),
        ]:
            patcher = mock.patch(target, **kwargs)
            setattr(self, target.rsplit(".", 1)[-1], patcher.start())
            self.addCleanup(patcher.stop)

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
             contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(argv)
        return code, stdout.getvalue()

    def test_default_jobs_is_auto_max(self) -> None:
        code, out = self.run_cli(["prs"])
        self.assertEqual(code, 0)
        self.assertIn("rendered", out)
        self.fetch_open_prs.assert_called_once_with(["o/r"], 7)

    def test_config_jobs_caps_the_pool(self) -> None:
        self.cfg.prs_jobs = 2
        self.run_cli(["prs"])
        self.fetch_open_prs.assert_called_once_with(["o/r"], 2)

    def test_jobs_flag_overrides_config(self) -> None:
        self.cfg.prs_jobs = 2
        self.run_cli(["prs", "--jobs", "5"])
        self.fetch_open_prs.assert_called_once_with(["o/r"], 5)

    def test_repo_args_reach_resolver(self) -> None:
        self.run_cli(["prs", ".", "example-org/crux"])
        self.resolve_repos.assert_called_once_with(
            [".", "example-org/crux"], self.cfg)

    def test_exit_1_when_nothing_resolves(self) -> None:
        self.resolve_repos.return_value = ([], ["no repo named 'x'"])
        code, out = self.run_cli(["prs", "x"])
        self.assertEqual(code, 1)
        self.assertIn("no repo named 'x'", out)

    def test_exit_1_when_every_repo_fails(self) -> None:
        self.fetch_open_prs.return_value = {"o/r": "gh pr list failed"}
        code, _ = self.run_cli(["prs"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
