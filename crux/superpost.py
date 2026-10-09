# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: publish a super PR — the sticky issue, and one pointer per member PR.

Placement follows from the cross-repo decision. GitHub has no repo-less issue
and no cross-repo grouping object, so the brief is filed as an issue in the
group's nominated home repo. The issue is not *about* that repo; it is parked
there because a URL has to live somewhere.

Cross-repo `owner/repo#12` references in the body link automatically and drop a
backlink event in each member PR's timeline, so the bundle reaches every repo it
touches without the brief being copied anywhere. The explicit one-line comment
on each PR is there because a timeline event is easy to miss — but it is only a
pointer. The brief exists in exactly one place, so it can only be stale one way.
"""
from __future__ import annotations

import json
import logging
import re

import crux.post as post
from crux.models import (SUPER_LINK_MARKER, SUPER_MARKER, TEST_MARKER, Bundle,
                         BundleMember, CruxError, PostError)

log = logging.getLogger("crux.superpost")


def issue_url(home: str, number: int) -> str:
    return f"https://github.com/{home}/issues/{number}"


_SUPER_TAG = re.compile(r"\[super-pr-(\d+)\]")


def _issue_titles(home: str) -> list[dict]:
    """Every issue in the home repo, open and closed. Empty when unreadable.

    Closed ones count: a bundle's number is retired when it merges, not
    forgotten, and its brief stays the permanent record of what landed.
    """
    try:
        out = post._run_gh(
            ["api", f"repos/{home}/issues?state=all&per_page=100", "--paginate"])
    except CruxError as exc:
        log.info("could not list issues on %s: %s", home, exc)
        return []
    return [row for row in post._iter_paginated(out) if isinstance(row, dict)]


def _find_issue(home: str, marker_number: int) -> int | None:
    """Find this bundle's existing issue by its title tag.

    The bundle's own state normally carries the number; this is the recovery
    path for state lost or moved between machines, so a second run edits the
    existing brief instead of filing a duplicate.
    """
    tag = f"[super-pr-{marker_number}]"
    for row in _issue_titles(home):
        if tag in str(row.get("title", "")):
            number = row.get("number")
            if isinstance(number, int):
                return number
    return None


def highest_brief(home: str) -> int:
    """The largest super-PR number that already has a brief filed in *home*.

    Bundle numbers are minted locally, but the briefs they name are shared, so
    "free on this machine" is not the same question as "free". A bundle made on
    another laptop — or one this machine has since forgotten — exists here only
    as its issue, and minting its number again does more than mislabel the new
    bundle: the refresh that follows a create looks the number up by tag, finds
    that brief, sees a revision far ahead of the one-revision-old bundle just
    saved, and adopts its membership. The selection the user actually made is
    replaced by a stranger's, and the brief they were shown reviews PRs they
    never picked.

    Returns 0 when the repo cannot be read, so an unreachable home or an
    unauthenticated `gh` degrades to the local-only answer rather than blocking
    a create that is otherwise fine.
    """
    highest = 0
    for row in _issue_titles(home):
        match = _SUPER_TAG.search(str(row.get("title", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def find_brief(home: str, number: int) -> int | None:
    """The issue number of super PR *number*'s brief in *home*, or None.

    Public counterpart of `_find_issue`: a machine that never made this bundle
    still has to find its brief, because that is where the bundle's own state
    now lives (D38).
    """
    return _find_issue(home, number)


def issue_body(home: str, number: int) -> str:
    """The raw body of an issue — the brief, as published."""
    try:
        out = post._run_gh(["api", f"repos/{home}/issues/{number}",
                            "--jq", ".body"])
    except CruxError as exc:
        log.info("could not read %s#%d: %s", home, number, exc)
        return ""
    return out


def publish(bundle: Bundle, card: str) -> tuple[int, str]:
    """Create or update the bundle's issue. Returns (issue_number, url)."""
    if not bundle.home or "/" not in bundle.home:
        raise PostError(f"super PR #{bundle.number} has no home repo to file its brief in")

    label = f": {bundle.name}" if bundle.name else ""
    title = f"🦸 Super PR #{bundle.number}{label} [super-pr-{bundle.number}]"
    number = bundle.issue or _find_issue(bundle.home, bundle.number)
    payload = json.dumps({"title": title, "body": card})

    if number is None:
        out = post._run_gh(
            ["api", "-X", "POST", f"repos/{bundle.home}/issues", "--input", "-"],
            stdin_text=payload)
        try:
            number = int(json.loads(out).get("number"))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PostError(f"could not read the new issue number: {exc}") from None
        log.info("filed super PR #%d as %s#%d", bundle.number, bundle.home, number)
    else:
        post._run_gh(
            ["api", "-X", "PATCH", f"repos/{bundle.home}/issues/{number}",
             "--input", "-"],
            stdin_text=payload)
        log.info("updated super PR #%d at %s#%d", bundle.number, bundle.home, number)

    return number, issue_url(bundle.home, number)


def _upsert(slug: str, number: int, body: str, marker: str) -> None:
    """Sticky comment on any repo's issue/PR, addressed by `owner/name`.

    post.upsert_comment takes a RepoInfo, which assumes one local repo. A
    bundle writes to repos it may have no clone of, so this addresses them by
    slug instead.
    """
    path = f"repos/{slug}/issues/{number}/comments"
    out = post._run_gh(["api", path, "--paginate"])
    existing = post._find_marker_comment_id(out, marker)
    payload = json.dumps({"body": body})
    if existing is not None:
        post._run_gh(["api", "-X", "PATCH",
                      f"repos/{slug}/issues/comments/{existing}", "--input", "-"],
                     stdin_text=payload)
    else:
        post._run_gh(["api", "-X", "POST", path, "--input", "-"],
                     stdin_text=payload)


def link_members(bundle: Bundle, url: str) -> list[str]:
    """Leave the pointer comment on every member PR.

    Returns problem lines. One unreachable repo must not sink the publish: the
    brief is already filed and useful, and a missing pointer is a smaller
    failure than an aborted run.
    """
    from crux.superrender import render_backlink
    problems: list[str] = []
    for member in bundle.members:
        slug = f"{member.owner}/{member.repo}"
        try:
            _upsert(slug, member.pr, render_backlink(bundle, url, member),
                    SUPER_LINK_MARKER)
        except CruxError as exc:
            problems.append(f"{slug}#{member.pr}: could not add the pointer ({exc})")
    return problems


def publish_test_steps(bundle: Bundle, steps: list[str], cfg=None) -> None:
    """Sticky the bundle's hand-verification steps under its brief issue.

    Sticky, not appended: a re-brief replaces the steps as the feature changes,
    the same way the brief itself is replaced. A log of stale walkthroughs is
    worse than none — the reader cannot tell which one is current.
    """
    from crux.superrender import render_test_comment
    if not bundle.issue:
        raise PostError(
            f"super PR #{bundle.number} has no issue to post its steps under")
    _upsert(bundle.home, bundle.issue, render_test_comment(bundle, steps, cfg),
            TEST_MARKER)
    log.info("published %d verification step(s) for super PR #%d",
             len(steps), bundle.number)


def comment(slug: str, number: int, body: str) -> None:
    """Append a plain comment (used for merge reports, which are a log, not a
    sticky state — each run's outcome stays readable in order)."""
    post._run_gh(["api", "-X", "POST", f"repos/{slug}/issues/{number}/comments",
                  "--input", "-"],
                 stdin_text=json.dumps({"body": body}))


def close_issue(bundle: Bundle) -> None:
    """Close the brief once every member has landed."""
    if not bundle.issue:
        return
    post._run_gh(
        ["api", "-X", "PATCH", f"repos/{bundle.home}/issues/{bundle.issue}",
         "--input", "-"],
        stdin_text=json.dumps({"state": "closed"}))
