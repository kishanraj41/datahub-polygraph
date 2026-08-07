"""Edge-by-edge reconciliation of declared lineage against observed lineage.

Three verdicts, and the semantics matter because they end up as tags on real
assets in someone's catalog:

``VERIFIED``
    The catalog declares this edge and the runtime capture proves data actually
    flowed along it. Note the asymmetry: this is evidence *for* the edge in the
    run that was captured. It is not proof the edge is correct in every run.

``PHANTOM``
    The catalog declares this edge and the runtime capture shows nothing flowed
    along it. Usually a stale declaration left behind after a refactor. This is
    a claim about *the captured run only* -- a genuinely conditional edge (a
    branch not taken) will look phantom, which is why the report always names
    the run it is based on.

``UNDECLARED``
    The runtime capture proves data flowed and the catalog says nothing about
    it. A shadow input. Usually the most interesting finding, because nobody
    reviewing the catalog could have known.

Edges whose observed nodes have no URN mapping are reported separately as
``UNMAPPED`` rather than being forced into one of the three verdicts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .fsutil import write_json_lf, write_text_lf

VERIFIED = "VERIFIED"
PHANTOM = "PHANTOM"
UNDECLARED = "UNDECLARED"

TAG_FOR_VERDICT = {
    VERIFIED: "polygraph:verified",
    PHANTOM: "polygraph:phantom",
    UNDECLARED: "polygraph:undeclared-source",
}


@dataclass(frozen=True)
class DeclaredEdge:
    """An edge the catalog asserts."""

    upstream: str  # URN
    downstream: str  # URN
    via: str = ""  # how it was declared (e.g. "dataJob.inputDatasets")


@dataclass
class Verdict:
    upstream: str
    downstream: str
    verdict: str
    reason: str
    operations: list[str] = field(default_factory=list)
    declared_via: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _edge_key(up: str, down: str) -> tuple[str, str]:
    return (up, down)


def reconcile(
    declared: Iterable[DeclaredEdge],
    observed_graph: dict[str, Any],
    key_to_urn: dict[str, str],
    unmapped_keys: Iterable[str] = (),
    scope_downstream: set[str] | None = None,
) -> dict[str, Any]:
    """Compare declared edges against an observed graph, edge by edge.

    ``scope_downstream`` restricts reconciliation to edges *into* a given set of
    consumers -- in practice, the training jobs. Polygraph answers "what does
    this asset actually read?", which is a question about inputs; its tags say
    ``undeclared-source``, not ``undeclared-output``. Scoping keeps the report
    honest: an observed edge into something outside the scope is recorded as
    out-of-scope rather than being reported as an undeclared source.
    """
    declared_list = list(declared)
    declared_map = {_edge_key(d.upstream, d.downstream): d for d in declared_list}

    # Project observed edges into URN space. Observed edges whose endpoints are
    # unmapped are set aside, not guessed at.
    observed_map: dict[tuple[str, str], dict[str, Any]] = {}
    skipped_edges: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    for edge in observed_graph.get("edges", []):
        up_urn = key_to_urn.get(edge["upstream"])
        down_urn = key_to_urn.get(edge["downstream"])
        if not up_urn or not down_urn:
            skipped_edges.append(
                {
                    "upstream": edge["upstream"],
                    "downstream": edge["downstream"],
                    "reason": "no URN mapping for "
                    + ", ".join(
                        k
                        for k, u in ((edge["upstream"], up_urn), (edge["downstream"], down_urn))
                        if not u
                    ),
                }
            )
            continue
        if scope_downstream is not None and down_urn not in scope_downstream:
            out_of_scope.append(
                {"upstream": edge["upstream"], "downstream": edge["downstream"]}
            )
            continue
        key = _edge_key(up_urn, down_urn)
        # Two observed keys can map to one URN pair; keep the richer op list.
        prev = observed_map.get(key)
        if prev is None or len(edge.get("operations", [])) > len(prev.get("operations", [])):
            observed_map[key] = edge

    verdicts: list[Verdict] = []

    for key, decl in declared_map.items():
        obs = observed_map.get(key)
        if obs is not None:
            verdicts.append(
                Verdict(
                    upstream=key[0],
                    downstream=key[1],
                    verdict=VERIFIED,
                    reason=(
                        "runtime capture shows data flowing along this edge via "
                        f"{len(obs.get('operations', []))} recorded operation(s)"
                    ),
                    operations=list(obs.get("operations", [])),
                    declared_via=decl.via,
                )
            )
        else:
            verdicts.append(
                Verdict(
                    upstream=key[0],
                    downstream=key[1],
                    verdict=PHANTOM,
                    reason=(
                        "declared in the catalog, but the captured run recorded no data "
                        "flow along this edge"
                    ),
                    declared_via=decl.via,
                )
            )

    for key, obs in observed_map.items():
        if key in declared_map:
            continue
        verdicts.append(
            Verdict(
                upstream=key[0],
                downstream=key[1],
                verdict=UNDECLARED,
                reason=(
                    "runtime capture proves this edge exists, but the catalog does not "
                    "declare it"
                ),
                operations=list(obs.get("operations", [])),
            )
        )

    verdicts.sort(key=lambda v: (v.verdict, v.upstream, v.downstream))
    counts = {v: 0 for v in (VERIFIED, PHANTOM, UNDECLARED)}
    for v in verdicts:
        counts[v.verdict] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": observed_graph.get("run", {}),
        "summary": {
            **counts,
            "declared_total": len(declared_map),
            "observed_total": len(observed_map),
            "unmapped_nodes": sorted(unmapped_keys),
            "skipped_observed_edges": skipped_edges,
            "out_of_scope_observed_edges": out_of_scope,
        },
        "verdicts": [v.to_dict() for v in verdicts],
    }


def has_discrepancies(report: dict[str, Any]) -> bool:
    s = report["summary"]
    return bool(s[PHANTOM] or s[UNDECLARED] or s["unmapped_nodes"] or s["skipped_observed_edges"])


_FABRIC_TYPES = {"PROD", "DEV", "TEST", "QA", "UAT", "EI", "PRE", "STG", "NON_PROD", "CORP"}


def _split_top_level(inner: str) -> list[str]:
    """Split on commas that are not inside a nested ``urn:...(...)``."""
    parts, depth, current = [], 0, []
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _short(urn: str) -> str:
    """Trim a URN down to something a human can scan in a table.

    Handles the two shapes that matter here, which nest differently:

    * ``urn:li:dataset:(urn:li:dataPlatform:file,NAME,PROD)`` -> ``NAME``
    * ``urn:li:dataJob:(urn:li:dataFlow:(f,flow,PROD),JOB_ID)`` -> ``JOB_ID``

    Naive comma splitting gets the second one wrong -- it returns ``PROD)`` --
    because the flow URN contains its own commas.
    """
    start = urn.find("(")
    if start == -1 or not urn.endswith(")"):
        return urn.rsplit(":", 1)[-1]
    parts = _split_top_level(urn[start + 1 : -1])
    if not parts:
        return urn
    # A trailing fabric (PROD/DEV/...) means the name is the part before it.
    if len(parts) >= 2 and parts[-1] in _FABRIC_TYPES:
        return parts[-2]
    return parts[-1]


def to_markdown(report: dict[str, Any], title: str = "Polygraph reconciliation") -> str:
    s = report["summary"]
    run = report.get("run") or {}
    lines = [
        f"# {title}",
        "",
        f"Generated: `{report['generated_at']}`  ",
        f"Run: `{json.dumps(run, sort_keys=True)}`",
        "",
        "## Summary",
        "",
        "| Verdict | Count |",
        "| --- | ---: |",
        f"| VERIFIED | {s[VERIFIED]} |",
        f"| PHANTOM | {s[PHANTOM]} |",
        f"| UNDECLARED | {s[UNDECLARED]} |",
        "",
        f"Declared edges: {s['declared_total']} · observed edges: {s['observed_total']}",
        "",
        "## Edges",
        "",
        "| Verdict | Upstream | Downstream | Operations observed |",
        "| --- | --- | --- | --- |",
    ]
    for v in report["verdicts"]:
        ops = " → ".join(v["operations"]) if v["operations"] else "—"
        lines.append(
            f"| `{v['verdict']}` | `{_short(v['upstream'])}` | `{_short(v['downstream'])}` | {ops} |"
        )

    if s["unmapped_nodes"]:
        lines += [
            "",
            "## Unmapped observed nodes",
            "",
            "These appeared at runtime but have no entry in `urn_map.yaml`, so no verdict "
            "could be reached. Polygraph does not guess mappings.",
            "",
        ]
        lines += [f"- `{k}`" for k in s["unmapped_nodes"]]

    lines += [
        "",
        "## What these verdicts do and do not mean",
        "",
        "- `VERIFIED` is evidence from **the captured run**, not a proof about all runs.",
        "- `PHANTOM` means nothing flowed along a declared edge **in this run**. A genuinely "
        "conditional edge on a branch that was not taken will look phantom.",
        "- `UNDECLARED` means the runtime proved an edge the catalog never mentioned.",
    ]
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], json_path: Path, md_path: Path, title: str) -> None:
    # LF-pinned: the markdown's sha-256 is published to DataHub, and Windows
    # CRLF translation would make the file on disk hash differently.
    write_json_lf(json_path, report)
    write_text_lf(md_path, to_markdown(report, title))
