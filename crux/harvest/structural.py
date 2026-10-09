# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Structural classification of diff hunks: generated / cosmetic / mechanical / behavioral.

Deterministic, no LLM (D6). `difft` (difftastic) is used opportunistically for a
syntax-aware cosmetic check; when it is missing, errors, or times out we fall
back to the regex/normalization path (D12).
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile

from crux.models import Config, Hunk, HunkClass

# Cosmetic checking must stay cheap: hard cap per difft invocation, seconds.
_DIFFT_TIMEOUT = 10

# Lines that are comments in their entirety: Python/shell `#`, C-family `//`,
# block comment open/close, JSDoc-style ` * ` continuations, HTML `<!--`,
# SQL/Lua `--`. `--` and `*` require a following space/EOL so that code like
# `--x` or `*args` is not mistaken for a comment.
_WHOLE_LINE_COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*/|<!--|--(?:\s|$)|\*(?:\s|$))")

_MIN_CLUSTER_HUNKS = 3


def strip_comment(line: str) -> str:
    """Best-effort removal of the comment portion of a single source line.

    Whole-line comments become "". Trailing `#` / `//` comments are cut, with
    single-line string awareness so `url = "http://x"` survives intact.
    Multi-line strings/comments are out of scope for this heuristic.
    """
    if _WHOLE_LINE_COMMENT_RE.match(line):
        return ""
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(ch)
        else:
            if ch in "'\"`":
                quote = ch
                out.append(ch)
            elif ch == "#":
                break
            elif ch == "/" and i + 1 < n and line[i + 1] == "/":
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def _normalize(line: str) -> str:
    """Comment-stripped line with ALL whitespace removed."""
    return "".join(strip_comment(line).split())


def _normalized_added(hunk: Hunk) -> tuple[str, ...]:
    return tuple(n for n in (_normalize(l) for l in hunk.added_lines) if n)


def mechanical_clusters(hunks: list[Hunk]) -> list[list[str]]:
    """Groups of hunk ids whose normalized added-line sequences are identical.

    A cluster requires >= 3 hunks spanning >= 2 files ("repeated edit threaded
    across the codebase"). Hunks whose added lines normalize to nothing
    (comment/blank-only) never cluster.
    """
    groups: dict[tuple[str, ...], list[Hunk]] = {}
    for hunk in hunks:
        key = _normalized_added(hunk)
        if not key:
            continue
        groups.setdefault(key, []).append(hunk)
    clusters: list[list[str]] = []
    for members in groups.values():
        if len(members) >= _MIN_CLUSTER_HUNKS and len({m.file for m in members}) >= 2:
            clusters.append([m.id for m in members])
    return clusters


def classify(hunks: list[Hunk], cfg: Config) -> None:
    """Set Hunk.klass for every hunk. Precedence: GENERATED > COSMETIC >
    MECHANICAL > BEHAVIORAL. Idempotent: every hunk is assigned explicitly."""
    mechanical_ids = {hid for cluster in mechanical_clusters(hunks) for hid in cluster}
    difft = shutil.which("difft")
    for hunk in hunks:
        if _is_generated_path(hunk.file, cfg):
            hunk.klass = HunkClass.GENERATED
        elif _is_cosmetic(hunk, difft):
            hunk.klass = HunkClass.COSMETIC
        elif hunk.id in mechanical_ids:
            hunk.klass = HunkClass.MECHANICAL
        else:
            hunk.klass = HunkClass.BEHAVIORAL


def _is_generated_path(path: str, cfg: Config) -> bool:
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    for dep in cfg.dependency_files:
        if norm == dep or base == dep or norm.endswith("/" + dep):
            return True
    for pattern in cfg.generated_globs:
        if pattern.endswith("/"):
            # Directory prefix at any depth: "dist/" hits "dist/x" and "web/dist/x".
            if ("/" + pattern) in ("/" + norm):
                return True
        elif fnmatch.fnmatch(base, pattern) or fnmatch.fnmatch(norm, pattern):
            return True
    return False


def _is_cosmetic(hunk: Hunk, difft: str | None) -> bool:
    if difft is not None:
        verdict = _difft_cosmetic(hunk, difft)
        if verdict is not None:
            return verdict
    return _regex_cosmetic(hunk)


def _regex_cosmetic(hunk: Hunk) -> bool:
    """Cosmetic iff normalized added content equals normalized removed content.

    Joining before comparing also treats pure line re-wraps as cosmetic; the
    all-comments/blank-changes case falls out as "" == "".
    """
    added = hunk.added_lines
    removed = hunk.removed_lines
    if not added and not removed:
        return False
    return (
        "".join(_normalize(l) for l in added)
        == "".join(_normalize(l) for l in removed)
    )


def _hunk_versions(hunk: Hunk) -> tuple[str, str]:
    """Reconstruct (old, new) text fragments of the hunk from its patch."""
    old: list[str] = []
    new: list[str] = []
    for line in hunk.patch.splitlines():
        if line.startswith("@@") or line.startswith("\\"):
            continue
        if line.startswith("+"):
            new.append(line[1:])
        elif line.startswith("-"):
            old.append(line[1:])
        else:
            text = line[1:] if line.startswith(" ") else line
            old.append(text)
            new.append(text)
    return "\n".join(old) + "\n", "\n".join(new) + "\n"


def _difft_cosmetic(hunk: Hunk, difft: str) -> bool | None:
    """Ask difftastic whether old vs new fragments differ syntactically.

    Returns True (no syntactic change => cosmetic), False (real change), or
    None when difft could not decide (missing/old binary, parse error,
    timeout) -- callers then use the regex path.
    """
    old_text, new_text = _hunk_versions(hunk)
    ext = os.path.splitext(hunk.file)[1] or ".txt"  # same ext => right parser
    try:
        with tempfile.TemporaryDirectory(prefix="crux-difft-") as tmp:
            old_path = os.path.join(tmp, "old" + ext)
            new_path = os.path.join(tmp, "new" + ext)
            for p, text in ((old_path, old_text), (new_path, new_text)):
                with open(p, "w", encoding="utf-8", errors="backslashreplace") as f:
                    f.write(text)
            proc = subprocess.run(
                [difft, "--check-only", "--exit-code", "--ignore-comments",
                 old_path, new_path],
                capture_output=True,
                timeout=_DIFFT_TIMEOUT,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None  # usage/parse failure (e.g. flag unsupported by old difft)
