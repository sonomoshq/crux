# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: the local clones a super PR's candidates come from.

A super PR draws from the repos already configured in `[scope]` — the same
allowlist every other command obeys. Candidates come from two places: branches
in clones of those repos found on this machine, and open PRs on GitHub. This
module owns the first half (disk discovery); `crux/candidates.py` joins the two.

Discovery is deliberately local and cheap. The picker has to feel instant, so
nothing here touches the network: every fact shown on a candidate line comes
from the clone's own git metadata.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from crux.gitio import _parse_owner_repo, _try_git
from crux.models import Config, LocalClone

# How far below a search root a clone may sit. Two levels covers the usual
# ~/src/<org>/<repo> and ~/src/<repo> layouts without walking a whole home
# directory; deeper trees should name a more specific root.
_MAX_DEPTH = 3
# Directories never worth descending into when hunting for clones.
_SKIP_DIRS = frozenset({
    "node_modules", "target", "venv", ".venv", "__pycache__", "dist", "build",
    ".cache", ".cargo", ".rustup", ".npm", "site-packages", ".tox", ".mypy_cache",
})


def qualify(repos: list[str], cfg: Config) -> list[str]:
    """Turn `[scope] repos` entries into `owner/name`.

    A bare name takes the first configured scope owner, which is what makes
    `repos = ["web", "api"]` work for a single-org setup. Entries
    that already carry an owner pass through untouched, so the allowlist can
    span orgs.
    """
    owner = cfg.scope_owners[0] if cfg.scope_owners else ""
    out: list[str] = []
    for entry in repos:
        entry = entry.strip().strip("/")
        if not entry:
            continue
        if "/" in entry:
            out.append(entry)
        elif owner:
            out.append(f"{owner}/{entry}")
    seen: set[str] = set()
    unique: list[str] = []
    for slug in out:
        if slug.lower() not in seen:
            seen.add(slug.lower())
            unique.append(slug)
    return unique


def search_roots(cfg: Config, repo_root: str | None = None) -> list[str]:
    """Directories to hunt for clones in.

    Unset config falls back to the PARENT of the repo the command runs in —
    sibling clones under one `src/<org>/` directory is the layout this is for,
    and it means a single-org user never has to configure roots at all.

    The climb to that parent is not a nicety. Without a *repo_root* the working
    directory is the only clue, and the working directory is usually INSIDE a
    clone — `crux serve` runs from wherever it was started, and the buttons on a
    brief call it with no repo of their own. Searching a clone finds one repo:
    itself. `_walk` stops at the first `.git`, so every other member of the
    bundle reads as "not on this machine" while sitting in the very next
    directory up. `superact.clone_root` climbs for exactly this reason, and the
    two must climb the same way or discovery searches one place while cloning
    targets another.
    """
    roots = [os.path.expanduser(r) for r in cfg.super_roots if r.strip()]
    if roots:
        return roots
    candidate = repo_root or os.getcwd()
    # A toplevel resolves to itself, so this is one branch for both cases.
    toplevel = _try_git(["rev-parse", "--show-toplevel"], cwd=candidate)
    if toplevel:
        return [str(Path(toplevel).parent)]
    return [candidate]


def _walk(root: str, depth: int, found: list[str]) -> None:
    """Collect git work-tree paths under *root*, not descending into clones."""
    if depth < 0:
        return
    try:
        entries = list(os.scandir(root))
    except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
        return
    if any(e.name == ".git" for e in entries):
        found.append(root)
        return  # a clone is a leaf: nested repos are not candidates
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        _walk(entry.path, depth - 1, found)


def clone_slug(path: str) -> str:
    """The `owner/name` the clone at *path* points at, or "" if it is not one.

    Identity only — no branch, no tip. A repo in detached HEAD is still a clone
    of its origin, and a caller asking "is this the repo I wanted?" must get
    "yes" for it. `_read_clone` needs more and rejects such a clone; this is the
    narrower question, and answering it here keeps the two from being confused.
    """
    url = _try_git(["remote", "get-url", "origin"], cwd=path)
    if not url:
        return ""
    owner, repo = _parse_owner_repo(url)
    return f"{owner}/{repo}" if owner and repo else ""


def _read_clone(path: str) -> LocalClone | None:
    """Read one clone's identity and current branch state, or None if it is
    not a GitHub clone we can place (no origin, detached, unborn branch)."""
    slug = clone_slug(path)
    if not slug:
        return None
    owner, repo = slug.split("/", 1)
    branch = _try_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if not branch or branch == "HEAD":
        return None  # detached: no branch to nominate as a candidate
    tip = _try_git(["log", "-1", "--format=%H %ct", "HEAD"], cwd=path)
    head_sha, ts = "", 0
    if tip:
        parts = tip.split()
        head_sha = parts[0] if parts else ""
        ts = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    default = _try_git(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=path)
    default_branch = default.split("/")[-1] if default else "main"
    return LocalClone(path=path, owner=owner, repo=repo, branch=branch,
                      head_sha=head_sha, last_commit_ts=ts,
                      default_branch=default_branch)


def find_clones(roots: list[str], wanted: list[str],
                jobs: int = 8) -> dict[str, LocalClone]:
    """Find clones of *wanted* (`owner/name` slugs) under *roots*.

    An empty *wanted* means "every clone found", which is what the picker falls
    back to when GitHub cannot be reached to list the scope's repos: local work
    is still selectable offline.

    Returns them keyed by lowercased slug. Directory names are ignored — a
    clone is placed by its origin remote, so a repo cloned under any local
    name is still recognised. When the same repo is cloned twice, the one with
    the newer branch tip wins: that is the copy being worked in.
    """
    want = {slug.lower() for slug in wanted}
    paths: list[str] = []
    for root in roots:
        _walk(root, _MAX_DEPTH, paths)
    if not paths:
        return {}

    out: dict[str, LocalClone] = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for clone in pool.map(_read_clone, paths):
            if clone is None:
                continue
            key = f"{clone.owner}/{clone.repo}".lower()
            if want and key not in want:
                continue
            previous = out.get(key)
            if previous is None or clone.last_commit_ts > previous.last_commit_ts:
                out[key] = clone
    return out


def age(ts: int, now: int | None = None) -> str:
    """"5 minutes ago" — the recency the picker sorts and reads on."""
    if ts <= 0:
        return "unknown"
    delta = max(0, (now if now is not None else int(time.time())) - ts)
    for seconds, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if delta >= seconds:
            n = delta // seconds
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"
