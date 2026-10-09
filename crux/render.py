# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Markdown card rendering for Crux (DESIGN D3, D4, D5, D8).

Public API (module-contract table):
    render_card(info, pr_number, items, annotation, gate_stats) -> str
    permalink(info, path, a, b) -> str
    diff_anchor(info, pr, path, line) -> str

Extras used by cli.py:
    render_failure_card(info, error) -> str   (loud D8 failure update)
    render_skip_note(stats) -> str            (single log line for gate skips)

Extras used by post.py:
    linkify(info, text) -> str   (turn `path:line` refs into links; D19 bodies)
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import quote

from crux.models import (
    CARD_MARKER,
    TEST_MARKER,
    Annotation,
    AuditRow,
    Badge,
    ChangeMap,
    Config,
    DagNode,
    DesignFinding,
    Item,
    RepoInfo,
    Tier,
    Verdict,
)

# Short, plain-English badge tag shown on each reviewed change. D32: a plain
# code change is the default and carries no information a reviewer can act on
# (of course it is code) — it gets no tag, so the title leads the line. The
# other three say something the title does not, and stay.
_BADGE_TAG: dict[Badge, str] = {
    Badge.CODE_CHANGE: "",
    Badge.DESIGN_DECISION: "🎯 design decision",
    Badge.CODE_CHANGE_EFFECTS: "↳ ripple effect",
    Badge.MECHANICAL_CHANGES: "⚙️ mechanical",
}


def _badge(item: Item) -> str:
    return _BADGE_TAG.get(item.badge, item.badge.value)

_VERDICT_EMOJI: dict[Verdict, str] = {
    Verdict.VERIFIED: "✅",
    Verdict.COULD_NOT_VERIFY: "⚠️",
    Verdict.CONTRADICTED: "❌",
}

TRIM_NOTE = "... trimmed"

log = logging.getLogger("crux.render")


# ---------------------------------------------------------------------------
# Links (D3)
# ---------------------------------------------------------------------------

def _blob_url(info: RepoInfo, sha: str, path: str, a: int, b: int) -> str:
    # Paths with spaces/#/? must be percent-encoded or GitHub-flavored
    # markdown refuses to render the inline link at all.
    base = f"https://github.com/{info.owner}/{info.repo}/blob/{sha}/{quote(path, safe='/')}"
    if b <= a:  # single line => #L{a}
        return f"{base}#L{a}"
    return f"{base}#L{a}-L{b}"


def permalink(info: RepoInfo, path: str, a: int, b: int) -> str:
    return _blob_url(info, info.head_sha, path, a, b)


def diff_anchor(info: RepoInfo, pr: int, path: str, line: int, side: str = "R") -> str:
    # GitHub Files-tab anchors hash the *path*, not the content. side "L"
    # targets the base column (used for files deleted at head).
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return (f"https://github.com/{info.owner}/{info.repo}/pull/{pr}"
            f"/files#diff-{digest}{side}{line}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _span(item: Item) -> int:
    return max(item.line_end - item.line_start + 1, 1)


def _line_label(path: str, a: int, b: int) -> str:
    return f"{path}:{a}" if b <= a else f"{path}:{a}-{b}"


def _cell(text: str) -> str:
    """Make text safe inside a markdown table cell."""
    return text.replace("\n", " ").replace("|", "\\|")


def _mermaid_label(text: str) -> str:
    # Mermaid double-quoted labels: escape embedded quotes via entity code.
    return text.replace('"', "#quot;").replace("\n", " ")


def _links_fragment(info: RepoInfo, pr_number: int | None, item: Item) -> str:
    plink = _item_permalink(info, item)
    label = _line_label(item.file, item.line_start, item.line_end)
    parts = [f"[{label}]({plink})"]
    if pr_number is not None:  # D3 secondary link; omitted when no PR yet
        dlink = item.diff_link or diff_anchor(
            info, pr_number, item.file, item.line_start,
            side="L" if item.deleted else "R")
        parts.append(f"[diff]({dlink})")
    return " · ".join(parts)


def _item_line(info: RepoInfo, pr_number: int | None, item: Item,
               note: str = "") -> str:
    """The headline every review item leads with: the badge tag when it says
    something, the title, an optional note, then the links."""
    parts = [f"**{item.title}**"]
    if note:
        parts.append(note)
    parts.append(_links_fragment(info, pr_number, item))
    tag = _badge(item)
    prefix = f"{tag} · " if tag else ""
    return f"- {prefix}" + " · ".join(parts)


def _flag_reason(item: Item) -> str:
    """Why an item nobody wrote about is on the card at all: the plain-words
    rule that flagged it (written by tiers). Without it the line is a bare
    machine-made title — "post.py additions" — the reviewer cannot act on."""
    return item.chips[0] if item.no_analysis and item.chips else ""


def _item_permalink(info: RepoInfo, item: Item) -> str:
    if item.permalink:
        return item.permalink
    # Deleted at head: the head-side blob 404s, so link the base-side blob.
    sha = info.base_sha if item.deleted else info.head_sha
    return _blob_url(info, sha, item.file, item.line_start, item.line_end)


def _design_citation(finding: DesignFinding) -> str:
    ref = finding.convention_ref.replace("\n", " ")
    return f"{finding.convention_source}: {ref}"


def _design_permalink(info: RepoInfo, finding: DesignFinding) -> str:
    return finding.permalink or permalink(
        info, finding.file, finding.line_start, finding.line_end)


def _audit_tally(audit: list[AuditRow]) -> str:
    v = sum(1 for r in audit if r.verdict is Verdict.VERIFIED)
    c = sum(1 for r in audit if r.verdict is Verdict.COULD_NOT_VERIFY)
    x = sum(1 for r in audit if r.verdict is Verdict.CONTRADICTED)
    return f"Claims audit: {v} verified · {c} could-not-verify · {x} CONTRADICTED"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _mermaid_section(cmap: ChangeMap) -> list[str]:
    # D33: the review's own user-level flow — plain-English steps, no numbers,
    # no badges, nothing named after a file or symbol. Step ids are renumbered
    # n1..nN here so whatever the model called them cannot reach Mermaid.
    ids = {step.id: f"n{i}" for i, step in enumerate(cmap.steps, 1)}
    lines = ["### Change map", "```mermaid", "graph LR"]
    for step in cmap.steps:
        lines.append(f'  {ids[step.id]}["{_mermaid_label(step.label)}"]')
    for arrow in cmap.arrows:
        label = _mermaid_label(arrow.label).replace("|", " ")
        link = f"-->|{label}|" if label else "-->"
        lines.append(f"  {ids[arrow.src]} {link} {ids[arrow.dst]}")
    lines.append("```")
    return lines


def _red_section(
    info: RepoInfo,
    pr_number: int | None,
    reds: list[Item],
    design_by_item: dict[int, list[DesignFinding]] | None = None,
) -> list[str]:
    design_by_item = design_by_item or {}
    lines = ["### 🔴 Must read — start at 1, the order tells the story"]
    for it in reds:
        lines.append("")
        lines.append(
            f"**{it.number}. {it.badge.value} · {it.title}** — "
            f"{_links_fragment(info, pr_number, it)} · ~{it.minutes} min"
        )
        if it.chips:
            lines.append(" ".join(f"`{c}`" for c in it.chips))
        if it.why:
            lines.append(it.why)
        for q in it.questions:
            lines.append(f"- [ ] {q}")
        # D14: design findings on this item's file live inside the item.
        for f in design_by_item.get(it.number, []):
            text = f.text.replace("\n", " ")
            lines.append(f"🧭 {text} — {_design_citation(f)}")
    return lines


def _yellow_rows(info: RepoInfo, yellows: list[Item]) -> list[str]:
    rows = []
    for it in yellows:
        gist = f"{it.title} — {it.why}" if it.why else it.title
        label = _line_label(it.file, it.line_start, it.line_end)
        rows.append(
            f"| {it.number} | {_cell(gist)} | [{_cell(label)}]({_item_permalink(info, it)}) |"
        )
    return rows


def _green_bullets(info: RepoInfo, greens: list[Item],
                   nodes: list[DagNode] | None = None) -> list[str]:
    # D15 phrasing: "Crux checked all N are identical; here is one of them".
    counts = {n.number: len(n.hunk_ids) for n in (nodes or [])}
    bullets = []
    for it in greens:
        label = _line_label(it.file, it.line_start, it.line_end)
        title = it.title.replace("\n", " ")
        count = counts.get(it.number, 0)
        if count > 1:
            proof = f"Crux checked all {count} are identical; here is one of them:"
        else:
            proof = "Crux checked this change is harmless; shown here:"
        bullets.append(
            f"- **{it.number} · {title}** — {proof} "
            f"[{label}]({_item_permalink(info, it)})"
        )
    return bullets


def _design_bullets(info: RepoInfo, findings: list[DesignFinding]) -> list[str]:
    bullets = []
    for f in findings:
        label = _line_label(f.file, f.line_start, f.line_end)
        text = f.text.replace("\n", " ")
        bullets.append(
            f"- 🧭 **{f.kind}** · {text} — [{label}]({_design_permalink(info, f)}) "
            f"— {_design_citation(f)}"
        )
    return bullets


def _split_design(
    design: list[DesignFinding], reds: list[Item],
) -> tuple[dict[int, list[DesignFinding]], list[DesignFinding]]:
    """D14 placement: a finding whose file matches a RED item's file renders
    inside that item; everything else goes to the standalone Design notes."""
    red_by_file: dict[str, int] = {}
    for it in reds:
        red_by_file.setdefault(it.file, it.number)
    by_item: dict[int, list[DesignFinding]] = {}
    standalone: list[DesignFinding] = []
    for f in design:
        number = red_by_file.get(f.file)
        if number is None:
            standalone.append(f)
        else:
            by_item.setdefault(number, []).append(f)
    return by_item, standalone


def _audit_section(audit: list[AuditRow]) -> list[str]:
    lines = [
        "### Claims audit — what the AI author said, checked against the actual code",
        "| The author claimed | Check result | Evidence |",
        "|--------------------|--------------|----------|",
    ]
    for row in audit:
        emoji = _VERDICT_EMOJI[row.verdict]
        lines.append(f"| {_cell(row.claim)} | {emoji} {row.verdict.value} | {_cell(row.evidence)} |")
    return lines


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

# Keep the card a <=5-minute brief (D30): cap the two review lists (the rest
# is in the diff). Must-read entries carry prose, before/now lines, and
# breakdowns, so five of them is already ~3-4 minutes of reading; skim entries
# are one line each. Field-flagged: 8 must-reads made ~8-minute cards.
_MUST_READ_MAX = 5
_SKIM_MAX = 8

def render_card(
    info: RepoInfo,
    pr_number: int | None,
    items: list[Item],
    annotation: Annotation | None,
    gate_stats: dict,
    cfg: Config | None = None,
) -> str:
    cap = (cfg or Config()).comment_char_cap
    max_read = (cfg or Config()).max_read_lines
    overview = list(annotation.overview) if annotation else []
    summary = (annotation.summary if annotation else "").strip()
    reds = [i for i in items if i.tier is Tier.RED]
    # D32: within the skim list, changes the review actually wrote about come
    # before the ones only a rule flagged — otherwise machine-titled pointers
    # eat the capped slots and push real findings into the "…and N more" line.
    # Causal (change-number) order is kept inside each group.
    yellows = sorted((i for i in items if i.tier is Tier.YELLOW),
                     key=lambda i: (i.no_analysis, i.number))
    greens = [i for i in items if i.tier is Tier.GREEN]

    # Read-time from the size of the change, not per-node LLM effort — keeps the
    # header honest with no extra model output (~120 changed lines per minute).
    # Capped at 5: the card is a <=5-minute brief by contract (D20) — its lists
    # are capped and its big items broken down (D23), so past a point more diff
    # does not mean more card. "~8 min read" headers were field-flagged as too
    # much; if the card itself ever needs longer, the list caps are the bug.
    total_lines = int(gate_stats.get("total_lines", 0) or 0)
    minutes = max(1, min(5, round(total_lines / 120) or 1))

    head = [
        CARD_MARKER,
        f"## Crux · `{info.head_sha[:7]}` · ~{minutes} min read",
    ]
    if summary:
        head.append(f"_{summary}_")
    sections: list[list[str]] = [head]

    # 1. The big picture — high-level ideas, each linked to where it lives.
    if overview:
        sections.append(["### The big picture",
                         *[f"- {linkify(info, b)}" for b in overview]])

    # 2. Change map (D33) — the PR's flow in a handful of user-level steps,
    # written by the review. Two steps and one arrow is the floor: below that
    # there is no shape to see, and a diagram of loose boxes is a bullet list
    # wearing a picture. Nothing is drawn when the review sent no map.
    cmap = annotation.change_map if annotation else None
    if cmap and len(cmap.steps) >= 2 and cmap.arrows:
        sections.append(_mermaid_section(cmap))

    # 3. Must read — the important changes, one tight line (+ one why) each,
    # capped so the card stays a brief, not a backlog.
    if reds:
        sec = ["### 🔴 Must read"]
        for it in reds[:_MUST_READ_MAX]:
            sec.append(_item_line(info, pr_number, it))
            if it.why:
                sec.append(f"  {it.why}")
            # D24: Crux states the comparison; the reviewer never diffs versions.
            if it.before and it.after:
                sec.append(f"  **Before:** {it.before} **Now:** {it.after}")
            sec.extend(_breakdown_lines(info, it, max_read))
        if len(reds) > _MUST_READ_MAX:
            sec.append(f"- …and {len(reds) - _MUST_READ_MAX} more — see the diff")
        sections.append(sec)

    # 4. Worth a skim — STRICTLY one line each (D30). No prose, no before/now,
    # no breakdown sub-bullets: a skim item is a headline with a link, and
    # anything that needs more reading belongs in must-read. Sub-bullets here
    # were a top length driver of the field-flagged ~8-minute cards.
    if yellows:
        sec = ["### 🟡 Worth a skim"]
        sec += [_item_line(info, pr_number, it, note=_flag_reason(it))
                for it in yellows[:_SKIM_MAX]]
        if len(yellows) > _SKIM_MAX:
            sec.append(f"- …and {len(yellows) - _SKIM_MAX} more")
        sections.append(sec)

    # 5. Safe to skip — a single machine-checked tally, not a wall of bullets.
    if greens:
        gl = sum((i.changed_lines or _span(i)) for i in greens)
        sections.append([
            f"### 🟢 Safe to skip",
            f"{gl} line{'s' if gl != 1 else ''} across {len(greens)} "
            f"group{'s' if len(greens) != 1 else ''}, machine-checked — no review needed.",
        ])

    actions = render_actions(info, pr_number, cfg or Config())
    if actions:
        sections.append(actions)

    card = "\n\n".join("\n".join(s) for s in sections)
    if len(card) > cap:
        card = card[:cap].rstrip() + "\n\n_(truncated)_"
    return card


def render_actions(info: RepoInfo, pr_number: int | None,
                   cfg: Config) -> list[str]:
    """D38: the card's Merge button, as a loopback link. Empty when disabled.

    The same mechanism as the super PR brief, on an ordinary card: `127.0.0.1`
    resolves on the machine of whoever clicks, so the request reaches THEIR
    Crux and the approval carries their name. And the same rule — your own PR
    is not yours to approve — enforced when the link opens, because a comment
    body renders identically for everyone and cannot hide a button from its
    author.
    """
    if not cfg.serve_port or not pr_number:
        return []
    root = (f"http://127.0.0.1:{cfg.serve_port}/pr/"
            f"{info.owner}/{info.repo}/{pr_number}")
    return [
        "---",
        f"**[Approve and merge this PR]({root}/merge)**",
        "_Runs on your machine (`crux serve`) — approves in your name, then "
        "merges. Not for the author; an admin can override on the record._",
    ]


def _breakdown_lines(info: RepoInfo, item: Item, max_read: int) -> list[str]:
    """D23 sub-bullets: shown only when the item's span would ask the reviewer
    to read more than *max_read* lines. Each entry carries its own <=max_read
    pointer, so the big span is navigation, not reading work."""
    if item.deleted or not item.breakdown or _span(item) <= max_read:
        return []
    return [f"  - {linkify(info, part)}" for part in item.breakdown]


# Backtick-wrapped `path:line` or `path:line-line` references in overview text
# are turned into links to the block of code they name.
_LOC_RE = re.compile(r"`([\w./\-]+):(\d+)(?:-(\d+))?`")


def linkify(info: RepoInfo, text: str) -> str:
    """Turn every backticked `path:line` reference in *text* into a link to
    that block of code — used by the card and by the PR description (D19)."""
    def repl(m: "re.Match[str]") -> str:
        path, a = m.group(1), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        label = m.group(0)  # keep the exact `path:line(-line)` text as the label
        return f"[{label}]({_blob_url(info, info.head_sha, path, a, b)})"
    return _LOC_RE.sub(repl, text)


def render_test_comment(info: RepoInfo, steps: list[str]) -> str:
    """The secondary sticky comment: minimal human steps to verify the PR's
    main feature end-to-end. Any `file:line` reference in a step is linked.

    Prepends a Prerequisites line that LINKS to the repo's existing setup docs
    (README install section, etc.) rather than duplicating install steps."""
    lines = [
        TEST_MARKER,
        "## 🧪 How to verify this",
        "_Minimal steps to see the main change working. Not needed for every "
        "review — here because this PR ships something worth trying by hand._",
        "",
    ]
    ref = _setup_reference(info)
    if ref:
        label, url = ref
        lines += [f"**Prerequisites:** make sure the project is installed and "
                  f"set up first — see [{label}]({url}).", ""]
    lines += [f"{i}. {linkify(info, s)}" for i, s in enumerate(steps, 1)]
    return "\n".join(lines)


# Repo docs that carry install/setup instructions, in the order we prefer them.
_SETUP_DOCS = ("README.md", "README.rst", "README.txt", "README",
               "CONTRIBUTING.md", "INSTALL.md", "docs/README.md",
               "docs/installation.md", "docs/getting-started.md")
_SETUP_HEADING = re.compile(
    r"^#{1,6}\s+(.*?(?:instal|set[ -]?up|getting started|quick ?start|"
    r"prerequisite|from source).*?)\s*$", re.IGNORECASE | re.MULTILINE)


def _gh_anchor(heading: str) -> str:
    """GitHub heading slug: lowercase, drop punctuation/emoji, spaces→hyphens."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", text).strip("-")


def _setup_reference(info: RepoInfo) -> tuple[str, str] | None:
    """(label, url) pointing at the repo's setup docs — the install section of
    the README when one exists, else the doc itself. None if no doc is found."""
    root = Path(info.root)
    for rel in _SETUP_DOCS:
        path = root / rel
        if not path.is_file():
            continue
        base = (f"https://github.com/{info.owner}/{info.repo}/blob/"
                f"{info.head_sha}/{quote(rel, safe='/')}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = _SETUP_HEADING.search(text)
        if m:
            return (m.group(1).strip(), f"{base}#{_gh_anchor(m.group(1))}")
        return (rel.rsplit("/", 1)[-1], base)
    return None


# ---------------------------------------------------------------------------
# Failure card + skip note
# ---------------------------------------------------------------------------

def render_failure_card(info: RepoInfo, error: str | Exception) -> str:
    """Short, loud replacement body for the sticky comment (D8): never silence."""
    msg = str(error).strip().splitlines()[0] if str(error).strip() else "unknown error"
    return "\n".join(
        [
            CARD_MARKER,
            f"## Crux · `{info.head_sha[:7]}` · ⚠️ RUN FAILED",
            f"**Crux could not analyze this push:** {msg}",
            "",
            "Any earlier card content is stale. Re-run locally with `crux run` to reproduce.",
        ]
    )


def render_skip_note(stats: dict) -> str:
    """One log line for gate skips (D6): nothing is posted, only logged."""
    detail = " ".join(f"{k}={stats[k]}" for k in sorted(stats)) if stats else "no stats"
    return f"crux: skipped — trivial per gate (D6): {detail}"
