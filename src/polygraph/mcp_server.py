"""Polygraph as an MCP server.

``mcp-server-datahub`` lets an agent read what the catalog *claims*. This server
lets the same agent read what the runtime *proved*, and the difference between
them. Run both and an agent can answer "can I trust this asset's lineage?" with
evidence instead of with the catalog's own testimony.

Every tool reads **live from DataHub** -- the tags, structured properties and
documents Polygraph wrote -- rather than from a local report file. That matters:
an agent asking about trust should see the current state of the catalog, not a
snapshot from whenever someone last ran the CLI. Where a local report adds
detail the catalog does not carry (the operation paths, for instance), it is
used as an optional enrichment and its absence is reported rather than hidden.

Tools deliberately return *structured evidence plus a plain-language verdict*.
An agent that only relays the verdict is still correct; one that reads the
evidence can disagree with it. Both should be possible.

Run it:

    python -m polygraph.mcp_server                    # stdio, for Claude Desktop / Cursor
    POLYGRAPH_GMS=http://localhost:8080 python -m polygraph.mcp_server

Register with Claude Desktop by adding to ``claude_desktop_config.json``:

    {
      "mcpServers": {
        "polygraph": {
          "command": "python",
          "args": ["-m", "polygraph.mcp_server"],
          "env": {"PYTHONPATH": "/path/to/datahub-polygraph/src"}
        }
      }
    }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .reconcile import TAG_FOR_VERDICT
from .score import PROP_PRECISION, PROP_RECALL, PROP_SCORE

GMS = os.environ.get("POLYGRAPH_GMS", "http://localhost:8080")
TOKEN = os.environ.get("POLYGRAPH_TOKEN") or None
REPORT_PATH = Path(os.environ.get("POLYGRAPH_REPORT", "examples/reconciliation_report.json"))

VERDICT_FOR_TAG = {f"urn:li:tag:{tag}": verdict for verdict, tag in TAG_FOR_VERDICT.items()}
INCIDENT_TAG_URN = "urn:li:tag:polygraph:incident"

mcp = FastMCP(
    name="polygraph",
    instructions=(
        "Polygraph reconciles what a DataHub catalog CLAIMS about lineage against what "
        "a pipeline's runtime actually DID. Use it whenever a question depends on "
        "whether lineage can be trusted, rather than on what lineage says.\n\n"
        "Key distinction: DataHub's own tools report declared lineage. These tools "
        "report whether those declarations survived contact with a real run.\n\n"
        "Every verdict describes ONE captured run. Do not restate them as universal "
        "properties of the pipeline. If a tool returns evidence_available=false, say "
        "so rather than answering from the catalog alone."
    ),
)


def _graph():
    from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

    return DataHubGraph(DatahubClientConfig(server=GMS, token=TOKEN))


def _local_report() -> dict[str, Any] | None:
    """Optional enrichment. Absence is reported, never silently ignored."""
    if REPORT_PATH.exists():
        try:
            return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def _tags_of(graph, urn: str) -> list[str]:
    from datahub.metadata.schema_classes import GlobalTagsClass

    aspect = graph.get_aspect(urn, GlobalTagsClass)
    return [t.tag for t in (aspect.tags if aspect else [])]


@mcp.tool
def can_i_trust(asset_urn: str) -> dict[str, Any]:
    """Answer whether an asset's declared lineage held up under runtime capture.

    This is the question Polygraph exists for. Returns the verdict Polygraph
    reached, the evidence behind it, and an explicit statement of what the
    verdict does and does not establish.

    Args:
        asset_urn: A DataHub dataset or dataJob URN.
    """
    graph = _graph()

    if not graph.exists(asset_urn):
        return {
            "asset": asset_urn,
            "evidence_available": False,
            "answer": "This URN does not exist in DataHub. Nothing to judge.",
        }

    tags = _tags_of(graph, asset_urn)
    verdicts = [VERDICT_FOR_TAG[t] for t in tags if t in VERDICT_FOR_TAG]
    has_incident = INCIDENT_TAG_URN in tags

    if not verdicts and not has_incident:
        return {
            "asset": asset_urn,
            "evidence_available": False,
            "answer": (
                "Polygraph has not examined this asset. Its lineage may be correct or "
                "may be stale -- there is no evidence either way. Do not treat this as "
                "a clean bill of health."
            ),
            "tags_present": tags,
        }

    report = _local_report()
    detail = []
    if report:
        for v in report.get("verdicts", []):
            if v["upstream"] == asset_urn or v["downstream"] == asset_urn:
                detail.append(
                    {
                        "upstream": v["upstream"],
                        "downstream": v["downstream"],
                        "verdict": v["verdict"],
                        "operations_observed": v.get("operations", []),
                        "reason": v.get("reason", ""),
                    }
                )

    answers = {
        "VERIFIED": (
            "Yes, for the run that was captured. Runtime capture confirmed data flowed "
            "along this declared edge. This is not proof about every run."
        ),
        "PHANTOM": (
            "No. The catalog declares this edge but no data flowed along it in the "
            "captured run. Either it is stale, or it is conditional and the branch was "
            "not taken. A human needs to decide which."
        ),
        "UNDECLARED": (
            "The catalog is incomplete here. Runtime capture proved this asset is read, "
            "but the catalog never declared the edge. Anyone reading the catalog alone "
            "would not know this asset is an input."
        ),
    }

    primary = verdicts[0] if verdicts else None
    return {
        "asset": asset_urn,
        "evidence_available": True,
        "verdicts": verdicts,
        "answer": answers.get(primary, "See verdicts."),
        "implicated_in_incident": has_incident,
        "edge_detail": detail,
        "edge_detail_source": str(REPORT_PATH) if report else None,
        "detail_note": (
            None
            if report
            else "Operation-level detail unavailable: no local reconciliation report found. "
            "Verdicts above come from DataHub tags and are still authoritative."
        ),
        "caveat": "Every Polygraph verdict describes a single captured run.",
    }


@mcp.tool
def get_integrity_score(job_urn: str) -> dict[str, Any]:
    """Get the Lineage Integrity Score for a job, with its components.

    The score is the Jaccard index of declared vs observed edges. Precision and
    recall are returned separately because a low score from stale claims and a
    low score from missing sources need different fixes.

    Args:
        job_urn: A DataHub dataJob URN.
    """
    from datahub.metadata.schema_classes import StructuredPropertiesClass

    graph = _graph()
    aspect = graph.get_aspect(job_urn, StructuredPropertiesClass)
    props = {p.propertyUrn: p.values[0] for p in (aspect.properties if aspect else [])}

    if PROP_SCORE not in props:
        return {
            "job": job_urn,
            "evidence_available": False,
            "answer": "No Polygraph integrity score on this job. Run `polygraph score`.",
        }

    score = float(props[PROP_SCORE])
    precision = float(props.get(PROP_PRECISION, 0))
    recall = float(props.get(PROP_RECALL, 0))

    if precision < recall:
        diagnosis = "The catalog asserts edges that are not real (stale declarations)."
    elif recall < precision:
        diagnosis = "Real data sources are missing from the catalog (shadow inputs)."
    elif score < 1.0:
        diagnosis = "The catalog is wrong in both directions equally."
    else:
        diagnosis = "Catalog and runtime agree for the captured run."

    return {
        "job": job_urn,
        "evidence_available": True,
        "lineage_integrity_score": score,
        "precision": precision,
        "recall": recall,
        "diagnosis": diagnosis,
        "definition": (
            "Jaccard index: |declared ∩ observed| / |declared ∪ observed|. Both stale "
            "declarations and undeclared sources reduce it. Unweighted -- DataHub OSS "
            "exposes no per-edge confidence signal."
        ),
    }


@mcp.tool
def list_undeclared_sources() -> dict[str, Any]:
    """List assets the runtime reads that the catalog never declared.

    These are usually the most consequential findings: nobody reviewing the
    catalog could have known these assets are inputs.
    """
    return _list_by_verdict("UNDECLARED")


@mcp.tool
def list_phantom_edges() -> dict[str, Any]:
    """List declared lineage edges that carried no data in the captured run.

    Usually stale declarations left behind by a refactor. A genuinely
    conditional edge whose branch was not taken looks identical, so these need
    human judgement rather than automatic deletion.
    """
    return _list_by_verdict("PHANTOM")


def _list_by_verdict(verdict: str) -> dict[str, Any]:
    report = _local_report()
    if not report:
        return {
            "evidence_available": False,
            "answer": (
                f"No local reconciliation report at {REPORT_PATH}. Run `polygraph reconcile` "
                "first. DataHub tags alone cannot reconstruct the edge list."
            ),
        }
    matches = [v for v in report.get("verdicts", []) if v["verdict"] == verdict]
    return {
        "evidence_available": True,
        "verdict": verdict,
        "count": len(matches),
        "edges": [
            {
                "upstream": v["upstream"],
                "downstream": v["downstream"],
                "operations_observed": v.get("operations", []),
            }
            for v in matches
        ],
        "run": report.get("run", {}),
        "caveat": "Describes a single captured run.",
    }


@mcp.tool
def get_incident_report(document_urn: str = "") -> dict[str, Any]:
    """Retrieve a Polygraph incident report from DataHub's knowledge base.

    Incident reports name the operation responsible for a model-quality
    collapse, the owning team, and the real before/after metrics.

    Args:
        document_urn: Full document URN. Omit to fetch the demo incident.
    """
    from datahub.metadata.schema_classes import DocumentInfoClass

    urn = document_urn or "urn:li:document:incident_515d772c4624"
    graph = _graph()
    aspect = graph.get_aspect(urn, DocumentInfoClass)

    if aspect is None:
        return {
            "document": urn,
            "evidence_available": False,
            "answer": "No such document in DataHub. Run `polygraph incident`.",
        }

    props = dict(aspect.customProperties or {})
    return {
        "document": urn,
        "evidence_available": True,
        "title": aspect.title,
        "markdown": aspect.contents.text if aspect.contents else "",
        "sha256": props.get("polygraph_sha256"),
        "root_operation": props.get("root_operation"),
        "baseline": props.get("baseline"),
        "degraded": props.get("degraded"),
        "verification_note": (
            "The sha256 is of the markdown as published. The report is byte-reproducible: "
            "regenerating it from the same code yields the same digest."
        ),
    }


@mcp.tool
def explain_verdict_semantics() -> dict[str, Any]:
    """Explain exactly what Polygraph's three verdicts mean and do not mean.

    Call this before presenting verdicts to a person. The verdicts are easy to
    overstate, and overstating them is the failure mode Polygraph exists to
    prevent in the first place.
    """
    return {
        "VERIFIED": {
            "means": "Declared, and runtime capture proved data flowed along it.",
            "does_not_mean": "That the edge is correct in every run. Evidence is single-run.",
        },
        "PHANTOM": {
            "means": "Declared, but nothing flowed along it in the captured run.",
            "does_not_mean": (
                "That the edge is definitely dead. A conditional edge on a branch not "
                "taken during capture is indistinguishable from a stale one."
            ),
        },
        "UNDECLARED": {
            "means": "Runtime proved the edge exists; the catalog never declared it.",
            "does_not_mean": "That anyone hid it deliberately. Usually it is simply drift.",
        },
        "unmapped": (
            "An observed asset with no entry in urn_map.yaml gets no verdict at all. "
            "Polygraph does no fuzzy matching and will not guess a correspondence."
        ),
        "scope": (
            "Polygraph reconciles INPUT edges into a job. It does not reconcile outputs, "
            "because the capture library cannot link a numpy predict() output into a "
            "newly constructed DataFrame -- a declared output edge would come back "
            "PHANTOM for tooling reasons rather than catalog reasons."
        ),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
