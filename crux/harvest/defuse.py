# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Def/use extraction from diff hunks.

Regex-level and language-agnostic: catches Python/JS/TS/Go/Rust-style
definitions introduced (or renamed / re-signed, which surface as fresh def
lines) in added lines, then links hunks by identifier usage. No parsing, no
subprocess.
"""
from __future__ import annotations

import re

from crux.harvest.structural import strip_comment
from crux.models import Hunk, HunkSignals

_ID = r"[A-Za-z_$][A-Za-z0-9_$]*"
_IDENT_RE = re.compile(_ID)

# Optional declaration modifiers that may precede the def keyword.
_MOD = (
    r"(?:(?:public|private|protected|pub(?:\([^)]*\))?|export|default|declare|"
    r"abstract|static|async|final|unsafe|extern(?:\s+\"[^\"]*\")?)\s+)*"
)

# One pattern per definition style; group 1 is always the symbol name.
# Anchored at line start (after modifiers) so e.g. `for (let i = 0; ...)`
# or `defer func() {` do not register as definitions.
_DEF_RES: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^\s*{_MOD}def\s+({_ID})"),                          # Python
    re.compile(rf"^\s*{_MOD}class\s+({_ID})"),                        # Python/JS/TS
    re.compile(rf"^\s*{_MOD}function\s*\*?\s*({_ID})"),               # JS/TS
    re.compile(rf"^\s*{_MOD}(?:const|let|var)\s+({_ID})\s*(?::[^=\n]+)?="),  # JS/TS/Rust binding
    re.compile(rf"^\s*{_MOD}fn\s+({_ID})"),                           # Rust
    re.compile(rf"^\s*{_MOD}func\s+(?:\([^)]*\)\s*)?({_ID})"),        # Go (incl. methods)
    re.compile(rf"^\s*{_MOD}interface\s+({_ID})"),                    # TS/Go/Java
    re.compile(rf"^\s*{_MOD}type\s+({_ID})"),                         # TS/Go/Python 3.12
)


def _defines_in(lines: list[str]) -> set[str]:
    defs: set[str] = set()
    for line in lines:
        for pat in _DEF_RES:
            m = pat.match(line)
            if m:
                defs.add(m.group(1))
    return defs


def extract_defs_uses(hunks: list[Hunk]) -> dict[str, HunkSignals]:
    """Create a HunkSignals entry for every hunk.

    defines: symbols introduced in the hunk's added lines.
    uses: identifiers in the hunk's added lines intersected with the union of
    defines from OTHER hunks in the diff (so a hunk never "uses" a symbol
    only it defines). Comments are stripped first so prose does not link hunks.
    """
    code_lines: dict[str, list[str]] = {}
    defines: dict[str, set[str]] = {}
    for hunk in hunks:
        lines = [strip_comment(l) for l in hunk.added_lines]
        code_lines[hunk.id] = lines
        defines[hunk.id] = _defines_in(lines)

    def_owners: dict[str, set[str]] = {}
    for hid, defs in defines.items():
        for symbol in defs:
            def_owners.setdefault(symbol, set()).add(hid)

    signals: dict[str, HunkSignals] = {}
    for hunk in hunks:
        other_defs = {s for s, owners in def_owners.items() if owners - {hunk.id}}
        idents: set[str] = set()
        for line in code_lines[hunk.id]:
            idents.update(_IDENT_RE.findall(line))
        signals[hunk.id] = HunkSignals(
            hunk_id=hunk.id,
            defines=sorted(defines[hunk.id]),
            uses=sorted(idents & other_defs),
        )
    return signals
