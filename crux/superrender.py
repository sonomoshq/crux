# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: render the super-PR brief — one screen, whatever the bundle's size.

The governing constraint is length. A brief over 30 PRs must be no longer than
one over 3; if it grows with the bundle it stops being a brief and becomes the
thing it was meant to replace. So every cap here is enforced in code, not
merely requested in the prompt: a model that ignores its word budget produces a
worse brief, never a longer one.

Anchors are cross-repo, so links cannot use the per-PR card's single-repo
`linkify`. An anchor here carries its own repo — `owner/repo path/file.py:42` —
and resolves against that repo's merged head.
"""
from __future__ import annotations

import re

from crux.models import (SUPER_MARKER, TEST_MARKER, Bundle, BundleMember,
                         ChangeMap, Config, SuperAnnotation, SuperCheck)
from crux.render import _mermaid_label
from crux.superdiff import Conflict, RepoDiff

# An anchor the model wrote: "owner/repo path/to/file.py:12" or "…:12-40",
# with the repo part optional for a single-repo bundle.
_ANCHOR_RE = re.compile(
    r"^(?:(?P<slug>[\w.\-]+/[\w.\-]+)\s+)?(?P<path>[\w./\-]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")


def _blob(slug: str, sha: str, path: str, a: int, b: int) -> str:
    from urllib.parse import quote
    base = f"https://github.com/{slug}/blob/{sha}/{quote(path, safe='/')}"
    return f"{base}#L{a}" if b <= a else f"{base}#L{a}-L{b}"


def _link_anchor(anchor: str, heads: dict[str, str], only: str | None) -> str:
    """Turn a model-written anchor into a clickable link, or plain text.

    Falls back to inline code when the anchor names a repo the bundle does not
    contain: a wrong link is worse than no link, because it reads as verified.
    """
    anchor = anchor.strip().strip("`")
    m = _ANCHOR_RE.match(anchor)
    if not m:
        if not anchor:
            return ""
        # No line number, so nothing to link to. Show just the path: the repo
        # slug is already carried by the check's PR references, and printing
        # "owner/repo some/file.py" reads like a broken link.
        tail = anchor.split(None, 1)[-1] if " " in anchor else anchor
        return f"`{tail}`"
    slug = m.group("slug") or only or ""
    sha = heads.get(slug.lower(), "")
    path, a = m.group("path"), int(m.group("a"))
    b = int(m.group("b")) if m.group("b") else a
    label = f"{path}:{a}" + (f"-{b}" if b > a else "")
    if not slug or not sha:
        return f"`{label}`"
    shown = label if only else f"{slug.split('/')[-1]} {label}"
    return f"[{shown}]({_blob(slug, sha, path, a, b)})"


def _pr_ref(ref: str) -> str:
    """`owner/repo#12` renders as a cross-repo GitHub reference, which links
    automatically and drops a backlink in the target PR's timeline."""
    ref = ref.strip().strip("`")
    return ref if re.match(r"^[\w.\-]+/[\w.\-]+#\d+$", ref) else ""


def _mermaid(cmap: ChangeMap) -> list[str]:
    """The bundle's one picture. Step ids are renumbered n1..nN so nothing the
    model invented reaches Mermaid (D33's rule, unchanged)."""
    ids = {step.id: f"n{i}" for i, step in enumerate(cmap.steps, 1)}
    lines = ["```mermaid", "graph LR"]
    for step in cmap.steps:
        lines.append(f'  {ids[step.id]}["{_mermaid_label(step.label)}"]')
    for arrow in cmap.arrows:
        label = _mermaid_label(arrow.label).replace("|", " ")
        link = f"-->|{label}|" if label else "-->"
        lines.append(f"  {ids[arrow.src]} {link} {ids[arrow.dst]}")
    lines.append("```")
    return lines


def _conflict_check(c: Conflict) -> str:
    """A conflict rendered as an action, never as a status.

    The two kinds mean different work — rebase your own branch, versus
    reconcile with someone else's change — and collapsing them would send the
    reader after the wrong fix.
    """
    files = ", ".join(f"`{f}`" for f in c.files[:3])
    more = f" (+{len(c.files) - 3} more)" if len(c.files) > 3 else ""
    if c.with_base:
        return (f"**{c.slug}#{c.pr}** is out of date and needs a rebase — "
                f"conflicts with its base on {files}{more}")
    others = ", ".join(f"#{n}" for n in c.against)
    return (f"**{c.slug}#{c.pr}** and {others} both change {files}{more} — "
            f"they cannot land as written")


def render_card(bundle: Bundle, ann: SuperAnnotation, diffs: list[RepoDiff],
                cfg: Config | None = None) -> str:
    cfg = cfg or Config()
    heads = {d.slug.lower(): d.head_sha for d in diffs}
    slugs = sorted({d.slug for d in diffs})
    only = slugs[0] if len(slugs) == 1 else None

    merged = [m for d in diffs for m in d.members]
    conflicts = [c for d in diffs for c in d.conflicts]
    lines_total = sum(
        len(h.added_lines) + len(h.removed_lines) for d in diffs for h in d.hunks)
    repos = len({m.repo for m in bundle.members})

    # Read-time from bundle size, capped at 5: the brief is a one-screen
    # document by contract, so past a point more diff cannot mean more card.
    minutes = max(1, min(5, round(lines_total / 400) or 1))

    head = [
        SUPER_MARKER,
        f"## 🦸 Super PR #{bundle.number} · {len(bundle.members)} PRs across "
        f"{repos} repo{'s' if repos != 1 else ''} · ~{minutes} min read",
    ]
    if ann.thesis:
        head.append(f"_{ann.thesis}_")
    sections: list[list[str]] = [head]

    # 1. The cross-cutting ideas — the reason a bundle is briefed at all.
    if ann.ideas:
        sections.append(["### The big picture",
                         *[f"- {idea}" for idea in ann.ideas[:cfg.super_ideas_max]]])

    # 2. One picture of the whole feature. Same floor as D33: two steps and an
    # arrow, else there is no shape to draw and loose boxes are a bullet list.
    if ann.change_map and len(ann.change_map.steps) >= 2 and ann.change_map.arrows:
        sections.append(["### How it works end to end", *_mermaid(ann.change_map)])

    # 3. Check before merging. Conflicts are prepended and are NOT subject to
    # the model's cap — a change that cannot land is never an optional read,
    # and dropping one to fit a budget would hide the bundle's worst news.
    checks: list[str] = [f"- [ ] {_conflict_check(c)}" for c in conflicts]
    for check in ann.checks[:cfg.super_checks_max]:
        bits = [check.text.rstrip(".")]
        anchor = _link_anchor(check.anchor, heads, only)
        if anchor:
            bits.append(f"— {anchor}")
        refs = [r for r in (_pr_ref(p) for p in check.prs) if r]
        if refs:
            bits.append(f"({', '.join(refs[:3])})")
        checks.append(f"- [ ] {' '.join(bits)}")
    if checks:
        sections.append(["### 🔴 Check before merging", *checks])

    # 4. The landing order — the one operational thing the brief must answer.
    landing = render_landing(bundle, cfg, ann.order, ann.order_why)
    if landing:
        sections.append(landing)

    # 5. The machine-checked remainder, as one tally line.
    if merged:
        sections.append([
            f"### 🟢 The rest",
            f"{lines_total} changed line{'s' if lines_total != 1 else ''} "
            f"across {len(merged)} pull request"
            f"{'s' if len(merged) != 1 else ''} combined cleanly — "
            f"reviewed in each PR, no bundle-level risk found.",
        ])

    actions = render_actions(bundle, cfg)
    if actions:
        sections.append(actions)

    card = "\n\n".join("\n".join(s) for s in sections)
    cap = cfg.comment_char_cap
    if len(card) > cap:
        card = card[:cap].rstrip() + "\n\n_(truncated)_"
    # D38: appended AFTER the cap, because this is the part a reader never sees
    # and the part a teammate's machine cannot work without. Truncating the
    # brief must never cost them the bundle.
    from crux.bundle import encode_state
    return card + "\n\n" + encode_state(bundle)


def render_landing(bundle: Bundle, cfg: Config, suggested: list[str],
                   why: str) -> list[str]:
    """The "Landing order" section: the order, the merge method, and why.

    The order shown is the one the merge will FOLLOW (`order_members`), not a
    second rendering of the model's list: the two used to disagree whenever
    the review pass left a member out — the brief appended it alphabetically,
    the merge in member order — and a brief whose plan is not the plan is
    worse than none. Every member appears; none is dropped for being unlisted.

    D41: when a human has pinned the order, the section says so, and the
    review pass's own proposal survives only as reasoning — shown when it
    disagrees, so a reviewer can still see the dependency the model spotted.
    *suggested* and *why* are that proposal: the live annotation on a
    re-brief, the copy kept on the bundle when the section is restamped.

    Lines only, never a blank one: `restamp` finds the section by its heading
    and replaces it up to the next blank line.
    """
    from crux.supermerge import method_label, order_members, resolve_method
    order = [f"{m.owner}/{m.repo}#{m.pr}" for m in order_members(bundle)]
    if not order:
        return []
    why = " ".join((why or "").split())
    method = method_label(resolve_method(bundle, cfg))
    if bundle.merge_method:
        method_line = f"Merge method: **{method}**"
    else:
        # Not the bundle's own choice, so not binding on anyone else's Merge
        # button either: each presser's Crux falls back to its OWN config.
        method_line = f"Merge method: {method} (default)"

    if not bundle.order_pinned:
        lines = ["### Landing order", " → ".join(order), method_line]
        if why:
            lines.append(f"_{why}_")
        return lines

    known = set(order)
    model = [r for r in (_pr_ref(x) for x in suggested) if r in known]
    note = (f"Pinned by hand with `crux super order {bundle.number}`, so a "
            f"re-brief keeps it.")
    if model and model != [r for r in order if r in set(model)]:
        note += f" The review pass suggested {' → '.join(model)}"
        note += f": {why}" if why else "."
    elif why:
        note += f" {why}"
    return ["### Landing order · 📌 pinned", " → ".join(order), method_line,
            f"_{note}_"]


# The landing section as `render_landing` writes it: a heading, then lines, up
# to the first blank line.
_LANDING_RE = re.compile(r"^### Landing order.*?(?=\n\n|\Z)", re.M | re.S)


def restamp(body: str, bundle: Bundle, cfg: Config | None = None) -> str:
    """The published brief, with its landing section and state block redone.

    D41: pinning an order or choosing a merge method changes two things on the
    brief — what a reader is told ("Landing order") and what a teammate's Crux
    will do (the state block) — and they must never disagree. Neither needs a
    new diff or a model call, and re-running the review to print a different
    arrow would cost a model call and rewrite analysis nobody asked to change.
    So everything else in the body is kept exactly as published.

    A brief with no landing section (hand-edited, or cut by the length cap)
    gets one ahead of its buttons, so the change is never silently invisible.
    """
    from crux.bundle import encode_state, strip_state
    cfg = cfg or Config()
    text = strip_state(body)
    landing = "\n".join(render_landing(bundle, cfg, bundle.suggested_order,
                                       bundle.order_why))
    if landing and _LANDING_RE.search(text):
        text = _LANDING_RE.sub(lambda _m: landing, text, count=1)
    elif landing:
        head, sep, tail = text.partition("\n\n---\n")   # the buttons
        text = (head.rstrip() + "\n\n" + landing
                + (sep + tail if sep else ""))
    return text + "\n\n" + encode_state(bundle)


def render_actions(bundle: Bundle, cfg: Config) -> list[str]:
    """D38: the brief's two buttons, as loopback links. Empty when disabled.

    The same URL for every reader, on purpose: `127.0.0.1` resolves on the
    machine of whoever clicks, so one line of markdown reaches each person's
    own Crux — and an approval can therefore carry their name. There is no way
    to render a button for some readers and not others (GitHub serves one issue
    body to everyone), so the rule that you cannot land a bundle you wrote is
    enforced when the link is opened, not by hiding it. The small print says so
    up front, rather than letting an author discover it by pressing.
    """
    port = cfg.serve_port
    if not port:
        return []
    root = f"http://127.0.0.1:{port}/super/{bundle.number}"
    return [
        "---",
        f"🦸 **[Merge this super PR]({root}/merge)** · "
        f"[Close it]({root}/close)",
        "_Both run on your machine (`crux serve`). **Merge** approves each PR "
        "as you, then lands them in the order and with the method above — and "
        "not for the author of this work. To try the feature first, use **Set "
        "up to test** on the verification comment below._",
    ]


def render_backlink(bundle: Bundle, url: str, member: BundleMember) -> str:
    """The one line left on a member PR. Deliberately not the card: the brief
    lives in exactly one place, and N copies of it would go stale N ways."""
    from crux.models import SUPER_LINK_MARKER
    others = len(bundle.members) - 1
    with_others = (f" with {others} other PR{'s' if others != 1 else ''}"
                   if others else "")
    return (f"{SUPER_LINK_MARKER}\n"
            f"Part of **Super PR #{bundle.number}**{with_others} — {url}")


def render_test_comment(bundle: Bundle, steps: list[str],
                        cfg: Config | None = None) -> str:
    """The brief's second sticky comment: how to verify the whole bundle.

    Separate from the card for the same reason the per-PR card keeps it
    separate (`render.render_test_comment`): the brief answers "what must I
    look at", and this answers "how do I see it work" — different questions,
    different moments, and folding them together is what pushes the brief off
    its one screen. The steps are the bundle's, never a member PR's: the
    walkthrough that crosses repos is the only one worth writing here.

    The **Set up to test** button (D38) belongs HERE rather than beside Merge
    and Close, because it is the first step of these instructions: it puts
    every repo on its branch, which is the prerequisite the steps assume. A
    reader who has scrolled to the walkthrough is exactly the person who wants
    it, and one next to the merge buttons is a click away from the wrong one.
    """
    cfg = cfg or Config()
    lines = [
        TEST_MARKER,
        "## 🧪 How to verify this",
        "_Minimal steps to see the whole feature working across its repos. "
        "Not a test plan — the member PRs carry their own checks._",
        "",
    ]
    if cfg.serve_port:
        lines += [
            f"**[🧪 Set up to test](http://127.0.0.1:{cfg.serve_port}/super/"
            f"{bundle.number}/checkout)** — puts all "
            f"{len(bundle.members)} repos on their pull request branches, on "
            f"your machine (`crux serve`). Nothing is merged or stashed; a repo "
            f"with uncommitted work is reported and left alone.",
            "",
        ]
    else:
        lines += [
            f"**Prerequisites:** every repo in Super PR #{bundle.number} "
            f"checked out on its PR branch and set up as its own README "
            f"describes.",
            "",
        ]
    # Steps are commands to paste, so they are left exactly as written — the
    # card's anchor linking would only mangle a path inside one.
    lines += [f"{i}. {step}" for i, step in enumerate(steps, 1)]
    return "\n".join(lines)


def render_merge_report(bundle: Bundle, results: list[BundleMember],
                        note: str = "") -> str:
    """What landed and what did not, after a merge run.

    Every member appears with an explicit outcome. A bundle that half-landed is
    a state someone has to act on, so the report never summarizes it as a count
    — it names each blocked PR and the reason.
    """
    landed = [m for m in results if m.state == "merged"]
    blocked = [m for m in results if m.state == "blocked"]
    lines = [f"### Merge run — {len(landed)} of {len(results)} landed"]
    # How it landed, when that was not the ordinary way. An admin override is
    # a fact about this merge, and it belongs where the merge is read.
    if note:
        lines += ["", f"> {note}"]
    if landed:
        lines.append("")
        lines += [f"- ✅ {m.owner}/{m.repo}#{m.pr}" for m in landed]
    if blocked:
        lines.append("")
        lines += [f"- ❌ {m.owner}/{m.repo}#{m.pr} — {m.error or 'blocked'}"
                  for m in blocked]
        lines.append("")
        lines.append("_Fix the blocked pull requests and run the merge again; "
                     "the ones that already landed are skipped._")
    return "\n".join(lines)
