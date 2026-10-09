# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D40: which tickets a PR closes — the store, the picker, and the closing.

``crux/zenhub.py`` talks to Zenhub. This module decides WHAT to say to it, and
remembers the answer. The two are split because the remembering has to outlive
any one command: the run that links a ticket and the merge that closes it are
usually days and several machines apart.

**One store, two shapes.** A regular PR and a super PR reach Zenhub by
different roads, and the store flattens that difference into one key:

* ``pr:owner/repo#12`` — an ordinary pull request in its own repo. Its tickets
  close the moment that one PR lands.
* ``super:7`` — a bundle. Its brief is an issue in the super-PR home repo, but
  the brief is Crux's own artifact, not the ticket: the tickets are attached
  to the BUNDLE and close only once **every** member PR has landed, because a
  cross-repo ticket is not done while half its repos are unmerged.

That single index is also what makes ``crux zenhub sync`` possible: one file to
walk, whatever shape the work took, so a PR merged in the GitHub UI still
retires its ticket the next time anyone syncs.

Store: ``$XDG_CONFIG_HOME/crux/zenhub/links.json``, written atomically. One
file rather than one-per-link: the whole point is a single index to scan, and
it stays small (a link is a few hundred bytes).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import crux.zenhub as zenhub
from crux.models import ZEN_MARKER, Config, CruxError, ZenLink, ZenTicket

log = logging.getLogger("crux.zenlink")

STORE_VERSION = 1
# How many ranked tickets the picker shows before it stops being a list a
# person can read. `--all` is there for the rare case that is not enough.
PICK_LIMIT = 12
# Characters of a ticket's description shown under its title. Enough to tell
# two similarly-titled tickets apart, short enough that twelve of them fit on
# a screen.
EXCERPT_CHARS = 200

# "#29", "issue-29", "29-fix-the-thing", "ZH-29", "fix/issue-29-zero-diff".
_REF_RE = re.compile(r"(?:^|[^0-9A-Za-z])(?:#|issue[-_/]?|zh-|gh-)(\d{1,6})\b",
                     re.IGNORECASE)
_LEADING_NUM_RE = re.compile(r"^(\d{1,6})[-_]")
# Words too common to say anything about which ticket a branch is about.
_STOP = frozenset("""
a an and the of for to in on at by with from is are be as it this that fix
fixes fixed add adds added update updates updated remove removes removed
make makes made use uses used into onto over via not no new
""".split())


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def store_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "crux" / "zenhub" / "links.json"


def pr_key(owner: str, repo: str, pr: int) -> str:
    return f"pr:{owner}/{repo}#{int(pr)}"


def bundle_key(number: int) -> str:
    return f"super:{int(number)}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ticket_from(data: dict) -> ZenTicket:
    return ZenTicket(
        id=str(data.get("id", "")), number=int(data.get("number", 0) or 0),
        owner=str(data.get("owner", "")), repo=str(data.get("repo", "")),
        title=str(data.get("title", "")), body=str(data.get("body", "")),
        url=str(data.get("url", "")), pipeline=str(data.get("pipeline", "")),
        kind=str(data.get("kind", "")), planning=bool(data.get("planning", False)))


def load() -> dict[str, ZenLink]:
    """Every link, by key. A missing/corrupt store reads as empty — this file
    is bookkeeping, and losing it must never stop a merge."""
    try:
        raw = store_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("zenhub: %s is not readable JSON; treating it as empty",
                    store_path())
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, ZenLink] = {}
    for key, entry in (data.get("links") or {}).items():
        if not isinstance(entry, dict):
            continue
        out[str(key)] = ZenLink(
            key=str(key),
            tickets=[_ticket_from(t) for t in entry.get("tickets") or []
                     if isinstance(t, dict)],
            prs=[str(p) for p in entry.get("prs") or [] if isinstance(p, str)],
            created_at=str(entry.get("created_at", "")),
            closed=bool(entry.get("closed", False)),
        )
    return out


def save(links: dict[str, ZenLink]) -> None:
    """Replace the store atomically (tempfile + os.replace)."""
    path = store_path()
    payload = {"version": STORE_VERSION, "links": {
        key: {
            "tickets": [vars(t) for t in link.tickets],
            "prs": link.prs, "created_at": link.created_at,
            "closed": link.closed,
        } for key, link in sorted(links.items())
    }}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".links-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(key: str) -> ZenLink | None:
    return load().get(key)


def put(link: ZenLink) -> None:
    links = load()
    link.created_at = link.created_at or _now()
    links[link.key] = link
    save(links)


def drop(key: str) -> bool:
    links = load()
    if key not in links:
        return False
    del links[key]
    save(links)
    return True


# ---------------------------------------------------------------------------
# The pre-push answer, held until the PR it belongs to exists
# ---------------------------------------------------------------------------

def pending_path(owner: str, repo: str, branch: str) -> Path:
    """Where a pre-push ticket choice waits, per branch.

    Beside the D11/D39 PR-intent file and shaped like it, but a SEPARATE file
    on purpose: `post.consume_intent` is emptied the moment `ensure_pr` runs —
    including when a PR already exists, where the base answer is moot but a
    ticket choice is not. Sharing that file would throw the answer away in the
    commonest case there is.
    """
    safe = branch.replace("/", "__")
    return (Path.home() / ".cache" / "crux" / f"{owner}__{repo}"
            / f"{safe}.zenhub-pending.json")


def _pending_path(info) -> Path:
    return pending_path(info.owner, info.repo, info.branch)


def record_pending(info, tickets: list[ZenTicket]) -> None:
    """Hold the tickets chosen at pre-push until the PR exists to attach them to.

    Stamped with HEAD so a choice stranded by a run that never happened cannot
    be applied, branch-state later, to work the user never answered for — the
    same guard `post.consume_intent` carries, for the same reason.
    """
    path = _pending_path(info)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"head_sha": info.head_sha,
                                    "tickets": [vars(t) for t in tickets]}),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("zenhub: could not record the ticket choice for %s (%s)",
                    info.branch, exc)


def pending(info) -> bool:
    """Whether a choice is already waiting, so the push does not ask twice."""
    return _pending_path(info).exists()


def consume_pending(info) -> list[ZenTicket]:
    """Read and delete the pre-push choice; [] when there is none or it is stale.

    Consumed exactly once whatever happens, so a rejected (stale) choice cannot
    sit around waiting to surprise a later push.
    """
    path = _pending_path(info)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(data, dict) or not data.get("tickets"):
        return []
    recorded = str(data.get("head_sha") or "")
    if recorded:
        import crux.post as post
        if not post._is_ancestor(info, recorded):
            log.info("zenhub: discarding the pre-push ticket choice for %s — "
                     "it was recorded for %s, which this branch no longer "
                     "contains", info.branch, recorded[:12])
            return []
    return [_ticket_from(t) for t in data["tickets"] if isinstance(t, dict)]


# ---------------------------------------------------------------------------
# Working out which tickets a branch is probably about
# ---------------------------------------------------------------------------

def referenced_numbers(*texts: str) -> set[int]:
    """Issue numbers a branch name, PR title, body or commit subjects mention.

    Deliberately generous about form — `#29`, `issue-29`, `29-fix-thing`,
    `ZH-29` — because there is no house convention to rely on and a false
    candidate costs a line in a list a human is about to read anyway. The
    human confirming is what makes generosity safe here.
    """
    found: set[int] = set()
    for text in texts:
        for chunk in re.split(r"[\s,;]+", text or ""):
            lead = _LEADING_NUM_RE.match(chunk)
            if lead:
                found.add(int(lead.group(1)))
        for match in _REF_RE.finditer(text or ""):
            found.add(int(match.group(1)))
    return found


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^0-9A-Za-z]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


def rank(tickets: list[ZenTicket], *hints: str) -> list[tuple[int, ZenTicket]]:
    """(score, ticket) best first — a guess, never a decision.

    An explicit reference outranks everything: a branch called
    `fix/issue-29-…` is telling you the answer. Below that, shared words
    between the ticket's title and the branch/PR text order the rest, so the
    likely handful float to the top of a list the human still confirms.
    """
    text = " ".join(h for h in hints if h)
    numbers = referenced_numbers(text)
    hint_words = _words(text)
    scored: list[tuple[int, ZenTicket]] = []
    for ticket in tickets:
        score = 0
        if ticket.number and ticket.number in numbers:
            score += 100
        overlap = hint_words & _words(ticket.title)
        score += 8 * len(overlap)
        if overlap and _words(ticket.title) <= hint_words | _STOP:
            score += 5  # the branch says everything the title does
        scored.append((score, ticket))
    scored.sort(key=lambda pair: (-pair[0], pair[1].number))
    return scored


def excerpt(body: str, limit: int = EXCERPT_CHARS) -> str:
    """The first real sentence or two of a ticket description, on one line.

    Markdown headings, HTML comments, images and quote markers are stripped:
    a picker line has room for what the ticket is about, not for its template.
    """
    text = re.sub(r"<!--.*?-->", " ", body or "", flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    lines = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("#>-*").strip()
        if not line or set(line) <= set("-=|"):
            continue
        lines.append(line)
        if sum(len(x) for x in lines) >= limit:
            break
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined[:limit].rstrip() + "…" if len(joined) > limit else joined


# ---------------------------------------------------------------------------
# The picker
# ---------------------------------------------------------------------------

def render_choices(scored: list[tuple[int, ZenTicket]], limit: int) -> str:
    """The numbered list a human picks from: number, title, description."""
    lines: list[str] = []
    for index, (score, ticket) in enumerate(scored[:limit], start=1):
        pipe = f"  [{ticket.pipeline}]" if ticket.pipeline else ""
        hint = "  ← named by this branch" if score >= 100 else ""
        lines.append(f"  {index:>2}  {zenhub.describe(ticket)} · "
                     f"{ticket.title}{pipe}{hint}")
        summary = excerpt(ticket.body)
        if summary:
            lines.append(f"      {summary}")
    return "\n".join(lines)


def parse_choice(answer: str, scored: list[tuple[int, ZenTicket]],
                 limit: int) -> list[ZenTicket]:
    """Turn "1,3" / "2" / "" into tickets. Unknown entries are ignored.

    Empty means "none" — the default, and the reason this whole feature can be
    on without being in the way.
    """
    picked: list[ZenTicket] = []
    for part in re.split(r"[\s,]+", (answer or "").strip()):
        if not part.isdigit():
            continue
        index = int(part)
        if 1 <= index <= min(limit, len(scored)):
            ticket = scored[index - 1][1]
            if ticket not in picked:
                picked.append(ticket)
    return picked


def ask(cfg: Config, scored: list[tuple[int, ZenTicket]], what: str,
        limit: int = PICK_LIMIT) -> list[ZenTicket]:
    """Show the ranked tickets on the terminal and return the ones chosen.

    Returns [] for "none", which is also what a run with no terminal gets: a
    hook, a detached run, CI and an agent must never block on a question, and
    a link not made is a thing `crux zenhub link` fixes in ten seconds.
    """
    import crux.post as post
    if not scored:
        return []
    header = (f"\n🎫 Zenhub — which ticket does {what} close?\n\n"
              + render_choices(scored, limit)
              + f"\n\n  Numbers (e.g. 1 or 1,3), or enter for none: ")
    answer = post._prompt_tty(header)
    if answer is None:
        log.info("zenhub: no terminal to ask about tickets for %s; "
                 "link it later with `crux zenhub link`", what)
        return []
    return parse_choice(answer, scored, limit)


# ---------------------------------------------------------------------------
# Attaching
# ---------------------------------------------------------------------------

def render_note(tickets: list[ZenTicket]) -> str:
    """The sticky comment that says, where the work is read, what it closes."""
    lines = [ZEN_MARKER, "### 🎫 Zenhub", "", "Landing this closes:"]
    for ticket in tickets:
        ref = zenhub.describe(ticket)
        link = f"[{ref}]({ticket.url})" if ticket.url else ref
        lines.append(f"- Closes {link} — {ticket.title}")
    lines += ["", "<sub>Crux closes these when the pull request lands "
              "(`crux zenhub sync` catches merges made elsewhere).</sub>"]
    return "\n".join(lines)


def attach(cfg: Config, key: str, tickets: list[ZenTicket], prs: list[str],
           note_on: list[tuple[str, int]]) -> list[str]:
    """Record the link, draw it in Zenhub, and say so where the work is read.

    *prs* is what must land before the tickets close; *note_on* is where the
    sticky comment goes — the PR itself for a regular one, the brief AND each
    member PR for a bundle, so a reader on any of them sees the ticket.

    Returns problems. The store write is what actually matters and happens
    first: a Zenhub connection that fails is cosmetic, and a comment that
    fails is a missing signpost, but a link Crux forgot is a ticket left open
    forever.
    """
    import crux.superpost as superpost
    problems: list[str] = []
    existing = get(key)
    merged = list(existing.tickets) if existing else []
    for ticket in tickets:
        if not any(t.id == ticket.id for t in merged):
            merged.append(ticket)
    put(ZenLink(key=key, tickets=merged, prs=sorted(set(prs)),
                created_at=existing.created_at if existing else _now(),
                closed=False))
    log.info("zenhub: %s now closes %s", key,
             ", ".join(zenhub.describe(t) for t in merged))

    for slug, number in note_on:
        for ticket in tickets:
            if not zenhub.connect_pr(cfg, ticket, slug, number):
                log.info("zenhub: could not draw the connection between "
                         "%s and %s#%d (the link itself is recorded)",
                         zenhub.describe(ticket), slug, number)
        try:
            superpost._upsert(slug, number, render_note(merged), ZEN_MARKER)
        except CruxError as exc:
            problems.append(f"{slug}#{number}: could not post the ticket note ({exc})")
    return problems


def detach(cfg: Config, key: str, note_on: list[tuple[str, int]]) -> bool:
    """Forget a link. The sticky comment is emptied rather than left lying."""
    import crux.superpost as superpost
    if not drop(key):
        return False
    for slug, number in note_on:
        try:
            superpost._upsert(slug, number,
                              f"{ZEN_MARKER}\n<sub>No Zenhub ticket is linked "
                              f"to this any more.</sub>", ZEN_MARKER)
        except CruxError:
            pass
    return True


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------

def pr_landed(slug: str, pr: int) -> bool | None:
    """True if merged, False if still open, None if GitHub could not say.

    None is NOT False. A network blip must not read as "still open" (harmless)
    and must certainly not read as "merged" (closes a live ticket).
    """
    import crux.post as post
    try:
        out = post._run_gh(["api", f"repos/{slug}/pulls/{pr}"])
        data = json.loads(out or "{}")
    except (CruxError, ValueError) as exc:
        log.info("zenhub: could not read %s#%d (%s)", slug, pr, exc)
        return None
    if not isinstance(data, dict) or "state" not in data:
        return None
    return bool(data.get("merged"))


def close_for(cfg: Config, key: str, force: bool = False) -> list[str]:
    """Close the tickets behind *key*, once its PRs have landed.

    *force* skips the landed check, for the caller that just did the merging
    and has no reason to ask GitHub about work it watched land.

    Idempotent: an already-closed link is a no-op, so a second merge attempt,
    a sync right after a merge, and a hook that fires twice all cost nothing.
    """
    if not zenhub.enabled(cfg) or not cfg.zenhub_close_on_merge:
        return []
    link = get(key)
    if link is None or link.closed or not link.tickets:
        return []
    if not force:
        for ref in link.prs:
            slug, _, number = ref.partition("#")
            if not number.isdigit():
                continue
            if pr_landed(slug, int(number)) is not True:
                return []  # not all in yet, or GitHub could not say
    problems = zenhub.close_tickets(cfg, link.tickets)
    if problems:
        return problems
    link.closed = True
    put(link)
    return []


def sync(cfg: Config, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Reconcile every open link against GitHub. Returns (done, problems).

    The catch-all for everything Crux did not merge itself — a PR landed from
    the GitHub UI, by a teammate, by automerge. Crux has no webhook and is not
    going to grow one for this: a command anyone can run (or cron) is honest
    about what it is, where a daemon pretending to be live would not be.
    """
    if not zenhub.enabled(cfg):
        return [], [zenhub.why_disabled(cfg)]
    done: list[str] = []
    problems: list[str] = []
    for key, link in sorted(load().items()):
        if link.closed or not link.tickets:
            continue
        states = {}
        for ref in link.prs:
            slug, _, number = ref.partition("#")
            if number.isdigit():
                states[ref] = pr_landed(slug, int(number))
        if not states or not all(state is True for state in states.values()):
            continue
        names = ", ".join(zenhub.describe(t) for t in link.tickets)
        if dry_run:
            done.append(f"{key}: would close {names}")
            continue
        trouble = close_for(cfg, key, force=True)
        if trouble:
            problems.extend(trouble)
        else:
            done.append(f"{key}: closed {names}")
    return done, problems
