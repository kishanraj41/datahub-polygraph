"""Observed lineage: AutoLineage capture -> dataset-level graph.

AutoLineage records lineage at *operation* granularity: every pandas transform
and every sklearn call produces a ``TransformationRecord`` linking dataframe
versions. DataHub declares lineage at *dataset* granularity. This module bridges
the two.

The bridge is the notion of an **anchor** -- a node in the operation graph that
corresponds to something a catalog would actually name:

* a file read      (``read_csv`` etc., node carries ``filepath``)
* a file written   (``to_csv`` etc., record carries ``metadata.filepath``)
* a fitted model   (any record with ``category == "train"``)

Two anchors are connected by an observed edge when a path exists between them in
the operation graph that passes through no other anchor. The ordered list of
operations along that path is retained on the edge -- that is what lets the
incident report name the operation responsible, rather than just the edge.

Output is a stable, diffable ``observed_graph.json``. Node keys are logical
(``file:demo/data/raw_claims.csv``, ``model:LogisticRegression``), never
absolute paths or run-specific UUIDs, so the graph is comparable across runs and
across machines.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .fsutil import write_json_lf

SCHEMA_VERSION = "1.0"

# Records in these categories mark a trained model.
_MODEL_CATEGORIES = {"train"}
# Records in this category are file I/O.
_IO_CATEGORY = "io"


@dataclass(frozen=True)
class Anchor:
    """A node in the operation graph that a catalog would name."""

    key: str  # logical, stable across runs, e.g. "file:demo/data/raw_claims.csv"
    kind: str  # "file" | "model"
    node_id: str | None  # id in the operation graph, when one exists
    event_ts: str = ""  # capture timestamp of the event that created this anchor
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ObservedEdge:
    upstream: str
    downstream: str
    operations: list[str]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "upstream": self.upstream,
            "downstream": self.downstream,
            "operations": self.operations,
            "evidence": self.evidence,
        }


def _relativise(path: str, root: Path) -> str:
    """Absolute capture path -> repo-relative logical path (POSIX separators)."""
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        # Outside the repo: fall back to the basename so the key stays stable
        # across machines rather than embedding someone's home directory.
        return p.name


def _iter_records(trace: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return trace.get("edges", [])


def find_anchors(trace: dict[str, Any], root: Path) -> dict[str, Anchor]:
    """Locate every anchor in the trace, keyed by operation-graph node id.

    A written file has no node of its own (AutoLineage emits the write record
    with an empty ``child_id``), so writes are anchored on the node that was
    written *from*. When that node is already a read anchor -- a dataframe read
    and immediately written back -- the write wins, because that is the asset
    downstream consumers see.
    """
    anchors: dict[str, Anchor] = {}

    # Capture timestamp of the record that produced each node, used below to
    # reject temporally impossible edges.
    produced_at: dict[str, str] = {}
    for rec in _iter_records(trace):
        child = rec.get("child_id")
        if child and child not in produced_at:
            produced_at[child] = str(rec.get("timestamp", ""))

    # 1. File reads: the node itself carries the filepath.
    for node_id, node in trace.get("nodes", {}).items():
        fp = node.get("filepath")
        if fp:
            rel = _relativise(fp, root)
            anchors[node_id] = Anchor(
                key=f"file:{rel}",
                kind="file",
                node_id=node_id,
                event_ts=produced_at.get(node_id, str(node.get("created_at", ""))),
                detail={
                    "operation": node.get("source"),
                    "shape": node.get("shape"),
                    "columns": node.get("columns"),
                    "content_hash": node.get("content_hash"),
                },
            )

    # 2. Trained models: the record's child node is the model.
    for rec in _iter_records(trace):
        if rec.get("category") in _MODEL_CATEGORIES:
            child = rec.get("child_id")
            if not child:
                continue
            estimator = str(rec.get("operation", "model")).split(".")[0]
            anchors[child] = Anchor(
                key=f"model:{estimator}",
                kind="model",
                node_id=child,
                event_ts=str(rec.get("timestamp", "")),
                detail={"operation": rec.get("operation"), "input_shape": rec.get("input_shape")},
            )

    # 3. File writes: anchor on the parent node.
    for rec in _iter_records(trace):
        if rec.get("category") != _IO_CATEGORY:
            continue
        op = str(rec.get("operation", ""))
        if not op.startswith("to_"):
            continue
        fp = (rec.get("metadata") or {}).get("filepath")
        parents = rec.get("parent_ids") or []
        if not fp or not parents:
            continue
        rel = _relativise(fp, root)
        anchors[parents[0]] = Anchor(
            key=f"file:{rel}",
            kind="file",
            node_id=parents[0],
            event_ts=str(rec.get("timestamp", "")),
            detail={"operation": op, "rows": rec.get("rows_before")},
        )

    return anchors


def _build_adjacency(
    trace: dict[str, Any],
) -> tuple[dict[str, list[tuple[str, str, str]]], dict[str, list[tuple[str, str]]]]:
    """Adjacency and in-place operations over the operation graph.

    AutoLineage reuses a node id when a transform does not change the dataframe
    identity, so ``parent_id == child_id`` is common -- ``filter``, ``drop`` and
    ``select`` all land that way. Those records carry no reachability
    information but they carry the operations that matter most: the planted bug
    in the demo *is* one of them. So they are collected separately as in-place
    operations on the node rather than discarded.

    Returns ``(adj, inplace)`` where ``adj[parent]`` is a list of
    ``(child, operation, timestamp)`` and ``inplace[node]`` is a list of
    ``(operation, timestamp)``.
    """
    adj: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    inplace: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for rec in _iter_records(trace):
        child = rec.get("child_id")
        if not child:
            continue
        op = str(rec.get("operation", "?"))
        ts = str(rec.get("timestamp", ""))
        for parent in rec.get("parent_ids") or []:
            if parent == child:
                inplace[child].append((op, ts))
            else:
                adj[parent].append((child, op, ts))

    return adj, inplace


def _anchor_to_anchor_paths(
    start: str,
    adj: dict[str, list[tuple[str, str, str]]],
    inplace: dict[str, list[tuple[str, str]]],
    anchor_ids: set[str],
) -> dict[str, list[str]]:
    """Every anchor reachable from ``start`` without crossing another anchor.

    Returns ``{anchor_node_id: [operations, in execution order]}``.

    The operation list is the *union* of every operation on every anchor-free
    path between the two anchors, ordered by record timestamp -- not the
    shortest path. Shortest-path is actively misleading here: AutoLineage links
    ``LogisticRegression.fit`` directly back to an early hub node, so the
    shortest route from a source file to the model skips the filter, the merge
    and the split. Those operations really did run between the two assets, and
    the incident report needs to name them.
    """
    found: dict[str, set[tuple[str, str]]] = defaultdict(set)
    # (node, ops-accumulated-so-far). Revisits are allowed via distinct paths,
    # bounded by `seen_states` so diamonds cannot blow up.
    queue: deque[tuple[str, tuple[tuple[str, str], ...]]] = deque()
    start_ops = tuple(inplace.get(start, []))
    queue.append((start, start_ops))
    seen_states: set[tuple[str, tuple[tuple[str, str], ...]]] = {(start, start_ops)}

    while queue:
        node, ops = queue.popleft()
        for child, op, ts in adj.get(node, []):
            next_ops = ops + ((op, ts),) + tuple(inplace.get(child, []))
            if child in anchor_ids:
                found[child].update(next_ops)
                continue
            state = (child, next_ops)
            if state in seen_states:
                continue
            seen_states.add(state)
            queue.append(state)

    # Order by capture timestamp, de-duplicating repeated operation names while
    # keeping their first occurrence.
    ordered: dict[str, list[str]] = {}
    for anchor_id, op_set in found.items():
        seen_names: set[str] = set()
        names: list[str] = []
        for op, _ts in sorted(op_set, key=lambda pair: (pair[1], pair[0])):
            if op not in seen_names:
                seen_names.add(op)
                names.append(op)
        ordered[anchor_id] = names
    return ordered


def build_observed_graph(
    trace: dict[str, Any],
    root: Path,
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reduce an AutoLineage trace to a dataset-level observed graph."""
    anchors = find_anchors(trace, root)
    adj, inplace = _build_adjacency(trace)
    anchor_ids = set(anchors)

    edges: dict[tuple[str, str], ObservedEdge] = {}
    for node_id, anchor in anchors.items():
        for target_id, ops in _anchor_to_anchor_paths(node_id, adj, inplace, anchor_ids).items():
            target = anchors[target_id]
            up, down = anchor.key, target.key
            if up == down:
                continue
            # Temporal guard. A write anchor sits on the node it was written
            # *from*, so that node can also feed assets produced earlier in the
            # run -- which would emit a backwards edge (predictions -> model).
            # An asset cannot be upstream of something that already existed.
            if anchor.event_ts and target.event_ts and target.event_ts < anchor.event_ts:
                continue
            existing = edges.get((up, down))
            if existing is None or len(ops) > len(existing.operations):
                edges[(up, down)] = ObservedEdge(
                    upstream=up,
                    downstream=down,
                    operations=ops,
                    evidence={
                        "upstream_kind": anchor.kind,
                        "downstream_kind": anchors[target_id].kind,
                        "hop_count": len(ops),
                    },
                )

    nodes_out = {}
    for anchor in anchors.values():
        # Several operation-graph nodes can collapse to one logical asset; keep
        # the richest detail rather than whichever happened to come last.
        prev = nodes_out.get(anchor.key)
        if prev is None or len(anchor.detail) > len(prev.get("detail", {})):
            nodes_out[anchor.key] = {"key": anchor.key, "kind": anchor.kind, "detail": anchor.detail}

    return {
        "schema_version": SCHEMA_VERSION,
        "run": run_metadata or {},
        "nodes": [nodes_out[k] for k in sorted(nodes_out)],
        "edges": [
            edges[k].to_dict() for k in sorted(edges, key=lambda kv: (kv[0], kv[1]))
        ],
    }


def export(trace_path: Path, out_path: Path, root: Path, run_metadata=None) -> dict[str, Any]:
    trace = json.loads(Path(trace_path).read_text(encoding="utf-8"))
    graph = build_observed_graph(trace, root, run_metadata)
    write_json_lf(out_path, graph)
    return graph
