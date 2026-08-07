"""Phase 2 -- seed DataHub with deliberately imperfect testimony.

This is the "immaculate catalog" beat of the demo. Everything registered here
looks correct in the DataHub UI: a fraud-scoring flow, a training job, named
inputs and outputs, a real owning team. Nothing about it hints that two of its
three input claims are wrong.

The imperfections are deliberate and are recorded in ``seed_manifest.json``,
which is the oracle the Phase 4 test asserts against:

* ``legacy_claims_archive`` is declared as an input. The pipeline never opens
  it. -> must come back ``PHANTOM``.
* ``fee_schedule`` is registered as a dataset but is *not* declared as an input,
  even though the pipeline reads and merges it. -> must come back
  ``UNDECLARED``.
* ``raw_claims`` is declared and genuinely read. -> must come back ``VERIFIED``.

Idempotent: re-running overwrites the same aspects on the same URNs.

    python demo/seed_catalog.py --gms http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    DataFlowInfoClass,
    DataJobInfoClass,
    DataJobInputOutputClass,
    DatasetPropertiesClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    TagPropertiesClass,
)

PLATFORM = "urn:li:dataPlatform:file"
ENV = "PROD"
FLOW_URN = "urn:li:dataFlow:(polygraph,fraud_scoring,PROD)"
JOB_URN = f"urn:li:dataJob:({FLOW_URN},train_fraud_model)"
OWNER_URN = "urn:li:corpGroup:ml-platform-team"


def dataset_urn(name: str) -> str:
    return f"urn:li:dataset:({PLATFORM},{name},{ENV})"


RAW_CLAIMS = dataset_urn("polygraph.demo.raw_claims")
FEE_SCHEDULE = dataset_urn("polygraph.demo.fee_schedule")
LEGACY_ARCHIVE = dataset_urn("polygraph.demo.legacy_claims_archive")
PREDICTIONS = dataset_urn("polygraph.demo.fraud_predictions")

DATASETS = [
    (
        RAW_CLAIMS,
        "polygraph.demo.raw_claims",
        "Raw fraud claims, one row per claim. Declared input to the training job "
        "and genuinely read by it.",
    ),
    (
        FEE_SCHEDULE,
        "polygraph.demo.fee_schedule",
        "Per-region fee lookup. Registered as an asset. NOT declared as an input to "
        "the training job -- which is the point: the pipeline reads it anyway.",
    ),
    (
        LEGACY_ARCHIVE,
        "polygraph.demo.legacy_claims_archive",
        "Archived claims from the previous system. Declared as an input to the "
        "training job. The pipeline has not read it since the 2025 refactor.",
    ),
    (
        PREDICTIONS,
        "polygraph.demo.fraud_predictions",
        "Scored output of the fraud model.",
    ),
]

# Tag entities are created up front so they render with descriptions in the UI
# rather than appearing as bare strings the first time Polygraph applies one.
TAGS = [
    ("polygraph:verified", "Runtime capture confirmed data flows along this declared edge."),
    ("polygraph:phantom", "Declared in the catalog, but no data flowed along it in the captured run."),
    (
        "polygraph:undeclared-source",
        "Runtime capture proved this asset is read, but the catalog never declared it.",
    ),
    ("polygraph:incident", "Implicated in a model-quality incident by Polygraph."),
]


def ownership() -> OwnershipClass:
    return OwnershipClass(
        owners=[OwnerClass(owner=OWNER_URN, type=OwnershipTypeClass.DATAOWNER)]
    )


def build_mcps() -> list[MetadataChangeProposalWrapper]:
    mcps: list[MetadataChangeProposalWrapper] = []

    for tag_name, desc in TAGS:
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=f"urn:li:tag:{tag_name}",
                aspect=TagPropertiesClass(name=tag_name, description=desc),
            )
        )

    for urn, name, desc in DATASETS:
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=DatasetPropertiesClass(name=name, description=desc),
            )
        )
        mcps.append(MetadataChangeProposalWrapper(entityUrn=urn, aspect=ownership()))

    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=FLOW_URN,
            aspect=DataFlowInfoClass(
                name="fraud_scoring",
                description="Nightly fraud-scoring pipeline (pandas + scikit-learn).",
            ),
        )
    )
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=JOB_URN,
            aspect=DataJobInfoClass(
                name="train_fraud_model",
                type="COMMAND",
                description="Trains the fraud classifier and writes scored predictions.",
            ),
        )
    )
    mcps.append(MetadataChangeProposalWrapper(entityUrn=JOB_URN, aspect=ownership()))

    # THE IMPERFECT TESTIMONY.
    # inputDatasets omits fee_schedule (which is read) and includes
    # legacy_claims_archive (which is not).
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=JOB_URN,
            aspect=DataJobInputOutputClass(
                inputDatasets=[RAW_CLAIMS, LEGACY_ARCHIVE],
                outputDatasets=[PREDICTIONS],
            ),
        )
    )

    return mcps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gms", default="http://localhost:8080")
    ap.add_argument("--dry-run", action="store_true", help="print URNs, emit nothing")
    args = ap.parse_args()

    mcps = build_mcps()

    if args.dry_run:
        for m in mcps:
            print(f"{type(m.aspect).__name__:28} {m.entityUrn}")
        print(f"\n{len(mcps)} aspects (dry run, nothing emitted)")
        return 0

    graph = DataHubGraph(DatahubClientConfig(server=args.gms))
    for m in mcps:
        graph.emit_mcp(m)
    graph.flush()

    # Verify rather than assume. A seed that silently half-applied would make
    # every downstream verdict wrong.
    missing = [urn for urn, _, _ in DATASETS if not graph.exists(urn)]
    for urn in (FLOW_URN, JOB_URN):
        if not graph.exists(urn):
            missing.append(urn)

    io = graph.get_aspect(JOB_URN, DataJobInputOutputClass)
    declared_inputs = sorted(io.inputDatasets) if io else []
    expected_inputs = sorted([RAW_CLAIMS, LEGACY_ARCHIVE])

    result = {
        "emitted_aspects": len(mcps),
        "missing_entities": missing,
        "declared_inputs": declared_inputs,
        "inputs_match_intent": declared_inputs == expected_inputs,
        "fee_schedule_registered_but_undeclared": (
            graph.exists(FEE_SCHEDULE) and FEE_SCHEDULE not in declared_inputs
        ),
    }
    print(json.dumps(result, indent=2))

    ok = (
        not missing
        and result["inputs_match_intent"]
        and result["fee_schedule_registered_but_undeclared"]
    )
    if not ok:
        print("\nGATE 2: FAIL", file=sys.stderr)
        return 1

    print("\nGATE 2: PASS")
    print(f"Lineage in the UI: http://localhost:9002/tasks/{JOB_URN}/Lineage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
