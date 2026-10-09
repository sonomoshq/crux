# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tier assignment: deterministic rule floors (LLM may promote, never demote),
line spans, minutes budget, and evidence chips.

Contract (DESIGN.md): assign(nodes, hunks, signals, annotation, cfg) -> list[Item].
Items are returned in DAG numbering order, so RED items stay in causal order.
"""
from __future__ import annotations

import math

from crux.models import (
    Annotation,
    Badge,
    Config,
    DagNode,
    Hunk,
    HunkClass,
    HunkSignals,
    Item,
    NodeAnnotation,
    Tier,
)

_TIER_RANK = {Tier.GREEN: 0, Tier.YELLOW: 1, Tier.RED: 2}

# Markers whose addition to a test file weakens the suite (D: tier floors).
_SKIP_MARKERS = (
    ".skip", "@unittest.skip", "@pytest.mark.skip", "xit(", "xdescribe(", "@Disabled",
)

_MAX_CHIPS = 6


def assign(
    nodes: list[DagNode],
    hunks: list[Hunk],
    signals: dict[str, HunkSignals],
    annotation: Annotation | None,
    cfg: Config,
) -> list[Item]:
    by_id = {h.id: h for h in hunks}
    ann_nodes: dict[int, NodeAnnotation] = annotation.nodes if annotation else {}
    claims = annotation.claims if annotation else []

    items: list[Item] = []
    for node in sorted(nodes, key=lambda n: n.number):
        node_hunks = [by_id[hid] for hid in node.hunk_ids if hid in by_id]
        if not node_hunks:
            continue
        sigs = [signals.get(h.id) or HunkSignals(hunk_id=h.id) for h in node_hunks]
        ann = ann_nodes.get(node.number)

        tier = _floor_tier(node_hunks, sigs, cfg)
        badge = node.badge
        if ann and ann.design_decision:
            # Badge only; the tier stays floor-governed but never drops below YELLOW.
            badge = Badge.DESIGN_DECISION
            if _TIER_RANK[tier] < _TIER_RANK[Tier.YELLOW]:
                tier = Tier.YELLOW
        # Pipeline step 5: the LLM's opinion can RAISE an item above its rule
        # floor, never lower it.
        suggestion = _suggested_tier(ann)
        if suggestion is not None and _TIER_RANK[suggestion] > _TIER_RANK[tier]:
            tier = suggestion
        # D25: tests ride along with the code they test — a test-only change
        # never outranks the code change (sensitive keywords inside a test
        # can't force RED, and the LLM can't promote it there), UNLESS it
        # weakens the suite: that floor stays RED.
        if (
            _TIER_RANK[tier] > _TIER_RANK[Tier.YELLOW]
            and all(is_test_file(h.file, cfg) for h in node_hunks)
            and not _weakens_suite(node_hunks, cfg)
        ):
            tier = Tier.YELLOW
        # D32: a change the model deliberately left out has no analysis behind
        # it — its title is machine-made and its why is empty, so as a must-read
        # it is a line the reviewer cannot act on. The rule floor still keeps it
        # ON the card (nothing is ever hidden), one line down, where the card
        # shows the rule that flagged it instead.
        no_analysis = bool(ann and ann.omitted_by_model)
        if no_analysis and tier is Tier.RED:
            tier = Tier.YELLOW

        primary = node_hunks[0].file
        prim_hunks = [h for h in node_hunks if h.file == primary]
        # File gone at head (whole-file deletion hunks are 0,0 on the new
        # side): evidence must point at the base-side blob and old lines (D3).
        deleted = all(h.new_count == 0 and h.new_start == 0 for h in prim_hunks)
        if deleted:
            line_start = min(h.old_start for h in prim_hunks)
            line_end = max(h.old_start + max(h.old_count, 1) - 1 for h in prim_hunks)
        else:
            line_start = min(h.new_start for h in prim_hunks)
            line_end = max(h.new_start + max(h.new_count, 1) - 1 for h in prim_hunks)

        # D23: no pointer asks the reviewer to read more than max_read_lines
        # lines. Prefer the LLM's analysis breakdown; when it gave none and
        # the span is too big, pick the few most important chunks by harvest
        # score and account for the rest in one line — never tile the whole
        # item into cap-sized windows.
        breakdown = list(ann.breakdown) if ann and ann.breakdown else []
        if (not breakdown and not deleted
                and line_end - line_start + 1 > cfg.max_read_lines):
            breakdown = _fallback_breakdown(prim_hunks, signals,
                                            cfg.max_read_lines)

        items.append(Item(
            number=node.number,
            tier=tier,
            badge=badge,
            title=(ann.title if ann and ann.title else node.title),
            file=primary,
            line_start=line_start,
            line_end=line_end,
            minutes=(ann.minutes if ann else _minutes_heuristic(node_hunks)),
            chips=_chips(node, node_hunks, sigs, ann, claims),
            why=(ann.why if ann else ""),
            questions=(list(ann.questions) if ann else []),
            deleted=deleted,
            changed_lines=sum(
                len(h.added_lines) + len(h.removed_lines) for h in node_hunks),
            before=(ann.before if ann else ""),
            after=(ann.after if ann else ""),
            breakdown=breakdown,
            no_analysis=no_analysis,
        ))
    return items


# D23 fallback: a curated reading list, never a tiling. At most this many
# pointed-at windows (each <= max_read_lines), so total reading stays around
# 3 x cap lines no matter how big the item is; everything left out is
# accounted for in one closing line with no pointer.
_BREAKDOWN_WINDOWS_MAX = 3


def _fallback_breakdown(prim_hunks: list[Hunk],
                        signals: dict[str, HunkSignals],
                        cap: int) -> list[str]:
    """Select the most important parts of an oversized item (no-LLM path).

    Chunks are ranked by their harvest score (call counts, sensitivity,
    churn — computed before the gate), size as the tie-breaker; the top few
    each get one <=cap-line pointer, most important first. The remaining
    changed lines get a single summary line instead of pointers — sending
    the reviewer into every 50-line window of a 600-line change is exactly
    the wall of code D23 exists to prevent.
    """
    def importance(h: Hunk) -> tuple[float, int]:
        sig = signals.get(h.id)
        return (sig.score if sig else 0.0,
                len(h.added_lines) + len(h.removed_lines))

    ranked = sorted(prim_hunks, key=importance, reverse=True)
    parts: list[str] = []
    covered = 0
    seen: set[str] = set()
    for h in ranked[:_BREAKDOWN_WINDOWS_MAX]:
        span = max(h.new_count, 1)
        a = h.new_start
        b = a + min(span, cap) - 1
        loc = f"{h.file}:{a}" if b <= a else f"{h.file}:{a}-{b}"
        symbol = h.enclosing_symbol
        # Two chunks inside one function would otherwise both render as "the
        # `f` part", which reads like a duplicated line rather than a second
        # place to look.
        if symbol and symbol in seen:
            label = f"another part of `{symbol}`"
        elif symbol:
            label = f"the `{symbol}` part"
        else:
            label = "another changed chunk" if "" in seen else "one changed chunk"
        seen.add(symbol or "")
        parts.append(f"{label} — `{loc}`")
        covered += min(span, cap)
    leftover = sum(max(h.new_count, 1) for h in prim_hunks) - covered
    if leftover > 0:
        parts.append(
            f"the remaining ~{leftover} lines look routine to Crux — "
            "skim them only if something above raises a question")
    return parts


def _suggested_tier(ann: NodeAnnotation | None) -> Tier | None:
    if ann is None or not ann.suggested_tier:
        return None
    try:
        return Tier(str(ann.suggested_tier).strip().lower())
    except ValueError:
        return None


def _floor_tier(node_hunks: list[Hunk], sigs: list[HunkSignals], cfg: Config) -> Tier:
    behavioral = any(h.klass is HunkClass.BEHAVIORAL for h in node_hunks)
    max_blast = max((s.blast_radius for s in sigs), default=0)
    if any(s.sensitive for s in sigs):
        return Tier.RED
    if behavioral and max_blast >= cfg.gate_max_blast:
        return Tier.RED
    if any(_is_workflow_file(h.file) for h in node_hunks):
        return Tier.RED
    if _weakens_suite(node_hunks, cfg):
        return Tier.RED
    if max_blast == 0 and all(
        h.klass in (HunkClass.MECHANICAL, HunkClass.GENERATED) for h in node_hunks
    ):
        return Tier.GREEN
    return Tier.YELLOW


def _is_workflow_file(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.startswith(".github/workflows/") or "/.github/workflows/" in p


def is_test_file(path: str, cfg: Config) -> bool:
    """Test files by directory (cfg.test_dirs) OR filename shape: covers
    pytest/unittest (test_x.py, x_test.py, conftest.py), JS/TS (x.test.ts,
    x.spec.ts), Go (x_test.go), Ruby (x_spec.rb), and the PascalCase
    Test/Tests/Spec suffixes of Java/C#/Swift. D25 misfired on a real PR
    because directory matching alone missed suffix-named test files."""
    parts = path.replace("\\", "/").split("/")
    if any(part in cfg.test_dirs for part in parts[:-1]):
        return True
    base = parts[-1]
    if base == "conftest.py" or base.startswith("test_"):
        return True
    if ".test." in base or ".spec." in base:
        return True
    stem = base.rsplit(".", 1)[0]
    # Case-sensitive on purpose: "RequestSpec.java" is a test, "inspect.py"
    # and "contest.js" are not.
    return stem.endswith(("_test", "_spec", "Test", "Tests", "Spec"))


def _marker_in_code(line: str, marker: str) -> bool:
    """True when *marker* appears in *line* OUTSIDE any string literal.

    A test ABOUT skip detection adds fixture lines like
    ``added=["@unittest.skip('later')"]`` — the marker is data, not a skipped
    test, and reading it as one forced the whole node red on a real PR (the
    detector detected itself). Quote parity before the match is a cheap,
    language-agnostic tell for "inside a string"."""
    idx = line.find(marker)
    while idx != -1:
        prefix = line[:idx]
        if prefix.count('"') % 2 == 0 and prefix.count("'") % 2 == 0:
            return True
        idx = line.find(marker, idx + 1)
    return False


def _weakens_suite(node_hunks: list[Hunk], cfg: Config) -> bool:
    """Added skip markers, or NET assertion loss across the node's test
    chunks taken together. Netting must span the whole node: rewriting a
    test moves assertions between chunks (one chunk deletes 2, another adds
    3) and must not read as a deleted assertion — counting per chunk forced
    honest test rewrites red on a real PR."""
    test_hunks = [h for h in node_hunks if is_test_file(h.file, cfg)]
    if not test_hunks:
        return False
    if any(_marker_in_code(line, marker)
           for h in test_hunks for line in h.added_lines
           for marker in _SKIP_MARKERS):
        return True
    removed = sum(1 for h in test_hunks for line in h.removed_lines
                  if "assert" in line)
    added = sum(1 for h in test_hunks for line in h.added_lines
                if "assert" in line)
    return removed > added


def _minutes_heuristic(node_hunks: list[Hunk]) -> int:
    changed = sum(len(h.added_lines) + len(h.removed_lines) for h in node_hunks)
    # ~15 changed lines per minute of careful review, clamped to 1..10.
    return max(1, min(10, math.ceil(changed / 15)))


def _chips(
    node: DagNode,
    node_hunks: list[Hunk],
    sigs: list[HunkSignals],
    ann: NodeAnnotation | None,
    claims: list,
) -> list[str]:
    # D15: chips render straight into the card, so they are written in plain
    # words a new hire could follow — never machine-chip syntax.
    chips: list[str] = []
    for s in sigs:
        for rule in s.sensitive:
            word = rule.split(":", 1)[1] if rule.startswith(("path:", "keyword:")) else rule
            chips.append(f"touches a sensitive area ({word})")
    max_blast = max((s.blast_radius for s in sigs), default=0)
    if max_blast > 0:
        noun = "place" if max_blast == 1 else "places"
        chips.append(f"called from {max_blast} {noun}")
    for s in sigs:
        for partner in s.co_change_miss:
            chips.append(
                f"usually changes together with {partner}, which was not changed here")
    behavioral = any(h.klass is HunkClass.BEHAVIORAL for h in node_hunks)
    if behavioral and not any(s.test_touched for s in sigs):
        chips.append("no tests cover this change")
    node_ids = set(node.hunk_ids)
    for claim in claims:
        if claim.uncertain and node_ids & set(claim.hunk_ids):
            chips.append(f'the AI author said it was unsure: "{claim.text}"')
    if ann:
        chips.extend(ann.chips)
    return list(dict.fromkeys(chips))[:_MAX_CHIPS]
