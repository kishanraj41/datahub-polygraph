"""The MCP server must never present absent evidence as a clean bill of health.

An agent asking "can I trust this?" and getting a confident-sounding answer
built on no evidence is worse than getting no answer. These tests pin the
distinction: every tool reports ``evidence_available`` and says so in prose when
it is false.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

fastmcp = pytest.importorskip("fastmcp", reason="fastmcp not installed")
from fastmcp import Client  # noqa: E402

from polygraph import mcp_server  # noqa: E402


def call(tool: str, args: dict | None = None):
    async def _run():
        async with Client(mcp_server.mcp) as c:
            return (await c.call_tool(tool, args or {})).data

    return asyncio.run(_run())


def test_all_tools_registered():
    async def _run():
        async with Client(mcp_server.mcp) as c:
            return {t.name for t in await c.list_tools()}

    names = asyncio.run(_run())
    assert names == {
        "can_i_trust",
        "get_integrity_score",
        "list_undeclared_sources",
        "list_phantom_edges",
        "get_incident_report",
        "explain_verdict_semantics",
    }


def test_every_tool_documents_itself():
    """An MCP tool with a thin description gets called wrongly by agents."""

    async def _run():
        async with Client(mcp_server.mcp) as c:
            return await c.list_tools()

    for t in asyncio.run(_run()):
        assert t.description and len(t.description) > 80, f"{t.name} needs a real description"


def test_undeclared_sources_names_the_shadow_input():
    r = call("list_undeclared_sources")
    assert r["evidence_available"] is True
    assert r["count"] == 1
    assert "fee_schedule" in r["edges"][0]["upstream"]
    assert r["edges"][0]["operations_observed"], "evidence must include the operations"
    assert "single captured run" in r["caveat"]


def test_phantom_edges_names_the_stale_edge():
    r = call("list_phantom_edges")
    assert r["evidence_available"] is True
    assert r["count"] == 1
    assert "legacy_claims_archive" in r["edges"][0]["upstream"]


def test_missing_report_is_reported_not_hidden(monkeypatch, tmp_path):
    """With no reconciliation report, the tool must say it has no evidence
    rather than returning an empty list that reads as 'nothing wrong'."""
    monkeypatch.setattr(mcp_server, "REPORT_PATH", tmp_path / "nope.json")
    r = call("list_undeclared_sources")
    assert r["evidence_available"] is False
    assert "polygraph reconcile" in r["answer"]
    assert "edges" not in r, "must not return an empty edge list that looks like a clean result"


def test_verdict_semantics_state_the_negatives():
    r = call("explain_verdict_semantics")
    for verdict in ("VERIFIED", "PHANTOM", "UNDECLARED"):
        assert "does_not_mean" in r[verdict], f"{verdict} must state what it does not establish"
    assert "conditional" in r["PHANTOM"]["does_not_mean"].lower()
    assert "single-run" in r["VERIFIED"]["does_not_mean"].lower()


def test_server_instructions_warn_against_overstating():
    text = mcp_server.mcp.instructions.lower()
    assert "one captured run" in text
    assert "evidence_available" in text
