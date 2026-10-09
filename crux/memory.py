# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Per-repo long-term memory store (D31).

Layout: ``$XDG_CONFIG_HOME/crux/memory/<owner>__<repo>.json`` (default
``~/.config/crux/memory/…``) — next to the global config, NOT under
``~/.cache``: rebases and cache clears drop RunState, and what Crux has
learned about a repo must survive both. load() treats a missing, unreadable,
or corrupt file as an empty store. save() is atomic (temp file in the same
directory, then os.replace), mirroring crux/cache.py.

A memory is one durable plain-English fact about the repo (a convention, an
architectural quirk, a recurring pitfall), optionally anchored to the file
that proves it. The review pass reads the store into its prompt and may
propose new facts AND ask for stored ones to be retired (D36); ``absorb`` is
the single write path for both — it dedupes, drops proposals anchored to
files that do not exist, prunes stored facts whose anchor has since vanished,
retires the facts this PR contradicted, and enforces the cap. Facts a human
added via ``crux memory add`` are evicted only after every review-written
fact is gone, and a review can never retire one.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import date
from pathlib import Path

from crux.models import Config, Memory, MemoryRetraction, RepoInfo

__all__ = ["load", "save", "absorb", "add", "forget", "clear", "memory_id",
           "anchor_exists", "store_path"]


def store_path(info: RepoInfo) -> Path:
    """Resolved like config.global_config_path: $XDG_CONFIG_HOME, else ~/.config."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "crux" / "memory" / f"{info.owner}__{info.repo}.json"


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def memory_id(text: str) -> str:
    """Content-derived id: the same fact (modulo case/whitespace) always maps
    to the same id, which is what makes dedupe across runs trivial."""
    return hashlib.sha1(_normalize(text).encode("utf-8", "replace")).hexdigest()[:8]


def load(info: RepoInfo) -> list[Memory]:
    try:
        raw = json.loads(store_path(info).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    memories: list[Memory] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        memories.append(Memory(
            id=str(entry.get("id") or memory_id(text)),
            text=text,
            anchor=str(entry.get("anchor") or ""),
            source=str(entry.get("source") or "review"),
            created=str(entry.get("created") or ""),
        ))
    return memories


def save(info: RepoInfo, memories: list[Memory]) -> None:
    path = store_path(info)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([asdict(m) for m in memories], indent=1)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# "path" or "path:123" — the trailing :line is optional and stripped before
# the existence check.
_ANCHOR_LINE_RE = re.compile(r":(\d+)(?:-\d+)?$")


def anchor_exists(anchor: str, repo_root: str) -> bool:
    """True when the anchor's file exists under repo_root. Empty anchors are
    repo-wide facts and always pass. Absolute paths and ``..`` escapes are
    rejected — an anchor must point inside the repo."""
    if not anchor:
        return True
    path = _ANCHOR_LINE_RE.sub("", anchor)
    if Path(path).is_absolute() or ".." in Path(path).parts:
        return False
    return (Path(repo_root) / path).exists()


def _evict_order(memory: Memory) -> tuple[int, str]:
    """Sort key for cap eviction: review-written facts go before human ones,
    oldest first within each group (empty created sorts oldest)."""
    return (0 if memory.source != "human" else 1, memory.created)


def absorb(info: RepoInfo, proposed: list[Memory], cfg: Config,
           retract: list[MemoryRetraction] | None = None) -> tuple[list[Memory], list[str]]:
    """Fold the review's proposed facts into the store and retire the ones it
    contradicted; the ONE write path for review-sourced memories. Returns
    (store after saving, human-readable notes of what changed) — notes are for
    the log, not the card.

    Retirement (D36) is what keeps the store from being append-only: without
    it a fact stays true forever in the prompt as long as its anchor file
    survives, and a stale fact misleads every later review. It is deliberately
    the weaker power of the two — a review may retire only what IT wrote, so a
    fact a human pinned with ``crux memory add`` still needs
    ``crux memory forget``.
    """
    notes: list[str] = []
    # Requested retirements, id -> reason; entries are popped as they are
    # matched, so whatever is left named a fact the store does not have.
    wanted = {r.id: r.why for r in (retract or []) if r.id}
    kept: list[Memory] = []
    for memory in load(info):
        if not anchor_exists(memory.anchor, info.root):
            notes.append(f"forgot [{memory.id}] — its anchor {memory.anchor} is gone")
            continue
        if memory.id not in wanted:
            kept.append(memory)
            continue
        why = wanted.pop(memory.id) or "the review gave no reason"
        if memory.source == "human":
            kept.append(memory)
            notes.append(f"kept [{memory.id}] — added by hand, so only `crux memory "
                         f"forget` drops it (the review argued: {why})")
            continue
        notes.append(f"forgot [{memory.id}] — this PR contradicts it: {why}")
    for unknown in sorted(wanted):
        notes.append(f"ignored a retirement — no memory with id {unknown}")
    known = {m.id for m in kept}

    today = date.today().isoformat()
    for memory in proposed:
        if memory.id in known:
            continue
        if not anchor_exists(memory.anchor, info.root):
            notes.append(f"dropped a proposed fact — anchor {memory.anchor} "
                         f"does not exist: {memory.text[:60]!r}")
            continue
        kept.append(Memory(id=memory.id, text=memory.text, anchor=memory.anchor,
                           source="review", created=today))
        known.add(memory.id)
        notes.append(f"remembered [{memory.id}] {memory.text}")

    cap = max(cfg.memory_max, 0)
    while len(kept) > cap:
        evicted = min(kept, key=_evict_order)
        kept.remove(evicted)
        notes.append(f"evicted [{evicted.id}] (store over {cap}): {evicted.text[:60]!r}")

    save(info, kept)
    return kept, notes


def add(info: RepoInfo, text: str, anchor: str = "") -> Memory | None:
    """`crux memory add`: store one human-written fact. Returns the new Memory,
    or None when the same fact (by normalized text) is already stored."""
    memories = load(info)
    new = Memory(id=memory_id(text), text=text.strip(), anchor=anchor.strip(),
                 source="human", created=date.today().isoformat())
    if any(m.id == new.id for m in memories):
        return None
    memories.append(new)
    save(info, memories)
    return new


def forget(info: RepoInfo, ids: list[str]) -> tuple[int, list[str]]:
    """`crux memory forget`: drop by id. Returns (#removed, ids not found)."""
    memories = load(info)
    wanted = set(ids)
    kept = [m for m in memories if m.id not in wanted]
    missing = sorted(wanted - {m.id for m in memories})
    if len(kept) != len(memories):
        save(info, kept)
    return len(memories) - len(kept), missing


def clear(info: RepoInfo) -> int:
    """`crux memory clear --yes`: forget everything. Returns how many."""
    memories = load(info)
    if memories:
        save(info, [])
    return len(memories)
