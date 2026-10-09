# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: the super-PR workflow, as one core both front ends call.

`crux super …` on the terminal and the Claude plugin skill must do the SAME
thing — same selection rules, same single analysis pass, same brief, same merge
policy. So neither owns any logic: both call this module, and the only
difference between them is how the picker's numbers get chosen.

The whole feature is three verbs:

    create  — pick branches/PRs from the configured scope, open PRs for
              anything without one, and record the bundle
    refresh — combined diff, one harvest, ONE model call, publish the brief
    merge   — land what can, report the rest

— plus the human's say over the last one (D41): `set_landing` pins the order
and the merge method, and `republish` restamps the brief without a model call.

`create` and `refresh` are separate because selection is a human decision and
analysis is expensive; every later `refresh` re-briefs the same bundle as its
PRs move, without asking the picker again.
"""
from __future__ import annotations

import copy
import logging
from typing import Callable

import crux.bundle as bundle_store
import crux.candidates as candidates
import crux.clones as clones
import crux.config as config
import crux.gitio as gitio
import crux.memory as memory
import crux.post as post
import crux.superanalyze as superanalyze
import crux.superdiff as superdiff
import crux.supermerge as supermerge
import crux.superpost as superpost
import crux.superrender as superrender
from crux.models import (MERGE_METHODS, SUPER_ENV, SUPER_MARKER, Bundle,
                         BundleMember, Candidate, Config, CruxError, Memory)

log = logging.getLogger("crux.superpr")

# A front end's way of showing slow cross-repo work as it happens.
Progress = Callable[[str], None]


class SuperError(CruxError):
    """Raised when a super PR cannot be created, briefed, or merged."""


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

# Every push below carries SUPER_ENV (crux/models.py), so the pre-push hook
# stands down: no per-repo review card, no per-repo Slack announcement, and no
# per-repo "Create a PR for <branch>?" prompt — which arrives with no repo name
# on it and asks a question the picker already answered. The env var, rather
# than `push --no-verify`, keeps the repo's OWN pre-push hook running: crux
# suppresses crux, not the user's tests.
def _ensure_pr(cand: Candidate, cfg: Config,
               progress: "Progress | None" = None,
               base: str = "") -> tuple[int | None, str, str]:
    """Open a PR for a selected branch that has none (D37).

    Selecting a branch is the point of commitment, so this pushes and opens the
    PR rather than sending the user away to do it by hand. *base* overrides the
    branch the PR targets (the answer to the front end's ask); empty means the
    branch this one was created from. Returns (pr, base, error).
    """
    if cand.pr:
        return cand.pr, "", ""
    if not cand.path:
        return None, "", (f"{cand.slug} has no local clone here, so its branch "
                          f"{cand.branch!r} cannot be pushed")
    try:
        info = gitio.repo_info(cwd=cand.path)
    except CruxError as exc:
        return None, "", f"{cand.slug}: could not read the repo ({exc})"
    if info.branch != cand.branch:
        return None, "", (f"{cand.slug} is no longer on {cand.branch!r} "
                          f"(now {info.branch!r}) — check it out and try again")
    try:
        gitio.run_git(["push", "--set-upstream", "origin", cand.branch],
                      cwd=cand.path, env={SUPER_ENV: "1"})
    except CruxError as exc:
        return None, "", f"{cand.slug}: could not push {cand.branch!r} ({exc})"
    base = base.strip() or post.default_base(info, cfg)
    try:
        # Non-interactive: the human already chose this branch AND its base from
        # the super picker's own ask, so a second prompt would ask twice.
        number = post._create_pr(info, base, cfg)
    except CruxError as exc:
        return None, "", (f"{cand.slug}: could not open a PR for "
                          f"{cand.branch!r} into {base!r} ({exc})")
    log.info("opened %s#%d for %s into %s", cand.slug, number, cand.branch, base)
    if progress is not None:
        progress(f"opened {cand.slug}#{number} — {cand.branch} → {base}")
    return number, base, ""


def needs_pr(picks: list[Candidate]) -> list[Candidate]:
    """The selected candidates that have no PR yet, in picked order.

    Both front ends name these — with their repos — before anything is pushed,
    so the one question the super flow asks is its own.
    """
    return [c for c in picks if not c.pr]


def pr_plan(picks: list[Candidate], cfg: Config) -> list[tuple[Candidate, str]]:
    """(candidate, base) for every pick that still needs a PR opened.

    The base is read the same way a single-repo PR reads it — the branch this
    branch was created from (`branch.<name>.cruxBase`, recorded at checkout),
    else the configured default — so the front end can SHOW where each new PR
    would land and let the user redirect it, instead of finding out afterwards.
    A clone that cannot be read yields an empty base here rather than raising:
    the real, specific failure belongs to `_ensure_pr`, once, with its repo
    named. Existing PRs are absent — their base is already decided.
    """
    plan: list[tuple[Candidate, str]] = []
    for cand in needs_pr(picks):
        base = ""
        if cand.path:
            try:
                base = post.default_base(gitio.repo_info(cwd=cand.path), cfg)
            except CruxError:
                base = ""
        plan.append((cand, base))
    return plan


def home_repo(cfg: Config) -> str:
    """The `owner/name` repo holding super-PR issues.

    A cross-repo brief has no natural repo of its own — GitHub has no repo-less
    issue — so one is nominated in config rather than guessed: parking the
    brief in whichever member repo happened to be picked first would be
    arbitrary, and would move as the bundle changes.
    """
    home = cfg.super_home.strip().strip("/")
    if not home or "/" not in home:
        raise SuperError(
            "no home repo for super PR issues — "
            'set [super] home = "owner/name" in crux.toml')
    return home


def _members_from(cfg: Config, picks: list[Candidate],
                  progress: "Progress | None",
                  base: str, dry_run: bool = False,
                  ) -> tuple[list[BundleMember], list[str]]:
    """Chosen candidates as bundle members, opening PRs for the ones without.

    Shared by `create` and `add` so the two cannot drift: a PR joining a bundle
    on its second day has to be built exactly like one that was there on its
    first, or the brief would report them differently for no reason a reader
    could see.

    *dry_run* opens nothing. Opening a PR means pushing the branch and filing
    it on GitHub — about as outward-facing as Crux gets — and a preview must
    not do either. A branch with no PR has no head to diff yet, so it is left
    out of the preview and named, rather than silently dropped.
    """
    members: list[BundleMember] = []
    problems: list[str] = []
    for cand in picks:
        if dry_run and not cand.pr:
            problems.append(f"{cand.slug}: {cand.branch!r} has no PR yet — a "
                            f"dry run opens none, so it is left out of this "
                            f"preview")
            continue
        number, opened_base, error = _ensure_pr(cand, cfg, progress, base)
        if number is None:
            problems.append(error)
            continue
        member = BundleMember(
            owner=cand.owner, repo=cand.repo, branch=cand.branch, pr=number,
            author=cand.author)
        if opened_base:
            # Only for PRs opened here: an existing PR's base lives on GitHub,
            # and guessing it locally would be a worse answer than the default.
            member.base = opened_base
        members.append(member)
    return members, problems


def add(cfg: Config, bundle: Bundle, picks: list[Candidate],
        progress: "Progress | None" = None,
        base: str = "", dry_run: bool = False) -> tuple[Bundle, list[str]]:
    """Add PRs to a bundle that already exists. Returns (bundle, problems).

    A cross-repo change does not always arrive knowing its own extent: the repo
    nobody expected to touch turns up on day three, and before this the only
    ways to include it were to hand-edit the stored bundle or to throw the
    super PR away and rebuild it — losing its number, its brief, and the Slack
    thread every refresh has been replying to. The number is the thing people
    link to, so keeping it is the whole point.

    Members already in the bundle are reported, not re-added. The picker
    already hides them (`candidates.gather` drops every PR any open bundle
    holds), so this is for an explicit `--pick`, and for the case where opening
    a PR for a branch lands on a number the bundle turns out to have.

    Nothing is removed here, and nothing merged is touched: this only ever
    grows the set. A member that has already landed stays exactly as it is.

    *dry_run* returns what the bundle WOULD be — a copy, never saved, with no
    PR opened — so the preview that follows can brief it (see `_members_from`).
    """
    if bundle.closed:
        raise SuperError(
            f"super PR #{bundle.number} is closed — its PRs are released back "
            f"to the picker, so bundle them into a new one with `crux super new`")

    if dry_run:
        bundle = copy.deepcopy(bundle)
    members, problems = _members_from(cfg, picks, progress, base, dry_run)
    have = {bundle_store.key(m.owner, m.repo, m.pr) for m in bundle.members}
    fresh: list[BundleMember] = []
    for member in members:
        marker = bundle_store.key(member.owner, member.repo, member.pr)
        if marker in have:
            problems.append(f"{member.owner}/{member.repo}#{member.pr} is "
                            f"already in super PR #{bundle.number}")
            continue
        have.add(marker)
        fresh.append(member)

    if not fresh:
        raise SuperError(
            f"nothing was added to super PR #{bundle.number}:\n  "
            + "\n  ".join(problems or ["no candidates were selected"]))

    bundle.members.extend(fresh)
    # D41: a pinned order is the plan, so it names every member — the new ones
    # go last, after everything a human already put in sequence. An unpinned
    # one is recomputed by the refresh that follows, so it is left alone.
    if bundle.order_pinned:
        _settle_order(bundle)
    # Saved before the brief is rebuilt, so a re-brief that fails (no network,
    # a repo that will not diff) still leaves the membership change on disk —
    # `crux super refresh` then finishes the job rather than starting it.
    if not dry_run:
        bundle_store.save(bundle)
    return bundle, problems


def remove(bundle: Bundle, picks: list[BundleMember],
           dry_run: bool = False) -> tuple[Bundle, list[str]]:
    """Detach PRs from a bundle. Returns (bundle, problems).

    Membership only. The pull request itself is untouched on GitHub — not
    closed, not commented on, not retargeted: it goes back to being an ordinary
    PR, reviewed on its own card like any other. It also goes back into the
    picker without anything having to say so, because `bundled_prs` reads the
    bundles, so a PR stops being taken the moment it stops being a member.

    A member that has already MERGED stays, and says why. The brief is the
    record of what landed as one change; dropping a piece of it afterwards
    would leave the record describing something that never happened.

    The last member cannot be removed either — a super PR holding nothing is a
    brief about nothing, and `crux super close` is how a reading is retired.

    *dry_run* works on a copy and saves nothing, so a preview of the smaller
    bundle leaves the real one exactly as it was.
    """
    if bundle.closed:
        raise SuperError(
            f"super PR #{bundle.number} is closed — its PRs are already "
            f"released, so there is nothing to detach")
    if dry_run:
        bundle = copy.deepcopy(bundle)

    wanted = {bundle_store.key(m.owner, m.repo, m.pr) for m in picks}
    problems: list[str] = []
    keep: list[BundleMember] = []
    dropped: list[BundleMember] = []
    for member in bundle.members:
        marker = bundle_store.key(member.owner, member.repo, member.pr)
        if marker not in wanted:
            keep.append(member)
            continue
        if member.state == "merged":
            problems.append(
                f"{member.owner}/{member.repo}#{member.pr} already merged as "
                f"part of this super PR — it stays on the brief")
            keep.append(member)
            continue
        dropped.append(member)

    if not dropped:
        raise SuperError(
            f"nothing was removed from super PR #{bundle.number}:\n  "
            + "\n  ".join(problems or ["no members were selected"]))
    if not keep:
        raise SuperError(
            f"super PR #{bundle.number} would be left with no pull requests — "
            f"retire the reading with `crux super close {bundle.number}` "
            f"instead, which releases every PR in it")

    bundle.members = keep
    # The merge order names members by "owner/repo#pr"; a dropped one left in
    # it would still be printed by `crux super show` after a --no-brief removal.
    # Refresh recomputes the list, this keeps it honest until then.
    surviving = {f"{m.owner}/{m.repo}#{m.pr}" for m in keep}
    bundle.order = [ref for ref in bundle.order if ref in surviving]
    if not dry_run:
        bundle_store.save(bundle)
    return bundle, problems


def render_members(b: Bundle) -> str:
    """The bundle's own members, numbered for the remove picker.

    In `members` order rather than merge order: the number a person types has
    to mean the same thing it meant when they read it, and `order` is
    recomputed by every refresh that is not pinned (D41).
    """
    width = len(str(len(b.members)))
    lines: list[str] = []
    for i, m in enumerate(b.members, 1):
        mark = {"merged": "✅", "blocked": "❌"}.get(m.state, "  ")
        lines.append(f"  {str(i).rjust(width)}. {mark} "
                     f"{m.owner}/{m.repo}#{m.pr}  ({m.branch})")
    return "\n".join(lines)


def create(cfg: Config, picks: list[Candidate], name: str = "",
           progress: "Progress | None" = None,
           base: str = "", dry_run: bool = False) -> tuple[Bundle, list[str]]:
    """Turn chosen candidates into a saved bundle, opening PRs as needed.

    *progress* receives one line per PR opened, each naming its repo — opening
    PRs across several repos is the one slow, outward-facing step here, and it
    should read as the super flow's own work rather than going silent.

    *base* is the branch every NEW PR targets, from the front end's ask. Empty
    (the default) means each takes the branch it was created from, which is
    what `pr_plan` showed. It never touches a PR that already exists.

    *dry_run* builds the bundle a real run would create — under the number it
    would get, so the preview reads like the brief — and neither saves it nor
    opens any PR. A dry run that left a bundle file behind was not a dry run:
    `crux super list` showed a super PR nobody made, its members were barred
    from every other bundle, and the next push to one of its branches re-briefed
    it — publishing a brief for a bundle that was only ever a preview.
    """
    home = home_repo(cfg)
    members, problems = _members_from(cfg, picks, progress, base, dry_run)

    if not members:
        raise SuperError(
            "nothing could be added to the super PR:\n  "
            + "\n  ".join(problems or ["no candidates were selected"]))

    # Unnamed bundles take the first member's branch: work that spans repos
    # usually shares a branch name, so this is the label the user would have
    # typed anyway — and a nameless entry in `crux super list` helps no one.
    new = Bundle(number=bundle_store.next_number(home),
                 name=(name.strip() or members[0].branch),
                 members=members, home=home)
    if not dry_run:
        bundle_store.save(new)
    return new, problems


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------

def _clone_paths(cfg: Config, members: list[BundleMember],
                 repo_root: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """Local clone path and default branch per member repo, keyed by slug."""
    wanted = sorted({f"{m.owner}/{m.repo}" for m in members})
    found = clones.find_clones(clones.search_roots(cfg, repo_root), wanted)
    paths = {slug: clone.path for slug, clone in found.items()}
    defaults = {slug: clone.default_branch for slug, clone in found.items()}
    return paths, defaults


def _memories(diffs: list[superdiff.RepoDiff], cfg: Config) -> list[Memory]:
    """What Crux remembers about the repos in this bundle (D31)."""
    if not cfg.memory_enabled:
        return []
    out: list[Memory] = []
    for rd in diffs:
        try:
            out.extend(memory.load(rd.info()))
        except CruxError:
            continue
    return out


def refresh(bundle: Bundle, cfg: Config, repo_root: str | None = None,
            no_llm: bool = False, publish: bool = True,
            ) -> tuple[str, str, list[str]]:
    """Re-brief a bundle. Returns (card, issue_url, problems).

    This is the expensive verb and the one that must stay honest about cost:
    exactly one model call happens inside it, regardless of how many PRs the
    bundle holds.
    """
    paths, defaults = _clone_paths(cfg, bundle.members, repo_root)
    diffs, problems = superdiff.build_all(bundle.members, paths=paths,
                                          defaults=defaults)
    if not diffs:
        raise SuperError("no repository in this super PR could be diffed:\n  "
                         + "\n  ".join(problems or ["unknown reason"]))

    evidence = superanalyze.harvest(diffs, cfg)
    if no_llm:
        annotation = superanalyze.SuperAnnotation()
    else:
        annotation = superanalyze.annotate(
            bundle, evidence, diffs, cfg, memories=_memories(diffs, cfg))

    # D41: the model's proposal is always kept — it is the reasoning a pinned
    # brief still shows, and what `--unpin` hands back — but it only becomes
    # the landing order when no human has decided one.
    bundle.suggested_order = list(annotation.order)
    bundle.order_why = annotation.order_why
    if bundle.order_pinned:
        _settle_order(bundle)
    else:
        bundle.order = [r for r in annotation.order] or [
            _ref(m) for m in bundle.members]
    # Set before rendering: the card carries the bundle's state (D38), and the
    # steps are part of what a teammate's machine needs.
    bundle.test_steps = list(annotation.integration_test)
    card = superrender.render_card(bundle, annotation, diffs, cfg)
    if not publish:
        # A dry run must show everything the real one would post, or the
        # preview quietly omits the steps and they land unreviewed.
        if annotation.integration_test:
            card += "\n\n---\n\n" + superrender.render_test_comment(
                bundle, annotation.integration_test, cfg)
        return card, "", problems

    # D38: the brief about to be published carries Merge/Close/Set-up-to-test
    # buttons, which are dead links unless the loopback service is up.
    try:
        import crux.serve as serve
        serve.ensure_running(cfg, log)
    except Exception as exc:  # noqa: BLE001 — never sink a publish over this
        log.info("could not check the crux serve port: %s", exc)

    number, url = superpost.publish(bundle, card)
    bundle.issue = number
    bundle_store.save(bundle)
    if annotation.integration_test:
        # Second sticky comment on the brief, exactly as the per-PR card does
        # it: "what to look at" and "how to see it work" are different reads,
        # and keeping them apart is what holds the brief to one screen.
        try:
            superpost.publish_test_steps(bundle, annotation.integration_test,
                                         cfg)
        except CruxError as exc:
            problems.append(f"could not post the verification steps ({exc})")
    problems.extend(superpost.link_members(bundle, url))
    _announce_slack(bundle, cfg, url, annotation.thesis)
    bundle_store.save(bundle)
    return card, url, problems


def _announce_slack(bundle: Bundle, cfg: Config, url: str,
                    thesis: str = "") -> None:
    """D16/D37: tell Slack, if it is configured. Best-effort, exactly like the
    per-PR announcement — a bundle is published and useful whether or not the
    chat message lands, so this never raises into the caller."""
    log_ = logging.getLogger("crux.superpr")
    try:
        import crux.slack as slack
        if not slack.enabled(cfg):
            return
        authors = [slack.short_name(m.author) for m in bundle.members if m.author]
        channel, ts = slack.announce_super(cfg, bundle, url, thesis,
                                           previous_ts=bundle.slack_ts,
                                           authors=authors)
        if ts:
            bundle.slack_channel, bundle.slack_ts = channel, ts
            log_.info("announced super PR #%d to slack channel %s",
                      bundle.number, channel)
            post.notify_tty(f"💬 Crux: posted Super PR #{bundle.number} to Slack")
        else:
            # Configured but failed (bad scope, bot not in channel…). Silence
            # here is how a broken Slack setup stays invisible for weeks.
            post.notify_tty(
                f"⚠️ Crux: could not post Super PR #{bundle.number} to Slack "
                f"(channel {cfg.slack_channel!r}) — details in "
                "~/.cache/crux/crux.log")
    except Exception as exc:
        log_.warning("slack announce failed for super PR #%d: %s",
                     bundle.number, exc)


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def merge(bundle: Bundle, cfg: Config, method: str = "",
          report: bool = True, skip: set[str] | None = None,
          note: str = "") -> tuple[list[BundleMember], str]:
    """Land the bundle and record the outcome. Returns (members, summary).

    *skip* is passed through to supermerge (members the caller already ruled
    out), and *note* is one line the report and the Slack reply both carry —
    how this merge happened, when that is not the ordinary way (D38).

    *method* is an explicit override ("" for none). Resolved HERE, the one
    function both doors reach, so the terminal and the brief's button cannot
    come to different answers about how a bundle lands (D41).
    """
    method = supermerge.resolve_method(bundle, cfg, method)
    log.info("merging super PR #%d with merge method %s", bundle.number, method)
    results = supermerge.run(bundle, method, skip=skip)
    bundle_store.save(bundle)

    if report and bundle.issue:
        try:
            superpost.comment(bundle.home, bundle.issue,
                              superrender.render_merge_report(bundle, results,
                                                              note=note))
            if all(m.state == "merged" for m in results):
                superpost.close_issue(bundle)
        except CruxError as exc:
            log.warning("could not post the merge report: %s", exc)
        _report_slack(bundle, cfg, results, note=note)
        bundle_store.save(bundle)
    return results, supermerge.summary(results)


def _report_slack(bundle: Bundle, cfg: Config,
                  results: list[BundleMember], note: str = "") -> None:
    """Reply to the bundle's Slack thread with what landed. Best-effort."""
    try:
        import crux.slack as slack
        if not slack.enabled(cfg):
            return
        url = superpost.issue_url(bundle.home, bundle.issue) if bundle.issue else ""
        channel, ts = slack.announce_super_merge(
            cfg, bundle, url, results, root_ts=bundle.slack_ts, note=note)
        if ts and not bundle.slack_ts:
            bundle.slack_channel, bundle.slack_ts = channel, ts
    except Exception as exc:
        log.warning("slack merge report failed for super PR #%d: %s",
                    bundle.number, exc)


# ---------------------------------------------------------------------------
# landing order and merge method (D41)
# ---------------------------------------------------------------------------

def _ref(m: BundleMember) -> str:
    return bundle_store.key(m.owner, m.repo, m.pr)


def _settle_order(bundle: Bundle) -> None:
    """Make a pinned order name every member exactly once, and nothing else.

    Members removed since the pin drop out; members added since go LAST, in
    the order they joined, because everything already listed was placed by a
    human and a newcomer has no claim to go ahead of it.
    """
    have = [_ref(m) for m in bundle.members]
    members = set(have)
    keep = [ref for ref in dict.fromkeys(bundle.order) if ref in members]
    placed = set(keep)
    bundle.order = keep + [ref for ref in have if ref not in placed]


def resolve_refs(bundle: Bundle, refs: list[str]) -> list[str]:
    """*refs* as the bundle's own member keys, validated as a whole order.

    Each ref is `owner/repo#N` or `repo#N` (case-insensitive; the owner can be
    left off whenever only one member repo has that name). Every member still
    to land must be listed, exactly once. A prefix is NOT accepted: a pin is a
    human's decision about the whole sequence, and letting the rest trail in
    whatever order happens to be stored would print "pinned" over an order no
    human chose. Members that already merged may be left out — they have
    landed, so they go first — or listed, for anyone copying a full list.

    Raises SuperError naming every problem at once, with the full command to
    paste when members are missing, so a strict rule costs one copy-paste.
    """
    by_full: dict[str, str] = {}
    by_short: dict[str, list[str]] = {}
    for m in bundle.members:
        key = _ref(m)
        by_full[key.lower()] = key
        by_short.setdefault(f"{m.repo}#{m.pr}".lower(), []).append(key)

    chosen: list[str] = []
    problems: list[str] = []
    for raw in refs:
        text = raw.strip()
        if "/" in text:
            found = [by_full[text.lower()]] if text.lower() in by_full else []
        else:
            found = by_short.get(text.lower(), [])
        if not found:
            problems.append(f"{raw} is not in super PR #{bundle.number}")
        elif len(found) > 1:
            problems.append(f"{raw} is ambiguous — say which: "
                            f"{', '.join(found)}")
        elif found[0] in chosen:
            problems.append(f"{found[0]} is listed twice")
        else:
            chosen.append(found[0])

    missing = [_ref(m) for m in bundle.members
               if _ref(m) not in chosen and m.state != "merged"]
    if missing:
        problems.append(f"missing {', '.join(missing)} — list every member "
                        f"still to land")
    if problems:
        hint = (f"\n  e.g. crux super order {bundle.number} "
                f"{' '.join(chosen + missing)}" if missing else "")
        members = ", ".join(_ref(m) for m in bundle.members)
        raise SuperError(
            f"that is not a landing order for super PR #{bundle.number} "
            f"(members: {members}):\n  " + "\n  ".join(problems) + hint)

    landed = [_ref(m) for m in bundle.members
              if m.state == "merged" and _ref(m) not in chosen]
    return landed + chosen


def set_landing(bundle: Bundle, refs: list[str] | None = None,
                unpin: bool = False, method: str | None = None) -> Bundle:
    """Pin (or unpin) the landing order and/or set the merge method, and save.

    Everything is validated before anything changes, so a typo never leaves a
    bundle half-edited. *method* is "merge" | "squash" | "rebase", "default"
    to drop the bundle's own and fall back to `[super] merge_method`, or None
    to leave it as it is.

    Unpinning hands the order back to the review pass at once — its last
    proposal is kept on the bundle — rather than leaving a human's order in
    place under a brief that no longer says it is pinned.
    """
    if bundle.closed:
        raise SuperError(
            f"super PR #{bundle.number} is closed — there is nothing left to "
            f"land")
    if refs and unpin:
        raise SuperError("pin an order or --unpin it, not both at once")
    if method is not None and method not in (*MERGE_METHODS, "default"):
        raise SuperError(f"unknown merge method {method!r} — use one of "
                         f"{', '.join(MERGE_METHODS)} or default")
    order = resolve_refs(bundle, refs) if refs else None

    if order is not None:
        bundle.order = order
        bundle.order_pinned = True
    elif unpin:
        bundle.order_pinned = False
        if bundle.suggested_order:
            bundle.order = list(bundle.suggested_order)
    if method is not None:
        bundle.merge_method = "" if method == "default" else method
    bundle_store.save(bundle)
    return bundle


def republish(bundle: Bundle, cfg: Config) -> tuple[str, list[str]]:
    """Rewrite the published brief's landing section and state block in place.

    Returns (url, problems). No diff, no harvest, no model call: an order or a
    method is a human's decision, not new evidence, and re-reviewing the whole
    bundle to print a different arrow would rewrite analysis nobody asked to
    change (`superrender.restamp`). A bundle with no brief yet is not an error
    — the next refresh publishes one, and it will carry the change.

    Called after the bundle is SAVED, so a publish that fails still leaves the
    decision on disk, where `crux super refresh` picks it up.
    """
    home = bundle.home or home_repo(cfg)
    issue = bundle.issue or superpost.find_brief(home, bundle.number)
    if issue is None:
        return "", [f"super PR #{bundle.number} has no brief yet — "
                    f"`crux super refresh {bundle.number}` publishes one "
                    f"with this order"]
    body = superpost.issue_body(home, issue)
    if SUPER_MARKER not in body:
        # An empty read is a failed read (issue_body swallows its errors), and
        # publishing a body rebuilt from nothing would wipe the brief. Nor is
        # an issue that no longer opens with the brief's marker Crux's to edit.
        raise SuperError(
            f"could not read the brief for super PR #{bundle.number} "
            f"({superpost.issue_url(home, issue)}) — the change is saved here; "
            f"`crux super refresh {bundle.number}` will publish it")
    bundle.home, bundle.issue = home, issue
    _number, url = superpost.publish(bundle, superrender.restamp(body, bundle,
                                                                 cfg))
    return url, []


def _landing_line(b: Bundle, cfg: Config | None) -> str:
    """One line for `show`: who chose the order, and how it will merge."""
    method = supermerge.method_label(supermerge.resolve_method(b, cfg))
    source = ("set on this super PR" if b.merge_method
              else "not set here — the [super] merge_method default")
    chosen = ("📌 pinned by hand" if b.order_pinned
              else "chosen by the review pass")
    return f"Landing order {chosen} · merge method: {method} ({source})"


def render_landing_plan(b: Bundle, cfg: Config | None = None) -> str:
    """`crux super order N` with nothing to change: the plan, numbered, and
    the command that would pin it as it stands — the easiest edit to make."""
    lines = [f"Super PR #{b.number}" + (f" — {b.name}" if b.name else ""),
             _landing_line(b, cfg), ""]
    ordered = supermerge.order_members(b)
    width = len(str(len(ordered)))
    for i, m in enumerate(ordered, 1):
        mark = " ✅ merged" if m.state == "merged" else ""
        lines.append(f"  {str(i).rjust(width)}. {_ref(m)}{mark}")
    verb = "re-pin" if b.order_pinned else "pin an order"
    lines += ["", f"To {verb}, reorder this and run it: crux super order "
              f"{b.number} " + " ".join(_ref(m) for m in ordered)]
    if b.order_pinned:
        lines.append(f"Hand it back to the review pass: crux super order "
                     f"{b.number} --unpin")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

def render_list(bundles: list[Bundle]) -> str:
    if not [b for b in bundles if not b.closed]:
        return "No super PRs yet — create one with `crux super new`."
    lines: list[str] = []
    for b in bundles:
        # A closed bundle is retired, not deleted: its number stays spent so
        # old links keep meaning what they meant, but it drops out of the list
        # a person reads to find live work.
        if b.closed:
            continue
        landed = sum(1 for m in b.members if m.state == "merged")
        state = f"{landed}/{len(b.members)} landed" if landed else f"{len(b.members)} PRs"
        where = superpost.issue_url(b.home, b.issue) if b.issue else "(not published)"
        lines.append(f"#{b.number}  {b.name:<20}  {state:<14}  {where}")
    return "\n".join(lines)


def render_show(b: Bundle, cfg: Config | None = None) -> str:
    lines = [f"Super PR #{b.number} — {b.name}" if b.name
             else f"Super PR #{b.number}"]
    if b.issue:
        lines.append(superpost.issue_url(b.home, b.issue))
    lines.append(_landing_line(b, cfg))
    lines.append("")
    order = {ref: i for i, ref in enumerate(b.order)}
    for m in sorted(b.members,
                    key=lambda m: order.get(f"{m.owner}/{m.repo}#{m.pr}", 99)):
        mark = {"merged": "✅", "blocked": "❌"}.get(m.state, "  ")
        line = f"  {mark} {m.owner}/{m.repo}#{m.pr}  ({m.branch})"
        if m.error:
            line += f"  — {m.error}"
        lines.append(line)
    return "\n".join(lines)
