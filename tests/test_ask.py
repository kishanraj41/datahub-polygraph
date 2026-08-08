"""`polygraph ask` must decline rather than guess, and must not fake an agent.

Two failure modes this pins:

1. A keyword router that answers the *nearest* intent when it does not
   understand the question. Answering a different question than the one asked,
   confidently, is worse than declining.
2. The LLM backend silently degrading to something else when no API key is
   present. If it cannot run, it says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polygraph import ask as ask_mod  # noqa: E402


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What undeclared sources does the pipeline read?", "undeclared"),
        ("show me the shadow inputs", "undeclared"),
        ("which declared edges are phantom?", "phantom"),
        ("are there any stale edges", "phantom"),
        ("what's the integrity score", "score"),
        ("how trustworthy is this catalog", "score"),
        ("what caused the incident", "incident"),
        ("why did f1 drop", "incident"),
        ("what do the verdicts mean", "semantics"),
        ("can I trust fee_schedule", "trust"),
    ],
)
def test_intent_classification(question: str, expected: str) -> None:
    assert ask_mod.classify(question) == expected


def test_unclassifiable_question_declines_rather_than_guessing() -> None:
    a = ask_mod.answer_deterministic("what is the capital of France")
    assert a.understood is False
    assert a.intent == "unrecognised"
    assert "will not guess" in a.text
    assert not a.tool_calls, "must not call a tool for a question it did not understand"
    # It must also be honest about what it is.
    assert "not an agent" in a.text


def test_router_is_not_described_as_an_agent() -> None:
    """The docstring and output must not claim agency the router does not have."""
    assert "not an agent" in ask_mod.__doc__
    a = ask_mod.answer_deterministic("nonsense question with no intent")
    assert "keyword router" in a.text


def test_asset_resolution_prefers_the_longest_alias() -> None:
    """'legacy_claims_archive' contains 'archive'; the specific name must win."""
    urn = ask_mod._extract_urn("can I trust legacy_claims_archive")
    assert "legacy_claims_archive" in urn


def test_explicit_urn_in_question_is_used_verbatim() -> None:
    urn = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.raw_claims,PROD)"
    assert ask_mod._extract_urn(f"can I trust {urn}?") == urn


def test_trust_question_without_an_asset_asks_rather_than_assumes() -> None:
    a = ask_mod.answer_deterministic("can I trust it")
    assert a.understood is False
    assert "which asset" in a.text.lower()


def test_llm_backend_without_key_says_so(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = ask_mod.answer_llm("can I trust the fraud job")
    assert a.understood is False
    assert "ANTHROPIC_API_KEY is not set" in a.text
    assert not a.tool_calls
    # And it must point at the credential-free paths, since the README's claims
    # all reproduce without a key.
    assert "without it" in a.text


def test_llm_system_prompt_forbids_overstating() -> None:
    s = ask_mod.SYSTEM
    assert "ONE captured run" in s
    assert "evidence_available=false" in s
    assert "only from tool results" in s.lower()


POLYGRAPH_TOOLS = {
    "can_i_trust", "get_integrity_score", "list_undeclared_sources",
    "list_phantom_edges", "get_incident_report", "explain_verdict_semantics",
}
# Tools in the agent loop that proxy to DataHub's MCP Server. They return the
# catalog's testimony, not Polygraph's evidence.
DATAHUB_PROXY_TOOLS = {"datahub_get_entities", "datahub_search"}


def test_both_backends_share_one_tool_implementation() -> None:
    """Two implementations of 'can I trust this' would drift. That is the whole
    failure mode Polygraph exists to catch, so it must not exist internally."""
    from polygraph import mcp_server, tools

    assert ask_mod.TOOL_FUNCS["can_i_trust"] is tools.can_i_trust
    assert mcp_server.tools is tools

    for name in POLYGRAPH_TOOLS:
        assert ask_mod.TOOL_FUNCS[name] is getattr(tools, name), (
            f"{name} in the agent loop is not the same object as polygraph.tools.{name}"
        )

    assert set(ask_mod.TOOL_FUNCS) == POLYGRAPH_TOOLS | DATAHUB_PROXY_TOOLS


def test_polygraph_does_not_readvertise_datahubs_tools() -> None:
    """The agent loop may hold tools from both servers. Polygraph's OWN MCP server
    must not: re-exporting another server's tools under Polygraph's name would
    make catalog testimony look like Polygraph evidence to any client that reads
    the tool list."""
    import asyncio

    from fastmcp import Client

    from polygraph import mcp_server

    async def _run() -> set[str]:
        async with Client(mcp_server.mcp) as c:
            return {t.name for t in await c.list_tools()}

    advertised = asyncio.run(_run())
    assert advertised == POLYGRAPH_TOOLS
    assert not (advertised & DATAHUB_PROXY_TOOLS)


def test_every_schema_has_an_implementation() -> None:
    """A schema without a function is a tool the model can call into a hole."""
    schema_names = {s["name"] for s in ask_mod.TOOL_SCHEMAS}
    assert schema_names == set(ask_mod.TOOL_FUNCS)
    for s in ask_mod.TOOL_SCHEMAS:
        assert s["description"], f"{s['name']} has no description for the model to read"


def test_system_prompt_separates_evidence_from_testimony() -> None:
    """The two tool families answer different kinds of question. If the model is
    allowed to blur them, a catalog description can end up standing in for proof
    about what ran -- which is the exact confusion Polygraph exists to expose."""
    s = ask_mod.SYSTEM
    assert "EVIDENCE" in s and "TESTIMONY" in s
    assert "the catalog says" in s
    assert "Polygraph observed" in s
