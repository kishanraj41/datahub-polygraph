"""Catalog context must not quietly invent facts about who owns what.

`catalog_mcp` reads DataHub through its MCP Server and hands the result to an
LLM. Two failure modes matter more than the rest:

* Naming the wrong person as owner. A DataHub entity carries several corpuser
  URNs -- `ownership.owners[].owner`, but also `lastModified.actor` and
  `created.actor`. Collecting them indiscriminately would report the person who
  last edited the metadata as the asset's owner. Plausible, confident, wrong.

* Dropping URNs the catalog does not know. If a missing asset simply vanishes
  from the result, "this asset has no owner" and "this asset does not exist"
  become the same answer.

Everything here is hermetic: the transport is patched, so these run in CI with
no DataHub.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polygraph import catalog_mcp  # noqa: E402
from polygraph.catalog_mcp import (  # noqa: E402
    CatalogContextError,
    _index_by_urn,
    extract_owners,
    fetch_catalog_context,
    search_catalog,
)

RAW = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.raw_claims,PROD)"
FEE = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.fee_schedule,PROD)"
GHOST = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.not_registered,PROD)"
JOB = "urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"
TEAM = "urn:li:corpGroup:ml-platform-team"
EDITOR = "urn:li:corpuser:datahub"


def _patch(monkeypatch, results: dict) -> list:
    """Replace the transport. Returns a list that records the calls made."""
    recorded: list = []

    def fake(calls, gms=None, token=None):
        recorded.append((calls, gms, token))
        return {"tools": ["get_entities", "search", "get_lineage"], "results": results}

    monkeypatch.setattr(catalog_mcp.dh_mcp, "call_tools", fake)
    return recorded


# --------------------------------------------------------------------- owners

def test_owner_extraction_ignores_the_last_editor():
    """The whole reason extraction is scoped to the ownership subtree."""
    entity = {
        "urn": RAW,
        "ownership": {"owners": [{"owner": {"urn": TEAM}}]},
        "lastModified": {"actor": EDITOR},
        "created": {"actor": EDITOR},
    }
    assert extract_owners(entity) == [TEAM]


def test_owner_extraction_survives_a_flattened_shape():
    """The MCP server cleans responses; the nesting is not a stable contract."""
    assert extract_owners({"urn": RAW, "ownership": [TEAM]}) == [TEAM]
    assert extract_owners({"urn": RAW, "ownership": {"owners": [TEAM]}}) == [TEAM]


def test_no_ownership_means_no_owners_not_a_guess():
    assert extract_owners({"urn": RAW, "lastModified": {"actor": EDITOR}}) == []
    assert extract_owners({"urn": RAW, "ownership": None}) == []


# ---------------------------------------------------------------- indexing

def test_bare_urn_references_are_not_treated_as_entities():
    """`{"urn": ...}` appears inside ownership and lineage payloads. Treating one
    as an entity record would produce an asset with a name of None and no
    owners, indistinguishable from a real but undocumented asset."""
    payload = [
        {"urn": RAW, "properties": {"name": "raw_claims"}},
        {"urn": JOB},  # bare reference
    ]
    index = _index_by_urn(payload)
    assert RAW in index
    assert JOB not in index


# ------------------------------------------------------------------ context

def test_missing_urns_come_back_marked_missing(monkeypatch):
    _patch(monkeypatch, {"get_entities": [{"urn": RAW, "properties": {"name": "raw_claims"}}]})

    ctx = fetch_catalog_context([RAW, GHOST])

    assert set(ctx) == {RAW, GHOST}, "a URN the catalog does not know must not vanish"
    assert ctx[RAW].found is True
    assert ctx[GHOST].found is False
    assert ctx[GHOST].owners == []


def test_context_prefers_registered_name_over_display_name(monkeypatch):
    _patch(monkeypatch, {
        "get_entities": [{
            "urn": RAW,
            "name": "Raw Claims (display)",
            "properties": {"name": "polygraph.demo.raw_claims", "description": "Raw claims."},
            "ownership": {"owners": [{"owner": {"urn": TEAM}}]},
        }]
    })

    ctx = fetch_catalog_context([RAW])[RAW]
    assert ctx.name == "polygraph.demo.raw_claims"
    assert ctx.description == "Raw claims."
    assert ctx.owners == [TEAM]


def test_single_entity_response_is_handled(monkeypatch):
    """get_entities returns a bare dict for a single-urn input, a list for many."""
    _patch(monkeypatch, {"get_entities": {"urn": RAW, "properties": {"name": "raw_claims"}}})
    assert fetch_catalog_context([RAW])[RAW].found is True


def test_empty_input_makes_no_call(monkeypatch):
    recorded = _patch(monkeypatch, {})
    assert fetch_catalog_context([]) == {}
    assert recorded == [], "an empty lookup must not launch the server"


def test_to_dict_labels_the_source_as_testimony(monkeypatch):
    _patch(monkeypatch, {"get_entities": [{"urn": RAW, "properties": {"name": "raw"}}]})
    d = fetch_catalog_context([RAW])[RAW].to_dict()
    assert d["source"] == "mcp-server-datahub:get_entities"
    assert "does not verify" in d["note"], (
        "catalog claims handed to an LLM must carry their own disclaimer"
    )


# ------------------------------------------------------------------- search

def test_search_keeps_assets_and_drops_platform_noise(monkeypatch):
    _patch(monkeypatch, {"search": {"searchResults": [
        {"entity": {"urn": FEE, "properties": {"name": "fee_schedule"}}},
        {"entity": {"urn": JOB, "properties": {"name": "train_fraud_model"}}},
        {"entity": {"urn": "urn:li:dataPlatform:file", "name": "File"}},
        {"entity": {"urn": "urn:li:tag:polygraph:phantom", "name": "phantom"}},
    ]}})

    out = search_catalog("fee")
    urns = {h["urn"] for h in out["hits"]}
    assert urns == {FEE, JOB}
    assert out["count"] == 2
    assert out["source"] == "mcp-server-datahub:search"


def test_search_passes_num_results_through(monkeypatch):
    recorded = _patch(monkeypatch, {"search": {"searchResults": []}})
    search_catalog("fee", count=25)
    (calls, _gms, _token) = recorded[0]
    assert calls == [("search", {"query": "fee", "num_results": 25})]


# -------------------------------------------------------------------- errors

def test_transport_failure_is_wrapped_with_context(monkeypatch):
    """`Connection closed` reaches here only after the preflight found GMS
    healthy, so the message must send the reader to the child's traceback rather
    than back to 'is DataHub running'."""
    def boom(*a, **k):
        raise ConnectionError("Connection closed")

    monkeypatch.setattr(catalog_mcp.dh_mcp, "call_tools", boom)

    with pytest.raises(CatalogContextError) as exc:
        fetch_catalog_context([RAW])

    msg = str(exc.value)
    assert "MCP Server" in msg
    assert "NOT simple unreachability" in msg
    assert "Resolved GMS" in msg, "show what it actually tried to talk to"


def test_missing_credentials_are_named_as_such(monkeypatch):
    """The Gate 10a red. MissingConfigError must not be reported as a generic
    transport fault."""
    def boom(*a, **k):
        raise RuntimeError("MissingConfigError: No ~/.datahubenv file found")

    monkeypatch.setattr(catalog_mcp.dh_mcp, "call_tools", boom)

    with pytest.raises(CatalogContextError, match="credentials"):
        fetch_catalog_context([RAW])


def test_dead_search_backend_is_distinguished_from_a_config_problem(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("java.lang.RuntimeException: Failed to execute search: ...")

    monkeypatch.setattr(catalog_mcp.dh_mcp, "call_tools", boom)

    with pytest.raises(CatalogContextError, match="search backend"):
        search_catalog("anything")


def test_point_in_time_failure_names_the_probe_script(monkeypatch):
    """The failure that red-gated the lineage path must point at the diagnosis,
    not leave the reader with a bare GraphQL 500."""
    def boom(*a, **k):
        raise RuntimeError("Failed to generate PointInTime Identifier.. Root cause: search")

    monkeypatch.setattr(catalog_mcp.dh_mcp, "call_tools", boom)

    with pytest.raises(CatalogContextError, match="probe_gms"):
        search_catalog("anything")
