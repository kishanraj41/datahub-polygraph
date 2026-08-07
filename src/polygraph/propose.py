"""Ring 3 — propose lineage corrections, with a human in the loop.

Everything Polygraph has written so far is **additive and namespaced**: tags
under `polygraph:`, structured properties under `polygraph.`, documents of its
own. None of it changes what the catalog *claims*. This module does, and that
makes it categorically different.

Two asymmetric operations, deliberately gated differently:

**Adding an undeclared source is safe.** Runtime capture proved data flowed
along the edge. Adding it makes the catalog more true. Still requires
``--approve``, but that is the only barrier.

**Removing a phantom edge is not safe, and is gated harder.** A `PHANTOM`
verdict means nothing flowed *in the captured run*. A conditional edge on a
branch that was not taken is indistinguishable from a genuinely dead one. If
Polygraph deletes a real-but-conditional edge, it has done exactly what it
accuses catalogs of: made a confident claim that is wrong. So removals need a
separate ``--remove-phantom`` flag on top of ``--approve``, and the report says
plainly that this cannot be decided from one run.

Every applied change writes a revert file first. A tool that edits your catalog
and cannot undo it has no business editing your catalog.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fsutil import write_json_lf, write_text_lf
from .reconcile import PHANTOM, UNDECLARED, _short

ADD = "ADD_INPUT"
REMOVE = "REMOVE_INPUT"


@dataclass
class Proposal:
    action: str
    upstream: str
    downstream: str
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)
    safety: str = ""
    requires_extra_flag: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_proposals(report: dict[str, Any]) -> list[Proposal]:
    """Turn reconciliation verdicts into concrete lineage edits."""
    proposals: list[Proposal] = []

    for v in report.get("verdicts", []):
        if v["verdict"] == UNDECLARED:
            proposals.append(
                Proposal(
                    action=ADD,
                    upstream=v["upstream"],
                    downstream=v["downstream"],
                    rationale=(
                        "Runtime capture recorded data flowing along this edge through "
                        f"{len(v.get('operations', []))} operation(s), but the catalog "
                        "does not declare it."
                    ),
                    evidence={
                        "operations": v.get("operations", []),
                        "run": report.get("run", {}),
                    },
                    safety=(
                        "Additive and evidence-backed. The edge demonstrably exists; "
                        "declaring it can only make the catalog more accurate."
                    ),
                    requires_extra_flag=False,
                )
            )
        elif v["verdict"] == PHANTOM:
            proposals.append(
                Proposal(
                    action=REMOVE,
                    upstream=v["upstream"],
                    downstream=v["downstream"],
                    rationale=(
                        "The catalog declares this edge but the captured run recorded no "
                        "data flowing along it."
                    ),
                    evidence={"run": report.get("run", {})},
                    safety=(
                        "NOT SAFE FROM ONE RUN. A conditional edge whose branch was not "
                        "taken during capture is indistinguishable from a dead one. "
                        "Removing a real-but-conditional edge would make the catalog "
                        "less true — the exact failure Polygraph exists to catch. "
                        "Confirm across several runs, or ask the owning team, before "
                        "approving."
                    ),
                    requires_extra_flag=True,
                )
            )

    proposals.sort(key=lambda p: (p.action, p.upstream))
    return proposals


def plan(
    proposals: list[Proposal],
    current_inputs: list[str],
    job_urn: str,
    allow_removals: bool = False,
) -> dict[str, Any]:
    """Compute the resulting input set without applying anything."""
    applied: list[Proposal] = []
    skipped: list[dict[str, Any]] = []

    inputs = list(current_inputs)
    for p in proposals:
        if p.downstream != job_urn:
            skipped.append({"proposal": p.to_dict(), "why": "targets a different job"})
            continue
        if p.action == ADD:
            if p.upstream in inputs:
                skipped.append({"proposal": p.to_dict(), "why": "already declared"})
                continue
            inputs.append(p.upstream)
            applied.append(p)
        elif p.action == REMOVE:
            if not allow_removals:
                skipped.append(
                    {"proposal": p.to_dict(), "why": "removal requires --remove-phantom"}
                )
                continue
            if p.upstream not in inputs:
                skipped.append({"proposal": p.to_dict(), "why": "not currently declared"})
                continue
            inputs.remove(p.upstream)
            applied.append(p)

    return {
        "job": job_urn,
        "before": sorted(current_inputs),
        "after": sorted(inputs),
        "applied": [p.to_dict() for p in applied],
        "skipped": skipped,
        "changed": sorted(current_inputs) != sorted(inputs),
    }


def write_revert(path: Path, job_urn: str, before_inputs: list[str], before_outputs: list[str]) -> None:
    """Snapshot the aspect before touching it. No revert file, no write."""
    write_json_lf(
        path,
        {
            "note": (
                "Polygraph modified this job's declared lineage. Restore with: "
                "polygraph propose --revert <this file>"
            ),
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "job": job_urn,
            "inputDatasets": sorted(before_inputs),
            "outputDatasets": sorted(before_outputs),
        },
    )


def apply_plan(graph, job_urn: str, new_inputs: list[str], outputs: list[str]) -> None:
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.metadata.schema_classes import DataJobInputOutputClass

    graph.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=job_urn,
            aspect=DataJobInputOutputClass(
                inputDatasets=sorted(new_inputs), outputDatasets=sorted(outputs)
            ),
        )
    )
    graph.flush()


def to_markdown(proposals: list[Proposal], planned: dict[str, Any]) -> str:
    lines = [
        "# Polygraph lineage proposals",
        "",
        f"Job: `{planned['job']}`",
        "",
        "Polygraph's tags and scores are additive and namespaced. These proposals are "
        "different: they change what the catalog **claims**. Nothing here is applied "
        "without `--approve`, and removals need `--remove-phantom` on top of that.",
        "",
        "## Proposed changes",
        "",
        "| Action | Edge | Needs extra flag | Rationale |",
        "| --- | --- | :-: | --- |",
    ]
    for p in proposals:
        flag = "yes" if p.requires_extra_flag else "no"
        lines.append(
            f"| `{p.action}` | `{_short(p.upstream)}` → `{_short(p.downstream)}` | {flag} | "
            f"{p.rationale} |"
        )

    lines += ["", "## Safety", ""]
    for p in proposals:
        lines += [f"**`{p.action}` {_short(p.upstream)}**", "", p.safety, ""]

    lines += [
        "## Resulting declared inputs",
        "",
        "| | inputs |",
        "| --- | --- |",
        "| before | " + ", ".join(f"`{_short(u)}`" for u in planned["before"]) + " |",
        "| after | " + ", ".join(f"`{_short(u)}`" for u in planned["after"]) + " |",
        "",
    ]

    if planned["skipped"]:
        lines += ["## Not applied", ""]
        for s in planned["skipped"]:
            p = s["proposal"]
            lines.append(
                f"- `{p['action']}` {_short(p['upstream'])} — {s['why']}"
            )
        lines.append("")

    lines += [
        "## Why removals are gated harder than additions",
        "",
        "Adding an undeclared source is backed by positive evidence: the runtime "
        "recorded data flowing along that edge. Declaring it can only make the catalog "
        "more accurate.",
        "",
        "Removing a phantom edge is backed by *absence* of evidence from a single run. "
        "A conditional edge whose branch was not taken looks identical to a dead one. "
        "Deleting a real edge on that basis would be Polygraph making exactly the kind "
        "of confident-but-wrong claim it was built to detect, so it requires a separate "
        "explicit flag and a human who has looked at more than one run.",
    ]
    return "\n".join(lines) + "\n"


def write_report(proposals: list[Proposal], planned: dict[str, Any], json_path: Path, md_path: Path) -> None:
    write_json_lf(json_path, {"proposals": [p.to_dict() for p in proposals], "plan": planned})
    write_text_lf(md_path, to_markdown(proposals, planned))
