# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""DAG construction: cluster hunks into nodes, wire causal def/use edges,
break cycles, and number nodes in topological (causal) order.

Contract (DESIGN.md): build(hunks, signals, clusters) -> (nodes, edges).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from crux.models import Badge, DagEdge, DagNode, Hunk, HunkClass, HunkSignals


@dataclass
class _Proto:
    """A node under construction, identified by its index in the proto list."""
    hunk_ids: list[str]
    mechanical: bool
    defines: set[str] = field(default_factory=set)
    uses: set[str] = field(default_factory=set)
    score: float = 0.0
    first_index: int = 0  # position of the node's earliest hunk in the diff


def build(
    hunks: list[Hunk],
    signals: dict[str, HunkSignals],
    clusters: list[list[str]],
) -> tuple[list[DagNode], list[DagEdge]]:
    if not hunks:
        return [], []

    by_id = {h.id: h for h in hunks}
    pos = {h.id: i for i, h in enumerate(hunks)}

    protos = _make_protos(hunks, by_id, pos, clusters)
    for p in protos:
        for hid in p.hunk_ids:
            sig = signals.get(hid)
            if sig is not None:
                p.defines.update(sig.defines)
                p.uses.update(sig.uses)
                p.score = max(p.score, sig.score)
        p.first_index = min(pos[hid] for hid in p.hunk_ids)

    raw_edges = _make_edges(protos)
    edges = _break_cycles(protos, raw_edges)

    order = _topo_order(protos, edges)
    number = {idx: n + 1 for n, idx in enumerate(order)}

    has_incoming = {dst for _src, dst, _r in edges}
    nodes: list[DagNode] = []
    for idx in order:
        p = protos[idx]
        nodes.append(DagNode(
            number=number[idx],
            title=_title(p, by_id, pos),
            hunk_ids=list(p.hunk_ids),
            badge=_badge(p, by_id, is_root=idx not in has_incoming),
        ))

    out_edges = sorted(
        (DagEdge(src=number[s], dst=number[d], reason=r) for s, d, r in edges),
        key=lambda e: (e.src, e.dst),
    )
    return nodes, out_edges


def _make_protos(
    hunks: list[Hunk],
    by_id: dict[str, Hunk],
    pos: dict[str, int],
    clusters: list[list[str]],
) -> list[_Proto]:
    protos: list[_Proto] = []
    in_cluster: set[str] = set()
    for cluster in clusters:
        ids = [hid for hid in cluster if hid in by_id and hid not in in_cluster]
        if not ids:
            continue
        in_cluster.update(ids)
        protos.append(_Proto(hunk_ids=sorted(ids, key=pos.__getitem__), mechanical=True))
    # Remaining hunks group by (file, enclosing_symbol); insertion order = diff order.
    grouped: dict[tuple[str, str | None], list[str]] = {}
    for h in hunks:
        if h.id in in_cluster:
            continue
        grouped.setdefault((h.file, h.enclosing_symbol), []).append(h.id)
    for ids in grouped.values():
        protos.append(_Proto(hunk_ids=ids, mechanical=False))
    return protos


def _make_edges(protos: list[_Proto]) -> list[tuple[int, int, str]]:
    """src -> dst when dst uses a symbol src defines; self-edges dropped."""
    edges: list[tuple[int, int, str]] = []
    for s, src in enumerate(protos):
        if not src.defines:
            continue
        for d, dst in enumerate(protos):
            if s == d:
                continue
            shared = src.defines & dst.uses
            if shared:
                edges.append((s, d, sorted(shared)[0]))
    return edges


def _break_cycles(
    protos: list[_Proto], edges: list[tuple[int, int, str]]
) -> list[tuple[int, int, str]]:
    """Remove, per cycle found, the edge whose dst has the lowest max score.

    Score ties break by dst diff position then src diff position (deterministic).
    """
    edges = list(edges)
    n = len(protos)
    while True:
        adj: dict[int, set[int]] = {i: set() for i in range(n)}
        for s, d, _r in edges:
            adj[s].add(d)
        cycle = _find_cycle(n, adj)
        if cycle is None:
            return edges
        s, d = min(
            cycle,
            key=lambda e: (protos[e[1]].score, protos[e[1]].first_index, protos[e[0]].first_index),
        )
        edges = [e for e in edges if (e[0], e[1]) != (s, d)]


def _find_cycle(n: int, adj: dict[int, set[int]]) -> list[tuple[int, int]] | None:
    """Iterative DFS; returns one cycle as a list of (src, dst) edges, or None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * n
    for start in range(n):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        path = [start]
        stack = [(start, iter(sorted(adj[start])))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color[nxt] == GRAY:
                    cyc = path[path.index(nxt):] + [nxt]
                    return list(zip(cyc, cyc[1:]))
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, iter(sorted(adj[nxt]))))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                path.pop()
                color[node] = BLACK
    return None


def _topo_order(protos: list[_Proto], edges: list[tuple[int, int, str]]) -> list[int]:
    """Kahn's algorithm; ties by descending max score, then diff/file order."""
    n = len(protos)
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    indeg = [0] * n
    for s, d, _r in edges:
        if d not in adj[s]:
            adj[s].add(d)
            indeg[d] += 1
    ready = [i for i in range(n) if indeg[i] == 0]
    order: list[int] = []
    while ready:
        ready.sort(key=lambda i: (-protos[i].score, protos[i].first_index))
        u = ready.pop(0)
        order.append(u)
        for v in sorted(adj[u]):
            indeg[v] -= 1
            if indeg[v] == 0:
                ready.append(v)
    return order


def _badge(p: _Proto, by_id: dict[str, Hunk], is_root: bool) -> Badge:
    if p.mechanical:
        return Badge.MECHANICAL_CHANGES
    behavioral = any(by_id[hid].klass is HunkClass.BEHAVIORAL for hid in p.hunk_ids)
    if is_root and behavioral:
        return Badge.CODE_CHANGE
    return Badge.CODE_CHANGE_EFFECTS


def _title(p: _Proto, by_id: dict[str, Hunk], pos: dict[str, int]) -> str:
    """Enclosing symbol when unambiguous, else file basename + short action."""
    hs = [by_id[hid] for hid in p.hunk_ids]
    symbols = {h.enclosing_symbol for h in hs if h.enclosing_symbol}
    if len(symbols) == 1:
        return next(iter(symbols))
    primary = min(hs, key=lambda h: pos[h.id])
    base = primary.file.replace("\\", "/").rsplit("/", 1)[-1]
    if p.mechanical and len(hs) > 1:
        action = f"{len(hs)} similar edits"
    else:
        added = any(h.added_lines for h in hs)
        removed = any(h.removed_lines for h in hs)
        if added and not removed:
            action = "additions"
        elif removed and not added:
            action = "deletions"
        else:
            action = "edits"
    return f"{base} {action}"
