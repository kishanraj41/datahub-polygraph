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

Requires `mcp-server-datahub` on PATH (it is in `requirements.txt`) and DataHub
credentials from `DATAHUB_GMS_URL` / `DATAHUB_GMS_TOKEN` or `~/.datahubenv`,
which `datahub init` writes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from .declared import DeclaredLineage
from .reconcile import DeclaredEdge

DATASET_URN_RE = re.compile(r"^urn:li:dataset:\(")
SERVER_COMMAND = "mcp-server-datahub"
CALL_TIMEOUT_S = 60


class McpLineageError(RuntimeError):
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


def _server_env(gms: str | None, token: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if gms:
        env["DATAHUB_GMS_URL"] = gms
    if token:
        env["DATAHUB_GMS_TOKEN"] = token
    # The server logs to stderr at INFO; keep stdout clean for the protocol.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


async def _fetch(job_urn: str, gms: str | None, token: str | None) -> dict[str, Any]:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=SERVER_COMMAND,
        args=["--transport", "stdio"],
        env=_server_env(gms, token),
    )

    async with Client(transport) as client:
        tools = {t.name for t in await client.list_tools()}
        if "get_lineage" not in tools:
            raise McpLineageError(
                "DataHub's MCP Server did not advertise a `get_lineage` tool. "
                f"Advertised: {sorted(tools)}. Check the GMS version -- read tools "
                "are version-gated in mcp_server_datahub/version_requirements.py."
            )

        result = await asyncio.wait_for(
            client.call_tool(
                "get_lineage",
                {"urn": job_urn, "upstream": True, "max_hops": 1, "max_results": 100},
            ),
            timeout=CALL_TIMEOUT_S,
        )
        return {"tools": sorted(tools), "payload": result.data}


def fetch_declared_via_mcp(
    job_urn: str,
    gms: str | None = None,
    token: str | None = None,
) -> DeclaredLineage:
    """Read a dataJob's declared upstreams through DataHub's MCP Server."""
    try:
        out = asyncio.run(_fetch(job_urn, gms, token))
    except McpLineageError:
        raise
    except Exception as e:  # noqa: BLE001 - surfaced with context, never swallowed
        raise McpLineageError(
            f"Could not read lineage via DataHub's MCP Server: {type(e).__name__}: {e}\n"
            f"Check that `{SERVER_COMMAND}` is on PATH and that DataHub credentials are "
            "available (DATAHUB_GMS_URL / ~/.datahubenv from `datahub init`)."
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
