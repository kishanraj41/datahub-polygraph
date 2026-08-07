"""Lineage Integrity Score.

The obvious score -- "what fraction of declared edges were verified?" -- is
misleading, and badly so. A catalog that declares one correct edge and misses
five real ones scores a perfect 1.0. That is precision without recall, and
lineage drift is overwhelmingly a recall problem: the edges nobody wrote down.

So the headline number is the **Jaccard index** over the declared and observed
edge sets:

    LIS = |declared ∩ observed| / |declared ∪ observed|

Both a phantom edge and an undeclared source push it down. A perfect 1.0 means
the catalog's claims and the runtime's behaviour are the same set -- nothing
stale, nothing hidden.

Precision and recall are reported alongside it, because the single number does
not tell you *which way* a catalog is wrong, and those are different problems
with different fixes:

    precision = |declared ∩ observed| / |declared|   -- how much of what the catalog claims is true
    recall    = |declared ∩ observed| / |observed|   -- how much of reality the catalog captured

A note on the brief's "weighted by link confidence where available": DataHub OSS
exposes no per-edge confidence signal on ``dataJobInputOutput``, so there is
nothing to weight by. Rather than invent a weight, the score is unweighted and
this is stated. If a future DataHub version exposes confidence, the weighting
belongs here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .reconcile import PHANTOM, UNDECLARED, VERIFIED

# Structured property URNs. Defined once, then assigned per entity.
PROP_SCORE = "urn:li:structuredProperty:polygraph.lineageIntegrityScore"
PROP_PRECISION = "urn:li:structuredProperty:polygraph.lineagePrecision"
PROP_RECALL = "urn:li:structuredProperty:polygraph.lineageRecall"


@dataclass
class IntegrityScore:
    entity_urn: str
    score: float
    precision: float
    recall: float
    verified: int
    phantom: int
    undeclared: int
    declared_total: int
    observed_total: int
    interpretation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ratio(numerator: int, denominator: int) -> float:
    """Undefined ratios are 0.0, not 1.0.

    A job with no declared edges has not proven anything; scoring it 1.0 for
    having made no false claims would reward an empty catalog entry, which is
    exactly the failure mode Polygraph exists to catch.
    """
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


def _interpret(score: float, phantom: int, undeclared: int) -> str:
    if phantom == 0 and undeclared == 0:
        return "Catalog and runtime agree completely for the captured run."
    parts = []
    if phantom:
        parts.append(f"{phantom} declared edge(s) carried no data in this run")
    if undeclared:
        parts.append(f"{undeclared} real source(s) are missing from the catalog")
    detail = "; ".join(parts)
    if score >= 0.8:
        band = "Mostly accurate"
    elif score >= 0.5:
        band = "Materially wrong"
    else:
        band = "Not trustworthy"
    return f"{band}: {detail}."


def score_consumer(report: dict[str, Any], entity_urn: str) -> IntegrityScore:
    """Score one consumer (a dataJob) from a reconciliation report."""
    verdicts = [v for v in report["verdicts"] if v["downstream"] == entity_urn]
    verified = sum(1 for v in verdicts if v["verdict"] == VERIFIED)
    phantom = sum(1 for v in verdicts if v["verdict"] == PHANTOM)
    undeclared = sum(1 for v in verdicts if v["verdict"] == UNDECLARED)

    declared_total = verified + phantom
    observed_total = verified + undeclared
    union = verified + phantom + undeclared

    score = _ratio(verified, union)

    return IntegrityScore(
        entity_urn=entity_urn,
        score=score,
        precision=_ratio(verified, declared_total),
        recall=_ratio(verified, observed_total),
        verified=verified,
        phantom=phantom,
        undeclared=undeclared,
        declared_total=declared_total,
        observed_total=observed_total,
        interpretation=_interpret(score, phantom, undeclared),
    )


def score_all_consumers(report: dict[str, Any]) -> list[IntegrityScore]:
    consumers = sorted({v["downstream"] for v in report["verdicts"]})
    return [score_consumer(report, urn) for urn in consumers]


# --------------------------------------------------------------------------
# DataHub write-back
# --------------------------------------------------------------------------

PROPERTY_DEFINITIONS = [
    (
        PROP_SCORE,
        "polygraph.lineageIntegrityScore",
        "Lineage Integrity Score",
        "Jaccard index of declared vs runtime-observed lineage edges. "
        "1.0 means the catalog's claims and the runtime's behaviour are the same set. "
        "Phantom edges and undeclared sources both reduce it.",
    ),
    (
        PROP_PRECISION,
        "polygraph.lineagePrecision",
        "Lineage Precision",
        "Fraction of declared edges that runtime capture confirmed. Low precision means "
        "the catalog asserts edges that are not real.",
    ),
    (
        PROP_RECALL,
        "polygraph.lineageRecall",
        "Lineage Recall",
        "Fraction of observed edges that the catalog declared. Low recall means real "
        "data sources are missing from the catalog.",
    ),
]


def define_properties(graph) -> list[str]:
    """Create the structured property definitions. Idempotent."""
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import StructuredPropertyDefinitionClass

    created = []
    for urn, qualified_name, display, description in PROPERTY_DEFINITIONS:
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=StructuredPropertyDefinitionClass(
                    qualifiedName=qualified_name,
                    displayName=display,
                    valueType="urn:li:dataType:datahub.number",
                    entityTypes=["urn:li:entityType:datahub.dataJob"],
                    cardinality="SINGLE",
                    description=description,
                ),
            )
        )
        created.append(urn)
    graph.flush()
    return created


def write_scores(graph, scores: list[IntegrityScore], dry_run: bool = False) -> list[dict]:
    """Assign the score properties to each scored entity."""
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import (
        StructuredPropertiesClass,
        StructuredPropertyValueAssignmentClass,
    )

    written = []
    for s in scores:
        assignments = [
            StructuredPropertyValueAssignmentClass(propertyUrn=PROP_SCORE, values=[s.score]),
            StructuredPropertyValueAssignmentClass(
                propertyUrn=PROP_PRECISION, values=[s.precision]
            ),
            StructuredPropertyValueAssignmentClass(propertyUrn=PROP_RECALL, values=[s.recall]),
        ]
        if not dry_run:
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=s.entity_urn,
                    aspect=StructuredPropertiesClass(properties=assignments),
                )
            )
        written.append(s.to_dict())

    if not dry_run:
        graph.flush()
    return written


def verify_scores(graph, scores: list[IntegrityScore]) -> dict[str, Any]:
    """Read the properties back. A score that did not land is a failed gate."""
    from datahub.metadata.schema_classes import StructuredPropertiesClass

    problems = []
    confirmed = {}
    for s in scores:
        aspect = graph.get_aspect(s.entity_urn, StructuredPropertiesClass)
        found = {p.propertyUrn: p.values[0] for p in (aspect.properties if aspect else [])}
        if found.get(PROP_SCORE) != s.score:
            problems.append(
                {"urn": s.entity_urn, "expected": s.score, "found": found.get(PROP_SCORE)}
            )
        else:
            confirmed[s.entity_urn] = found
    return {"confirmed": confirmed, "problems": problems, "ok": not problems}


def to_markdown(scores: list[IntegrityScore]) -> str:
    lines = [
        "# Polygraph lineage integrity scores",
        "",
        "| Asset | Score | Precision | Recall | Verified | Phantom | Undeclared |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for s in scores:
        short = s.entity_urn.rsplit(",", 1)[-1].rstrip(")")
        lines.append(
            f"| `{short}` | **{s.score}** | {s.precision} | {s.recall} | "
            f"{s.verified} | {s.phantom} | {s.undeclared} |"
        )
    lines += ["", "## Interpretation", ""]
    for s in scores:
        lines.append(f"- `{s.entity_urn}` — {s.interpretation}")
    lines += [
        "",
        "## How the score is defined",
        "",
        "`LIS = |declared ∩ observed| / |declared ∪ observed|` (Jaccard index).",
        "",
        "The naive alternative — verified over declared — scores a catalog 1.0 when it "
        "declares one correct edge and misses five real ones. Lineage drift is mostly a "
        "recall problem, so the headline number has to punish omissions as hard as it "
        "punishes stale claims. Precision and recall are reported separately because they "
        "point at different fixes.",
        "",
        "Unweighted: DataHub OSS exposes no per-edge confidence on `dataJobInputOutput`, "
        "so there is nothing to weight by. No weight was invented.",
    ]
    return "\n".join(lines) + "\n"
