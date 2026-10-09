# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: what can go into a super PR, and how it is offered for selection.

The repos on offer are the ones already configured in `[scope]` — the
`repos` allowlist when set, else every repo of the `owners`. There is no
second place to declare which repos a bundle may draw from: a super PR is
simply the PRs you picked off that list.

Candidates come from two sources, joined on (repo, branch):

  * **local branches** — clones of the in-scope repos found on this machine,
    each showing the branch currently checked out. This is where in-flight work
    lives before it is ever pushed, and it is why the picker leads with disk
    rather than GitHub.
  * **open PRs** — anything already up for review in those repos.

A branch with a PR is ONE candidate, not two. A branch without a PR is still
offered: selecting it opens the PR (D37), because the goal is one command, not
a detour to go make PRs by hand.

Anything already in another bundle is dropped — a PR belongs to at most one
super PR (see `crux/bundle.py`), so a candidate that is spoken for is never
shown as available.

Ordering is by last commit time, newest first: the work you touched minutes ago
is the work you are bundling, and it should be candidate 1.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import crux.bundle as bundle
import crux.clones as clones_mod
import crux.prs as prs
from crux.gitio import _try_git
from crux.models import Candidate, Config, LocalClone

log = logging.getLogger("crux.candidates")


def _pr_ts(row: dict) -> int:
    """Epoch seconds from a PR's updatedAt, 0 when unparseable."""
    raw = str(row.get("updatedAt", "") or "")
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(
            raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except ValueError:
        return 0


def scope_repos(cfg: Config) -> tuple[list[str], list[str]]:
    """The `owner/name` repos a super PR may draw from, from `[scope]`.

    An explicit `repos` allowlist is taken as written; otherwise the scope's
    owners are expanded to their repos, which costs one `gh repo list` per
    owner. Returns (repos, problems) — an owner that cannot be listed is a
    problem line, never an exception, so the picker still runs on what is left.
    """
    if cfg.scope_repos:
        return clones_mod.qualify(cfg.scope_repos, cfg), []
    problems: list[str] = []
    return prs._discover_all(cfg.scope_owners, problems), problems


def gather(cfg: Config, repo_root: str | None = None,
           editing: int | None = None) -> tuple[list[Candidate], list[str]]:
    """Build the numbered candidate list from the configured scope.

    Returns (candidates, problems). Problems are human sentences about repos
    that could not be reached; they never raise, so one unreachable repo still
    leaves the rest selectable.
    """
    repos, problems = scope_repos(cfg)
    roots = clones_mod.search_roots(cfg, repo_root)

    if repos:
        clones = clones_mod.find_clones(roots, repos)
        missing = [r for r in repos if r.lower() not in clones]
        if missing:
            log.info("no local clone under %s for: %s", roots, ", ".join(missing))
    else:
        # Nothing could be listed from GitHub (offline, or gh unauthenticated).
        # Local clones of the scope's owners are still real work to bundle, so
        # fall back to disk rather than showing an empty picker.
        owners = {o.lower() for o in cfg.scope_owners}
        clones = {slug: clone
                  for slug, clone in clones_mod.find_clones(roots, []).items()
                  if not owners or clone.owner.lower() in owners}

    jobs = cfg.prs_jobs if cfg.prs_jobs > 0 else prs.auto_jobs()
    fetched = prs.fetch_open_prs(repos, jobs)

    taken = bundle.bundled_prs(exclude=editing)
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()  # (slug.lower(), branch)

    # 1. Open PRs first — they carry the richer identity (number, title), and
    # a local branch matching one must attach to it rather than duplicate it.
    for slug, rows in fetched.items():
        if isinstance(rows, str):
            problems.append(f"{slug}: {rows}")
            continue
        owner, _, repo = slug.partition("/")
        clone = clones.get(slug.lower())
        for row in rows:
            branch = str(row.get("headRefName", "") or "")
            number = int(row.get("number", 0) or 0)
            if not branch or not number:
                continue
            if bundle.key(owner, repo, number) in taken:
                continue
            # Prefer the local commit time when this branch is checked out
            # here: it reflects work in progress, which updatedAt may lag.
            ts = _pr_ts(row)
            path = ""
            if clone and clone.branch == branch:
                ts = max(ts, clone.last_commit_ts)
                path = clone.path
            author = row.get("author")
            out.append(Candidate(
                owner=owner, repo=repo, branch=branch, last_commit_ts=ts,
                pr=number, title=str(row.get("title", "") or ""), path=path,
                author=(str(author.get("name") or author.get("login") or "")
                        if isinstance(author, dict) else "")))
            seen.add((slug.lower(), branch))

    # 2. Local branches with no PR yet. The default branch is not a candidate:
    # a super PR bundles proposed changes, and trunk is not one.
    for slug_lower, clone in clones.items():
        if clone.branch == clone.default_branch:
            continue
        if (slug_lower, clone.branch) in seen:
            continue
        # No PR yet, so no GitHub author: the branch tip's committer is who
        # will own the PR this opens.
        out.append(Candidate(
            owner=clone.owner, repo=clone.repo, branch=clone.branch,
            last_commit_ts=clone.last_commit_ts, pr=None,
            title="", path=clone.path,
            author=_try_git(["log", "-1", "--format=%an"], cwd=clone.path) or ""))

    out.sort(key=lambda c: (-c.last_commit_ts, c.repo.lower(), c.branch))
    return out, problems


def render(cands: list[Candidate], now: int | None = None) -> str:
    """The numbered picker, e.g.

        1. web (on branch "fix", last commit 5 minutes ago)
        2. api #7 (on branch "feat/seed-catalog", 2 hours ago) — Add seed catalog

    The repo leads because a cross-repo bundle is chosen repo by repo; the PR
    number appears only when one exists, so "no number" reads as "this will
    open a PR".
    """
    if not cands:
        return ("No candidates — every branch in the configured scope is "
                "trunk, or already in a super PR.")
    heads = [c.repo if c.pr is None else f"{c.repo} #{c.pr}" for c in cands]
    width = max(len(h) for h in heads)
    lines: list[str] = []
    for i, (c, head) in enumerate(zip(cands, heads), 1):
        line = (f"{i:>3}. {head:<{width}}  (on branch \"{c.branch}\", "
                f"{clones_mod.age(c.last_commit_ts, now)})")
        if c.title:
            line += f" — {c.title}"
        if c.pr is None:
            line += "  [no PR yet]"
        lines.append(line)
    return "\n".join(lines)


def parse_selection(text: str, count: int) -> tuple[list[int], list[str]]:
    """Parse "1,3,5-7" into zero-based indices.

    Ranges and commas because a bundle is usually "the top few" — being made to
    type six numbers separated by spaces is friction on the one command this
    feature exists to make effortless. Out-of-range entries become problems
    rather than raising, so one typo does not discard the whole selection.
    """
    picked: list[int] = []
    problems: list[str] = []
    for chunk in text.replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:
            lo_s, _, hi_s = chunk.partition("-")
            if not (lo_s.isdigit() and hi_s.isdigit()):
                problems.append(f"{chunk!r} is not a number or range")
                continue
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            values = range(lo, hi + 1)
        elif chunk.isdigit():
            values = range(int(chunk), int(chunk) + 1)
        else:
            problems.append(f"{chunk!r} is not a number or range")
            continue
        for value in values:
            if not 1 <= value <= count:
                problems.append(f"{value} is not on the list")
                continue
            if value - 1 not in picked:
                picked.append(value - 1)
    return picked, problems
