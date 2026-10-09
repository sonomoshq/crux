# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""History signals: churn, fix frequency and co-change misses from git log.

Contract (DESIGN.md): run
``git log --since="<cfg.history_window_days> days ago" --name-only
--pretty=format:%H%x01%s`` once, then derive per-file commit sets.

- ``churn``          = number of window commits touching the hunk's file
- ``fix_frequency``  = those commits whose subject matches fix|bug|revert
                       (case-insensitive substring)
- ``co_change_miss`` = partner files that historically changed together with
  the hunk's file in >= cfg.co_change_threshold of its window commits but are
  absent from this diff.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter

from crux.models import Config, Hunk, HunkSignals

_FIX_RE = re.compile(r"fix|bug|revert", re.IGNORECASE)
# %x01 keeps hash and subject on one line without colliding with file paths.
_SEP = "\x01"
_TOOL_TIMEOUT = 60


def _git_log(repo_root: str, window_days: int) -> str | None:
    argv = [
        "git", "log",
        f"--since={window_days} days ago",
        "--name-only",
        f"--pretty=format:%H{_SEP}%s",
    ]
    try:
        cp = subprocess.run(argv, cwd=repo_root, capture_output=True,
                            encoding="utf-8", errors="replace",
                            timeout=_TOOL_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:  # not a repo, or repo without commits
        return None
    return cp.stdout


def _parse_log(text: str) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, str]]:
    """Returns (file -> commits, commit -> files, commit -> subject)."""
    file_commits: dict[str, set[str]] = {}
    commit_files: dict[str, set[str]] = {}
    subjects: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if _SEP in line:
            sha, _, subject = line.partition(_SEP)
            current = sha
            subjects[sha] = subject
            commit_files[sha] = set()
            continue
        path = line.strip()
        if path and current is not None:
            file_commits.setdefault(path, set()).add(current)
            commit_files[current].add(path)
    return file_commits, commit_files, subjects


def add_history(signals: dict[str, HunkSignals], hunks: list[Hunk],
                repo_root: str, cfg: Config) -> None:
    for hunk in hunks:  # reset so repeated runs stay idempotent
        sig = signals.setdefault(hunk.id, HunkSignals(hunk_id=hunk.id))
        sig.churn = 0
        sig.fix_frequency = 0
        sig.co_change_miss = []

    log_text = _git_log(repo_root, cfg.history_window_days)
    if not log_text:
        return
    file_commits, commit_files, subjects = _parse_log(log_text)

    for hunk in hunks:
        commits = file_commits.get(hunk.file, set())
        sig = signals[hunk.id]
        sig.churn = len(commits)
        sig.fix_frequency = sum(
            1 for sha in commits if _FIX_RE.search(subjects.get(sha, "")))

    diff_files = {h.file for h in hunks}
    for path in diff_files:
        commits = file_commits.get(path)
        if not commits:
            continue
        partner_counts: Counter[str] = Counter()
        for sha in commits:
            for partner in commit_files.get(sha, ()):
                if partner != path:
                    partner_counts[partner] += 1
        misses = sorted(
            partner for partner, shared in partner_counts.items()
            if partner not in diff_files
            and shared / len(commits) >= cfg.co_change_threshold
        )
        if not misses:
            continue
        for hunk in hunks:
            if hunk.file == path:
                signals[hunk.id].co_change_miss = list(misses)
