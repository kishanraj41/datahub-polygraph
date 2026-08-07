"""Phase 6 -- the incident path.

When a run's model quality collapses, Polygraph answers three questions that a
catalog alone cannot:

1. **What changed?** The metric delta between a healthy baseline capture and the
   degraded run -- both real captures, no simulation.
2. **Which operation is responsible?** AutoLineage's analyzer ranks operations
   by deviation and localises a root cause. Polygraph reports the ranking, not
   just the winner, so a reader can judge the confidence themselves.
3. **Who owns the affected assets?** Resolved from DataHub ownership, so the
   report names a team rather than a URN.

The report is hashed with sha-256 and the digest is stored on the DataHub
document as a custom property, so the document in the catalog can be verified
byte-for-byte against the copy in ``examples/``.

Honesty constraints baked in here:

* The incident is attributed to the **training job**, because that is where the
  operation ran. It is not attributed to a single upstream dataset -- the
  offending ``filter`` sits on the path from more than one source, and naming
  one would be a guess dressed up as a finding.
* Every number in the report is read from ``metrics.json`` files produced by
  actual runs. Nothing is computed for narrative effect.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Anomaly severities included in the report. ``info`` is excluded on purpose:
# AutoLineage's info-level signals include timing-sensitive counters such as
# ``pipeline / operation_count``, which vary by one or two between identical
# runs. Including them made the incident document non-reproducible -- two runs
# of the same code produced different sha-256 digests, which reads as the
# integrity claim failing. The localisation result does not depend on them;
# AutoLineage's own planted-bug README says so explicitly.
REPORTED_SEVERITIES = ("critical", "warning")


@dataclass
class Incident:
    incident_id: str
    metric: str
    baseline_value: float
    degraded_value: float
    delta: float
    root_operation: str | None
    impact_score: float | None
    anomalies: list[dict[str, Any]]
    implicated_edges: list[dict[str, Any]]
    job_urn: str
    owners: list[str]
    baseline_run: dict[str, Any]
    degraded_run: dict[str, Any]
    markdown: str = ""
    sha256: str = ""
    affected_urns: list[str] = field(default_factory=list)


def _fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.4f}"


def implicated_edges(observed_graph: dict[str, Any], operation: str | None) -> list[dict[str, Any]]:
    """Observed edges whose operation path contains the root-cause operation.

    This is a factual containment check, not an inference: the operation really
    was recorded on the path between those two assets.
    """
    if not operation:
        return []
    return [
        {
            "upstream": e["upstream"],
            "downstream": e["downstream"],
            "operations": e["operations"],
        }
        for e in observed_graph.get("edges", [])
        if operation in e.get("operations", [])
    ]


def build_incident(
    baseline_metrics: dict[str, Any],
    degraded_metrics: dict[str, Any],
    observed_graph: dict[str, Any],
    job_urn: str,
    owners: list[str],
    metric: str = "f1",
) -> Incident:
    base_v = float(baseline_metrics[metric])
    bad_v = float(degraded_metrics[metric])
    root = degraded_metrics.get("root_cause") or {}
    root_op = root.get("operation")

    # Deterministic subset, sorted deterministically. Two runs of the same code
    # must produce byte-identical reports or the published digest is worthless.
    anomalies = [
        a
        for a in (degraded_metrics.get("anomalies") or [])
        if str(a.get("severity", "")).lower() in REPORTED_SEVERITIES
    ]
    anomalies.sort(key=lambda a: (-float(a.get("deviation", 0)), str(a.get("operation", ""))))

    edges = implicated_edges(observed_graph, root_op)

    # Content-addressed id: the same collapse produces the same id, so re-running
    # the demo updates one document instead of littering the knowledge base.
    fingerprint = json.dumps(
        {
            "metric": metric,
            "baseline": base_v,
            "degraded": bad_v,
            "root": root_op,
            "job": job_urn,
        },
        sort_keys=True,
    )
    incident_id = "incident_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:12]

    inc = Incident(
        incident_id=incident_id,
        metric=metric,
        baseline_value=base_v,
        degraded_value=bad_v,
        delta=bad_v - base_v,
        root_operation=root_op,
        impact_score=root.get("impact"),
        anomalies=anomalies,
        implicated_edges=edges,
        job_urn=job_urn,
        owners=owners,
        baseline_run=baseline_metrics,
        degraded_run=degraded_metrics,
    )
    inc.affected_urns = [job_urn]
    inc.markdown = render(inc)
    inc.sha256 = hashlib.sha256(inc.markdown.encode("utf-8")).hexdigest()
    return inc


def render(inc: Incident) -> str:
    owner_line = ", ".join(f"`{o}`" for o in inc.owners) if inc.owners else "_no owner recorded_"

    lines = [
        f"# {inc.incident_id}: {inc.metric.upper()} collapse in `fraud_scoring`",
        "",
        "## What happened",
        "",
        f"| | baseline | degraded | delta |",
        f"| --- | ---: | ---: | ---: |",
        f"| **{inc.metric}** | {_fmt(inc.baseline_value)} | {_fmt(inc.degraded_value)} | "
        f"{inc.delta:+.4f} |",
        f"| rows after filter | {inc.baseline_run.get('rows_after_filter', 'n/a')} | "
        f"{inc.degraded_run.get('rows_after_filter', 'n/a')} | |",
        f"| filter quantile | {inc.baseline_run.get('filter_quantile', 'n/a')} | "
        f"{inc.degraded_run.get('filter_quantile', 'n/a')} | |",
        "",
        "Both rows come from `metrics.json` files written by real runs of "
        "`demo/pipeline.py`. Neither number is illustrative.",
        "",
        "## Root cause",
        "",
    ]

    if inc.root_operation:
        lines += [
            f"AutoLineage's analyzer localises the collapse to the **`{inc.root_operation}`** "
            f"operation (impact score {inc.impact_score}).",
            "",
        ]
    else:
        lines += ["The analyzer did not localise a root cause for this run.", ""]

    if inc.anomalies:
        lines += [
            "Ranking of critical and warning anomalies, so the confidence is visible "
            "rather than asserted:",
            "",
            "| Operation | Metric | Severity | Deviation |",
            "| --- | --- | --- | ---: |",
        ]
        for a in inc.anomalies:
            lines.append(
                f"| `{a['operation']}` | {a['metric']} | {a['severity']} | {a['deviation']} |"
            )
        lines.append("")

        lines += [
            "Info-level signals are excluded: they include timing-sensitive counters "
            "that vary between identical runs, which would make this document's "
            "sha-256 meaningless. They do not affect localisation.",
            "",
        ]

    lines += [
        "## Where it sits in the lineage",
        "",
    ]
    if inc.implicated_edges:
        lines += [
            f"The `{inc.root_operation}` operation was recorded on the path between these "
            "assets, according to runtime capture:",
            "",
            "| Upstream | Downstream | Operations recorded |",
            "| --- | --- | --- |",
        ]
        for e in inc.implicated_edges:
            ops = " → ".join(e["operations"])
            lines.append(f"| `{e['upstream']}` | `{e['downstream']}` | {ops} |")
        lines += [
            "",
            "Polygraph attributes the incident to the **job**, not to any single upstream "
            "dataset. The operation lies on the path from more than one source, and picking "
            "one would be a guess presented as a finding.",
            "",
        ]
    else:
        lines += ["No observed edge contained the root-cause operation.", ""]

    lines += [
        "## Ownership",
        "",
        f"Affected job: `{inc.job_urn}`  ",
        f"Owners (from DataHub): {owner_line}",
        "",
        "## Verification",
        "",
        "The sha-256 of this document is stored as the `polygraph_sha256` custom property "
        "on the DataHub document, so the catalog copy can be checked against the file in "
        "`examples/` byte for byte.",
        "",
        "## What this does not tell you",
        "",
        "- The analyzer ranks **row-count and column-count deviations**. A bug that "
        "preserves both shapes -- a unit error scaling a column by 1000, say -- would not "
        "appear here at all.",
        "- Localisation is to an *operation*, not a line number.",
        "- The baseline is a single captured run with a fixed seed, not a distribution.",
    ]
    return "\n".join(lines) + "\n"
