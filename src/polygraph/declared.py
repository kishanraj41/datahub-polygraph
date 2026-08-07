"""Declared lineage: what the DataHub catalog claims.

Reads the training job's ``dataJobInputOutput`` aspect and projects it into the
same ``DeclaredEdge`` shape the reconciler consumes. Input edges only -- see the
docstring on ``reconcile()`` for why Polygraph scopes to sources.

This deliberately reads the *aspect* rather than the rendered lineage graph.
The aspect is what a human or an ingestion job actually asserted; the rendered
graph can include edges DataHub inferred, and Polygraph should be judging
claims, not inferences.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import DataJobInputOutputClass

from .reconcile import DeclaredEdge


@dataclass
class DeclaredLineage:
    edges: list[DeclaredEdge]
    job_urn: str
    output_datasets: list[str]
    raw: dict[str, Any]


def connect(gms: str = "http://localhost:8080", token: str | None = None) -> DataHubGraph:
    return DataHubGraph(DatahubClientConfig(server=gms, token=token))


def fetch_declared(graph: DataHubGraph, job_urn: str) -> DeclaredLineage:
    """Read the declared inputs of a dataJob."""
    aspect = graph.get_aspect(job_urn, DataJobInputOutputClass)
    if aspect is None:
        raise LookupError(
            f"{job_urn} has no dataJobInputOutput aspect. Has demo/seed_catalog.py run?"
        )

    inputs = list(aspect.inputDatasets or [])
    outputs = list(aspect.outputDatasets or [])

    edges = [
        DeclaredEdge(upstream=ds, downstream=job_urn, via="dataJob.inputDatasets")
        for ds in inputs
    ]

    return DeclaredLineage(
        edges=edges,
        job_urn=job_urn,
        output_datasets=outputs,
        raw={"inputDatasets": inputs, "outputDatasets": outputs},
    )


def owners_of(graph: DataHubGraph, urn: str) -> list[str]:
    """Owning actors for an asset, used to name a responsible team in incidents."""
    ownership = graph.get_ownership(urn)
    if not ownership or not ownership.owners:
        return []
    return [o.owner for o in ownership.owners]
