"""The MCP lineage reader must never turn a parsing failure into a finding.

Polygraph reports `PHANTOM` when a declared edge shows no runtime flow. If the
declared side comes back empty because a response shape changed, every declared
edge becomes PHANTOM and Polygraph confidently reports a catalog full of stale
lineage that is in fact correct. That is the worst failure this tool can have:
wrong, loud, and plausible.

So an empty upstream set is an error, not a result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polygraph.declared_mcp import (  # noqa: E402
    McpLineageError,
    _extract_dataset_urns,
    fetch_declared_via_mcp,
)

RAW = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.raw_claims,PROD)"
ARCHIVE = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.legacy_claims_archive,PROD)"
JOB = "urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"


def test_extracts_urns_from_realistic_nesting():
    """The shape searchAcrossLineage actually returns."""
    payload = {
        "upstreams": {
            "searchResults": [
                {"entity": {"urn": RAW, "type": "DATASET", "properties": {"name": "raw"}}},
                {"entity": {"urn": ARCHIVE, "type": "DATASET"}},
            ],
            "total": 2,
        }
    }
    assert _extract_dataset_urns(payload) == sorted([RAW, ARCHIVE])


def test_extraction_survives_a_different_nesting():
    """A server upgrade that moves the URNs must not silently yield nothing."""
    payload = {"data": {"results": [{"node": {"entity": {"urn": RAW}}}]}}
    assert _extract_dataset_urns(payload) == [RAW]


def test_ignores_non_dataset_urns():
    """Tags, jobs and platforms appear in the same payload and are not upstreams."""
    payload = {
        "searchResults": [
            {"entity": {"urn": RAW}},
            {"entity": {"urn": JOB}},
            {"entity": {"urn": "urn:li:tag:polygraph:verified"}},
            {"entity": {"urn": "urn:li:dataPlatform:file"}},
            {"entity": {"urn": "urn:li:corpGroup:ml-platform-team"}},
        ]
    }
    assert _extract_dataset_urns(payload) == [RAW]


def test_deduplicates():
    payload = {"a": {"urn": RAW}, "b": {"urn": RAW}, "c": [{"urn": RAW}]}
    assert _extract_dataset_urns(payload) == [RAW]


def test_empty_payload_yields_nothing():
    assert _extract_dataset_urns({}) == []
    assert _extract_dataset_urns({"upstreams": {"searchResults": []}}) == []


def test_empty_upstreams_raises_rather_than_reporting_all_phantom(monkeypatch):
    """The whole point. No upstreams must be an error, not a verdict."""
    import polygraph.declared_mcp as mod

    monkeypatch.setattr(
        mod, "_fetch", lambda *a, **k: None
    )  # not used; asyncio.run is patched below
    monkeypatch.setattr(
        mod.asyncio, "run", lambda coro: {"tools": ["get_lineage"], "payload": {"upstreams": {}}}
    )

    with pytest.raises(McpLineageError) as exc:
        fetch_declared_via_mcp(JOB)

    msg = str(exc.value)
    assert "no upstream datasets" in msg
    assert "PHANTOM" in msg, "the error must explain why an empty set is not a finding"


def test_successful_read_builds_declared_edges(monkeypatch):
    import polygraph.declared_mcp as mod

    payload = {"upstreams": {"searchResults": [
        {"entity": {"urn": RAW}}, {"entity": {"urn": ARCHIVE}}]}}
    monkeypatch.setattr(
        mod.asyncio, "run", lambda coro: {"tools": ["get_lineage"], "payload": payload}
    )

    lineage = fetch_declared_via_mcp(JOB)
    assert {e.upstream for e in lineage.edges} == {RAW, ARCHIVE}
    assert all(e.downstream == JOB for e in lineage.edges)
    assert all(e.via == "mcp-server-datahub:get_lineage" for e in lineage.edges)
    assert lineage.raw["source"] == "mcp-server-datahub"


def test_missing_tool_is_reported_with_the_advertised_list(monkeypatch):
    """A version-gated server that hides get_lineage must say so plainly."""
    import polygraph.declared_mcp as mod

    def boom(coro):
        raise mod.McpLineageError(
            "DataHub's MCP Server did not advertise a `get_lineage` tool. "
            "Advertised: ['search']. Check the GMS version"
        )

    monkeypatch.setattr(mod.asyncio, "run", boom)
    with pytest.raises(McpLineageError, match="did not advertise"):
        fetch_declared_via_mcp(JOB)
