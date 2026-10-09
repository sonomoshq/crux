# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.harvest.defuse — extract_defs_uses()."""
from __future__ import annotations

import unittest

from crux.harvest.defuse import extract_defs_uses
from crux.models import Hunk, HunkSignals


def mk(file: str, start: int, added: list[str] | None = None,
       removed: list[str] | None = None) -> Hunk:
    added = added or []
    removed = removed or []
    lines = [f"@@ -{start},{max(len(removed), 1)} +{start},{max(len(added), 1)} @@"]
    lines += ["-" + l for l in removed]
    lines += ["+" + l for l in added]
    return Hunk(
        id=f"{file}:{start}",
        file=file,
        old_start=start,
        old_count=len(removed),
        new_start=start,
        new_count=len(added),
        patch="\n".join(lines),
    )


def defines_of(added: list[str]) -> list[str]:
    h = mk("x.src", 1, added=added)
    return extract_defs_uses([h])[h.id].defines


class TestDefines(unittest.TestCase):
    def test_python(self) -> None:
        self.assertEqual(defines_of(["def flush(self):"]), ["flush"])
        self.assertEqual(defines_of(["async def poll():"]), ["poll"])
        self.assertEqual(defines_of(["class WriteBuffer(Base):"]), ["WriteBuffer"])
        self.assertEqual(defines_of(["type Alias = list[int]"]), ["Alias"])

    def test_js_ts(self) -> None:
        self.assertEqual(defines_of(["function render(props) {"]), ["render"])
        self.assertEqual(defines_of(["export default function App() {"]), ["App"])
        self.assertEqual(defines_of(["function* gen() {"]), ["gen"])
        self.assertEqual(defines_of(["const handler = async () => {}"]), ["handler"])
        self.assertEqual(defines_of(["let count = 0;"]), ["count"])
        self.assertEqual(defines_of(["var legacy = true;"]), ["legacy"])
        self.assertEqual(defines_of(["const port: number = 8080;"]), ["port"])
        self.assertEqual(defines_of(["export interface Shape {"]), ["Shape"])
        self.assertEqual(defines_of(["export type Point = { x: number };"]), ["Point"])

    def test_go(self) -> None:
        self.assertEqual(defines_of(["func Standalone(n int) error {"]), ["Standalone"])
        self.assertEqual(
            defines_of(["func (s *Server) Handle(w http.ResponseWriter) {"]),
            ["Handle"],
        )
        self.assertEqual(defines_of(["type Config struct {"]), ["Config"])

    def test_rust(self) -> None:
        self.assertEqual(defines_of(["fn helper() -> u32 {"]), ["helper"])
        self.assertEqual(defines_of(["pub fn compute(x: u32) -> u32 {"]), ["compute"])
        self.assertEqual(defines_of(["pub(crate) fn scoped() {"]), ["scoped"])
        self.assertEqual(defines_of(['extern "C" fn callback() {}']), ["callback"])

    def test_non_definitions_ignored(self) -> None:
        self.assertEqual(defines_of(["for (let i = 0; i < n; i++) {"]), [])
        self.assertEqual(defines_of(["defer func() {"]), [])
        self.assertEqual(defines_of(["result = compute(x)"]), [])
        self.assertEqual(defines_of(["# def phantom():"]), [])
        self.assertEqual(defines_of(["// function ghost() {"]), [])

    def test_defines_sorted_and_unique(self) -> None:
        defs = defines_of(["def beta():", "def alpha():", "def beta():"])
        self.assertEqual(defs, ["alpha", "beta"])


class TestUses(unittest.TestCase):
    def test_cross_hunk_use(self) -> None:
        a = mk("src/buffer.py", 1, added=["class WriteBuffer:", "    def add(self, e):"])
        b = mk("src/events.py", 40, added=["buf = WriteBuffer(size)"])
        signals = extract_defs_uses([a, b])
        self.assertEqual(signals[b.id].uses, ["WriteBuffer"])
        # 'buf' and 'size' are not defined by any other hunk.
        self.assertNotIn("buf", signals[b.id].uses)
        self.assertEqual(signals[a.id].uses, [])

    def test_self_defined_symbol_is_not_a_use(self) -> None:
        h = mk("src/a.py", 1, added=["def flush():", "    flush()"])
        signals = extract_defs_uses([h])
        self.assertEqual(signals[h.id].defines, ["flush"])
        self.assertEqual(signals[h.id].uses, [])

    def test_symbol_defined_in_two_hunks_links_both(self) -> None:
        # Rename threading: both hunks re-declare the same name.
        a = mk("src/a.py", 1, added=["def setup():", "    init_db()"])
        b = mk("src/b.py", 1, added=["def init_db():"])
        signals = extract_defs_uses([a, b])
        self.assertEqual(signals[a.id].uses, ["init_db"])

    def test_use_in_comment_does_not_count(self) -> None:
        a = mk("src/a.py", 1, added=["def flush():"])
        b = mk("src/b.py", 1, added=["x = 1  # calls flush eventually"])
        signals = extract_defs_uses([a, b])
        self.assertEqual(signals[b.id].uses, [])

    def test_every_hunk_gets_an_entry(self) -> None:
        hunks = [
            mk("src/a.py", 1, added=["def foo():"]),
            mk("src/gone.py", 9, removed=["obsolete = True"]),  # pure deletion
            mk("src/blank.py", 2, added=[""]),
        ]
        signals = extract_defs_uses(hunks)
        self.assertEqual(set(signals), {h.id for h in hunks})
        for h in hunks:
            self.assertIsInstance(signals[h.id], HunkSignals)
            self.assertEqual(signals[h.id].hunk_id, h.id)
        self.assertEqual(signals["src/gone.py:9"].defines, [])
        self.assertEqual(signals["src/gone.py:9"].uses, [])

    def test_uses_sorted(self) -> None:
        a = mk("src/a.py", 1, added=["def zeta():", "def alpha():"])
        b = mk("src/b.py", 1, added=["zeta(); alpha()"])
        signals = extract_defs_uses([a, b])
        self.assertEqual(signals[b.id].uses, ["alpha", "zeta"])

    def test_empty_input(self) -> None:
        self.assertEqual(extract_defs_uses([]), {})


if __name__ == "__main__":
    unittest.main()
