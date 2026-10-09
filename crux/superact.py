# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D38: what the brief's buttons actually do — approve-and-merge, and close.

The buttons exist because the brief is where a super PR is read, and the two
things a reader wants next ("land it", "abandon it") were a terminal command
away in a directory they may not have. They are links in the issue body that
point at `http://127.0.0.1:<port>` — the same URL in everyone's copy of the
card, because loopback resolves on the machine of whoever clicked. The request
therefore arrives at a Crux already authenticated as that person, which is the
only way an approval can carry their name: a hosted service would need their
GitHub token, and a CI token would approve as a bot.

The one rule this module enforces, and the reason it is not merely a wrapper
around `crux super merge`: **you cannot land a bundle you wrote.** GitHub
already refuses to take your approval on your own PR; the honest reading of
that is not "merge it unapproved" but "this is not yours to wave through". So
authorship is checked for the WHOLE bundle before anything happens — a super PR
is one change, and landing the three PRs you did not write leaves a cross-repo
feature half-applied, which is worse than not landing at all.

The gate is here, on the machine, not in the card: an issue body is one blob of
markdown that GitHub renders identically for everyone, so a link cannot be
hidden from its author. Everybody sees the button; it only works for someone
who did not write the work.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import crux.bundle as bundle_store
import crux.post as post
import crux.superpost as superpost
import crux.superpr as superpr
from crux.models import Bundle, BundleMember, Config, CruxError

log = logging.getLogger("crux.superact")


class ActError(CruxError):
    """Raised when a button's action cannot be carried out."""


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------

def actor_login() -> str:
    """The GitHub login of whoever this Crux is authenticated as.

    This is the whole identity model: the request reached this process because
    it was clicked on this machine, so `gh`'s own login IS the clicker. No
    session, no cookie, nothing to forge from outside.
    """
    try:
        out = post._run_gh(["api", "user", "--jq", ".login"])
    except CruxError as exc:
        raise ActError(f"could not read your GitHub login from gh ({exc})") from None
    login = out.strip()
    if not login:
        raise ActError("gh returned no login — run `gh auth login` first")
    return login


def author_login(member: BundleMember) -> str:
    """The login that opened this member PR, read from GitHub.

    Read live rather than trusted from bundle state: the bundle stores a display
    name for crediting, and a merge gate must not turn on a string that was
    right when the bundle was made.
    """
    slug = f"{member.owner}/{member.repo}"
    try:
        out = post._run_gh(
            ["api", f"repos/{slug}/pulls/{member.pr}", "--jq", ".user.login"])
    except CruxError as exc:
        raise ActError(f"could not read who opened {slug}#{member.pr} ({exc})") from None
    return out.strip()


def authored_by(bundle: Bundle, login: str) -> list[BundleMember]:
    """Members of *bundle* that *login* opened.

    Any hit at all disqualifies the whole bundle (see the module docstring), so
    callers ask "is this list empty", not "how many".
    """
    if not login:
        return []
    mine: list[BundleMember] = []
    for member in bundle.members:
        if author_login(member).lower() == login.lower():
            mine.append(member)
    return mine


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _approve(member: BundleMember) -> str:
    """Approve one member PR as the calling user. Returns "" or an error."""
    slug = f"{member.owner}/{member.repo}"
    payload = json.dumps({
        "event": "APPROVE",
        "body": "Approved as part of a Crux super PR.",
    })
    try:
        post._run_gh(
            ["api", "-X", "POST", f"repos/{slug}/pulls/{member.pr}/reviews",
             "--input", "-"],
            stdin_text=payload)
    except CruxError as exc:
        return f"{slug}#{member.pr}: could not approve ({exc})"
    log.info("approved %s#%d", slug, member.pr)
    return ""


def retire_tickets(key: str, cfg: Config | None = None) -> None:
    """D40: close whatever Zenhub tickets this landing retires.

    Here, and not in the CLI, because there are two doors onto every merge —
    the terminal and the brief's Merge button — and one policy has to serve
    both. `crux/serve.py` calls the same functions the CLI does, so hanging
    the close off them covers both without threading a Config through the HTTP
    handlers.

    Best-effort by construction, and swallowing broadly on purpose: the merge
    has ALREADY HAPPENED by the time this runs. A ticket left open is a
    nuisance someone fixes with `crux zenhub sync`; an exception raised here
    would report a successful landing as a failure, which is worse.
    """
    try:
        import crux.zenlink as zenlink
        if cfg is None:
            import crux.config as config
            cfg = config.load(os.getcwd())
        for problem in zenlink.close_for(cfg, key, force=True):
            log.warning("zenhub: %s", problem)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("zenhub: could not close the tickets for %s (%s)", key, exc)


def merge(bundle: Bundle, cfg: Config, login: str = "",
          method: str = "") -> tuple[list[BundleMember], str, list[str]]:
    """Approve every member PR as *login*, then land the bundle.

    *method* is an explicit override; "" — what the brief's button sends —
    lands with the bundle's own method, else `[super] merge_method`, else
    squash (D41, resolved in `superpr.merge`).

    The gate has two levels, because "you wrote it" is not one situation:

    * **You wrote the whole thing** — the button is not yours to press, and
      raising here stops it before a single approval is sent. Nothing you could
      approve, nothing that could land: an attempt would only produce N failures
      spelling out what one sentence says better.
    * **You wrote one of them** — the rest is other people's work and you are a
      real reviewer of it, so it approves and lands. Yours fails LOUDLY, by
      name, and the run carries on to the next: that is the same land-what-can
      policy every other blocker gets, and the report says exactly which PR is
      waiting for someone else.

    Returns (results, summary, problems).
    """
    who = login or actor_login()
    mine = authored_by(bundle, who)
    if mine and len(mine) == len(bundle.members):
        raise ActError(refusal(bundle, who, mine))

    problems: list[str] = []
    skip: set[str] = set()
    for member in bundle.members:
        ref = f"{member.owner}/{member.repo}#{member.pr}"
        if member.state == "merged":
            continue
        if member in mine:
            member.state = "blocked"
            member.error = (f"you opened it — GitHub will not take your own "
                            f"approval, so someone else has to approve and "
                            f"merge this one")
            skip.add(ref)
            problems.append(f"{ref}: skipped — {member.error}")
            log.warning("skipping %s: pressed by its own author %s", ref, who)
            continue
        error = _approve(member)
        if error:
            # An approval that did not stick is not fatal: the merge below
            # reports precisely what the missing review blocks, which is a
            # better message than anything guessed here.
            problems.append(error)
    results, line = superpr.merge(bundle, cfg, method=method, skip=skip)
    # A cross-repo ticket is not done while half its repos are unmerged, so the
    # bundle's tickets wait for every member — not for the first one to land.
    if results and all(m.state == "merged" for m in results):
        import crux.zenlink as zenlink
        retire_tickets(zenlink.bundle_key(bundle.number), cfg)
    return results, line, problems


def refusal(bundle: Bundle, login: str, mine: list[BundleMember]) -> str:
    """The sentence shown when the whole bundle is the presser's own work.

    One wording for the browser page, the terminal and the plugin, because the
    thing being explained is the same in all three and a reader who meets it
    twice should recognize it.
    """
    return (f"You opened all {len(mine)} pull requests in Super PR "
            f"#{bundle.number}, so it is not yours to merge — GitHub will not "
            f"take your approval on your own pull request. Someone other than "
            f"{login} has to press this.")


# ---------------------------------------------------------------------------
# Admin merge
# ---------------------------------------------------------------------------

def admin_on(slug: str) -> bool:
    """True when this user has admin rights on *slug*."""
    try:
        out = post._run_gh(["api", f"repos/{slug}", "--jq", ".permissions.admin"])
    except CruxError:
        return False
    return out.strip().lower() == "true"


def admin_merge(bundle: Bundle, cfg: Config, login: str = "",
                method: str = "") -> tuple[list[BundleMember], str, list[str]]:
    """Land the bundle on admin rights, WITHOUT approving anything.

    The escape hatch for the case the approval flow cannot serve: a repo that
    needs a review nobody available can give, or an admin who has already read
    the work and does not want a review record per repo to say so. It merges
    directly, so it is the one path that can land a bundle its presser wrote.

    What keeps that honest is the record, not a gate: the merge report and the
    Slack reply both name who overrode and state plainly that nothing was
    approved. An override nobody can see is the thing to avoid; an override
    written down where the work is read is a normal, reviewable decision.
    """
    who = login or actor_login()
    slugs = sorted({f"{m.owner}/{m.repo}" for m in bundle.members})
    missing = [slug for slug in slugs if not admin_on(slug)]
    if missing:
        raise ActError(
            f"an admin merge needs admin rights on every repo in the bundle, "
            f"and {who} does not have them on {', '.join(missing)}")

    note = (f"⚠️ Landed by @{who} with an **admin merge** — merged directly on "
            f"admin rights, with no approval recorded on any of these pull "
            f"requests.")
    log.warning("admin merge of super PR #%d by %s", bundle.number, who)
    results, line = superpr.merge(bundle, cfg, method=method, note=note)
    if results and all(m.state == "merged" for m in results):
        import crux.zenlink as zenlink
        retire_tickets(zenlink.bundle_key(bundle.number), cfg)
    return results, line, []


def ask_for_merge(bundle: Bundle, cfg: Config, login: str,
                  mine: list[BundleMember]) -> str:
    """Ask the team, in the bundle's own Slack thread, for a different pair of
    hands. Returns a sentence for the caller to show; never raises."""
    url = (superpost.issue_url(bundle.home, bundle.issue)
           if bundle.issue else "")
    try:
        import crux.slack as slack
        if not slack.enabled(cfg):
            return ("Slack is not configured, so nobody was notified — send "
                    f"the brief to someone yourself: {url or 'no issue yet'}")
        channel, ts = slack.announce_super_needs_merger(
            cfg, bundle, url, login, mine, root_ts=bundle.slack_ts)
        if not ts:
            return "Slack would not take the message — ask someone directly."
        if not bundle.slack_ts:
            bundle.slack_channel, bundle.slack_ts = channel, ts
            bundle_store.save(bundle)
        return "Asked in Slack for someone else to merge it."
    except Exception as exc:  # noqa: BLE001 — a chat failure is never fatal here
        log.warning("could not ask Slack for a merger: %s", exc)
        return f"Could not reach Slack ({exc}) — ask someone directly."


# ---------------------------------------------------------------------------
# One ordinary PR — the same two buttons on the per-PR card
# ---------------------------------------------------------------------------

def pr_author(slug: str, pr: int) -> str:
    return author_login(BundleMember(owner=slug.split("/")[0],
                                     repo=slug.split("/")[-1],
                                     branch="", pr=pr))


def merge_pr(slug: str, pr: int, login: str = "",
             method: str = "squash") -> tuple[bool, str]:
    """Approve one PR as *login* and merge it. Returns (merged, detail).

    The single-PR twin of `merge`, and the same rule: your own PR is not yours
    to approve. There is no bundle to half-apply here, so the refusal is simply
    that — no approval, no merge.
    """
    import crux.supermerge as supermerge
    who = login or actor_login()
    if pr_author(slug, pr).lower() == who.lower():
        raise ActError(
            f"You opened {slug}#{pr}, so it is not yours to merge — GitHub "
            f"will not take your approval on your own pull request. Someone "
            f"other than {who} has to press this.")
    member = BundleMember(owner=slug.split("/")[0], repo=slug.split("/")[-1],
                          branch="", pr=pr)
    error = _approve(member)
    merged, why = supermerge._merge_one(slug, pr, method)
    if merged:
        import crux.zenlink as zenlink
        owner, _, name = slug.partition("/")
        retire_tickets(zenlink.pr_key(owner, name, pr))
        return True, f"approved and merged {slug}#{pr}"
    return False, (error + "; " if error else "") + f"{slug}#{pr} {why}"


def admin_merge_pr(slug: str, pr: int, login: str = "",
                   method: str = "squash") -> tuple[bool, str]:
    """Merge one PR on admin rights, approving nothing, and say so on the PR.

    Same bargain as the bundle version: the override is allowed, including on
    your own work, and it is written where the work is read rather than only in
    a log.
    """
    import crux.supermerge as supermerge
    who = login or actor_login()
    if not admin_on(slug):
        raise ActError(f"an admin merge needs admin rights on {slug}, and "
                       f"{who} does not have them")
    log.warning("admin merge of %s#%d by %s", slug, pr, who)
    merged, why = supermerge._merge_one(slug, pr, method)
    if not merged:
        return False, f"{slug}#{pr} {why}"
    try:
        superpost.comment(slug, pr, (
            f"⚠️ Landed by @{who} with an **admin merge** — merged directly on "
            f"admin rights, with no approval recorded on this pull request."))
    except CruxError as exc:
        log.warning("could not record the admin merge on %s#%d: %s", slug, pr, exc)
    import crux.zenlink as zenlink
    owner, _, name = slug.partition("/")
    retire_tickets(zenlink.pr_key(owner, name, pr))
    return True, f"merged {slug}#{pr} on admin rights, with no approval"


# ---------------------------------------------------------------------------
# Check out for testing
# ---------------------------------------------------------------------------

@dataclass
class Checkout:
    """What happened to one repo when setting the bundle up to be tried."""
    slug: str
    branch: str
    path: str = ""
    was: str = ""          # the branch it was on before
    state: str = ""        # "ready" | "skipped" | "failed"
    detail: str = ""

    @property
    def mark(self) -> str:
        return {"ready": "✅", "skipped": "⚠️"}.get(self.state, "❌")


def _git(args: list[str], cwd: str) -> str:
    import crux.gitio as gitio
    return gitio.run_git(args, cwd=cwd, timeout=120)


def clone_root(cfg: Config, repo_root: str | None = None) -> str:
    """Where a missing member repo gets cloned. "" when there is nowhere sane.

    `[super] roots` is the answer whenever it is set — the directory named
    there IS where clones go, with no path built underneath it beyond the
    repo's own name. Without it, the parent of the repo this ran in, which is
    the sibling-clones layout `roots` would have described anyway.

    What this must never do is fall back to the CURRENT directory: the buttons'
    service is started from wherever the user happened to be — usually inside a
    repo — and cloning there buries a bundle's other repos INSIDE one of its
    members. So a candidate that sits inside a git work tree is climbed out of,
    and if that fails there is no root and the repo is reported instead.

    Deliberately the same answer `clones.search_roots` gives, from the same
    code: where clones are LOOKED FOR and where a missing one is PUT have to be
    the same directory. When they drifted apart, discovery searched inside one
    repo while cloning targeted its parent, so every member of a bundle that
    was already checked out was reported as missing and then as in the way.
    """
    import os
    import crux.clones as clones
    for root in cfg.super_roots:
        if root.strip():
            return os.path.expanduser(root.strip())
    roots = clones.search_roots(cfg, repo_root)
    return roots[0] if roots else ""


def _clone_missing(slug: str, root: str) -> tuple[str, str]:
    """Clone *slug* into *root*. Returns (path, error).

    A teammate who presses "set up to test" wants the feature running, not a
    list of repos to go and clone by hand — and a bundle usually spans repos
    somebody has never touched. Cloning is additive and reversible, which is
    what makes it safe to do without asking: nothing existing is changed.
    """
    import os
    import crux.clones as clones
    target = os.path.join(os.path.expanduser(root), slug.split("/")[-1])
    if os.path.exists(target):
        # Ask before accusing. A directory here is far more often the clone the
        # user already has than a collision — discovery runs from the service's
        # working directory and can miss a clone that is plainly there — and
        # "exists but is not a clone" was being said without ever looking,
        # which turned a working checkout into seven false failures.
        found = clones.clone_slug(target)
        if found.lower() == slug.lower():
            return target, ""
        if found:
            return "", f"{target} already exists and is a clone of {found}"
        return "", f"{target} already exists and is not a git clone"
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        post._run_gh(["repo", "clone", slug, target], timeout=600)
    except CruxError as exc:
        return "", f"could not be cloned ({exc})"
    except OSError as exc:
        return "", f"could not be cloned into {root} ({exc})"
    log.info("cloned %s into %s", slug, target)
    return target, ""


def _checkout_one(member: BundleMember, path: str, root: str = "") -> Checkout:
    """Put one clone on its member branch, up to date with the remote.

    With no clone on this machine and a *root* to put one in, it clones first —
    "set up to test" is a promise to make the bundle runnable here.
    """
    out = Checkout(slug=f"{member.owner}/{member.repo}", branch=member.branch,
                   path=path)
    if not path and root:
        path, error = _clone_missing(out.slug, root)
        out.path = path
        if error:
            out.state = "failed"
            out.detail = error
            return out
        out.was = ""
    if not path:
        out.state = "failed"
        out.detail = ("no clone of this repo on this machine, and no "
                      "[super] roots directory to put one in")
        return out
    try:
        out.was = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
        # -uno: TRACKED changes only. A plain --porcelain lists untracked files
        # too, and untracked is the normal state of a working clone — a build
        # directory, a scratch file, a venv someone never gitignored. Counting
        # those as "uncommitted work" refused the checkout on repos with
        # nothing at stake, which is how a bundle came back skipped over an
        # untracked folder. Nothing is lost by ignoring them either: git itself
        # refuses a checkout that would clobber an untracked file, and that
        # refusal is reported below, so the one case that matters is still
        # caught by the tool that can actually see it coming.
        dirty = _git(["status", "--porcelain", "-uno"], path)
    except CruxError as exc:
        out.state = "failed"
        out.detail = f"could not be read ({exc})"
        return out

    # Uncommitted work is never touched. Switching branches under someone's
    # unsaved edits to save them a command is not a trade Crux gets to make —
    # and a checkout that fails half-way is worse than one that never started.
    if dirty and out.was != member.branch:
        out.state = "skipped"
        out.detail = (f"has uncommitted changes and is on {out.was} — commit or "
                      f"stash them, then press again")
        return out

    try:
        _git(["fetch", "origin", member.branch], path)
    except CruxError as exc:
        out.state = "failed"
        out.detail = f"could not fetch {member.branch} ({exc})"
        return out
    # Reported apart from the fast-forward below, because this is where git's
    # own refusal lands — an untracked file the switch would overwrite is the
    # one thing `-uno` stopped us checking for, and git says so precisely.
    # Folded into the next message it would have read "is on <branch> but could
    # not be fast-forwarded", naming a branch the repo never reached.
    try:
        if out.was != member.branch:
            _git(["checkout", member.branch], path)
    except CruxError as exc:
        out.state = "failed"
        out.detail = f"could not be switched to {member.branch} ({exc})"
        return out
    try:
        # Fast-forward only: a merge or rebase here would be Crux rewriting
        # someone's branch to set up a test.
        _git(["merge", "--ff-only", f"origin/{member.branch}"], path)
    except CruxError as exc:
        out.state = "failed"
        out.detail = (f"is on {member.branch} but could not be fast-forwarded "
                      f"to origin ({exc})")
        return out

    out.state = "ready"
    out.detail = (f"{out.was} → {member.branch}" if out.was != member.branch
                  else f"already on {member.branch}, updated")
    log.info("checked out %s in %s", member.branch, path)
    return out


def checkout(bundle: Bundle, cfg: Config,
             repo_root: str | None = None) -> list[Checkout]:
    """Put every member repo on its branch so the bundle can be tried by hand.

    The verification steps assume all of it is checked out at once — that is
    what makes them the walkthrough no single PR can offer — and doing it by
    hand is N fetches and N checkouts in N directories, in a shell that is
    somewhere else. This is that, reported per repo.

    Nothing is merged, rebased or stashed: a repo with uncommitted work is
    reported and left exactly as it was.
    """
    paths, _defaults = superpr._clone_paths(cfg, bundle.members, repo_root)
    root = clone_root(cfg, repo_root)
    return [_checkout_one(member,
                          paths.get(f"{member.owner}/{member.repo}", ""), root)
            for member in bundle.members]


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

def close(bundle: Bundle, cfg: Config, with_prs: bool = False) -> list[str]:
    """Close the brief, and optionally every member PR. Returns problems.

    Closing the brief alone is the ordinary case: a bundle is a way of reading
    the work, and retiring the reading does not retire the work. Closing the
    PRs too is the deliberate second choice, offered separately rather than
    folded into one button — it discards open work in several repos at once,
    possibly other people's.
    """
    problems: list[str] = []
    if not bundle.issue:
        problems.append(f"Super PR #{bundle.number} has no brief issue to close")
    else:
        try:
            superpost.close_issue(bundle)
            log.info("closed the brief for super PR #%d", bundle.number)
        except CruxError as exc:
            problems.append(f"could not close the brief ({exc})")

    if with_prs:
        for member in bundle.members:
            slug = f"{member.owner}/{member.repo}"
            if member.state == "merged":
                continue
            try:
                post._run_gh(
                    ["api", "-X", "PATCH", f"repos/{slug}/pulls/{member.pr}",
                     "--input", "-"],
                    stdin_text=json.dumps({"state": "closed"}))
                member.state = "closed"
                log.info("closed %s#%d", slug, member.pr)
            except CruxError as exc:
                problems.append(f"{slug}#{member.pr}: could not close it ({exc})")

    bundle.closed = True
    bundle_store.save(bundle)
    return problems
