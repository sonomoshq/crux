# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Guard: every text-mode subprocess call in crux/ pins UTF-8.

`text=True` decodes with the locale encoding — cp1252 on native Windows — so a
card full of emoji piped through `gh` (or any non-ASCII git/claude output)
raises UnicodeDecodeError there. Every text-reading subprocess call must pass
`encoding="utf-8"` instead. This AST walk fails if a `text=True` sneaks back
in, keeping Crux decodable identically on Windows and POSIX.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

CRUX_DIR = Path(__file__).resolve().parent.parent / "crux"


def _subprocess_calls(tree: ast.AST):
    """Yield ast.Call nodes that are subprocess.run(...) / subprocess.Popen(...)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and func.attr in ("run", "Popen")
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"):
            yield node


class TestSubprocessEncodingPinned(unittest.TestCase):
    def test_no_text_true_subprocess_calls(self) -> None:
        offenders: list[str] = []
        for path in sorted(CRUX_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for call in _subprocess_calls(tree):
                for kw in call.keywords:
                    if (kw.arg == "text"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value is True):
                        rel = path.relative_to(CRUX_DIR.parent)
                        offenders.append(f"{rel}:{call.lineno}")
        self.assertEqual(
            offenders, [],
            "text=True decodes with the locale encoding (cp1252 on Windows); "
            "use encoding=\"utf-8\", errors=\"replace\" instead. Offenders: "
            + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
