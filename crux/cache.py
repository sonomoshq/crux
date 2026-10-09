# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Per-branch RunState cache (D9).

Layout: ~/.cache/crux/<owner>__<repo>/<branch>.json, with '/' in branch names
replaced by '__' so e.g. "feat/x" stays a single filename. load() treats a
missing file, unreadable/corrupt JSON, a STATE_VERSION mismatch, a base_sha
mismatch (rebase/force-push), or a state belonging to a different PR as "no
cache" and returns None. save() is atomic: write to a temp file in the same
directory, then os.replace.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from crux.models import STATE_VERSION, RepoInfo, RunState, state_from_json, state_to_json


def _state_path(info: RepoInfo) -> Path:
    branch_slug = info.branch.replace("/", "__")
    # expanduser (not a literal path) so tests and users can redirect via $HOME
    return (
        Path(os.path.expanduser("~/.cache/crux"))
        / f"{info.owner}__{info.repo}"
        / f"{branch_slug}.json"
    )


def load(info: RepoInfo, pr: int | None = None) -> RunState | None:
    """Last run's state for this branch, or None when it cannot be trusted.

    *pr* is the PR this run is about. One branch can have several open PRs
    (stacked work), and this file is keyed by branch alone — so a state saved
    for another PR must not be handed back: its Slack thread ts would thread
    THIS PR's update under THAT PR's announcement, and its annotations describe
    a diff against a different base. Passing None (a preview, with no PR) skips
    the check, as does a state saved before the PR number was recorded.
    """
    path = _state_path(info)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        state = state_from_json(text)
    except (ValueError, KeyError, TypeError):
        # Corrupt or structurally stale cache is the same as no cache.
        return None
    if state.version != STATE_VERSION:
        return None
    if state.base_sha != info.base_sha:
        return None  # merge-base moved (rebase/force-push) => invalidate all (D9)
    if pr is not None and state.pr_number is not None and state.pr_number != pr:
        return None  # this branch's other PR — nothing here belongs to ours
    return state


def save(info: RepoInfo, state: RunState) -> None:
    path = _state_path(info)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name in the destination directory so concurrent runs never
    # clobber each other's partial writes; os.replace is atomic on POSIX.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(state_to_json(state))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
