# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.config and crux.gate. Pure stdlib; no subprocess, no network."""
from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from crux import config, gate
from crux.models import Config, CruxError, GateDecision, Hunk, HunkClass, HunkSignals

ROOT = Path(__file__).resolve().parent.parent


def write_toml(root: Path, text: str) -> None:
    (root / "crux.toml").write_text(text, encoding="utf-8")


def make_hunk(
    file: str,
    added: list[str] | None = None,
    removed: list[str] | None = None,
    klass: HunkClass = HunkClass.BEHAVIORAL,
    new_start: int = 1,
) -> Hunk:
    added = added or []
    removed = removed or []
    patch_lines = ["@@ -%d,%d +%d,%d @@" % (new_start, len(removed), new_start, len(added))]
    patch_lines += ["-" + l for l in removed]
    patch_lines += ["+" + l for l in added]
    return Hunk(
        id=f"{file}:{new_start}",
        file=file,
        old_start=new_start,
        old_count=len(removed),
        new_start=new_start,
        new_count=len(added),
        patch="\n".join(patch_lines),
        klass=klass,
    )


class ConfigLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # Point the global-config layer inside the temp dir so a real
        # ~/.config/crux/crux.toml on this machine never leaks into tests.
        self.xdg = self.root / "xdg"
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)})
        env.start()
        self.addCleanup(env.stop)

    def write_global(self, text: str) -> None:
        (self.xdg / "crux").mkdir(parents=True, exist_ok=True)
        (self.xdg / "crux" / "crux.toml").write_text(text, encoding="utf-8")

    def test_missing_file_gives_pure_defaults(self) -> None:
        cfg = config.load(self.root)
        self.assertEqual(cfg, Config())

    def test_global_config_applies_to_any_repo(self) -> None:
        # Slack set once machine-wide (D16) works without a repo crux.toml.
        self.write_global('[slack]\nchannel = "pull-requests"\n')
        cfg = config.load(self.root)
        self.assertEqual(cfg.slack_channel, "pull-requests")

    def test_repo_config_overrides_global_key_by_key(self) -> None:
        self.write_global(
            '[slack]\nchannel = "pull-requests"\ntoken_env = "GLOBAL_TOKEN"\n')
        write_toml(self.root, '[slack]\nchannel = "my-repo-prs"\n')
        cfg = config.load(self.root)
        self.assertEqual(cfg.slack_channel, "my-repo-prs")  # repo wins
        self.assertEqual(cfg.slack_token_env, "GLOBAL_TOKEN")  # inherited

    def test_repo_can_disable_global_slack(self) -> None:
        self.write_global('[slack]\nchannel = "pull-requests"\n')
        write_toml(self.root, '[slack]\nchannel = ""\n')
        cfg = config.load(self.root)
        self.assertEqual(cfg.slack_channel, "")

    def test_malformed_global_toml_raises_crux_error(self) -> None:
        self.write_global("not [valid toml")
        with self.assertRaises(CruxError):
            config.load(self.root)

    def test_all_mapped_keys(self) -> None:
        write_toml(self.root, """
[scope]
owners = ["acme", "other"]

[gate]
min_behavioral_lines = 10
max_blast = 3

[sensitivity]
paths = ["vault"]
keywords = ["hunter2"]

[history]
window_days = 30
co_change_threshold = 0.5

[llm]
model = "opus"
timeout = 120

[pr]
default_base = "develop"
auto_create = true

[slack]
channel = "prs"
token_env = "MY_SLACK_TOKEN"
""")
        cfg = config.load(self.root)
        self.assertEqual(cfg.scope_owners, ["acme", "other"])
        self.assertEqual(cfg.gate_min_behavioral_lines, 10)
        self.assertEqual(cfg.gate_max_blast, 3)
        self.assertEqual(cfg.sensitive_paths, ["vault"])
        self.assertEqual(cfg.sensitive_keywords, ["hunter2"])
        self.assertEqual(cfg.history_window_days, 30)
        self.assertEqual(cfg.co_change_threshold, 0.5)
        self.assertEqual(cfg.llm_model, "opus")
        self.assertEqual(cfg.llm_timeout, 120)
        self.assertEqual(cfg.pr_default_base, "develop")
        self.assertTrue(cfg.pr_auto_create)
        self.assertEqual(cfg.slack_channel, "prs")
        self.assertEqual(cfg.slack_token_env, "MY_SLACK_TOKEN")
        # unmapped Config fields keep their defaults
        self.assertEqual(cfg.dependency_files, Config().dependency_files)
        self.assertEqual(cfg.test_dirs, Config().test_dirs)

    def test_standards_mapping(self) -> None:
        """D14: [standards] rules -> standards_rules, standards_max -> standards_max."""
        write_toml(self.root, """
[standards]
rules = ["Prefer composition over inheritance", "No new singletons"]
standards_max = 3
""")
        cfg = config.load(self.root)
        self.assertEqual(cfg.standards_rules,
                         ["Prefer composition over inheritance", "No new singletons"])
        self.assertEqual(cfg.standards_max, 3)

    def test_standards_defaults_and_bad_types(self) -> None:
        write_toml(self.root, """
[standards]
rules = "not-a-list"
standards_max = true
""")
        cfg = config.load(self.root)
        self.assertEqual(cfg.standards_rules, [])
        self.assertEqual(cfg.standards_max, 5)

    def test_unknown_tables_and_keys_ignored(self) -> None:
        write_toml(self.root, """
[gate]
min_behavioral_lines = 25
frobnicate = true

[mystery]
answer = 42
""")
        cfg = config.load(self.root)
        self.assertEqual(cfg.gate_min_behavioral_lines, 25)
        self.assertEqual(cfg.gate_max_blast, Config().gate_max_blast)
        self.assertFalse(hasattr(cfg, "frobnicate"))

    def test_wrong_types_fall_back_to_defaults(self) -> None:
        write_toml(self.root, """
[gate]
min_behavioral_lines = "lots"
max_blast = true

[scope]
owners = "not-a-list"

[history]
co_change_threshold = 1
""")
        cfg = config.load(self.root)
        self.assertEqual(cfg.gate_min_behavioral_lines, Config().gate_min_behavioral_lines)
        self.assertEqual(cfg.gate_max_blast, Config().gate_max_blast)
        self.assertEqual(cfg.scope_owners, Config().scope_owners)
        # int is acceptable where the default is a float
        self.assertEqual(cfg.co_change_threshold, 1.0)
        self.assertIsInstance(cfg.co_change_threshold, float)

    def test_malformed_toml_raises_crux_error(self) -> None:
        write_toml(self.root, "[gate\nmin_behavioral_lines = 10")
        with self.assertRaises(CruxError):
            config.load(self.root)

    def test_example_file_parses_and_maps_cleanly(self) -> None:
        """crux.toml.example must stay consistent with config.load's mapping."""
        example = ROOT / "crux.toml.example"
        text = example.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        # every table and key in the example is one config.load understands
        for table_name, table in data.items():
            self.assertIn(table_name, config._MAPPING, f"unknown table [{table_name}]")
            for key in table:
                self.assertIn(
                    key, config._MAPPING[table_name],
                    f"unmapped key {key!r} in [{table_name}]",
                )
        # and loading it applies every value (types all match the defaults)
        write_toml(self.root, text)
        cfg = config.load(self.root)
        self.assertEqual(cfg.scope_owners, data["scope"]["owners"])
        self.assertEqual(cfg.gate_min_behavioral_lines, data["gate"]["min_behavioral_lines"])
        self.assertEqual(cfg.gate_max_blast, data["gate"]["max_blast"])
        self.assertEqual(cfg.sensitive_paths, data["sensitivity"]["paths"])
        self.assertEqual(cfg.sensitive_keywords, data["sensitivity"]["keywords"])
        self.assertEqual(cfg.history_window_days, data["history"]["window_days"])
        self.assertEqual(cfg.co_change_threshold, data["history"]["co_change_threshold"])
        self.assertEqual(cfg.llm_model, data["llm"]["model"])
        self.assertEqual(cfg.llm_timeout, data["llm"]["timeout"])
        self.assertEqual(cfg.pr_default_base, data["pr"]["default_base"])
        self.assertEqual(cfg.standards_rules, data["standards"]["rules"])
        self.assertEqual(cfg.standards_max, data["standards"]["standards_max"])


class GateDecideTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()

    def signals_for(self, hunks: list[Hunk]) -> dict[str, HunkSignals]:
        return {h.id: HunkSignals(hunk_id=h.id) for h in hunks}

    def test_small_clean_diff_skips(self) -> None:
        hunks = [make_hunk("src/util.py", added=["x = 1", "y = 2"], removed=["x = 0"])]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertTrue(decision.skip)
        self.assertTrue(decision.reasons)  # skip reason is logged, never silent
        self.assertEqual(decision.stats["behavioral_lines"], 3)
        self.assertEqual(decision.stats["total_lines"], 3)

    def test_behavioral_line_threshold_blocks_skip(self) -> None:
        hunks = [make_hunk("src/big.py", added=[f"line{i}" for i in range(50)])]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertFalse(decision.skip)
        self.assertTrue(any("behavioral lines 50" in r for r in decision.reasons))

    def test_non_behavioral_lines_do_not_count(self) -> None:
        hunks = [
            make_hunk("src/gen.lock", added=[f"dep{i}" for i in range(200)],
                      klass=HunkClass.GENERATED),
            make_hunk("src/fmt.py", added=["a = 1 "], removed=["a = 1"],
                      klass=HunkClass.COSMETIC),
            make_hunk("src/small.py", added=["real change"]),
        ]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertEqual(decision.stats["behavioral_lines"], 1)
        self.assertEqual(decision.stats["total_lines"], 203)
        self.assertTrue(decision.skip)

    def test_sensitive_path_blocks_skip_and_tags_signals(self) -> None:
        hunks = [make_hunk("src/auth/login.py", added=["return True"])]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertFalse(decision.skip)
        self.assertIn("path:auth", signals[hunks[0].id].sensitive)
        self.assertTrue(any("sensitive" in r for r in decision.reasons))

    def test_sensitive_keyword_matches_added_lines_case_insensitively(self) -> None:
        hunks = [make_hunk("src/db.py", added=['cur.execute("drop table users")'])]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertFalse(decision.skip)
        self.assertIn("keyword:DROP TABLE", signals[hunks[0].id].sensitive)

    def test_sensitive_keyword_ignores_removed_lines(self) -> None:
        hunks = [make_hunk("src/db.py", removed=["password = input()"],
                           added=["value = input()"])]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertEqual(signals[hunks[0].id].sensitive, [])
        self.assertTrue(decision.skip)

    def test_tagging_creates_missing_signal_entries(self) -> None:
        hunks = [make_hunk("billing/invoice.py", added=["charge()"])]
        signals: dict[str, HunkSignals] = {}
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertIn(hunks[0].id, signals)
        self.assertIn("path:billing", signals[hunks[0].id].sensitive)
        self.assertFalse(decision.skip)

    def test_tagging_is_idempotent(self) -> None:
        hunks = [make_hunk("src/auth/login.py", added=["token = mint()"])]
        signals = self.signals_for(hunks)
        gate.decide(hunks, signals, self.cfg)
        gate.decide(hunks, signals, self.cfg)
        sens = signals[hunks[0].id].sensitive
        self.assertEqual(len(sens), len(set(sens)))
        self.assertEqual(sorted(set(sens)), sorted(["path:auth", "keyword:token"]))

    def test_dependency_file_blocks_skip(self) -> None:
        hunks = [make_hunk("requirements.txt", added=["requests==2.32.0"],
                           klass=HunkClass.GENERATED)]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertFalse(decision.skip)
        self.assertTrue(any("requirements.txt" in r for r in decision.reasons))

    def test_nested_dependency_file_matches_by_basename(self) -> None:
        hunks = [make_hunk("services/api/package-lock.json", added=["{}"],
                           klass=HunkClass.GENERATED)]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertFalse(decision.skip)

    def test_blast_radius_blocks_skip(self) -> None:
        hunks = [make_hunk("src/core.py", added=["def widely_used(): pass"])]
        signals = self.signals_for(hunks)
        signals[hunks[0].id].blast_radius = 5
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertFalse(decision.skip)
        self.assertTrue(any("blast" in r for r in decision.reasons))

    def test_blast_radius_below_threshold_skips(self) -> None:
        hunks = [make_hunk("src/core.py", added=["def local(): pass"])]
        signals = self.signals_for(hunks)
        signals[hunks[0].id].blast_radius = 4
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertTrue(decision.skip)

    def test_stats_contract_keys(self) -> None:
        hunks = [
            make_hunk("src/auth/login.py", added=["a", "b"]),
            make_hunk("src/other.py", added=["c"]),
        ]
        signals = self.signals_for(hunks)
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertIsInstance(decision, GateDecision)
        for key in ("behavioral_lines", "total_lines", "red_estimate", "files"):
            self.assertIn(key, decision.stats)
        self.assertEqual(decision.stats["files"],
                         ["src/auth/login.py", "src/other.py"])
        # only the sensitive hunk's 2 changed lines are red-estimated
        self.assertEqual(decision.stats["red_estimate"], 2)

    def test_red_estimate_counts_workflow_and_high_blast(self) -> None:
        hunks = [
            make_hunk(".github/workflows/ci.yml", added=["- run: make"]),
            make_hunk("src/core.py", added=["def f(): pass", "f()"]),
            make_hunk("src/quiet.py", added=["pass"]),
        ]
        signals = self.signals_for(hunks)
        signals[hunks[1].id].blast_radius = 9
        decision = gate.decide(hunks, signals, self.cfg)
        self.assertEqual(decision.stats["red_estimate"], 3)  # 1 workflow + 2 blast

    def test_empty_diff_skips(self) -> None:
        decision = gate.decide([], {}, self.cfg)
        self.assertTrue(decision.skip)
        self.assertEqual(decision.stats["behavioral_lines"], 0)
        self.assertEqual(decision.stats["total_lines"], 0)
        self.assertEqual(decision.stats["files"], [])
        self.assertEqual(decision.stats["red_estimate"], 0)


if __name__ == "__main__":
    unittest.main()
