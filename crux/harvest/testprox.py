# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Test proximity: is any defined symbol referenced from the repo's test dirs?

Contract (DESIGN.md): ``rg -l -w`` (fallback ``git grep -l -w``) each defined
symbol, restricted to the ``cfg.test_dirs`` directories that actually exist
under the repo root; any hit => ``test_touched``.

This is the last harvest stage, so it also finalizes ``HunkSignals.score``.
"""
from __future__ import annotations

import os
import subprocess
from bisect import bisect_right

from crux.models import Config, Hunk, HunkSignals

_TOOL_TIMEOUT = 30


def _run(argv: list[str], cwd: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True,
                              encoding="utf-8", errors="replace",
                              timeout=_TOOL_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _symbol_in_tests(symbol: str, repo_root: str, test_dirs: list[str]) -> bool:
    cp = _run(["rg", "-l", "-w", "-F", "-e", symbol, *test_dirs], repo_root)
    if cp is None or cp.returncode > 1:  # rg missing or errored (1 = no matches)
        cp = _run(["git", "grep", "-I", "-l", "-w", "-F", "-e", symbol, "--", *test_dirs],
                  repo_root)
        if cp is None:
            return False
    return cp.returncode == 0 and bool(cp.stdout.strip())


def add_test_proximity(signals: dict[str, HunkSignals], hunks: list[Hunk],
                       repo_root: str, cfg: Config) -> None:
    test_dirs = [d for d in cfg.test_dirs
                 if os.path.isdir(os.path.join(repo_root, d))]
    touched_cache: dict[str, bool] = {}

    for hunk in hunks:
        sig = signals.setdefault(hunk.id, HunkSignals(hunk_id=hunk.id))
        sig.test_touched = False
        if not test_dirs or not sig.defines:
            continue
        for symbol in sig.defines:
            if symbol not in touched_cache:
                touched_cache[symbol] = _symbol_in_tests(symbol, repo_root, test_dirs)
            if touched_cache[symbol]:
                sig.test_touched = True
                break

    compute_scores(signals)


# Score weights. The score only ranks hunks (gate stats, DAG tie-breaking), so
# absolute scale is arbitrary; components and their ranges:
#   blast:     min(blast_radius, 30) / 10          -> 0.0..3.0 (normalized)
#   sensitive: +2.0 when any sensitive rule matched
#   churn:     percentile of this hunk's churn among all hunks in the diff
#              (churn 0 contributes 0.0)           -> 0.0..1.0
#   fixes:     min(fix_frequency, 3)               -> 0.0..3.0
#   untested:  +1.0 when no test file references a defined symbol
def compute_scores(signals: dict[str, HunkSignals]) -> None:
    churns = sorted(s.churn for s in signals.values())
    n = len(churns)
    for sig in signals.values():
        blast = min(sig.blast_radius, 30) / 10.0
        sensitive = 2.0 if sig.sensitive else 0.0
        if n == 0 or sig.churn <= 0:
            churn_pct = 0.0
        else:
            churn_pct = bisect_right(churns, sig.churn) / n
        fixes = float(min(sig.fix_frequency, 3))
        untested = 0.0 if sig.test_touched else 1.0
        sig.score = blast + sensitive + churn_pct + fixes + untested
