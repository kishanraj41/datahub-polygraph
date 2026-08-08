"""Transport for talking to **DataHub's own MCP Server** over stdio.

`declared_mcp` and `catalog_mcp` both launch `mcp-server-datahub` as a
subprocess and call tools on it. Everything about *how* that subprocess is
found, configured and spoken to lives here, once.

Two copies of "how do we launch DataHub's MCP Server" would drift, and one of
them would keep working while the other silently did not. Polygraph exists to
complain about exactly that class of divergence; it should not ship one.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shlex
import shutil
import sys
from typing import Any

SERVER_MODULE = "mcp_server_datahub"
SERVER_SCRIPT = "mcp-server-datahub"
CALL_TIMEOUT_S = 60


class DataHubMcpError(RuntimeError):
    """Anything that went wrong reaching DataHub through its MCP Server."""


def resolve_server_command() -> list[str]:
    """Build the argv that launches DataHub's MCP Server.

    Launching by bare console-script name fails on Windows: ``CreateProcess``
    does not search PATH the way a shell does, and a venv's ``Scripts``
    directory is not on the subprocess PATH, so ``mcp-server-datahub`` raises
    ``[WinError 2] The system cannot find the file specified``.

    Running it as a module through the *current* interpreter avoids the problem
    entirely and additionally guarantees the server runs in the same virtualenv
    as Polygraph -- so it sees the same DataHub credentials and the same pinned
    ``acryl-datahub``. The console script is only a fallback, and
    ``POLYGRAPH_MCP_SERVER_CMD`` overrides both.
    """
    override = os.environ.get("POLYGRAPH_MCP_SERVER_CMD")
    if override:
        return shlex.split(override, posix=(os.name != "nt"))

    if importlib.util.find_spec(SERVER_MODULE) is not None:
        return [sys.executable, "-m", SERVER_MODULE]

    script = shutil.which(SERVER_SCRIPT)
    if script:
        return [script]

    raise DataHubMcpError(
        f"DataHub's MCP Server is not available. `{SERVER_MODULE}` is not importable "
        f"by {sys.executable} and `{SERVER_SCRIPT}` is not on PATH.\n"
        "Install it into the same environment as Polygraph:\n"
        "    pip install -r requirements.txt\n"
        "Or set POLYGRAPH_MCP_SERVER_CMD to an explicit command."
    )


def server_env(gms: str | None, token: str | None) -> dict[str, str]:
    env = dict(os.environ)
    if gms:
        env["DATAHUB_GMS_URL"] = gms
    if token:
        env["DATAHUB_GMS_TOKEN"] = token
    # The server logs to stderr at INFO; keep stdout clean for the protocol.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    # The server phones home to Mixpanel on startup. Polygraph spawns it on
    # every call, so that is repeated latency and a repeated network dependency
    # for a step that should be local and deterministic.
    env.setdefault("DATAHUB_TELEMETRY_ENABLED", "false")
    return env


async def _session_call(
    calls: list[tuple[str, dict[str, Any]]],
    gms: str | None,
    token: str | None,
) -> dict[str, Any]:
    """Run every call in ``calls`` inside ONE server session.

    Startup dominates the cost -- the server constructs a DataHub client and
    round-trips ``test_connection()`` before it will serve anything. Batching
    into a single session turns N startups into one.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    argv = resolve_server_command()
    transport = StdioTransport(
        command=argv[0],
        args=argv[1:] + ["--transport", "stdio"],
        env=server_env(gms, token),
    )

    async with Client(transport) as client:
        advertised = sorted(t.name for t in await client.list_tools())

        missing = sorted({name for name, _ in calls} - set(advertised))
        if missing:
            raise DataHubMcpError(
                f"DataHub's MCP Server did not advertise: {missing}.\n"
                f"Advertised: {advertised}.\n"
                "Read tools are version-gated -- see "
                "mcp_server_datahub/version_requirements.py. Check the GMS version."
            )

        results: dict[str, Any] = {}
        for name, args in calls:
            out = await asyncio.wait_for(
                client.call_tool(name, args), timeout=CALL_TIMEOUT_S
            )
            results[name] = out.data
        return {"tools": advertised, "results": results}


def call_tools(
    calls: list[tuple[str, dict[str, Any]]],
    gms: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Sync wrapper. Exists as a patch point so tests do not have to stub
    ``asyncio.run``, which leaves an un-awaited coroutine and a RuntimeWarning
    in the output."""
    return asyncio.run(_session_call(calls, gms, token))


def explain_failure(e: Exception) -> str:
    """Turn a transport exception into something a human can act on."""
    hint = ""
    text = str(e)
    if "Connection closed" in text or "failed to connect" in text.lower():
        hint = (
            "\nThe server process exited during startup. It calls "
            "DataHubClient.from_env() -> test_connection() before serving, so the "
            "usual cause is that GMS is unreachable, not a protocol problem. "
            "Confirm http://localhost:8080/config answers.\n"
        )
    elif "PointInTime" in text:
        hint = (
            "\nGMS failed to create a point-in-time snapshot. This is a server-side "
            "mismatch between GMS's search dialect and the running search engine, not "
            "a fault in this client. Diagnose with scripts/probe_gms.ps1.\n"
        )
    return (
        f"{type(e).__name__}: {e}\n"
        f"{hint}"
        f"Server command: {' '.join(resolve_server_command())}\n"
        "Check DataHub credentials are available (DATAHUB_GMS_URL / ~/.datahubenv "
        "from `datahub init`)."
    )
