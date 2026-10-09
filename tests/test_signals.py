# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.harvest.blast / history / testprox against tempdir git repos.

Real `rg` and `git` are used where their output is deterministic for a fixed
tree; subprocess is mocked only to simulate a missing `rg` binary (fallback
paths).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from crux.harvest.blast import add_blast
from crux.harvest.history import add_history
from crux.harvest.testprox import add_test_proximity, compute_scores
from crux.models import Config, Hunk, HunkSignals

HAVE_RG = shutil.which("rg") is not None

# Modules under test do `import subprocess`, so patching their `subprocess.run`
# patches the shared module object; keep a real reference for the fake to call.
_REAL_RUN = subprocess.run

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Crux Test",
    "GIT_AUTHOR_EMAIL": "crux@example.com",
    "GIT_COMMITTER_NAME": "Crux Test",
    "GIT_COMMITTER_EMAIL": "crux@example.com",
}


def _git(cwd: str, *args: str, date: str | None = None) -> None:
    env = dict(_GIT_ENV)
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   capture_output=True, text=True)


def _write(root: str, rel: str, text: str) -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _commit(root: str, message: str, files: dict[str, str],
            date: str | None = None) -> None:
    for rel, text in files.items():
        _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message, date=date)


def _hunk(file: str, new_start: int, new_count: int,
          added: list[str] | None = None) -> Hunk:
    patch = "\n".join("+" + line for line in (added or []))
    return Hunk(id=f"{file}:{new_start}", file=file, old_start=new_start,
                old_count=0, new_start=new_start, new_count=new_count,
                patch=patch)


def _no_rg(argv: list[str], **kwargs):
    """subprocess.run stand-in: rg is 'not installed', everything else is real."""
    if argv and argv[0] == "rg":
        raise FileNotFoundError("rg")
    return _REAL_RUN(argv, **kwargs)


class TempRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="crux-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def init_git(self) -> None:
        _git(self.root, "init", "-q")


class BlastTests(TempRepoTest):
    def _blast_fixture(self) -> tuple[dict[str, HunkSignals], list[Hunk]]:
        _write(self.root, "lib.py", "def frobnicate(x):\n    return x + 1\n")
        _write(self.root, "app.py",
               "from lib import frobnicate\nprint(frobnicate(1))\n")
        _write(self.root, "other.py", "value = frobnicate(2)\n")
        hunk = _hunk("lib.py", 1, 2, ["def frobnicate(x):", "    return x + 1"])
        signals = {hunk.id: HunkSignals(hunk_id=hunk.id, defines=["frobnicate"])}
        return signals, [hunk]

    @unittest.skipUnless(HAVE_RG, "rg not installed")
    def test_blast_counts_and_excludes_defining_hunk(self) -> None:
        signals, hunks = self._blast_fixture()
        add_blast(signals, hunks, self.root)
        sig = signals[hunks[0].id]
        # 4 word matches repo-wide, minus the def line inside the hunk itself.
        self.assertEqual(sig.blast_radius, 3)
        self.assertEqual(sorted(sig.callers),
                         ["app.py:1", "app.py:2", "other.py:1"])

    def test_blast_falls_back_to_git_grep_when_rg_missing(self) -> None:
        signals, hunks = self._blast_fixture()
        self.init_git()
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "seed")
        with mock.patch("crux.harvest.blast.subprocess.run", side_effect=_no_rg):
            add_blast(signals, hunks, self.root)
        sig = signals[hunks[0].id]
        self.assertEqual(sig.blast_radius, 3)
        self.assertEqual(sorted(sig.callers),
                         ["app.py:1", "app.py:2", "other.py:1"])

    @unittest.skipUnless(HAVE_RG, "rg not installed")
    def test_blast_zero_when_symbol_unused(self) -> None:
        _write(self.root, "lib.py", "def lonely_helper():\n    pass\n")
        hunk = _hunk("lib.py", 1, 2)
        signals = {hunk.id: HunkSignals(hunk_id=hunk.id, defines=["lonely_helper"])}
        add_blast(signals, [hunk], self.root)
        self.assertEqual(signals[hunk.id].blast_radius, 0)
        self.assertEqual(signals[hunk.id].callers, [])

    @unittest.skipUnless(HAVE_RG, "rg not installed")
    def test_callers_sample_capped_at_eight(self) -> None:
        _write(self.root, "lib.py", "def frobnicate(x):\n    return x\n")
        _write(self.root, "many.py",
               "".join(f"frobnicate({i})\n" for i in range(12)))
        hunk = _hunk("lib.py", 1, 2)
        signals = {hunk.id: HunkSignals(hunk_id=hunk.id, defines=["frobnicate"])}
        add_blast(signals, [hunk], self.root)
        self.assertEqual(signals[hunk.id].blast_radius, 12)
        self.assertEqual(len(signals[hunk.id].callers), 8)

    def test_missing_rg_and_untracked_repo_yields_zero(self) -> None:
        # No git repo at all: both tools fail, blast degrades to zero.
        _write(self.root, "lib.py", "def frobnicate():\n    pass\n")
        hunk = _hunk("lib.py", 1, 2)
        signals = {hunk.id: HunkSignals(hunk_id=hunk.id, defines=["frobnicate"])}
        with mock.patch("crux.harvest.blast.subprocess.run", side_effect=_no_rg):
            add_blast(signals, [hunk], self.root)
        self.assertEqual(signals[hunk.id].blast_radius, 0)
        self.assertEqual(signals[hunk.id].callers, [])


class HistoryTests(TempRepoTest):
    def _signals_for(self, hunks: list[Hunk]) -> dict[str, HunkSignals]:
        return {h.id: HunkSignals(hunk_id=h.id) for h in hunks}

    def test_churn_fix_frequency_and_co_change(self) -> None:
        self.init_git()
        _commit(self.root, "initial work", {"a.py": "a = 1\n", "b.py": "b = 1\n"})
        _commit(self.root, "Fix crash on load", {"a.py": "a = 2\n", "b.py": "b = 2\n"})
        _commit(self.root, "tweak formatting", {"a.py": "a = 3\n"})
        hunks = [_hunk("a.py", 1, 1)]
        cfg = Config()

        signals = self._signals_for(hunks)
        add_history(signals, hunks, self.root, cfg)
        sig = signals[hunks[0].id]
        self.assertEqual(sig.churn, 3)
        self.assertEqual(sig.fix_frequency, 1)
        # b.py shares 2/3 of a.py's commits, below the 0.7 default threshold.
        self.assertEqual(sig.co_change_miss, [])

        cfg.co_change_threshold = 0.6
        signals = self._signals_for(hunks)
        add_history(signals, hunks, self.root, cfg)
        self.assertEqual(signals[hunks[0].id].co_change_miss, ["b.py"])

    def test_partner_present_in_diff_is_not_a_miss(self) -> None:
        self.init_git()
        _commit(self.root, "one", {"a.py": "a = 1\n", "b.py": "b = 1\n"})
        _commit(self.root, "two", {"a.py": "a = 2\n", "b.py": "b = 2\n"})
        hunks = [_hunk("a.py", 1, 1), _hunk("b.py", 1, 1)]
        cfg = Config()
        signals = self._signals_for(hunks)
        add_history(signals, hunks, self.root, cfg)
        self.assertEqual(signals[hunks[0].id].co_change_miss, [])
        self.assertEqual(signals[hunks[1].id].co_change_miss, [])
        self.assertEqual(signals[hunks[0].id].churn, 2)

    def test_fix_regex_is_case_insensitive(self) -> None:
        self.init_git()
        _commit(self.root, "Revert the widget", {"a.py": "a = 1\n"})
        _commit(self.root, "BUGfix: hotpath", {"a.py": "a = 2\n"})
        _commit(self.root, "add docs", {"a.py": "a = 3\n"})
        hunks = [_hunk("a.py", 1, 1)]
        signals = self._signals_for(hunks)
        add_history(signals, hunks, self.root, Config())
        self.assertEqual(signals[hunks[0].id].fix_frequency, 2)

    def test_commits_outside_window_are_ignored(self) -> None:
        self.init_git()
        old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S")
        _commit(self.root, "fix ancient bug", {"a.py": "a = 0\n"}, date=old)
        _commit(self.root, "recent work", {"a.py": "a = 1\n"})
        hunks = [_hunk("a.py", 1, 1)]
        signals = self._signals_for(hunks)
        add_history(signals, hunks, self.root, Config())
        sig = signals[hunks[0].id]
        self.assertEqual(sig.churn, 1)
        self.assertEqual(sig.fix_frequency, 0)

    def test_repo_without_commits_leaves_defaults(self) -> None:
        self.init_git()
        hunks = [_hunk("a.py", 1, 1)]
        signals = self._signals_for(hunks)
        add_history(signals, hunks, self.root, Config())
        sig = signals[hunks[0].id]
        self.assertEqual((sig.churn, sig.fix_frequency, sig.co_change_miss),
                         (0, 0, []))

    def test_non_git_directory_leaves_defaults(self) -> None:
        hunks = [_hunk("a.py", 1, 1)]
        signals = self._signals_for(hunks)
        add_history(signals, hunks, self.root, Config())
        self.assertEqual(signals[hunks[0].id].churn, 0)


class TestProximityTests(TempRepoTest):
    def _proximity_fixture(self) -> tuple[dict[str, HunkSignals], list[Hunk]]:
        _write(self.root, "lib.py",
               "def frobnicate(x):\n    return x\n\ndef lonely_helper():\n    pass\n")
        _write(self.root, "tests/test_lib.py",
               "from lib import frobnicate\n\ndef test_frob():\n"
               "    assert frobnicate(1) == 1\n")
        h1 = _hunk("lib.py", 1, 2)
        h2 = _hunk("lib.py", 4, 2)
        signals = {
            h1.id: HunkSignals(hunk_id=h1.id, defines=["frobnicate"]),
            h2.id: HunkSignals(hunk_id=h2.id, defines=["lonely_helper"]),
        }
        return signals, [h1, h2]

    @unittest.skipUnless(HAVE_RG, "rg not installed")
    def test_symbol_referenced_in_test_dir_sets_test_touched(self) -> None:
        signals, hunks = self._proximity_fixture()
        add_test_proximity(signals, hunks, self.root, Config())
        self.assertTrue(signals[hunks[0].id].test_touched)
        self.assertFalse(signals[hunks[1].id].test_touched)

    def test_fallback_to_git_grep_when_rg_missing(self) -> None:
        signals, hunks = self._proximity_fixture()
        self.init_git()
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "seed")
        with mock.patch("crux.harvest.testprox.subprocess.run", side_effect=_no_rg):
            add_test_proximity(signals, hunks, self.root, Config())
        self.assertTrue(signals[hunks[0].id].test_touched)
        self.assertFalse(signals[hunks[1].id].test_touched)

    def test_no_existing_test_dirs_means_untouched(self) -> None:
        _write(self.root, "lib.py", "def frobnicate():\n    pass\n")
        hunk = _hunk("lib.py", 1, 2)
        signals = {hunk.id: HunkSignals(hunk_id=hunk.id, defines=["frobnicate"])}
        add_test_proximity(signals, [hunk], self.root, Config())
        self.assertFalse(signals[hunk.id].test_touched)
        # score is still finalized by this stage
        self.assertGreater(signals[hunk.id].score, 0.0)


class ScoreTests(unittest.TestCase):
    def test_weighted_sum_matches_documented_weights(self) -> None:
        hot = HunkSignals(hunk_id="a.py:1", blast_radius=12, sensitive=["auth"],
                          churn=4, fix_frequency=2, test_touched=False)
        cold = HunkSignals(hunk_id="b.py:1")
        cold.test_touched = True
        signals = {hot.hunk_id: hot, cold.hunk_id: cold}
        compute_scores(signals)
        # blast 12/10 + sensitive 2 + churn pct 2/2 + fixes 2 + untested 1
        self.assertAlmostEqual(hot.score, 1.2 + 2.0 + 1.0 + 2.0 + 1.0)
        self.assertAlmostEqual(cold.score, 0.0)

    def test_blast_component_caps_at_three(self) -> None:
        huge = HunkSignals(hunk_id="a.py:1", blast_radius=500, test_touched=True)
        compute_scores({huge.hunk_id: huge})
        self.assertAlmostEqual(huge.score, 3.0)

    def test_churn_percentile_ranks_within_diff(self) -> None:
        low = HunkSignals(hunk_id="a.py:1", churn=1, test_touched=True)
        high = HunkSignals(hunk_id="b.py:1", churn=9, test_touched=True)
        zero = HunkSignals(hunk_id="c.py:1", churn=0, test_touched=True)
        compute_scores({s.hunk_id: s for s in (low, high, zero)})
        self.assertAlmostEqual(zero.score, 0.0)
        self.assertAlmostEqual(low.score, 2 / 3)
        self.assertAlmostEqual(high.score, 1.0)


if __name__ == "__main__":
    unittest.main()
