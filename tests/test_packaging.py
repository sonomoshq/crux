# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Packaging regression tests.

Crux's first live run failed because ``prompts/analyze.md`` lived outside the
``crux`` package and was never shipped: every non-editable install (pipx, plain
pip) raised "prompt template missing". These tests pin the fix: the template
must load package-relatively and must land inside a built wheel.

The wheel test invokes the setuptools build backend directly (no pip, no
network, no build isolation) on a clean temp copy of the source tree, so a
stale local ``build/`` directory can't poison the result.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from crux import analyze, commitmsg

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    import setuptools  # noqa: F401  (only checking availability)
    _HAVE_SETUPTOOLS = True
except ImportError:
    _HAVE_SETUPTOOLS = False


class TestPromptTemplate(unittest.TestCase):
    def test_prompt_resolves_inside_the_package(self):
        # importlib.resources works for editable, pipx, and wheel installs
        # alike; a repo-relative path only works from a checkout.
        text = analyze.PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("{dag_section}", text)

    def test_prompt_file_lives_under_the_package_dir(self):
        # The template must sit inside crux/ or package-data can't ship it.
        self.assertTrue((REPO_ROOT / "crux" / "prompts" / "analyze.md").is_file())

    def test_commit_prompt_resolves_inside_the_package(self):
        text = commitmsg.PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("{diff}", text)
        self.assertTrue(
            (REPO_ROOT / "crux" / "prompts" / "commit.md").is_file())


@unittest.skipUnless(_HAVE_SETUPTOOLS, "setuptools not importable")
class TestWheelContents(unittest.TestCase):
    def test_wheel_ships_the_prompt_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            shutil.copy(REPO_ROOT / "pyproject.toml", src)
            shutil.copytree(
                REPO_ROOT / "crux", src / "crux",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            out = Path(tmp) / "dist"
            out.mkdir()
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import sys; from setuptools import build_meta; "
                 "print(build_meta.build_wheel(sys.argv[1]))",
                 str(out)],
                cwd=src, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            wheel = out / proc.stdout.strip().splitlines()[-1]
            with zipfile.ZipFile(wheel) as zf:
                names = zf.namelist()
            self.assertIn("crux/prompts/analyze.md", names,
                          "prompt template missing from the wheel — check "
                          "[tool.setuptools.package-data] in pyproject.toml")
            self.assertIn("crux/prompts/commit.md", names,
                          "commit prompt (D27) missing from the wheel")
            # The crux.harvest subpackage must ship too: a packages.find
            # include of "crux" (no wildcard) drops it, and every `crux run`
            # then crashes at `import crux.harvest.blast` after a push.
            self.assertIn("crux/harvest/blast.py", names,
                          "crux.harvest subpackage missing from the wheel — "
                          "check [tool.setuptools.packages.find] include")


if __name__ == "__main__":
    unittest.main()
