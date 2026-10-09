# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""`crux prs`: list open PRs across one repo, selected repos, or all repos.

Public API (DESIGN.md module contracts):
    resolve_repos(repo_args, cfg) -> tuple[list[str], list[str]]
    fetch_open_prs(repos, jobs) -> dict[str, list[dict] | str]
    render_prs(results) -> str
    auto_jobs() -> int

Everything here is read-only `gh` queries — nothing posts — so the D13 owner
allowlist scopes only *discovery* (which repos "all repos" means: every repo
of the [scope] owners), never an explicit owner/name the user asked about.

The per-repo `gh pr list` calls are network-bound subprocesses, so they fan
out on a thread pool. Pool size comes from `crux.toml [prs] jobs` (or --jobs);
0 means auto: max parallel tasks for the machine (CPU count + 4, capped at
32 — the same heuristic as Python's own default thread pool).
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import crux.post as post
from crux.models import Config, CruxError, PostError

log = logging.getLogger("crux.prs")

# Interactive command: a hung network should fail a repo, not the terminal.
_GH_TIMEOUT = 60
# gh pr list defaults to 30 rows; raise it so a busy repo's list is complete.
_PR_LIMIT = 100
# Repos fetched per owner during discovery (render notes when the cap is hit).
_REPO_LIMIT = 200

_PR_FIELDS = "number,title,headRefName,baseRefName,author,isDraft,updatedAt"

# Widest the title/branch columns may render; longer values are ellipsized
# (one runaway branch name must not pad every line in the table).
_TITLE_W = 60
_BRANCH_W = 40


def auto_jobs() -> int:
    """Hardware-derived parallelism: one worker per CPU plus network headroom,
    capped at 32 (the stdlib ThreadPoolExecutor default heuristic)."""
    return min(32, (os.cpu_count() or 4) + 4)


# ---------------------------------------------------------------------------
# repo resolution
# ---------------------------------------------------------------------------

def resolve_repos(repo_args: list[str], cfg: Config) -> tuple[list[str], list[str]]:
    """Turn the command's REPO arguments into `owner/name` strings.

    No arguments => every repo of the configured scope owners (D13 defines the
    universe of "all"). Each argument is one of: `owner/name` taken verbatim,
    a bare name matched (case-insensitively) against the scope owners' repos,
    or `.` for the repo the command runs in.

    Returns (repos, problems): repos deduped in input order; problems are
    human sentences for arguments/owners that could not be resolved. Never
    raises — a bad argument or an unreachable owner degrades to a problem
    line, so the rest of the sweep still runs.
    """
    problems: list[str] = []
    if not repo_args:
        return _discover_all(cfg.scope_owners, problems), problems

    discovered: list[str] | None = None
    repos: list[str] = []
    for arg in repo_args:
        if arg == ".":
            try:
                repos.append(_current_repo())
            except CruxError as exc:
                problems.append(f"'.': {exc}")
            continue
        if "/" in arg:
            repos.append(arg.strip("/"))
            continue
        if discovered is None:  # first bare name pays for discovery, once
            discovered = _discover_all(cfg.scope_owners, problems)
        matches = [r for r in discovered
                   if r.split("/", 1)[1].lower() == arg.lower()]
        if matches:
            repos.extend(matches)
        else:
            problems.append(
                f"no repo named {arg!r} under owners {cfg.scope_owners}")

    seen: set[str] = set()
    unique: list[str] = []
    for repo in repos:
        if repo.lower() not in seen:
            seen.add(repo.lower())
            unique.append(repo)
    return unique, problems


def _current_repo() -> str:
    """`owner/name` of the repo the command runs in, from its origin URL."""
    import crux.gitio as gitio
    url = gitio.run_git(["remote", "get-url", "origin"])
    owner, name = gitio._parse_owner_repo(url)
    if not owner:
        raise CruxError(
            f"origin remote is not owner/name shaped: {url.strip()!r}")
    return f"{owner}/{name}"


def _discover_all(owners: list[str], problems: list[str]) -> list[str]:
    """Every repo of *owners*, sorted; unreachable owners become problems."""
    repos: list[str] = []
    for owner in owners:
        try:
            rows = _discover_owner(owner)
        except CruxError as exc:
            problems.append(f"could not list repos for {owner}: {exc}")
            continue
        if len(rows) >= _REPO_LIMIT:
            problems.append(f"{owner} has {_REPO_LIMIT}+ repos; the list was "
                            f"cut at {_REPO_LIMIT} and some may be missing")
        repos.extend(rows)
    return sorted(repos, key=str.lower)


def _discover_owner(owner: str) -> list[str]:
    out = post._run_gh(
        ["repo", "list", owner, "--no-archived",
         "--limit", str(_REPO_LIMIT), "--json", "nameWithOwner"],
        timeout=_GH_TIMEOUT)
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise PostError(f"gh repo list returned unparseable JSON: {exc}") from None
    return [str(r["nameWithOwner"]) for r in rows
            if isinstance(r, dict) and r.get("nameWithOwner")]


# ---------------------------------------------------------------------------
# parallel fetch
# ---------------------------------------------------------------------------

def fetch_open_prs(repos: list[str], jobs: int) -> dict[str, list[dict] | str]:
    """One `gh pr list` per repo, fanned out over at most *jobs* threads.

    Returns {repo: rows} in the input order. A repo whose call failed maps to
    its error message (str) instead of rows, so one bad repo never sinks the
    sweep.
    """
    workers = max(1, min(jobs, len(repos)))
    results: dict[str, list[dict] | str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {repo: pool.submit(_fetch_one, repo) for repo in repos}
        for repo, future in futures.items():
            try:
                results[repo] = future.result()
            except (CruxError, ValueError) as exc:
                results[repo] = str(exc)
    return results


def _fetch_one(repo: str) -> list[dict]:
    out = post._run_gh(
        ["pr", "list", "--repo", repo, "--state", "open",
         "--limit", str(_PR_LIMIT), "--json", _PR_FIELDS],
        timeout=_GH_TIMEOUT)
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise PostError(f"gh pr list returned unparseable JSON: {exc}") from None
    return [r for r in rows if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_prs(results: dict[str, list[dict] | str]) -> str:
    """One aligned block per repo (in dict order) plus a summary line."""
    all_rows = [r for v in results.values() if isinstance(v, list) for r in v]
    num_w = max((len(f"#{r.get('number', '')}") for r in all_rows), default=2)
    title_w = min(_TITLE_W, max((len(_title(r)) for r in all_rows), default=0))
    branch_w = min(_BRANCH_W,
                   max((len(str(r.get("headRefName", ""))) for r in all_rows),
                       default=0))
    author_w = max((len(_author(r)) for r in all_rows), default=0)

    lines: list[str] = []
    total = failed = 0
    for repo, rows in results.items():
        if isinstance(rows, str):
            failed += 1
            lines.append(f"{repo} — FAILED: {rows}")
            continue
        if not rows:
            lines.append(f"{repo} — no open PRs")
            continue
        total += len(rows)
        lines.append(f"{repo} — {len(rows)} open")
        for row in rows:
            title = _clip(_title(row), title_w)
            num = f"#{row.get('number', '?')}"
            branch = _clip(str(row.get("headRefName", "")), branch_w)
            line = (f"  {num:<{num_w}}  {title:<{title_w}}  "
                    f"{branch:<{branch_w}}  {_author(row):<{author_w}}  "
                    f"{_age(str(row.get('updatedAt', '')))}")
            lines.append(line.rstrip())

    scanned = len(results)
    summary = (f"{total} open PR{'s' if total != 1 else ''} across "
               f"{scanned} repo{'s' if scanned != 1 else ''}")
    if failed:
        summary += f" ({failed} failed)"
    lines += ["", summary]
    return "\n".join(lines)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def _title(row: dict) -> str:
    title = " ".join(str(row.get("title", "")).split())
    return title + " [draft]" if row.get("isDraft") else title


def _author(row: dict) -> str:
    author = row.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "?")
    return str(author or "?")


def _age(iso: str) -> str:
    """'3d' / '5h' / '12m' since *iso*; '?' when it does not parse."""
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    seconds = max(0.0, (datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"
