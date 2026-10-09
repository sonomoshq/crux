# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Triviality gate (D6): decide, from deterministic signals only and before any
LLM spend, whether this PR is worth a Crux card at all.

This module is also the single place where sensitivity tagging happens:
`tag_sensitivity` fills `HunkSignals.sensitive` for every hunk. cli calls it
during harvest (before scores are computed) and `decide` re-runs it
idempotently, so downstream tiers (`crux/tiers.py` floors) can rely on the
tags being present.

Skip (post nothing) only when ALL hold:
  behavioral changed lines < cfg.gate_min_behavioral_lines
  AND no sensitive path/keyword hits
  AND no dependency-file changes
  AND max blast_radius < cfg.gate_max_blast
"""
from __future__ import annotations

from crux.models import Config, GateDecision, Hunk, HunkClass, HunkSignals

# CI/workflow files are a deterministic RED floor regardless of config.
_WORKFLOW_PREFIX = ".github/workflows"


def _changed_lines(hunk: Hunk) -> int:
    return len(hunk.added_lines) + len(hunk.removed_lines)


def _sensitive_rules(hunk: Hunk, cfg: Config) -> list[str]:
    """Matched rule names for one hunk, in config order, paths before keywords.

    Paths: literal substring match on the file path (case-sensitive; repo paths
    are literal). Keywords: case-insensitive substring match on ADDED lines only
    — removed code cannot introduce a sensitive change.
    """
    rules: list[str] = []
    for pat in cfg.sensitive_paths:
        if pat and pat in hunk.file:
            rules.append(f"path:{pat}")
    added_lower = "\n".join(hunk.added_lines).lower()
    for kw in cfg.sensitive_keywords:
        if kw and kw.lower() in added_lower:
            rules.append(f"keyword:{kw}")
    return rules


def _is_dependency_file(path: str, cfg: Config) -> bool:
    name = path.rsplit("/", 1)[-1]
    for entry in cfg.dependency_files:
        if "/" in entry:
            if path == entry or path.endswith("/" + entry):
                return True
        elif name == entry:
            return True
    return False


def tag_sensitivity(hunks: list[Hunk], signals: dict[str, HunkSignals], cfg: Config) -> None:
    """Fill HunkSignals.sensitive for every hunk (idempotent).

    cli runs this BEFORE the last harvest stage so the sensitive weight
    contributes to testprox.compute_scores; decide() re-runs it defensively
    so its own logic never depends on the caller's ordering.
    """
    for hunk in hunks:
        sig = signals.get(hunk.id)
        if sig is None:
            sig = HunkSignals(hunk_id=hunk.id)
            signals[hunk.id] = sig
        for rule in _sensitive_rules(hunk, cfg):
            if rule not in sig.sensitive:  # idempotent across repeated runs
                sig.sensitive.append(rule)


def decide(hunks: list[Hunk], signals: dict[str, HunkSignals], cfg: Config) -> GateDecision:
    # -- sensitivity tagging first (idempotent; usually already done by cli) --
    tag_sensitivity(hunks, signals, cfg)

    # -- deterministic stats -------------------------------------------------
    behavioral_lines = sum(
        _changed_lines(h) for h in hunks if h.klass is HunkClass.BEHAVIORAL
    )
    total_lines = sum(_changed_lines(h) for h in hunks)
    files = sorted({h.file for h in hunks})
    sensitive_hunks = [h for h in hunks if signals[h.id].sensitive]
    dependency_files = sorted(
        {h.file for h in hunks if _is_dependency_file(h.file, cfg)}
    )
    max_blast = max((signals[h.id].blast_radius for h in hunks), default=0)

    # Estimate of must-read (RED) lines using only the tier floors that are
    # computable at gate time: sensitive hit, behavioral + blast over
    # threshold, CI/workflow file.
    red_estimate = 0
    for h in hunks:
        sig = signals[h.id]
        is_red = (
            bool(sig.sensitive)
            or (h.klass is HunkClass.BEHAVIORAL and sig.blast_radius >= cfg.gate_max_blast)
            or h.file.startswith(_WORKFLOW_PREFIX)
        )
        if is_red:
            red_estimate += _changed_lines(h)

    # -- decision ------------------------------------------------------------
    triggers: list[str] = []
    if behavioral_lines >= cfg.gate_min_behavioral_lines:
        triggers.append(
            f"behavioral lines {behavioral_lines} >= {cfg.gate_min_behavioral_lines}"
        )
    if sensitive_hunks:
        sample = ", ".join(
            f"{h.file} ({signals[h.id].sensitive[0]})" for h in sensitive_hunks[:3]
        )
        triggers.append(f"sensitive hits in {len(sensitive_hunks)} hunk(s): {sample}")
    if dependency_files:
        triggers.append("dependency files changed: " + ", ".join(dependency_files))
    if max_blast >= cfg.gate_max_blast:
        triggers.append(f"max blast radius {max_blast} >= {cfg.gate_max_blast}")

    skip = not triggers
    if skip:
        reasons = [
            f"behavioral lines {behavioral_lines} < {cfg.gate_min_behavioral_lines}",
            "no sensitive hits",
            "no dependency-file changes",
            f"max blast radius {max_blast} < {cfg.gate_max_blast}",
        ]
    else:
        reasons = triggers

    stats = {
        "behavioral_lines": behavioral_lines,
        "total_lines": total_lines,
        "red_estimate": red_estimate,
        "files": files,
        # extras for logging; not part of the required contract
        "max_blast": max_blast,
        "sensitive_hunks": len(sensitive_hunks),
        "dependency_files": dependency_files,
    }
    return GateDecision(skip=skip, reasons=reasons, stats=stats)
