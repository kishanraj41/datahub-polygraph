"""Declared lineage, read through **DataHub's own MCP Server**.

`declared.py` reads the catalog with the `acryl-datahub` SDK. This module reads
the same claim through `mcp-server-datahub`, launched as a stdio subprocess --
the same way an agent client launches it.

Why both exist: the SDK path fetches one aspect and is exact. The MCP path goes
through the interface DataHub actually offers to agents, which is what Polygraph
is arguing about in the first place -- if an agent asks DataHub "what feeds this
job?", Polygraph should be checking *that* answer, not a different one obtained
by a privileged back door.

The two paths must agree. `scripts/run_gate10.ps1` reconciles twice, once per
path, and fails if the verdicts differ. That comparison is the real test: the
SDK result is the known-good oracle, and any divergence is either a bug here or
a genuine difference between what the aspect says and what the agent-facing API
reports -- and both are worth knowing about.

KNOWN LIMITATION. ``get_lineage`` resolves to GraphQL ``searchAcrossLineage``.
On a DataHub stack whose GMS speaks the Elasticsearch dialect to an OpenSearch
backend, that resolver fails to create a point-in-time snapshot and returns a
500 -- and so does the DataHub UI's own Lineage tab. This path is therefore not
the default. See ``docs/DATAHUB_MCP.md``, ``scripts/probe_gms.ps1`` and
``scripts/fix_gms_search.ps1``. ``catalog_mcp`` reaches DataHub through MCP tools
that do not touch that resolver.

Requires `mcp-server-datahub` importable in the same environment (it is in
`requirements.txt`) and DataHub credentials from `DATAHUB_GMS_URL` /
`DATAHUB_GMS_TOKEN` or `~/.datahubenv`, which `datahub init` writes.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import dh_mcp
from .declared import DeclaredLineage
from .dh_mcp import DataHubMcpError, resolve_server_command  # noqa: F401 - public re-export
from .reconcile import DeclaredEdge

DATASET_URN_RE = re.compile(r"^urn:li:dataset:\(")


class McpLineageError(DataHubMcpError):
    pass


def _extract_dataset_urns(payload: Any) -> list[str]:
    """Collect dataset URNs from an arbitrarily nested MCP response.

    Deliberately structural rather than positional. The `get_lineage` tool
    returns a cleaned GraphQL `searchAcrossLineage` payload whose exact nesting
    is an implementation detail of the server and has no stability guarantee.
    Hard-coding a path like ``["upstreams"]["searchResults"][i]["entity"]["urn"]``
    would break silently on a server upgrade and produce an empty upstream set --
    which Polygraph would then report as a catalog full of phantom edges. A
    walk cannot break that way: it either finds the URNs or finds nothing, and
    finding nothing is loud.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            urn = node.get("urn")
            if isinstance(urn, str) and DATASET_URN_RE.match(urn) and urn not in seen:
                seen.add(urn)
                found.append(urn)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return sorted(found)


def _run_fetch(job_urn: str, gms: str | None, token: str | None) -> dict[str, Any]:
    """One call to DataHub's MCP Server. Patch point for tests, so they never
    have to stub ``asyncio.run`` -- which leaves an un-awaited coroutine and a
    RuntimeWarning in the output."""
    out = dh_mcp.call_tools(
        [
            (
                "get_lineage",
                {"urn": job_urn, "upstream": True, "max_hops": 1, "max_results": 100},
            )
        ],
        gms,
        token,
    )
    return {"tools": out["tools"], "payload": out["results"]["get_lineage"]}


def fetch_declared_via_mcp(
    job_urn: str,
    gms: str | None = None,
    token: str | None = None,
) -> DeclaredLineage:
    """Read a dataJob's declared upstreams through DataHub's MCP Server."""
    try:
        out = _run_fetch(job_urn, gms, token)
    except DataHubMcpError:
        raise
    except Exception as e:  # noqa: BLE001 - surfaced with context, never swallowed
        raise McpLineageError(
            "Could not read lineage via DataHub's MCP Server.\n" + dh_mcp.explain_failure(e)
        ) from e

    payload = out["payload"]
    upstreams = _extract_dataset_urns(payload)

    if not upstreams:
        raise McpLineageError(
            "DataHub's MCP Server returned no upstream datasets for\n"
            f"  {job_urn}\n"
            "An empty upstream set would make every declared edge look PHANTOM, so "
            "this is treated as a failure rather than a finding. Either the job has "
            "no declared inputs (run demo/seed_catalog.py), or the response shape "
            "changed. Raw payload:\n"
            f"{json.dumps(payload, indent=2, default=str)[:1500]}"
        )

    edges = [
        DeclaredEdge(upstream=ds, downstream=job_urn, via="mcp-server-datahub:get_lineage")
        for ds in upstreams
    ]

    return DeclaredLineage(
        edges=edges,
        job_urn=job_urn,
        # get_lineage(upstream=True) does not report outputs; the reconciler
        # scopes to inputs anyway, and claiming an empty output set would be a
        # statement this call cannot support.
        output_datasets=[],
        raw={
            "source": "mcp-server-datahub",
            "tool": "get_lineage",
            "tools_advertised": out["tools"],
            "inputDatasets": upstreams,
        },
    )
