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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SERVER_MODULE = "mcp_server_datahub"
SERVER_SCRIPT = "mcp-server-datahub"
CALL_TIMEOUT_S = 60
DEFAULT_GMS = "http://localhost:8080"


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


def resolve_gms(gms: str | None = None, token: str | None = None) -> tuple[str, str | None]:
    """Work out which GMS the server should talk to, and with what token.

    The server calls ``DataHubClient.from_env()`` before it will serve anything.
    Left to its own devices that means ``~/.datahubenv``, which ``datahub init``
    writes -- and which is simply absent on a machine where nobody ran it, or
    where HOME differs between the shell that ran it and the one launching the
    server. The failure then arrives as ``McpError: Connection closed``, several
    layers away from the cause.

    So Polygraph resolves it and passes it explicitly, every time: caller
    argument, then environment, then ``~/.datahubenv``, then the quickstart
    default. Never left implicit.
    """
    if gms:
        return gms.rstrip("/"), token or os.environ.get("DATAHUB_GMS_TOKEN") or None

    env_url = os.environ.get("DATAHUB_GMS_URL")
    env_token = token or os.environ.get("DATAHUB_GMS_TOKEN") or None
    if env_url:
        return env_url.rstrip("/"), env_token

    # Parsed leniently: a malformed line should degrade to "use the default",
    # not crash a caller that only wanted to read some owners.
    config = Path.home() / ".datahubenv"
    file_url = file_token = None
    if config.exists():
        try:
            for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if stripped.startswith("server:") and not file_url:
                    file_url = stripped.split(":", 1)[1].strip().strip("'\"") or None
                elif stripped.startswith("token:") and not file_token:
                    file_token = stripped.split(":", 1)[1].strip().strip("'\"") or None
        except OSError:
            pass

    return (file_url or DEFAULT_GMS).rstrip("/"), env_token or file_token


def server_env(gms: str | None, token: str | None) -> dict[str, str]:
    env = dict(os.environ)
    resolved_gms, resolved_token = resolve_gms(gms, token)
    # Always set, never conditionally: an unset DATAHUB_GMS_URL is what makes
    # the server die on MissingConfigError during startup.
    env["DATAHUB_GMS_URL"] = resolved_gms
    if resolved_token:
        env["DATAHUB_GMS_TOKEN"] = resolved_token
    # The server logs to stderr at INFO; keep stdout clean for the protocol.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    # The server phones home to Mixpanel on startup. Polygraph spawns it on
    # every call, so that is repeated latency and a repeated network dependency
    # for a step that should be local and deterministic.
    env.setdefault("DATAHUB_TELEMETRY_ENABLED", "false")
    return env


def preflight(gms_url: str) -> None:
    """Confirm GMS answers before launching the server subprocess.

    Without this, an unreachable or misconfigured GMS surfaces as
    ``McpError: Connection closed`` -- the stdio transport noticing that the
    child died, with the child's real traceback buried in stderr. Checking
    first turns a protocol-shaped error back into the infrastructure-shaped
    error it actually is.
    """
    try:
        with urllib.request.urlopen(f"{gms_url}/config", timeout=10) as response:
            if response.status != 200:
                raise DataHubMcpError(
                    f"GMS at {gms_url}/config answered HTTP {response.status}. "
                    "Expected 200. DataHub is up but not healthy."
                )
    except urllib.error.URLError as e:
        raise DataHubMcpError(
            f"GMS is not answering at {gms_url}/config ({e.reason}).\n"
            "DataHub's MCP Server calls DataHubClient.from_env() -> test_connection() "
            "before it will serve a single tool, so it cannot start without this.\n"
            "Start the stack with scripts\\run_gate1.ps1, or pass --gms."
        ) from e


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

    env = server_env(gms, token)
    preflight(env["DATAHUB_GMS_URL"])

    argv = resolve_server_command()
    transport = StdioTransport(
        command=argv[0],
        args=argv[1:] + ["--transport", "stdio"],
        env=env,
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
    if "MissingConfigError" in text or "datahubenv" in text:
        hint = (
            "\nThe server could not find DataHub credentials. Polygraph sets "
            "DATAHUB_GMS_URL on the subprocess explicitly, so seeing this means the "
            "resolved URL was wrong rather than absent. Check `polygraph ... --gms`.\n"
        )
    elif "Connection closed" in text or "failed to connect" in text.lower():
        hint = (
            "\nThe server process exited during startup, before serving anything. GMS "
            "answered the preflight check, so this is NOT simple unreachability -- read "
            "the child's traceback above the fold. Common causes: a broken search "
            "backend, or a version mismatch between acryl-datahub and GMS.\n"
        )
    elif "PointInTime" in text:
        hint = (
            "\nGMS failed to create a point-in-time snapshot for a graph query. That "
            "is server-side. Diagnose with scripts/probe_gms.ps1 -- if plain search "
            "also fails, the search backend is down rather than misconfigured.\n"
        )
    elif "Failed to execute search" in text:
        hint = (
            "\nGMS could not reach its search backend at all. Check that the "
            "OpenSearch/Elasticsearch container is running: scripts/stack_status.ps1.\n"
        )
    resolved_gms, _ = resolve_gms()
    return (
        f"{type(e).__name__}: {e}\n"
        f"{hint}"
        f"Server command: {' '.join(resolve_server_command())}\n"
        f"Resolved GMS:   {resolved_gms}"
    )
