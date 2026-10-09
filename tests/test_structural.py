# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.harvest.structural — classify() and mechanical_clusters()."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from crux.harvest.structural import classify, mechanical_clusters, strip_comment
from crux.models import Config, Hunk, HunkClass


def mk(file: str, start: int, added: list[str] | None = None,
       removed: list[str] | None = None, context: list[str] | None = None) -> Hunk:
    added = added or []
    removed = removed or []
    lines = [f"@@ -{start},{max(len(removed), 1)} +{start},{max(len(added), 1)} @@"]
    lines += [" " + l for l in (context or [])]
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


def classify_no_difft(hunks: list[Hunk], cfg: Config | None = None) -> None:
    with mock.patch("crux.harvest.structural.shutil.which", return_value=None):
        classify(hunks, cfg or Config())


class TestStripComment(unittest.TestCase):
    def test_whole_line_variants(self) -> None:
        for line in ["# hey", "  // hey", "/* open", " */", " * jsdoc", "<!-- html", "-- sql"]:
            self.assertEqual(strip_comment(line), "", line)

    def test_code_not_treated_as_comment(self) -> None:
        self.assertEqual(strip_comment("*args,"), "*args,")
        self.assertEqual(strip_comment("--x"), "--x")

    def test_trailing_comment_cut(self) -> None:
        self.assertEqual(strip_comment("x = 1  # note").rstrip(), "x = 1")
        self.assertEqual(strip_comment("x = 1; // note").rstrip(), "x = 1;")

    def test_markers_inside_strings_survive(self) -> None:
        self.assertEqual(strip_comment('s = "a#b"'), 's = "a#b"')
        self.assertEqual(strip_comment('u = "http://x.io"'), 'u = "http://x.io"')


class TestGenerated(unittest.TestCase):
    def test_dependency_files(self) -> None:
        hunks = [
            mk("package-lock.json", 1, added=["x"]),
            mk("frontend/package-lock.json", 1, added=["x"]),
            mk("requirements.txt", 1, added=["flask==3.0"]),
        ]
        classify_no_difft(hunks)
        for h in hunks:
            self.assertEqual(h.klass, HunkClass.GENERATED, h.file)

    def test_generated_globs(self) -> None:
        hunks = [
            mk("app.min.js", 1, added=["x"]),
            mk("proto/schema_pb2.py", 1, added=["x"]),
            mk("dist/bundle.js", 1, added=["x"]),
            mk("web/dist/chunk.js", 1, added=["x"]),  # dir glob at any depth
            mk("api.generated.ts", 1, added=["x"]),
        ]
        classify_no_difft(hunks)
        for h in hunks:
            self.assertEqual(h.klass, HunkClass.GENERATED, h.file)

    def test_dir_glob_needs_path_segment(self) -> None:
        h = mk("src/distribution/x.py", 1, added=["y = 2"], removed=["y = 1"])
        classify_no_difft([h])
        self.assertEqual(h.klass, HunkClass.BEHAVIORAL)


class TestCosmeticRegexPath(unittest.TestCase):
    def test_whitespace_reformat(self) -> None:
        h = mk("src/a.py", 3, added=["x = 1"], removed=["x=1"])
        classify_no_difft([h])
        self.assertEqual(h.klass, HunkClass.COSMETIC)

    def test_line_rewrap(self) -> None:
        h = mk("src/a.py", 3, added=["r = foo(a, b, c)"],
               removed=["r = foo(a, b,", "        c)"])
        classify_no_difft([h])
        self.assertEqual(h.klass, HunkClass.COSMETIC)

    def test_comment_only_addition(self) -> None:
        h = mk("src/a.py", 3, added=["# explains the invariant", ""],
               context=["x = 1"])
        classify_no_difft([h])
        self.assertEqual(h.klass, HunkClass.COSMETIC)

    def test_comment_rewording(self) -> None:
        h = mk("src/a.js", 3, added=["// new note"], removed=["// old note"])
        classify_no_difft([h])
        self.assertEqual(h.klass, HunkClass.COSMETIC)

    def test_real_change_is_behavioral(self) -> None:
        h = mk("src/a.py", 3, added=["x = 2"], removed=["x = 1"])
        classify_no_difft([h])
        self.assertEqual(h.klass, HunkClass.BEHAVIORAL)

    def test_hash_inside_string_is_code(self) -> None:
        h = mk("src/a.py", 3, added=['s = "a#c"'], removed=['s = "a#b"'])
        classify_no_difft([h])
        self.assertEqual(h.klass, HunkClass.BEHAVIORAL)

    def test_pure_code_addition_not_cosmetic(self) -> None:
        h = mk("src/a.py", 3, added=["x = 1"])
        classify_no_difft([h])
        self.assertEqual(h.klass, HunkClass.BEHAVIORAL)


class TestMechanical(unittest.TestCase):
    def _cluster_hunks(self) -> list[Hunk]:
        return [
            mk("src/a.py", 10, added=["buffer.add(event)"], removed=["db.insert(event)"]),
            mk("src/b.py", 20, added=["buffer.add(event)"], removed=["db.insert(event)"]),
            mk("src/c.py", 30, added=["buffer.add(event)"], removed=["db.insert(event)"]),
        ]

    def test_cluster_of_three_across_files(self) -> None:
        hunks = self._cluster_hunks()
        clusters = mechanical_clusters(hunks)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0]), sorted(h.id for h in hunks))
        classify_no_difft(hunks)
        for h in hunks:
            self.assertEqual(h.klass, HunkClass.MECHANICAL, h.id)

    def test_whitespace_differences_still_cluster(self) -> None:
        hunks = self._cluster_hunks()
        hunks[1].patch = hunks[1].patch.replace("+buffer.add(event)",
                                                "+  buffer.add( event )")
        self.assertEqual(len(mechanical_clusters(hunks)), 1)

    def test_two_hunks_do_not_cluster(self) -> None:
        hunks = self._cluster_hunks()[:2]
        self.assertEqual(mechanical_clusters(hunks), [])
        classify_no_difft(hunks)
        for h in hunks:
            self.assertEqual(h.klass, HunkClass.BEHAVIORAL)

    def test_same_file_only_does_not_cluster(self) -> None:
        hunks = [
            mk("src/a.py", n, added=["buffer.add(event)"], removed=["db.insert(event)"])
            for n in (10, 20, 30)
        ]
        self.assertEqual(mechanical_clusters(hunks), [])

    def test_comment_only_hunks_never_cluster(self) -> None:
        hunks = [mk(f, 1, added=["# same comment"]) for f in ("a.py", "b.py", "c.py")]
        self.assertEqual(mechanical_clusters(hunks), [])

    def test_generated_wins_over_mechanical(self) -> None:
        hunks = self._cluster_hunks()
        hunks.append(mk("dist/gen.js", 5, added=["buffer.add(event)"],
                        removed=["db.insert(event)"]))
        classify_no_difft(hunks)
        self.assertEqual(hunks[3].klass, HunkClass.GENERATED)
        for h in hunks[:3]:
            self.assertEqual(h.klass, HunkClass.MECHANICAL)

    def test_cosmetic_wins_over_mechanical(self) -> None:
        hunks = [
            mk(f, 1, added=["x = 1"], removed=["x=1"])
            for f in ("src/a.py", "src/b.py", "src/c.py")
        ]
        classify_no_difft(hunks)
        for h in hunks:
            self.assertEqual(h.klass, HunkClass.COSMETIC)


class TestDifftPath(unittest.TestCase):
    """difft is authoritative when it runs; any failure falls back to regex."""

    def _run(self, hunk: Hunk, returncode: int | None = None,
             side_effect: Exception | None = None) -> mock.Mock:
        with mock.patch("crux.harvest.structural.shutil.which",
                        return_value="/usr/bin/difft"), \
             mock.patch("crux.harvest.structural.subprocess.run") as run:
            if side_effect is not None:
                run.side_effect = side_effect
            else:
                run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=returncode or 0)
            classify([hunk], Config())
        return run

    def test_exit_zero_means_cosmetic(self) -> None:
        # difft can prove cosmetic-ness the regex path cannot.
        h = mk("src/a.py", 1, added=["x = (1)"], removed=["x = 1"])
        run = self._run(h, returncode=0)
        self.assertEqual(h.klass, HunkClass.COSMETIC)
        argv = run.call_args[0][0]
        self.assertIsInstance(argv, list)  # never shell=True
        self.assertEqual(argv[0], "/usr/bin/difft")
        self.assertTrue(argv[-1].endswith(".py") and argv[-2].endswith(".py"))

    def test_exit_one_overrides_regex_cosmetic(self) -> None:
        # Regex would call this whitespace-only; difft says syntactic change.
        h = mk("src/a.py", 1, added=["x = 1"], removed=["x=1"])
        self._run(h, returncode=1)
        self.assertEqual(h.klass, HunkClass.BEHAVIORAL)

    def test_usage_error_falls_back_to_regex(self) -> None:
        h = mk("src/a.py", 1, added=["x = 1"], removed=["x=1"])
        self._run(h, returncode=2)
        self.assertEqual(h.klass, HunkClass.COSMETIC)

    def test_timeout_falls_back_to_regex(self) -> None:
        h = mk("src/a.py", 1, added=["x = 1"], removed=["x=1"])
        self._run(h, side_effect=subprocess.TimeoutExpired(cmd=["difft"], timeout=10))
        self.assertEqual(h.klass, HunkClass.COSMETIC)

    def test_missing_binary_never_spawns(self) -> None:
        h = mk("src/a.py", 1, added=["x = 1"], removed=["x=1"])
        with mock.patch("crux.harvest.structural.shutil.which", return_value=None), \
             mock.patch("crux.harvest.structural.subprocess.run") as run:
            classify([h], Config())
        run.assert_not_called()
        self.assertEqual(h.klass, HunkClass.COSMETIC)

    def test_generated_skips_difft(self) -> None:
        h = mk("package-lock.json", 1, added=["x"], removed=["y"])
        with mock.patch("crux.harvest.structural.shutil.which",
                        return_value="/usr/bin/difft"), \
             mock.patch("crux.harvest.structural.subprocess.run") as run:
            classify([h], Config())
        run.assert_not_called()
        self.assertEqual(h.klass, HunkClass.GENERATED)


if __name__ == "__main__":
    unittest.main()
