# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: harvest every repo in a bundle, then ONE LLM pass over all of it.

The cost rule that shapes this module: a super PR over 8 PRs makes **one**
model call, not nine. Running the normal review per PR and then a tenth pass to
summarize the summaries costs an order of magnitude more, and still cannot see
what only appears when the changes sit together — a contract that moved in one
repo and its caller in another.

So the pipeline is the familiar one, widened rather than repeated:

    per repo:  combined diff -> harvest -> DAG        (deterministic, free)
    once:      all repos' evidence -> annotate        (the single LLM call)

Harvest stays per-repo because every signal it computes is repo-local: git
history, call sites, test proximity. It costs nothing, so breadth here is free.
The evidence reaching the prompt is capped per repo, which is what keeps even a
30-PR bundle inside one call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import crux.analyze as analyze
import crux.dag as dag
import crux.gate as gate
import crux.harvest.blast as blast
import crux.harvest.defuse as defuse
import crux.harvest.history as history
import crux.harvest.structural as structural
import crux.harvest.testprox as testprox
from crux.llm import claude_json
from crux.models import (Bundle, ChangeMap, Config, CruxError, DagEdge, DagNode,
                         HunkSignals, MapArrow, MapStep, Memory,
                         SuperAnnotation, SuperCheck)
from crux.superdiff import RepoDiff

log = logging.getLogger("crux.superanalyze")

PROMPT_PATH = Path(__file__).with_name("prompts") / "super.md"

# Most changes described per repo in the prompt. A bundle is briefed at the
# level of ideas, so the model needs the SHAPE of each repo's change, not every
# chunk of it. This cap is what makes one call viable for a mammoth bundle.
_NODES_PER_REPO = 12
# Map steps kept, matching D33's cap on the per-PR card.
_MAP_STEPS_MAX = 8
# Remembered facts carried in, across all repos.
_MEMORIES_MAX = 12
# Hand-verification steps kept. The same ceiling as the per-PR card's, and for
# the same reason: enough to walk the feature end to end, never a test plan.
_TEST_STEPS_MAX = 10


class SuperAnalyzeError(CruxError):
    """Raised when the bundle's single analysis pass cannot be completed."""


def _hunk_lines(hunk) -> int:
    """Changed lines in a hunk — the same added+removed measure the per-PR
    card's tallies use, so bundle and card numbers stay comparable."""
    return len(hunk.added_lines) + len(hunk.removed_lines)


@dataclass
class RepoEvidence:
    """One repo's harvested combined change, ready for the prompt."""
    diff: RepoDiff
    nodes: list[DagNode] = field(default_factory=list)
    edges: list[DagEdge] = field(default_factory=list)
    signals: dict[str, HunkSignals] = field(default_factory=dict)

    @property
    def changed_lines(self) -> int:
        return sum(_hunk_lines(h) for h in self.diff.hunks)


def harvest(diffs: list[RepoDiff], cfg: Config) -> list[RepoEvidence]:
    """Run the deterministic harvest over each repo's combined diff.

    Identical to the per-PR path (crux run step 1) — same classifiers, same
    signals, same DAG — just pointed at the merged end state instead of one
    branch. No LLM spend here (D6), so a wide bundle costs no more than a
    narrow one.
    """
    out: list[RepoEvidence] = []
    for rd in diffs:
        if not rd.hunks:
            continue  # every member conflicted; nothing combined to review
        hunks = rd.hunks
        info = rd.info()
        try:
            structural.classify(hunks, cfg)
            clusters = structural.mechanical_clusters(hunks)
            signals = defuse.extract_defs_uses(hunks)
            blast.add_blast(signals, hunks, rd.root)
            history.add_history(signals, hunks, rd.root, cfg)
            gate.tag_sensitivity(hunks, signals, cfg)
            testprox.add_test_proximity(signals, hunks, rd.root, cfg)
            nodes, edges = dag.build(hunks, signals, clusters)
        except CruxError as exc:
            log.warning("harvest failed for %s: %s", rd.slug, exc)
            continue
        out.append(RepoEvidence(diff=rd, nodes=nodes, edges=edges, signals=signals))
    return out


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _bundle_section(bundle: Bundle, evidence: list[RepoEvidence]) -> str:
    """The member list: what is in this super PR, grouped by repo."""
    by_repo: dict[str, list[str]] = {}
    for member in bundle.members:
        slug = f"{member.owner}/{member.repo}"
        label = f"#{member.pr} (branch `{member.branch}`)"
        by_repo.setdefault(slug, []).append(label)
    lines = [f"Super PR #{bundle.number} — `{bundle.name}`, "
             f"{len(bundle.members)} pull request"
             f"{'s' if len(bundle.members) != 1 else ''} across "
             f"{len(by_repo)} repositor{'ies' if len(by_repo) != 1 else 'y'}."
             ""]
    for slug, labels in by_repo.items():
        lines.append(f"- **{slug}** — {', '.join(labels)}")
    return "\n".join(lines)


def _node_line(node: DagNode, ev: RepoEvidence, cap: int) -> str:
    """One change, compressed to a single evidence line.

    Deliberately terser than the per-PR prompt's node blocks: the brief is
    written at the level of ideas, so the model needs each change's identity,
    size and risk — not its patch text.
    """
    hunks = {h.id: h for h in ev.diff.hunks}
    mine = [hunks[hid] for hid in node.hunk_ids if hid in hunks]
    lines = sum(_hunk_lines(h) for h in mine)
    files = sorted({h.file for h in mine})
    where = files[0] if len(files) == 1 else f"{len(files)} files"
    if mine:
        where += f":{min(h.new_start for h in mine)}"

    chips: list[str] = []
    best = max((ev.signals[h.id] for h in mine if h.id in ev.signals),
               key=lambda s: s.score, default=None)
    if best:
        if best.blast_radius:
            chips.append(f"used in {best.blast_radius} places")
        if best.sensitive:
            chips.append("sensitive: " + ", ".join(best.sensitive))
        if not best.test_touched:
            chips.append("no tests found")
        if best.co_change_miss:
            chips.append("usually changes with " + ", ".join(best.co_change_miss[:2]))
    tail = f" — {'; '.join(chips)}" if chips else ""
    return (f"  - {node.title} ({where}, {lines} "
            f"line{'s' if lines != 1 else ''}){tail}")


def _repo_section(evidence: list[RepoEvidence], cfg: Config) -> str:
    """Per-repo combined-change evidence, capped so one call stays feasible."""
    if not evidence:
        return "_No repository produced a combined diff — see merge problems below._"
    blocks: list[str] = []
    for ev in evidence:
        prs = ", ".join(f"#{m.pr}" for m in ev.diff.members)
        files = len({h.file for h in ev.diff.hunks})
        head = (f"### {ev.diff.slug}  (PRs merged together: {prs or 'none'})\n"
                f"Combined: {ev.changed_lines} changed lines across {files} "
                f"file{'s' if files != 1 else ''}.")
        # Most-significant changes first, so the cap drops the least important.
        ranked = sorted(
            ev.nodes,
            key=lambda n: max((ev.signals[h].score for h in n.hunk_ids
                               if h in ev.signals), default=0.0),
            reverse=True)
        shown = ranked[:_NODES_PER_REPO]
        body = "\n".join(_node_line(n, ev, cfg.max_read_lines) for n in shown)
        if len(ranked) > len(shown):
            body += (f"\n  - …and {len(ranked) - len(shown)} smaller changes "
                     f"in this repo, not listed.")
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks)


def _conflict_section(diffs: list[RepoDiff]) -> str:
    """Merge problems, stated as the two different things they can mean."""
    lines: list[str] = []
    for rd in diffs:
        for c in rd.conflicts:
            files = ", ".join(f"`{f}`" for f in c.files[:6])
            if c.with_base:
                lines.append(
                    f"- **{c.slug}#{c.pr}** does not merge into its own base "
                    f"branch — it is out of date and needs rebasing. "
                    f"Conflicting files: {files}. It is NOT in the combined "
                    f"diff above.")
            else:
                others = ", ".join(f"#{n}" for n in c.against)
                lines.append(
                    f"- **{c.slug}#{c.pr}** collides with {others} in the same "
                    f"repo on {files}. Both changes cannot land as written. "
                    f"It is NOT in the combined diff above.")
    if not lines:
        return "_None — every pull request in this bundle combines cleanly._"
    return "\n".join(lines)


def build_prompt(bundle: Bundle, evidence: list[RepoEvidence],
                 diffs: list[RepoDiff], cfg: Config,
                 memories: list[Memory] | None = None) -> str:
    try:
        template = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise SuperAnalyzeError(f"prompt template missing: {PROMPT_PATH}") from exc
    try:
        return template.format(
            bundle_section=_bundle_section(bundle, evidence),
            repo_section=_repo_section(evidence, cfg),
            conflict_section=_conflict_section(diffs),
            memory_section=analyze._memory_section(
                list(memories or [])[:_MEMORIES_MAX]),
            ideas_max=cfg.super_ideas_max,
            checks_max=cfg.super_checks_max,
            test_steps_max=_TEST_STEPS_MAX,
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise SuperAnalyzeError(
            f"prompt template {PROMPT_PATH} has a bad placeholder: {exc}") from exc


# ---------------------------------------------------------------------------
# Response coercion
# ---------------------------------------------------------------------------

def _text(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _coerce_map(raw: object) -> ChangeMap | None:
    """Same contract as D33's per-PR map: renumbered at render, dropped whole
    if any step names code. Two steps and one arrow is the floor — below that
    there is no shape to see."""
    if not isinstance(raw, dict):
        return None
    steps: list[MapStep] = []
    for entry in raw.get("steps", []) or []:
        if not isinstance(entry, dict):
            continue
        sid, label = _text(entry.get("id")), _text(entry.get("label"))
        if sid and label:
            steps.append(MapStep(id=sid, label=label))
    steps = steps[:_MAP_STEPS_MAX]
    ids = {s.id for s in steps}
    arrows: list[MapArrow] = []
    for entry in raw.get("arrows", []) or []:
        if not isinstance(entry, dict):
            continue
        src = _text(entry.get("from") or entry.get("src"))
        dst = _text(entry.get("to") or entry.get("dst"))
        if src in ids and dst in ids and src != dst:
            arrows.append(MapArrow(src=src, dst=dst, label=_text(entry.get("label"))))
    if len(steps) < 2 or not arrows:
        return None
    # D33's rule, reused: a map whose steps name code is worse than no map, so
    # one bad label drops the whole diagram rather than leaving a broken flow.
    if any(analyze._is_code_label(s.label) for s in steps):
        log.info("super change map dropped: a step named code, not a user step")
        return None
    return ChangeMap(steps=steps, arrows=arrows)


def _coerce_checks(raw: object, cfg: Config) -> list[SuperCheck]:
    out: list[SuperCheck] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            text, anchor, prs = entry, "", []
        elif isinstance(entry, dict):
            text = _text(entry.get("text"))
            anchor = _text(entry.get("anchor"))
            prs = [_text(p) for p in entry.get("prs", []) or [] if _text(p)]
        else:
            continue
        if text:
            out.append(SuperCheck(text=text, anchor=anchor, prs=prs))
    return out[:cfg.super_checks_max]


def _coerce(data: dict, cfg: Config) -> SuperAnnotation:
    ideas = [_text(x) for x in data.get("ideas", []) or [] if _text(x)]
    order = [_text(x) for x in data.get("order", []) or [] if _text(x)]
    return SuperAnnotation(
        thesis=_text(data.get("thesis")),
        ideas=ideas[:cfg.super_ideas_max],
        change_map=_coerce_map(data.get("change_map")),
        checks=_coerce_checks(data.get("checks"), cfg),
        order=order,
        order_why=_text(data.get("order_why")),
        integration_test=[_text(s) for s in data.get("integration_test", []) or []
                          if _text(s)][:_TEST_STEPS_MAX],
    )


def _jargon(ann: SuperAnnotation) -> list[str]:
    """D15 applies to the brief too: reviewer-facing text carries no tool
    jargon. Reuses the per-PR audit's banned list so both stay in step."""
    texts = [ann.thesis, ann.order_why, *ann.ideas,
             *(c.text for c in ann.checks), *ann.integration_test]
    if ann.change_map:
        texts += [s.label for s in ann.change_map.steps]
        texts += [a.label for a in ann.change_map.arrows]
    hits: list[str] = []
    for text in texts:
        if text:
            hits.extend(analyze.jargon_hits(text))
    return hits


def annotate(bundle: Bundle, evidence: list[RepoEvidence],
             diffs: list[RepoDiff], cfg: Config,
             memories: list[Memory] | None = None) -> SuperAnnotation:
    """THE single LLM call of a super PR run.

    Everything above this line is deterministic and free; everything below is
    rendering. If you find yourself adding a second call here, the design has
    drifted — the whole point of D37 is that a bundle costs one pass.
    """
    prompt = build_prompt(bundle, evidence, diffs, cfg, memories)
    log.info("super analysis: one call over %d repo(s), %d PR(s), ~%d chars",
             len(evidence), len(bundle.members), len(prompt))
    annotation = _coerce(claude_json(prompt, cfg), cfg)

    hits = _jargon(annotation)
    if hits:
        retry = (
            prompt
            + "\n\nREWRITE REQUIRED: your previous answer used banned jargon ("
            + ", ".join(sorted(set(hits)))
            + "). Follow the Plain-English rule: rewrite ALL reviewer-facing text"
              " without those terms and return the complete JSON object again."
        )
        annotation = _coerce(claude_json(retry, cfg), cfg)
        remaining = _jargon(annotation)
        if remaining:
            log.warning("plain-english lint (D15): jargon kept after retry: %s",
                        sorted(set(remaining)))
    return annotation
