# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: land the whole bundle in one call — and say precisely what went wrong.

Policy is **land what can, report the rest**. A cross-repo bundle cannot merge
atomically: GitHub's atomic stack merge is single-repo, so N repos means N
independent merges and there is no transaction spanning them. Pretending
otherwise would be worse than admitting it — a "rollback" that force-pushes
reverts across three repos is far more dangerous than a half-landed bundle
plainly reported.

So this does the honest thing: try every PR in the computed order, stop nothing
on the first failure, and return a per-PR verdict. Re-running is safe and
expected — anything already merged is skipped, so the loop is "fix a blocker,
run again" until the report is all green.

The errors are the product here. "Merge failed" is useless; "required check
`build` is failing" tells someone what to go do.
"""
from __future__ import annotations

import json
import logging

import crux.post as post
from crux.models import MERGE_METHODS, Bundle, BundleMember, Config, CruxError

log = logging.getLogger("crux.supermerge")

# D41: the words a reader of the brief knows each merge method by.
_METHOD_LABEL = {"merge": "merge commits", "squash": "squash and merge",
                 "rebase": "rebase and merge"}


def method_label(method: str) -> str:
    return _METHOD_LABEL.get(method, method)


def resolve_method(bundle: Bundle, cfg: Config | None = None,
                   explicit: str = "") -> str:
    """The merge method this bundle lands with (D41).

    First answer wins: the caller's own `--method`, the bundle's (set with
    `crux super order N --method`, and carried in the brief so every presser's
    Crux reads the same one), `[super] merge_method`, then squash — which is
    what every merge did before any of this existed, so a bundle nobody
    configured lands exactly as it always has.

    The bundle's value is the one that matters for the brief's button: the
    config is per machine, so it is whatever the person who CLICKED has, not
    what the author has. A bundle that needs merge commits says so itself.
    """
    config_method = cfg.super_merge_method if cfg is not None else ""
    for method in (explicit, bundle.merge_method, config_method):
        method = (method or "").strip().lower()
        if method in MERGE_METHODS:
            return method
        if method:
            log.warning("ignoring unknown merge method %r for super PR #%d",
                        method, bundle.number)
    return "squash"

# Mergeable-state values GitHub reports, mapped to what a human should do.
# `mergeable_state` is the field that explains a refusal; `mergeable` alone
# only says yes/no/unknown.
_STATE_REASON = {
    "dirty": "has merge conflicts that must be resolved first",
    "blocked": "is blocked — a required review or status check has not passed",
    "behind": "is behind its base branch and must be updated first",
    "draft": "is still a draft",
    "unstable": "has a failing or pending check",
    "unknown": "could not be evaluated by GitHub yet — try again shortly",
}


def _pr_state(slug: str, pr: int) -> dict:
    out = post._run_gh(["api", f"repos/{slug}/pulls/{pr}"])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise CruxError(f"unreadable PR state for {slug}#{pr}: {exc}") from None
    return data if isinstance(data, dict) else {}


def _failing_checks(slug: str, sha: str) -> list[str]:
    """Names of check runs that are not passing, so the report can name them.

    Best-effort: a repo with no checks, or an API hiccup, degrades to an empty
    list rather than failing the merge run.
    """
    try:
        out = post._run_gh(
            ["api", f"repos/{slug}/commits/{sha}/check-runs?per_page=100"])
        data = json.loads(out)
    except (CruxError, json.JSONDecodeError):
        return []
    bad: list[str] = []
    for run in (data.get("check_runs") or []) if isinstance(data, dict) else []:
        if not isinstance(run, dict):
            continue
        conclusion = run.get("conclusion")
        status = run.get("status")
        if status != "completed":
            bad.append(f"{run.get('name', 'check')} (still running)")
        elif conclusion not in ("success", "neutral", "skipped"):
            bad.append(f"{run.get('name', 'check')} ({conclusion})")
    return bad[:4]


def _explain(slug: str, pr: int, data: dict) -> str:
    """Turn a refusal into an instruction."""
    if data.get("draft"):
        return "is still a draft — mark it ready for review"
    state = str(data.get("mergeable_state") or "unknown")
    reason = _STATE_REASON.get(state, f"is not mergeable ({state})")
    if state in ("blocked", "unstable"):
        sha = str((data.get("head") or {}).get("sha") or "")
        failing = _failing_checks(slug, sha) if sha else []
        if failing:
            return f"{reason}: {', '.join(failing)}"
    return reason


def _merge_one(slug: str, pr: int, method: str) -> tuple[bool, str]:
    """Attempt one merge. Returns (merged, error)."""
    try:
        data = _pr_state(slug, pr)
    except CruxError as exc:
        return False, f"could not be read from GitHub ({exc})"

    if data.get("merged"):
        return True, ""
    if str(data.get("state")) != "open":
        return False, f"is {data.get('state')}, not open"
    if data.get("mergeable") is False or data.get("draft"):
        return False, _explain(slug, pr, data)

    try:
        post._run_gh(
            ["api", "-X", "PUT", f"repos/{slug}/pulls/{pr}/merge", "--input", "-"],
            stdin_text=json.dumps({"merge_method": method}))
        return True, ""
    except CruxError as exc:
        # GitHub's 405 body carries the real reason; fall back to the state
        # check so the message is actionable either way.
        detail = str(exc)
        if "Merge conflict" in detail:
            return False, "has merge conflicts that must be resolved first"
        try:
            return False, _explain(slug, pr, _pr_state(slug, pr))
        except CruxError:
            return False, f"was refused by GitHub ({detail.splitlines()[0][:160]})"


def order_members(bundle: Bundle) -> list[BundleMember]:
    """Members in the bundle's computed landing order.

    A member missing from `order` still merges — it goes last. Dropping a PR
    from the plan because the model forgot to list it would silently ship a
    partial bundle.
    """
    index = {ref: i for i, ref in enumerate(bundle.order)}
    return sorted(
        bundle.members,
        key=lambda m: index.get(f"{m.owner}/{m.repo}#{m.pr}", len(index)))


def run(bundle: Bundle, method: str = "squash",
        skip: set[str] | None = None) -> list[BundleMember]:
    """Merge the bundle. Mutates and returns its members with verdicts set.

    *skip* holds `owner/repo#pr` refs the caller has already ruled out, with
    their `state`/`error` already set — a PR the presser wrote, say (D38).
    Skipped members still appear in the results, because a report that quietly
    omitted one would read as "everything landed".
    """
    skip = skip or set()
    results: list[BundleMember] = []
    for member in order_members(bundle):
        slug = f"{member.owner}/{member.repo}"
        if member.state == "merged" or f"{slug}#{member.pr}" in skip:
            results.append(member)
            continue
        merged, error = _merge_one(slug, member.pr, method)
        member.state = "merged" if merged else "blocked"
        member.error = "" if merged else error
        if merged:
            log.info("merged %s#%d", slug, member.pr)
        else:
            log.warning("could not merge %s#%d: %s", slug, member.pr, error)
        results.append(member)
    return results


def summary(results: list[BundleMember]) -> str:
    """One terminal line: the outcome at a glance."""
    landed = sum(1 for m in results if m.state == "merged")
    if landed == len(results):
        return f"✅ all {landed} pull request{'s' if landed != 1 else ''} landed"
    return (f"⚠️  {landed} of {len(results)} landed — "
            f"{len(results) - landed} still blocked")
