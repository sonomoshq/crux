# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Blast radius: how widely each hunk's newly-defined symbols are referenced.

Contract (DESIGN.md): for every symbol in ``HunkSignals.defines`` count word
matches across the repo with ``rg -w --count-matches`` (fallback
``git grep -w -c``), excluding matches that fall inside the defining hunk
itself.  ``blast_radius`` is the total remaining match count summed over the
hunk's defined symbols; ``callers`` holds up to 8 ``path:line`` samples taken
from a ``rg -w -n`` pass (fallback ``git grep -w -n``).
"""
from __future__ import annotations

import subprocess

from crux.models import Hunk, HunkSignals

_MAX_CALLERS = 8
_TOOL_TIMEOUT = 30  # seconds per external call; blast is a best-effort signal


def _run(argv: list[str], cwd: str) -> subprocess.CompletedProcess | None:
    """Run an external tool; None means the tool is missing or hung."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True,
                              encoding="utf-8", errors="replace",
                              timeout=_TOOL_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _sum_counts(text: str) -> int:
    """Sum per-file counts from `path:count` lines (rg --count-matches / git grep -c)."""
    total = 0
    for line in text.splitlines():
        _, _, count = line.rpartition(":")
        if count.isdigit():
            total += int(count)
    return total


def _total_count(symbol: str, repo_root: str) -> int:
    # -F: symbols are literal identifiers, never regexes; -e guards leading "-".
    cp = _run(["rg", "-w", "--count-matches", "-F", "-e", symbol], repo_root)
    if cp is None or cp.returncode > 1:  # rg missing or errored (1 = no matches)
        cp = _run(["git", "grep", "-I", "-w", "-c", "-F", "-e", symbol], repo_root)
        if cp is None or cp.returncode > 1:
            return 0
    return _sum_counts(cp.stdout)


def _line_matches(symbol: str, repo_root: str) -> list[tuple[str, int]]:
    """All word matches as (repo-relative path, line number)."""
    cp = _run(["rg", "-w", "-n", "-H", "-F", "-e", symbol], repo_root)
    if cp is None or cp.returncode > 1:
        cp = _run(["git", "grep", "-I", "-w", "-n", "-F", "-e", symbol], repo_root)
        if cp is None or cp.returncode > 1:
            return []
    matches: list[tuple[str, int]] = []
    for line in cp.stdout.splitlines():
        # "path:line:content"; assumes no ":" in paths (true for repo files here).
        parts = line.split(":", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            matches.append((parts[0].removeprefix("./"), int(parts[1])))
    return matches


def add_blast(signals: dict[str, HunkSignals], hunks: list[Hunk], repo_root: str) -> None:
    count_cache: dict[str, int] = {}
    match_cache: dict[str, list[tuple[str, int]]] = {}

    for hunk in hunks:
        sig = signals.setdefault(hunk.id, HunkSignals(hunk_id=hunk.id))
        sig.blast_radius = 0
        sig.callers = []
        if not sig.defines:
            continue

        lo = hunk.new_start
        hi = hunk.new_start + hunk.new_count  # exclusive; empty for pure deletions
        total = 0
        callers: list[str] = []
        for symbol in sig.defines:
            if symbol not in count_cache:
                count_cache[symbol] = _total_count(symbol, repo_root)
                match_cache[symbol] = _line_matches(symbol, repo_root)
            in_hunk = 0
            for path, line_no in match_cache[symbol]:
                if path == hunk.file and lo <= line_no < hi:
                    in_hunk += 1
                    continue
                entry = f"{path}:{line_no}"
                if len(callers) < _MAX_CALLERS and entry not in callers:
                    callers.append(entry)
            # --count-matches counts every match while the -n pass yields matching
            # lines; subtracting matched lines inside the hunk is a close-enough
            # exclusion for a ranking signal.
            total += max(count_cache[symbol] - in_hunk, 0)

        sig.blast_radius = total
        sig.callers = callers
