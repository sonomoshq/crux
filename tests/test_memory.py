# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.memory (D31) and its analyze/prompt integration.

The store location honors $XDG_CONFIG_HOME, so every test redirects it to a
temp dir — no test touches the real ~/.config/crux.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crux.analyze as analyze
import crux.memory as memory
from crux.models import (
    Annotation,
    Config,
    Memory,
    MemoryRetraction,
    RepoInfo,
    RunState,
    STATE_VERSION,
    state_from_json,
    state_to_json,
)


class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.config_dir = tempfile.mkdtemp()
        self.repo_root = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.config_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.info = RepoInfo(
            root=self.repo_root, branch="feat/x", head_sha="h" * 40,
            base_sha="b" * 40, owner="example-org", repo="crux",
        )

    def anchored_file(self, name: str = "src/thing.py") -> str:
        path = Path(self.repo_root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
        return name


class TestStore(MemoryTestCase):
    def test_load_missing_store_is_empty(self) -> None:
        self.assertEqual(memory.load(self.info), [])

    def test_save_load_round_trip(self) -> None:
        entries = [Memory(id="a" * 8, text="Config keys must land in the "
                          "example file too", anchor="crux.toml.example",
                          source="review", created="2026-07-22")]
        memory.save(self.info, entries)
        self.assertEqual(memory.load(self.info), entries)

    def test_corrupt_store_reads_as_empty(self) -> None:
        path = memory.store_path(self.info)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(memory.load(self.info), [])

    def test_entries_without_text_are_skipped(self) -> None:
        path = memory.store_path(self.info)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{"id": "x", "text": ""}, "not a dict",
                                    {"id": "y", "text": "kept"}]),
                        encoding="utf-8")
        self.assertEqual([m.text for m in memory.load(self.info)], ["kept"])

    def test_memory_id_ignores_case_and_whitespace(self) -> None:
        self.assertEqual(memory.memory_id("Tests  live in tests/"),
                         memory.memory_id("tests live in TESTS/"))


class TestAnchorExists(MemoryTestCase):
    def test_empty_anchor_is_repo_wide_and_passes(self) -> None:
        self.assertTrue(memory.anchor_exists("", self.repo_root))

    def test_line_suffix_is_stripped_before_the_check(self) -> None:
        name = self.anchored_file()
        self.assertTrue(memory.anchor_exists(f"{name}:12", self.repo_root))
        self.assertTrue(memory.anchor_exists(f"{name}:12-30", self.repo_root))

    def test_missing_file_fails(self) -> None:
        self.assertFalse(memory.anchor_exists("no/such.py", self.repo_root))

    def test_escapes_are_rejected(self) -> None:
        self.assertFalse(memory.anchor_exists("../etc/passwd", self.repo_root))
        self.assertFalse(memory.anchor_exists("/etc/passwd", self.repo_root))


class TestAbsorb(MemoryTestCase):
    def proposal(self, text: str, anchor: str = "") -> Memory:
        return Memory(id=memory.memory_id(text), text=text, anchor=anchor)

    def test_absorb_stores_facts_with_id_and_date(self) -> None:
        kept, notes = memory.absorb(
            self.info, [self.proposal("Hooks are sh shims")], Config())
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].source, "review")
        self.assertTrue(kept[0].created)
        self.assertTrue(any("remembered" in n for n in notes))
        self.assertEqual(memory.load(self.info), kept)

    def test_absorb_dedupes_against_the_store(self) -> None:
        memory.absorb(self.info, [self.proposal("Hooks are sh shims")], Config())
        kept, _ = memory.absorb(
            self.info, [self.proposal("hooks are SH shims")], Config())
        self.assertEqual(len(kept), 1)

    def test_proposal_with_missing_anchor_is_dropped(self) -> None:
        kept, notes = memory.absorb(
            self.info, [self.proposal("Invented", "no/such.py")], Config())
        self.assertEqual(kept, [])
        self.assertTrue(any("does not exist" in n for n in notes))

    def test_stored_fact_whose_anchor_vanished_is_forgotten(self) -> None:
        name = self.anchored_file()
        memory.absorb(self.info, [self.proposal("Anchored", name)], Config())
        (Path(self.repo_root) / name).unlink()
        kept, notes = memory.absorb(self.info, [], Config())
        self.assertEqual(kept, [])
        self.assertTrue(any("is gone" in n for n in notes))

    def test_cap_evicts_oldest_review_facts_but_spares_human_ones(self) -> None:
        memory.save(self.info, [
            Memory(id="h" * 8, text="human fact", source="human", created="2026-01-01"),
            Memory(id="r1" * 4, text="old review fact", source="review",
                   created="2026-01-02"),
            Memory(id="r2" * 4, text="new review fact", source="review",
                   created="2026-06-01"),
        ])
        cfg = Config(memory_max=2)
        kept, _ = memory.absorb(self.info, [], cfg)
        self.assertEqual({m.text for m in kept}, {"human fact", "new review fact"})


class TestRetraction(MemoryTestCase):
    """D36: a review retires the facts its PR disproved, so the store stops
    being append-only and `crux memory forget` stops being a chore."""

    def stored(self, text: str, source: str = "review") -> Memory:
        entry = Memory(id=memory.memory_id(text), text=text, source=source,
                       created="2026-01-01")
        memory.save(self.info, memory.load(self.info) + [entry])
        return entry

    def test_review_written_fact_is_retired_with_its_reason(self) -> None:
        stale = self.stored("Hooks are sh shims")
        kept, notes = memory.absorb(
            self.info, [], Config(),
            retract=[MemoryRetraction(id=stale.id, why="the shims are Python now")])
        self.assertEqual(kept, [])
        self.assertEqual(memory.load(self.info), [])
        self.assertTrue(any("contradicts it: the shims are Python now" in n
                            for n in notes))

    def test_human_fact_survives_a_review_retraction(self) -> None:
        pinned = self.stored("Config keys land in the example file", source="human")
        kept, notes = memory.absorb(
            self.info, [], Config(),
            retract=[MemoryRetraction(id=pinned.id, why="I disagree")])
        self.assertEqual([m.id for m in kept], [pinned.id])
        self.assertTrue(any("added by hand" in n for n in notes))

    def test_unknown_id_is_ignored_not_fatal(self) -> None:
        keeper = self.stored("A real fact")
        kept, notes = memory.absorb(
            self.info, [], Config(),
            retract=[MemoryRetraction(id="nope1234", why="hallucinated")])
        self.assertEqual([m.id for m in kept], [keeper.id])
        self.assertTrue(any("no memory with id nope1234" in n for n in notes))

    def test_supersede_retires_the_old_and_stores_the_new(self) -> None:
        old = self.stored("Reviews may add up to 3 facts")
        new_text = "Reviews may add and retire up to 3 facts each"
        kept, _ = memory.absorb(
            self.info, [Memory(id=memory.memory_id(new_text), text=new_text)],
            Config(), retract=[MemoryRetraction(id=old.id, why="the cap is two-way now")])
        self.assertEqual([m.text for m in kept], [new_text])

    def test_retraction_without_a_reason_still_works(self) -> None:
        stale = self.stored("Something outdated")
        kept, notes = memory.absorb(self.info, [], Config(),
                                    retract=[MemoryRetraction(id=stale.id)])
        self.assertEqual(kept, [])
        self.assertTrue(any("gave no reason" in n for n in notes))

    def test_absorb_without_retractions_is_unchanged(self) -> None:
        keeper = self.stored("Still true")
        kept, _ = memory.absorb(self.info, [], Config())
        self.assertEqual([m.id for m in kept], [keeper.id])


class TestAddForgetClear(MemoryTestCase):
    def test_add_and_duplicate_add(self) -> None:
        new = memory.add(self.info, "Tests mock every subprocess")
        self.assertIsNotNone(new)
        self.assertEqual(new.source, "human")
        self.assertIsNone(memory.add(self.info, "tests MOCK every subprocess"))
        self.assertEqual(len(memory.load(self.info)), 1)

    def test_forget_reports_missing_ids(self) -> None:
        new = memory.add(self.info, "A fact")
        removed, missing = memory.forget(self.info, [new.id, "nope1234"])
        self.assertEqual(removed, 1)
        self.assertEqual(missing, ["nope1234"])
        self.assertEqual(memory.load(self.info), [])

    def test_clear(self) -> None:
        memory.add(self.info, "A fact")
        self.assertEqual(memory.clear(self.info), 1)
        self.assertEqual(memory.load(self.info), [])


class TestAnalyzeIntegration(MemoryTestCase):
    def test_prompt_lists_remembered_facts(self) -> None:
        prompt = analyze.build_prompt(
            [], [], [], {}, None, {}, [],
            memories=[Memory(id="abc12345", text="Prompts live in crux/prompts")])
        self.assertIn("[abc12345] Prompts live in crux/prompts", prompt)

    def test_prompt_without_memories_says_so(self) -> None:
        prompt = analyze.build_prompt([], [], [], {}, None, {}, [])
        self.assertIn("nothing remembered about this repo yet", prompt)

    def test_coerce_caps_and_cleans_proposals(self) -> None:
        data = {"memories": [
            {"text": "fact one", "anchor": "a.py"},
            {"text": "  "},                      # empty text: dropped
            "not a dict",                        # dropped
            {"text": "fact two"},
            {"text": "fact three"},
            {"text": "fact four"},               # over the cap of 3
        ]}
        annotation = analyze._coerce(data, [], Config())
        self.assertEqual([m.text for m in annotation.memories],
                         ["fact one", "fact two", "fact three"])
        self.assertEqual(annotation.memories[0].anchor, "a.py")
        self.assertEqual(annotation.memories[0].source, "review")

    def test_coerce_caps_and_cleans_retractions(self) -> None:
        data = {"forget": [
            {"id": "aaaa1111", "why": "the convention changed"},
            {"why": "no id at all"},              # dropped
            "not a dict",                         # dropped
            {"id": "bbbb2222"},                   # reason optional
            {"id": "cccc3333", "why": "x"},
            {"id": "dddd4444", "why": "over the cap of 3"},
        ]}
        annotation = analyze._coerce(data, [], Config())
        self.assertEqual([r.id for r in annotation.forget_memories],
                         ["aaaa1111", "bbbb2222", "cccc3333"])
        self.assertEqual(annotation.forget_memories[0].why, "the convention changed")
        self.assertEqual(annotation.forget_memories[1].why, "")

    def test_prompt_asks_for_retirement_by_id(self) -> None:
        prompt = analyze.build_prompt([], [], [], {}, None, {}, [])
        self.assertIn('"forget"', prompt)
        self.assertIn("is now FALSE", prompt)

    def test_run_state_round_trips_memories(self) -> None:
        state = RunState(
            version=STATE_VERSION, branch="feat/x", base_sha="b" * 40,
            head_sha="h" * 40, pr_number=1, fingerprints={},
            annotation=Annotation(summary="s", memories=[
                Memory(id="abc12345", text="fact", anchor="a.py",
                       source="review", created="2026-07-22")]),
        )
        restored = state_from_json(state_to_json(state))
        self.assertEqual(restored.annotation.memories,
                         state.annotation.memories)


if __name__ == "__main__":
    unittest.main()
