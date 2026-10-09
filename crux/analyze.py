# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""LLM annotation pass (pipeline stage 4).

Builds one prompt from the packaged ``crux/prompts/analyze.md`` covering: PR summary sources
(commit subjects + ``.crux/intent.json``), the change DAG with truncated hunk
patches, per-hunk signal chips, and — incrementally (D9) — the previous run's
annotations for unchanged nodes so the model only writes the changed ones.
The model's JSON reply is validated/coerced into :class:`crux.models.Annotation`.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from importlib import resources
from pathlib import Path

from crux.llm import claude_json

from crux.models import (
    Annotation,
    AuditRow,
    ChangeMap,
    Claim,
    Config,
    CruxError,
    DagEdge,
    DagNode,
    DesignFinding,
    Hunk,
    HunkSignals,
    MapArrow,
    MapStep,
    Memory,
    MemoryRetraction,
    NodeAnnotation,
    RepoInfo,
    RunState,
    Verdict,
    fingerprint,  # canonical D9 fingerprint, shared with cli.py (re-exported)
    jargon_hits,
)

log = logging.getLogger("crux.analyze")

__all__ = ["annotate", "build_prompt", "fingerprint", "lint_word_budgets",
           "signal_chips", "validate_annotation"]

# Resolved relative to the installed package (not the repo checkout), so it
# works for editable, pipx, and wheel installs alike; the template is declared
# as package data in pyproject.toml.
PROMPT_PATH = resources.files("crux") / "prompts" / "analyze.md"
# The card leads with at most this many high-level overview bullets.
_OVERVIEW_MAX = 4
# D33: the change map is a picture a person takes in at a glance. The prompt
# asks for 3-6 steps; anything past this hard cap is dropped (with its arrows)
# rather than shown — a map that needs scrolling has stopped being a map.
_MAP_STEPS_MAX = 8
# D23: at most this many breakdown parts per large change.
_BREAKDOWN_MAX = 6
# Cap the integration-test steps — minimal, but enough for a real walkthrough.
_INTEGRATION_TEST_MAX = 10
# ~80 patch lines shown per DAG node (D9 keeps prompts small on re-runs anyway).
PATCH_LINES_PER_NODE = 80
# An over-budget patch is shown head + tail, never head-only — the
# tail of a file is where test modules live, and head-only truncation let the
# model assert "no tests" about a tail it never read. The budget is split, not
# grown: by default the tail gets 1/4 of it, or 1/2 when the elided region
# smells like tests (that is exactly the region a "no tests" claim needs).
_TAIL_SHARE = 4
_TAIL_SHARE_TESTS = 2
# Cheap cross-language test markers, matched against the raw patch text that
# would be elided: Rust, Python, JS/TS, Java/Kotlin, Go.
_TEST_MARKER_RE = re.compile(
    r"#\[cfg\(test\)\]|\bmod tests\b|\bdef test_|\bclass Test"
    r"|\bdescribe\(|\bit\(|\btest\(|@Test\b|\bfunc Test")
# D14: how much of the target repo's CLAUDE.md is quoted into the prompt.
CLAUDE_MD_MAX_CHARS = 4000
# D31: at most this many new repo facts absorbed per review.
_MEMORIES_MAX = 3
# D36: and at most this many retired per review — the same ceiling, so a single
# confused pass can never clear the store.
_FORGET_MAX = 3
_DESIGN_KINDS = {"oo-design", "duplicate-helper", "convention"}
_GIT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Signal chips
# ---------------------------------------------------------------------------

def signal_chips(sig: HunkSignals) -> list[str]:
    chips: list[str] = []
    if sig.blast_radius:
        chips.append(f"blast: {sig.blast_radius} call sites")
    if sig.defines:
        chips.append("defines: " + ", ".join(sig.defines[:6]))
    if sig.uses:
        chips.append("uses: " + ", ".join(sig.uses[:6]))
    if sig.sensitive:
        chips.append("sensitive: " + ", ".join(sig.sensitive))
    if sig.churn:
        chips.append(f"churn: {sig.churn} recent commits")
    if sig.fix_frequency:
        chips.append(f"fix-history: {sig.fix_frequency} fix commits")
    if sig.co_change_miss:
        chips.append("co-change miss: " + ", ".join(sig.co_change_miss))
    chips.append("tests touch this" if sig.test_touched else "no test coverage found")
    return chips


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _commit_subjects(info: RepoInfo | None) -> list[str]:
    """Effective subjects base..head (D27): for a commit Crux amended, the
    Crux-written subject — not the terse human line above it — so full
    messages are read (NUL-separated), never just %s."""
    if info is None:
        return []
    from crux.commitmsg import effective_subject
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%B%x00", f"{info.base_sha}..{info.head_sha}"],
            cwd=info.root, capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    subjects = [effective_subject(m) for m in proc.stdout.split("\x00")]
    return [s for s in subjects if s]


def _pr_body(info: RepoInfo | None) -> str:
    """Best-effort PR description via gh (D2). Empty when there is no PR yet
    (annotate runs before PR creation in the pipeline) or gh is unavailable."""
    if info is None or not info.branch:
        return ""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", info.branch, "--json", "body", "--jq", ".body"],
            cwd=info.root, capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError:
        return _pr_body_rest(info)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _pr_body_rest(info: RepoInfo) -> str:
    """The same lookup on a host without gh (cloud sessions), through
    crux.post's REST fallback. Best-effort like the gh path: any failure —
    including no token configured — is just an empty description."""
    from crux import post
    from crux.models import PostError
    try:
        number = post.find_pr(info)
        if number is None:
            return ""
        out = post._run_gh(
            ["api", f"repos/{info.owner}/{info.repo}/pulls/{number}"],
            cwd=info.root, timeout=_GIT_TIMEOUT,
        )
        return str(json.loads(out or "{}").get("body") or "").strip()
    except (PostError, ValueError, OSError):
        return ""


def _pr_sources(subjects: list[str], intent: dict | None, pr_body: str = "") -> str:
    parts: list[str] = []
    if pr_body:
        parts.append("PR description:\n" + pr_body)
    if subjects:
        parts.append("Commit subjects (base..head, newest first):\n"
                     + "\n".join(f"- {s}" for s in subjects))
    else:
        parts.append("Commit subjects: (none available)")
    if intent:
        parts.append("Author-agent intent (.crux/intent.json):\n"
                     + json.dumps(intent, indent=1, default=str))
    else:
        parts.append("Author-agent intent: (no .crux/intent.json)")
    return "\n\n".join(parts)


def _unread_range(patch_lines: list[str], new_start: int,
                  lo: int, hi: int) -> tuple[int, int] | None:
    """New-file line numbers (first, last) covered by patch_lines[lo:hi].

    Header (@@), removed (-) and no-newline (\\) lines occupy no new-file
    line; returns None when the slice holds only those (pure deletions)."""
    line = new_start
    first = last = None
    for i, pl in enumerate(patch_lines):
        if pl.startswith(("@@", "-", "\\")):
            continue
        if lo <= i < hi:
            if first is None:
                first = line
            last = line
        line += 1
    if first is None:
        return None
    return first, last


def _unread_marker(hunk: Hunk, patch_lines: list[str], lo: int, hi: int) -> str:
    """One explicit line naming the region of *hunk* the model was NOT shown.

    The old bare "[... truncated N more lines]" let the model
    describe the cut region with the same confidence as the part it read.
    Naming the exact unread line range gives the prompt rules (analyze.md)
    something concrete to bind to."""
    span = _unread_range(patch_lines, hunk.new_start, lo, hi)
    n = hi - lo
    if span:
        where = f"lines {span[0]}-{span[1]} of {hunk.file}"
    else:
        where = f"{n} removed lines of {hunk.file}"
    return (f"[... NOT READ: {where} ({n} patch lines) truncated for space — "
            "their content is UNKNOWN to you]")


def _truncated_patch(hunk: Hunk, patch_lines: list[str],
                     budget: int) -> list[str]:
    """Head + tail of an over-budget patch, with an explicit unread marker.

    The tail slice exists because "are there tests" is a question cards
    answer and tests sit at the bottom of a file; when the region past the
    default head contains test markers, the tail's share of the budget grows
    (the total shown never exceeds *budget*)."""
    tail = max(budget // _TAIL_SHARE, 1)
    if _TEST_MARKER_RE.search("\n".join(patch_lines[budget - tail:])):
        tail = max(budget // _TAIL_SHARE_TESTS, tail)
    head = max(budget - tail, 0)
    return (patch_lines[:head]
            + [_unread_marker(hunk, patch_lines, head, len(patch_lines) - tail)]
            + patch_lines[len(patch_lines) - tail:])


def _node_block(node: DagNode, hunks_by_id: dict[str, Hunk],
                large_cap: int) -> str:
    node_hunks = [hunks_by_id[hid] for hid in node.hunk_ids if hid in hunks_by_id]
    changed = sum(len(h.added_lines) + len(h.removed_lines) for h in node_hunks)
    header = (f"### Node {node.number} · {node.title}  "
              f"[badge: {node.badge.value}] [changed lines: {changed}]")
    # D23: the model must break large changes into <=large_cap-line parts.
    if changed > large_cap:
        header += ' [LARGE — "breakdown" REQUIRED]'
    lines = [header]
    remaining = PATCH_LINES_PER_NODE
    for hid in node.hunk_ids:
        hunk = hunks_by_id.get(hid)
        if hunk is None:
            continue
        klass = getattr(hunk.klass, "value", str(hunk.klass))
        symbol = f", symbol: {hunk.enclosing_symbol}" if hunk.enclosing_symbol else ""
        lines.append(f"- hunk `{hunk.id}` (file: {hunk.file}{symbol}, class: {klass})")
        patch_lines = hunk.patch.splitlines()
        if remaining <= 0:
            lines.append("  " + _unread_marker(hunk, patch_lines, 0, len(patch_lines))
                         + " [node budget exhausted]")
            continue
        if len(patch_lines) <= remaining:
            shown, used = patch_lines, len(patch_lines)
        else:
            shown, used = _truncated_patch(hunk, patch_lines, remaining), remaining
        lines.append("```")
        lines.extend(shown)
        lines.append("```")
        remaining -= used
    return "\n".join(lines)


def _dag_section(nodes: list[DagNode], edges: list[DagEdge],
                 hunks_by_id: dict[str, Hunk], large_cap: int) -> str:
    parts = [_node_block(n, hunks_by_id, large_cap) for n in nodes]
    if edges:
        parts.append("Edges (destination changed because of source):\n" + "\n".join(
            f"- {e.src} -> {e.dst} (reason: {e.reason})" for e in edges))
    else:
        parts.append("Edges: (none)")
    return "\n\n".join(parts)


def _chips_section(nodes: list[DagNode], signals: dict[str, HunkSignals]) -> str:
    lines: list[str] = []
    for node in nodes:
        lines.append(f"Node {node.number}:")
        for hid in node.hunk_ids:
            sig = signals.get(hid)
            chips = signal_chips(sig) if sig else ["(no signals harvested)"]
            lines.append(f"- `{hid}`: " + " · ".join(f"`{c}`" for c in chips))
    return "\n".join(lines)


def _claude_md(info: RepoInfo | None) -> str:
    """Target repo's CLAUDE.md content for the D14 conventions block,
    truncated to ~CLAUDE_MD_MAX_CHARS. Empty when absent/unreadable."""
    if info is None:
        return ""
    try:
        text = (Path(info.root) / "CLAUDE.md").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    if len(text) > CLAUDE_MD_MAX_CHARS:
        text = text[:CLAUDE_MD_MAX_CHARS] + "\n[... CLAUDE.md truncated]"
    return text


def _standards_section(claude_md: str, cfg: Config) -> str:
    parts: list[str] = []
    if claude_md:
        parts.append("Target repo CLAUDE.md (its conventions bind this review):\n"
                     + claude_md)
    else:
        parts.append("Target repo CLAUDE.md: (none found)")
    if cfg.standards_rules:
        parts.append("crux.toml [standards] rules:\n"
                     + "\n".join(f"- {r}" for r in cfg.standards_rules))
    else:
        parts.append("crux.toml [standards] rules: (none configured)")
    parts.append(f"Report at most {cfg.standards_max} design findings — "
                 "the strongest only.")
    return "\n\n".join(parts)


def _split_reused(
    nodes: list[DagNode], hunks: list[Hunk], previous: RunState | None,
) -> tuple[dict[int, NodeAnnotation], list[int]]:
    """Current node number -> previous annotation for unchanged nodes,
    plus the list of node numbers that still need fresh annotations.

    D9: matching is by CONTENT (the canonical fingerprint, which embeds the
    file path and normalized patch), never by hunk id — ids embed new_start,
    so pure line-number drift would change every downstream id and defeat
    reuse if ids were compared across runs.
    """
    if previous is None or previous.annotation is None:
        return {}, [n.number for n in nodes]
    current_fp = {h.id: fingerprint(h) for h in hunks}
    previous_fp = previous.fingerprints or {}
    # Previous node identity keyed by the set of its hunks' fingerprints.
    prev_by_fps: dict[frozenset[str], DagNode] = {}
    for prev in previous.nodes:
        if prev.hunk_ids and all(hid in previous_fp for hid in prev.hunk_ids):
            prev_by_fps[frozenset(previous_fp[hid] for hid in prev.hunk_ids)] = prev
    reused: dict[int, NodeAnnotation] = {}
    changed: list[int] = []
    for node in nodes:
        prev_node = None
        if node.hunk_ids and all(hid in current_fp for hid in node.hunk_ids):
            key = frozenset(current_fp[hid] for hid in node.hunk_ids)
            prev_node = prev_by_fps.get(key)
        prev_ann = previous.annotation.nodes.get(prev_node.number) if prev_node else None
        if prev_ann is not None:
            reused[node.number] = prev_ann
        else:
            changed.append(node.number)
    return reused, changed


def _reuse_section(reused: dict[int, NodeAnnotation], changed: list[int]) -> str:
    changed_text = ", ".join(str(n) for n in changed) if changed else "(none)"
    if not reused:
        return ("No previous run (or everything changed). "
                f"Write fresh annotations for ALL node numbers: {changed_text}.")
    lines = [
        "The previous run's annotations are given below for nodes whose code is",
        "UNCHANGED since that run. REUSE them: copy each one into your `nodes`",
        "output under the same number, VERBATIM — do not rewrite, rephrase, or",
        "re-estimate them.",
        "",
    ]
    for number in sorted(reused):
        ann = reused[number]
        lines.append(f"Node {number} (UNCHANGED — reuse verbatim):")
        lines.append(json.dumps({
            "title": ann.title,
            "why": ann.why,
            "questions": ann.questions,
            "chips": ann.chips,
            "minutes": ann.minutes,
            "design_decision": ann.design_decision,
            "suggested_tier": ann.suggested_tier,
            "before": ann.before,
            "after": ann.after,
            "breakdown": ann.breakdown,
        }, indent=1))
    lines.append("")
    lines.append(f"Write NEW annotations only for these changed node numbers: {changed_text}")
    return "\n".join(lines)


def _memory_section(memories: list[Memory]) -> str:
    """D31: the repo's remembered facts, formatted for the prompt. Ids are
    shown so the model can avoid re-proposing a fact it can already see."""
    if not memories:
        return "(nothing remembered about this repo yet)"
    lines: list[str] = []
    for memory in memories:
        anchor = f" (see {memory.anchor})" if memory.anchor else ""
        lines.append(f"- [{memory.id}] {memory.text}{anchor}")
    return "\n".join(lines)


def _previous_review(previous: RunState | None) -> str:
    """The last card's summary + big picture, fed back so the model keeps a
    through-line and can call out what changed since it last reviewed."""
    ann = previous.annotation if previous else None
    if not ann or not (ann.summary or ann.overview):
        return "(no earlier review — this is the first pass on this PR)"
    lines: list[str] = []
    if ann.summary:
        lines.append(f"You summed the PR up as: {ann.summary}")
    if ann.overview:
        lines.append("Your big-picture points were:")
        lines.extend(f"- {b}" for b in ann.overview)
    return "\n".join(lines)


def build_prompt(
    nodes: list[DagNode],
    edges: list[DagEdge],
    hunks: list[Hunk],
    signals: dict[str, HunkSignals],
    intent: dict | None,
    reused: dict[int, NodeAnnotation],
    changed: list[int],
    info: RepoInfo | None = None,
    cfg: Config | None = None,
    previous: RunState | None = None,
    memories: list[Memory] | None = None,
) -> str:
    try:
        template = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise CruxError(f"prompt template missing: {PROMPT_PATH}") from exc
    hunks_by_id = {h.id: h for h in hunks}
    large_cap = (cfg or Config()).max_read_lines
    try:
        return template.format(
            pr_sources=_pr_sources(_commit_subjects(info), intent, _pr_body(info)),
            dag_section=_dag_section(nodes, edges, hunks_by_id, large_cap),
            chips_section=_chips_section(nodes, signals),
            memory_section=_memory_section(memories or []),
            previous_review=_previous_review(previous),
            standards_section=_standards_section(_claude_md(info), cfg or Config()),
            reuse_section=_reuse_section(reused, changed),
        )
    except (KeyError, IndexError, ValueError) as exc:
        # A literal brace in the template must be doubled ({{ }}) for .format.
        raise CruxError(f"prompt template {PROMPT_PATH} has a bad placeholder: {exc}") from exc


# ---------------------------------------------------------------------------
# Response validation / coercion
# ---------------------------------------------------------------------------

def _as_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return [value]
    return []


def _coerce_verdict(raw: object) -> Verdict:
    text = " ".join(str(raw or "").upper().replace("_", " ").replace("-", " ").split())
    for verdict in Verdict:
        if text == verdict.value:
            return verdict
    return Verdict.COULD_NOT_VERIFY


def _coerce_minutes(raw: object) -> int:
    try:
        minutes = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        minutes = 2
    return max(1, min(10, minutes))


def _coerce_line(raw: object, default: int = 1) -> int:
    try:
        line = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        line = default
    return max(1, line)


_TIER_VALUES = {"red", "yellow", "green"}


def _coerce_suggested_tier(raw: object) -> str:
    """LLM tier opinion: "" unless exactly red/yellow/green. tiers.assign
    treats it as promote-only (it can never lower an item below its floor)."""
    text = str(raw or "").strip().lower()
    return text if text in _TIER_VALUES else ""


# The card prints the `**Before:** … **Now:** …` labels itself, so a value that
# opens with its own label renders as "Before: Before, X did Y" — a real stutter
# seen on shipped cards. The prompt forbids the lead; this strips it when the
# model writes one anyway.
_LABEL_LEAD_RE = re.compile(
    r"^(?:before(?: this(?: pr| change)?)?|prior to this(?: change)?|previously|"
    r"formerly|originally|after(?: this(?: pr| change)?)?|afterwards?|now|"
    r"today|currently)\b[,:]?\s+",
    re.IGNORECASE,
)


def _strip_label_lead(text: str) -> str:
    """Drop a leading "Before,"/"Previously"/"Now" from a before/after sentence.

    Only one lead is stripped, and only when what remains is still a sentence
    (3+ words) — so a value that is nothing but the lead, or a genuine temporal
    clause too short to survive, is left exactly as the model wrote it.
    """
    text = text.strip()
    stripped = _LABEL_LEAD_RE.sub("", text, count=1).lstrip()
    if len(stripped.split()) < 3:
        return text
    return stripped


def _coerce_design(data: dict, cfg: Config) -> list[DesignFinding]:
    """D14 validator: coerce raw design findings, DROP citation-less ones,
    clamp to cfg.standards_max. Findings never touch tiers — they only ever
    live on Annotation.design."""
    findings: list[DesignFinding] = []
    for raw in _as_list(data.get("design")):
        if not isinstance(raw, dict):
            continue
        ref = str(raw.get("convention_ref") or "").strip()
        text = str(raw.get("text") or "").strip()
        file = str(raw.get("file") or "").strip()
        # Evidence contract: no convention citation or no offending lines => drop.
        if not ref or not text or not file:
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in _DESIGN_KINDS:
            kind = "convention"
        line_start = _coerce_line(raw.get("line_start"))
        line_end = max(_coerce_line(raw.get("line_end"), line_start), line_start)
        source = str(raw.get("convention_source") or "").strip()
        if not source:
            # a `path:123`-shaped ref points at sibling code; anything else
            # reads as quoted rule text
            source = "sibling code" if ref.rsplit(":", 1)[-1].isdigit() else "CLAUDE.md"
        findings.append(DesignFinding(
            kind=kind, text=text, file=file,
            line_start=line_start, line_end=line_end,
            convention_source=source, convention_ref=ref,
        ))
    return findings[:max(cfg.standards_max, 0)]


def _coerce_change_map(data: dict) -> ChangeMap | None:
    """D33: coerce the model's user-level change map.

    A step needs a unique id and a label to survive; an arrow needs both ends
    to name surviving steps and must not loop back on itself. Steps past
    _MAP_STEPS_MAX are dropped in the order given (the prompt asks for the
    flow in order, so what goes is the tail), along with any arrow left
    dangling. Returns None when nothing usable came back.
    """
    raw = data.get("change_map")
    if not isinstance(raw, dict):
        return None

    steps: list[MapStep] = []
    seen: set[str] = set()
    for entry in _as_list(raw.get("steps")):
        if not isinstance(entry, dict):
            continue
        step_id = str(entry.get("id") or "").strip()
        label = " ".join(str(entry.get("label") or "").split())
        if not step_id or not label or step_id in seen:
            continue
        seen.add(step_id)
        steps.append(MapStep(id=step_id, label=label))
    if len(steps) > _MAP_STEPS_MAX:
        log.info("change map (D33): kept the first %d of %d steps",
                 _MAP_STEPS_MAX, len(steps))
        steps = steps[:_MAP_STEPS_MAX]

    kept = {s.id for s in steps}
    arrows: list[MapArrow] = []
    drawn: set[tuple[str, str]] = set()
    for entry in _as_list(raw.get("arrows")):
        if not isinstance(entry, dict):
            continue
        src = str(entry.get("from") or entry.get("src") or "").strip()
        dst = str(entry.get("to") or entry.get("dst") or "").strip()
        if src not in kept or dst not in kept or src == dst:
            continue
        if (src, dst) in drawn:
            continue
        drawn.add((src, dst))
        arrows.append(MapArrow(
            src=src, dst=dst,
            label=" ".join(str(entry.get("label") or "").split()),
        ))

    if not steps and not arrows:
        return None
    return ChangeMap(steps=steps, arrows=arrows)


def _coerce_memories(data: dict) -> list[Memory]:
    """D31: coerce the model's proposed repo facts. Text is required; ids and
    dates are filled by memory.absorb at store time. Anchor existence is
    checked there too — absorb owns the store, this only shapes the reply."""
    from crux.memory import memory_id
    memories: list[Memory] = []
    for raw in _as_list(data.get("memories")):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        memories.append(Memory(id=memory_id(text), text=text,
                               anchor=str(raw.get("anchor") or "").strip(),
                               source="review"))
    return memories[:_MEMORIES_MAX]


def _coerce_forget(data: dict) -> list[MemoryRetraction]:
    """D36: coerce the remembered facts the review says this PR made false.
    Capped like the additions, and for the same reason — one review may nudge
    the store, never rewrite it. Whether an id is really in the store, and
    whether it may go at all, is memory.absorb's call."""
    retractions: list[MemoryRetraction] = []
    for raw in _as_list(data.get("forget")):
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("id") or "").strip()
        if not mid:
            continue
        retractions.append(MemoryRetraction(
            id=mid, why=str(raw.get("why") or "").strip()))
    return retractions[:_FORGET_MAX]


def _coerce(data: dict, nodes: list[DagNode], cfg: Config) -> Annotation:
    known = {n.number: n for n in nodes}

    claims: list[Claim] = []
    for i, raw in enumerate(_as_list(data.get("claims")), 1):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        claims.append(Claim(
            id=str(raw.get("id") or f"C{i}"),
            text=text,
            hunk_ids=[str(h) for h in _as_list(raw.get("hunk_ids"))],
            kind=str(raw.get("kind") or "behavior"),
            uncertain=bool(raw.get("uncertain", False)),
        ))

    node_anns: dict[int, NodeAnnotation] = {}
    raw_nodes = data.get("nodes")
    if isinstance(raw_nodes, dict):
        for key, raw in raw_nodes.items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            if number not in known or not isinstance(raw, dict):
                continue  # drop unknown node numbers
            node_anns[number] = NodeAnnotation(
                number=number,
                title=str(raw.get("title") or known[number].title),
                why=str(raw.get("why") or ""),
                questions=[str(q) for q in _as_list(raw.get("questions"))],
                chips=[str(c) for c in _as_list(raw.get("chips"))],
                minutes=_coerce_minutes(raw.get("minutes", 2)),
                design_decision=bool(raw.get("design_decision", False)),
                suggested_tier=_coerce_suggested_tier(
                    raw.get("suggested_tier", raw.get("tier"))),
                before=_strip_label_lead(str(raw.get("before") or "")),
                after=_strip_label_lead(str(raw.get("after") or raw.get("now") or "")),
                breakdown=[str(b).strip()
                           for b in _as_list(raw.get("breakdown"))
                           if str(b).strip()][:_BREAKDOWN_MAX],
            )

    audit: list[AuditRow] = []
    for raw in _as_list(data.get("audit")):
        if not isinstance(raw, dict):
            continue
        claim = str(raw.get("claim") or "").strip()
        if not claim:
            continue
        audit.append(AuditRow(
            claim=claim,
            verdict=_coerce_verdict(raw.get("verdict")),
            evidence=str(raw.get("evidence") or ""),
        ))

    return Annotation(
        summary=str(data.get("summary") or "").strip(),
        pr_title=str(data.get("pr_title") or "").strip().rstrip("."),
        overview=[str(b).strip() for b in _as_list(data.get("overview"))
                  if str(b).strip()][:_OVERVIEW_MAX],
        change_map=_coerce_change_map(data),
        integration_test=[str(s).strip()
                          for s in _as_list(data.get("integration_test"))
                          if str(s).strip()][:_INTEGRATION_TEST_MAX],
        claims=claims,
        nodes=node_anns,
        audit=audit,
        design=_coerce_design(data, cfg),
        memories=_coerce_memories(data),
        forget_memories=_coerce_forget(data),
    )


# ---------------------------------------------------------------------------
# Validation gates (D29)
# ---------------------------------------------------------------------------

# `path:line(-line)` references in overview bullets (the shape render.py links).
_LOC_REF_RE = re.compile(r"`([\w./\-]+):\d+(?:-\d+)?`")


def _test_only_numbers(nodes: list[DagNode], hunks: list[Hunk],
                       cfg: Config) -> set[int]:
    from crux.tiers import is_test_file
    by_id = {h.id: h for h in hunks}
    numbers: set[int] = set()
    for node in nodes:
        files = [by_id[hid].file for hid in node.hunk_ids if hid in by_id]
        if files and all(is_test_file(f, cfg) for f in files):
            numbers.add(node.number)
    return numbers


# A token counts as a word only if it carries a letter or digit — separators
# like "—" and "·" are punctuation, not prose. Shared by the D33 gate below and
# the D32 word budgets further down.
_WORDLIKE_RE = re.compile(r"[^\W_]")

# D33: what a change-map step looks like when the model gave up on the flow and
# labelled the code instead — a filename ("post.py additions"), a call
# ("build()"), an identifier ("_pr_body", "WriteBuffer", "prTitle"). Single
# capitalized words ("Crux", "Slack") and acronyms ("PR") are not code.
_CODE_TOKEN_RE = re.compile(
    r"\S+\.[A-Za-z]{1,4}\b"      # file.ext
    r"|\w+\(\)"                  # call()
    r"|\w*_\w+"                  # snake_case, _private
    r"|\b\w+[a-z]\w*[A-Z]\w*"    # camelCase, CamelCase (needs a hump)
)


def _is_code_label(label: str) -> bool:
    """True when a map step names code rather than something that happens.

    Two conditions, both required: the label carries a code-shaped token, AND
    what is left once those tokens are removed no longer reads as a phrase. So
    "Read crux.toml first" survives (the file is incidental to a real step) and
    so does a plain short step like "Merged" (no code in it at all), while
    "post.py additions" and "_pr_body" — labels that never said what happens —
    do not.
    """
    if not _CODE_TOKEN_RE.search(label):
        return False
    plain = [w for w in _CODE_TOKEN_RE.sub(" ", label).split()
             if _WORDLIKE_RE.search(w)]
    return len(plain) < 2


def validate_annotation(annotation: Annotation, nodes: list[DagNode],
                        hunks: list[Hunk], cfg: Config) -> list[str]:
    """D29: deterministic validation gates run at the END of the LLM step.

    Each gate checks the model's output against a card directive and
    mechanically REPAIRS violations in place — a basic refactor, never a
    retry (retries are for jargon, D15). Returns the list of repairs made
    (already logged), so tests and callers can see which gates fired.

    Gates today (new gates land here):
    - D25 — tests never outrank the code they test: a `red` suggestion on a
      test-only change drops to yellow, and an overview bullet whose code
      pointers ALL land in test files is removed from the big picture.
    - D33 — the change map is user-level or absent: a map with a step labelled
      after a file, symbol, or call is dropped whole. Cutting that one step
      would break the flow the rest of the map draws, and a picture of the
      codebase's internals is the thing D33 replaced.
    """
    from crux.tiers import is_test_file
    repairs: list[str] = []

    cmap = annotation.change_map
    if cmap is not None:
        codey = [s.label for s in cmap.steps if _is_code_label(s.label)]
        if codey:
            annotation.change_map = None
            repairs.append(
                "dropped a change map written at code level (D33): "
                + ", ".join(repr(label) for label in codey[:3]))

    test_only = _test_only_numbers(nodes, hunks, cfg)
    for number in sorted(test_only):
        ann = annotation.nodes.get(number)
        if ann and ann.suggested_tier == "red":
            ann.suggested_tier = "yellow"
            repairs.append(
                f"demoted test-only change {number} out of must-read (D25)")

    kept: list[str] = []
    for bullet in annotation.overview:
        paths = _LOC_REF_RE.findall(bullet)
        if paths and all(is_test_file(p, cfg) for p in paths):
            repairs.append(
                "dropped a big-picture bullet that only pointed at tests "
                f"(D25): {bullet[:80]!r}")
            continue
        kept.append(bullet)
    annotation.overview[:] = kept

    for repair in repairs:
        log.info("validation gate (D29): %s", repair)
    return repairs


# D32 word budgets, mirroring the table in prompts/analyze.md. Long prose is
# what makes a card read long, and no mechanical edit can shorten a sentence
# without mangling it — so this is a LINT (logged), not a D29 repair. It says
# when the model has stopped honoring the budgets and the prompt needs work.
_WORD_BUDGETS = {"summary": 25, "overview bullet": 25, "title": 8, "why": 20,
                 "before": 15, "after": 15, "breakdown part": 20,
                 "map step": 5, "map arrow": 4}
# Backticked `path/file.py:line` pointers are navigation, not prose: they end
# most bullets by contract, so they don't count against the budget. Neither do
# the separators around them ("—", "·").
_POINTER_RE = re.compile(r"`[^`]*`")


def _prose_words(text: str) -> int:
    return sum(1 for token in _POINTER_RE.sub(" ", text).split()
               if _WORDLIKE_RE.search(token))


def lint_word_budgets(annotation: Annotation) -> list[str]:
    """Reviewer-facing fields whose prose exceeds its word budget (D32)."""
    over: list[str] = []

    def check(field: str, text: str) -> None:
        words = _prose_words(text)
        if words > _WORD_BUDGETS[field]:
            over.append(f"{field} {words} words (budget {_WORD_BUDGETS[field]})")

    check("summary", annotation.summary)
    for bullet in annotation.overview:
        check("overview bullet", bullet)
    if annotation.change_map is not None:
        for step in annotation.change_map.steps:
            check("map step", step.label)
        for arrow in annotation.change_map.arrows:
            check("map arrow", arrow.label)
    for ann in annotation.nodes.values():
        check("title", ann.title)
        check("why", ann.why)
        check("before", ann.before)
        check("after", ann.after)
        for part in ann.breakdown:
            check("breakdown part", part)
    return over


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _jargon_audit(annotation: Annotation) -> list[str]:
    """Collect banned-jargon hits (D15) from every reviewer-facing text field."""
    texts: list[str] = [annotation.summary, *annotation.overview,
                        *annotation.integration_test]
    if annotation.change_map is not None:
        texts.extend(s.label for s in annotation.change_map.steps)
        texts.extend(a.label for a in annotation.change_map.arrows)
    for claim in annotation.claims:
        texts.append(claim.text)
    for ann in annotation.nodes.values():
        texts.extend([ann.title, ann.why, ann.before, ann.after,
                      *ann.questions, *ann.chips, *ann.breakdown])
    for row in annotation.audit:
        texts.extend([row.claim, row.evidence])
    for finding in annotation.design:
        texts.append(finding.text)
    hits: list[str] = []
    for text in texts:
        hits.extend(jargon_hits(text))
    return hits


def annotate(
    nodes: list[DagNode],
    edges: list[DagEdge],
    hunks: list[Hunk],
    signals: dict[str, HunkSignals],
    intent: dict | None,
    previous: RunState | None,
    cfg: Config,
    info: RepoInfo | None = None,
    memories: list[Memory] | None = None,
) -> Annotation:
    """One LLM pass over the DAG -> validated Annotation.

    *info* is optional (not in the original contract table): when given, commit
    subjects base..head are added to the PR summary sources. *memories* is the
    repo's remembered facts (D31), read by the caller so --dry-run and a
    disabled [memory] section stay in the caller's control.
    """
    reused, changed = _split_reused(nodes, hunks, previous)
    prompt = build_prompt(nodes, edges, hunks, signals, intent, reused, changed,
                          info, cfg, previous, memories)
    data = claude_json(prompt, cfg)
    annotation = _coerce(data, nodes, cfg)

    # D15: one retry when the model used tool jargon in reviewer-facing text;
    # if it persists, keep the text (never block posting) but log loudly.
    hits = _jargon_audit(annotation)
    if hits:
        retry = (
            prompt
            + "\n\nREWRITE REQUIRED: your previous answer used banned jargon ("
            + ", ".join(sorted(set(hits)))
            + "). Follow the Plain-English contract: rewrite ALL reviewer-facing"
            " text without those terms and return the complete JSON object again."
        )
        data = claude_json(retry, cfg)
        annotation = _coerce(data, nodes, cfg)
        remaining = _jargon_audit(annotation)
        if remaining:
            log.warning("plain-english lint (D15): jargon kept after retry: %s",
                        sorted(set(remaining)))

    # D9: unchanged nodes carry the previous annotation verbatim regardless of
    # what the model returned, and are marked reused on the DagNode.
    by_number = {n.number: n for n in nodes}
    for number, prev in reused.items():
        annotation.nodes[number] = NodeAnnotation(
            number=number,
            title=prev.title,
            why=prev.why,
            questions=list(prev.questions),
            chips=list(prev.chips),
            minutes=_coerce_minutes(prev.minutes),
            design_decision=prev.design_decision,
            suggested_tier=_coerce_suggested_tier(prev.suggested_tier),
            before=prev.before,
            after=prev.after,
            breakdown=list(prev.breakdown),
            omitted_by_model=prev.omitted_by_model,
        )
        by_number[number].reused = True

    # Fill defaults for any node the model dropped, so downstream stages can
    # rely on every node number having an annotation. D32: the fill is marked
    # omitted_by_model — the prompt says leaving a change out marks it routine,
    # so tiers must not send the reviewer to read it as a must-read with a
    # machine-made title ("post.py additions") and nothing to say about it.
    for node in nodes:
        if node.number not in annotation.nodes:
            annotation.nodes[node.number] = NodeAnnotation(
                number=node.number, title=node.title, why="",
                omitted_by_model=True)

    # D29: validation gates, last — after the retry and the reuse merge, so
    # reused annotations from an older cache are held to today's directives too.
    validate_annotation(annotation, nodes, hunks, cfg)

    over_budget = lint_word_budgets(annotation)
    if over_budget:
        log.info("word budgets (D32): %d field(s) over budget: %s",
                 len(over_budget), "; ".join(over_budget[:5]))

    return annotation
