# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: the super-PR bundle store.

GitHub has no cross-repo grouping. Stacked PRs come closest, but the API is
`/repos/{owner}/{repo}/stacks` — single-repo by construction — so a bundle that
spans web, api and worker cannot be one. Crux therefore owns
bundle state itself, and with it the rule that makes bundles trustworthy:

    a PR belongs to at most one super PR.

Without that rule two bundles could each claim the same PR, each compute a
merge order around it, and each report a landing plan the other invalidates.
`bundled_prs` is the index that enforces it, and the picker filters on it.

Store: `$XDG_CONFIG_HOME/crux/bundles/<number>.json`, one file per bundle, so
concurrent work on two bundles cannot clobber a shared file.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from crux.models import MERGE_METHODS, Bundle, BundleMember, CruxError

log = logging.getLogger("crux.bundle")


class BundleError(CruxError):
    """Raised when bundle state cannot be read, written, or reconciled."""


def store_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "crux" / "bundles"


def _path(number: int) -> Path:
    return store_dir() / f"{number}.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _from_dict(data: dict) -> Bundle:
    members = [
        BundleMember(
            owner=str(m.get("owner", "")), repo=str(m.get("repo", "")),
            branch=str(m.get("branch", "")), pr=int(m.get("pr", 0) or 0),
            head_sha=str(m.get("head_sha", "")), base=str(m.get("base", "main")),
            author=str(m.get("author", "")),
            state=str(m.get("state", "")), error=str(m.get("error", "")),
        )
        for m in data.get("members", []) if isinstance(m, dict)
    ]
    issue = data.get("issue")
    return Bundle(
        number=int(data.get("number", 0) or 0),
        # "group" is what the field was called before super PRs drew from the
        # configured scope; bundles saved then still read back with a label.
        name=str(data.get("name") or data.get("group", "")),
        members=members,
        home=str(data.get("home", "")),
        issue=int(issue) if isinstance(issue, int) else None,
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
        order=[str(x) for x in data.get("order", []) if isinstance(x, str)],
        slack_channel=str(data.get("slack_channel", "")),
        slack_ts=str(data.get("slack_ts", "")),
        closed=bool(data.get("closed", False)),
        test_steps=[str(x) for x in data.get("test_steps", [])
                    if isinstance(x, str)],
        rev=int(data.get("rev", 0) or 0),
        # D41. Missing keys read as "never pinned, no method of its own", which
        # is exactly what a bundle saved — or a brief published — before they
        # existed meant. The method is checked against the three GitHub takes,
        # because a brief's state block is text anyone with write access to
        # the home repo can edit, and it ends up in a merge call.
        order_pinned=bool(data.get("order_pinned", False)),
        merge_method=_method(data.get("merge_method")),
        suggested_order=[str(x) for x in data.get("suggested_order", [])
                         if isinstance(x, str)],
        order_why=str(data.get("order_why", "") or ""),
    )


def _method(raw: object) -> str:
    method = raw.strip().lower() if isinstance(raw, str) else ""
    return method if method in MERGE_METHODS else ""


def load(number: int) -> Bundle | None:
    try:
        raw = _path(number).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return _from_dict(json.loads(raw))
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise BundleError(f"bundle {number} is unreadable: {exc}") from exc


def load_all() -> list[Bundle]:
    """Every bundle on this machine, newest number first. A single corrupt
    file must not hide the rest, so unreadable ones are skipped."""
    out: list[Bundle] = []
    try:
        entries = sorted(store_dir().glob("*.json"))
    except OSError:
        return []
    for entry in entries:
        try:
            out.append(_from_dict(json.loads(entry.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda b: b.number, reverse=True)


def save(bundle: Bundle) -> None:
    """Write the bundle, counting the write as a new revision.

    Every path that CHANGES a bundle comes through here — create, add, remove,
    merge, close, refresh — so bumping the revision here is what makes "has
    this copy moved on?" answerable without every caller having to remember to
    say so. The one write that must not count is the cache a reader keeps of
    someone else's brief (`_mirror`): counting that would let a stale copy
    out-number the brief it was made from, and pin itself in place forever.
    """
    bundle.rev += 1
    _write(bundle)


def _mirror(bundle: Bundle) -> None:
    """Cache a bundle rebuilt from its brief, at the revision the brief named."""
    _write(bundle)


def _write(bundle: Bundle) -> None:
    bundle.updated_at = _now()
    if not bundle.created_at:
        bundle.created_at = bundle.updated_at
    path = _path(bundle.number)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(bundle), indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash mid-write never truncates state
    except OSError as exc:
        raise BundleError(f"could not save bundle {bundle.number}: {exc}") from exc


def delete(number: int) -> bool:
    try:
        _path(number).unlink()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise BundleError(f"could not delete bundle {number}: {exc}") from exc


def next_number(home: str = "") -> int:
    """Bundle numbers are monotonic and never reused, so a deleted bundle's
    number stays retired and old links keep meaning what they meant.

    The local store alone cannot answer this. It is per-machine, while the
    briefs it numbers live in one shared repo, so a number free here may
    already name someone else's bundle — or one this machine made and has
    since forgotten. *home* is the group's brief repo; when it is given, the
    numbers already filed there are counted as taken too, which is what makes
    the number mean the same thing on every laptop. Left empty (or when the
    repo cannot be read) this falls back to the local-only answer.
    """
    existing = [b.number for b in load_all()]
    taken = max(existing) if existing else 0
    if home and "/" in home:
        import crux.superpost as superpost
        taken = max(taken, superpost.highest_brief(home))
    return taken + 1


def key(owner: str, repo: str, pr: int) -> str:
    """The cross-repo identity of a PR: `owner/repo#number`."""
    return f"{owner}/{repo}#{pr}"


# D38: the bundle's own state, carried inside the brief issue it publishes.
#
# Bundles are made on one machine, but the brief is read — and its buttons
# pressed — on everyone else's. The person guaranteed to hold the local file is
# the author, who is precisely the person not allowed to merge it, so
# local-only state made every button work for exactly the wrong person. The
# brief already travels to every reader, so it carries what it needs to be
# rebuilt anywhere. Hidden in a comment because it is machinery, not reading.
_STATE_RE = re.compile(r"<!--\s*crux:super-state\s+(\{.*?\})\s*-->", re.DOTALL)


def encode_state(bundle: Bundle) -> str:
    """The bundle as one hidden HTML comment, for the end of the brief."""
    payload = {
        "number": bundle.number,
        "name": bundle.name,
        "rev": bundle.rev,
        "order": list(bundle.order),
        "test_steps": list(bundle.test_steps),
        # D41: how it lands travels with what lands. Without these a teammate's
        # button would merge in the review pass's order with the default
        # method, whatever the author pinned.
        "order_pinned": bundle.order_pinned,
        "merge_method": bundle.merge_method,
        "suggested_order": list(bundle.suggested_order),
        "order_why": bundle.order_why,
        "members": [
            {"owner": m.owner, "repo": m.repo, "branch": m.branch, "pr": m.pr,
             "base": m.base, "author": m.author, "state": m.state}
            for m in bundle.members
        ],
    }
    # `>` is escaped so the payload can never contain `-->` and close its own
    # comment early — a branch named "a-->b" would otherwise truncate the state
    # and take the rest of the brief with it. JSON reads > back as `>`.
    return ("<!-- crux:super-state "
            + json.dumps(payload, separators=(",", ":")).replace(">", "\\u003e")
            + " -->")


def strip_state(body: str) -> str:
    """*body* without its state block — so a rewritten brief carries one."""
    return _STATE_RE.sub("", body or "").rstrip()


def decode_state(body: str, home: str) -> Bundle | None:
    """Rebuild a bundle from a brief's hidden state block, or None.

    *home* comes from where the issue was actually found, never from the
    payload: a doctored block must not be able to point Crux's next write at a
    repo of its choosing.
    """
    match = _STATE_RE.search(body or "")
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("members"):
        return None
    bundle = _from_dict({**data, "home": home})
    return bundle if bundle.number and bundle.members else None


def _adopt(local: Bundle, fresh: Bundle) -> Bundle:
    """The brief's membership, on top of this machine's own copy.

    A merge rather than a replacement, because the two carry different things.
    The brief is authoritative about WHAT IS IN the bundle — members, order,
    name, test steps, and how it lands (D41) — and carries nothing else, by
    design. The local copy holds the side-channel state no reader needs and
    the brief never sees: the Slack thread every refresh replies into, when it
    was created, whether it was closed here. Replacing it wholesale would drop `slack_ts` and quietly
    turn one bundle's conversation into a new channel post per re-brief.
    """
    local.name = fresh.name
    local.members = fresh.members
    local.order = fresh.order
    local.test_steps = fresh.test_steps
    local.order_pinned = fresh.order_pinned
    local.merge_method = fresh.merge_method
    local.suggested_order = fresh.suggested_order
    local.order_why = fresh.order_why
    local.rev = fresh.rev
    if fresh.issue:
        local.issue = fresh.issue
    return local


def _from_brief(number: int, home: str, issue: int | None) -> Bundle | None:
    """The bundle as its published brief has it, or None if it cannot be read."""
    import crux.superpost as superpost
    if issue is None:
        issue = superpost.find_brief(home, number)
    if issue is None:
        return None
    found = decode_state(superpost.issue_body(home, issue), home)
    if found is not None:
        found.issue = issue
    return found


def hydrate(number: int, cfg, home: str = "") -> Bundle | None:
    """This machine's bundle *number*, reconciled with its published brief.

    Local-first was the original rule and it was wrong in the case the buttons
    exist for. A teammate's first press cached the brief locally; every press
    after that read the cache and never looked at the brief again, so a member
    added later was invisible to everyone except the author — each reader
    pinned forever to the membership that happened to be published the first
    time they pressed anything. The brief is what changes when a bundle
    changes, so the brief has to be consulted when it can be.

    The local copy still wins when it is AHEAD: between a `--no-brief` edit and
    the next refresh, this machine holds membership the brief has not been told
    about yet, and a "refresh" that first reverted its own change would be a
    trap. `rev` is what distinguishes the two, and it is compared, never
    subtracted — a reader's cache and an author's file are numbered by
    different machines, so only "did the brief move past what I hold" is a
    question either can answer.

    Ties go to the brief. Equal revisions mean the same content, except in the
    one window that cannot be numbered — a bundle saved before revisions
    existed, where both sides read 0 — and there the published brief is the
    better guess than a cache of unknown age.

    Never raises and never blocks on the network: an unreachable brief, an
    unauthenticated `gh`, a read-only config dir all fall back to what is here.
    """
    try:
        local = load(number)
    except CruxError:
        local = None

    home = (home or (local.home if local else "")
            or getattr(cfg, "super_home", "") or "").strip().strip("/")
    if "/" not in home:
        return local

    try:
        fresh = _from_brief(number, home, local.issue if local else None)
    except CruxError as exc:
        log.info("could not read super PR #%d's brief: %s", number, exc)
        return local

    if fresh is None:
        return local
    if local is None:
        try:
            _mirror(fresh)
        except CruxError:
            pass  # a read-only config dir must not stop the button working
        return fresh
    if fresh.rev < local.rev:
        return local            # unpublished work here; the brief will catch up

    adopted = _adopt(local, fresh)
    try:
        _mirror(adopted)
    except CruxError:
        pass
    return adopted


def find_by_branch(owner: str, repo: str, branch: str) -> Bundle | None:
    """The bundle this branch belongs to, if any — the push-time lookup.

    A push knows its repo and branch, not its PR number, so membership has to
    be answerable without asking GitHub anything: a git hook must not make a
    network call to decide whether it has work to do. Newest bundle wins if a
    branch somehow appears twice; the one-PR-one-bundle rule makes that a
    corrupt-state case, not a normal one.
    """
    for bundle in sorted(load_all(), key=lambda b: b.number, reverse=True):
        for member in bundle.members:
            if (member.owner.lower() == owner.lower()
                    and member.repo.lower() == repo.lower()
                    and member.branch == branch):
                return bundle
    return None


def bundled_prs(exclude: int | None = None) -> dict[str, int]:
    """Map every already-bundled PR to its bundle number.

    *exclude* skips one bundle, so editing a bundle does not see its own
    members as taken. Members that already merged are still listed: their PR
    is spent, and re-bundling it would recompute a plan around a landed change.
    """
    out: dict[str, int] = {}
    for bundle in load_all():
        if exclude is not None and bundle.number == exclude:
            continue
        # A closed bundle releases its PRs: the reading was retired, so the
        # work in it is free to be bundled again rather than held forever.
        if bundle.closed:
            continue
        for member in bundle.members:
            if member.pr:
                out[key(member.owner, member.repo, member.pr)] = bundle.number
    return out
