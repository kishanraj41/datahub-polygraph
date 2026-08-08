"""Catalog context, read through **DataHub's own MCP Server**.

`declared_mcp` reads a job's declared lineage through the MCP Server's
`get_lineage` tool. That tool resolves to GraphQL `searchAcrossLineage`, which
on a stack whose GMS and search engine disagree about point-in-time snapshots
returns a 500 -- see `docs/DATAHUB_MCP.md` and `scripts/probe_gms.ps1`.

This module deliberately uses the two tools that do **not** touch that resolver:

* ``get_entities`` -> GraphQL ``entities(urns:)``. A direct entity fetch. No
  search, no point-in-time.
* ``search``       -> GraphQL ``searchAcrossEntities``. Ordinary search, also no
  point-in-time.

What it is for
--------------
A Polygraph verdict says *whether* a declared edge held up. It says nothing
about who to talk to or what the asset is supposed to be. That context lives in
the catalog, and reading it through the MCP Server means Polygraph asks DataHub
the same way an agent would -- which is the interface the whole project is
arguing about.

The most useful case is an ``UNDECLARED`` verdict. Runtime proved the pipeline
reads some file; the obvious next question is whether that file is registered in
the catalog at all, and under what name. ``search`` answers it.

What it is NOT for
------------------
Nothing here produces or modifies a verdict. Catalog context is what the catalog
*claims*, and Polygraph's entire premise is that a claim is not evidence. Owners
and descriptions returned here are reported as the catalog's testimony, labelled
as such, and never merged into the evidence side of the ledger.

Cost note: every call launches the server as a subprocess, and the server
round-trips ``test_connection()`` before serving. Batch what you need into one
``fetch_catalog_context`` call rather than looping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import dh_mcp
from .dh_mcp import DataHubMcpError

ACTOR_URN_RE = re.compile(r"^urn:li:(corpuser|corpGroup):")
# The entity kinds this project reasons about. Everything else in a response --
# platforms, tags, glossary terms, domains -- is context noise here.
INTERESTING_KINDS = ("dataset", "dataJob", "dataFlow")
URN_KIND_RE = re.compile(r"^urn:li:([a-zA-Z]+):")

SEARCH_COUNT = 10


class CatalogContextError(DataHubMcpError):
    pass


# A GraphQL entity carrying only these keys is a URN echo, not a catalog record.
IDENTITY_ONLY_KEYS = {"urn", "type"}


def _carries_metadata(entity: dict) -> bool:
    """Does this response actually describe an asset, or just repeat its URN?

    DataHub's ``entities(urns:)`` resolver answers for **any syntactically valid
    URN**, registered or not. Ask it about an asset that has never existed and it
    returns a shell: the urn, a `type` derived from the urn's own text, and every
    other field null. Nothing in the response says "I have never heard of this".

    Verified against a live GMS: a fabricated dataset URN came back as
    ``{"urn": ..., "type": "DATASET"}`` alongside three real assets, and the
    first version of this module reported it as found.

    That matters more here than in most places. An agent asking DataHub "does
    this asset exist?" through the MCP Server gets yes for anything URN-shaped --
    a confident answer with no knowledge behind it, which is the exact failure
    Polygraph exists to complain about. Polygraph would be a poor advertisement
    for itself if it repeated it.

    LIMITATION, stated rather than hidden: an entity that is genuinely
    registered but carries no properties, ownership, tags or description at all
    is indistinguishable from a shell by this test, and will be reported as not
    found. Distinguishing them needs an existence check the MCP Server does not
    expose.
    """
    for key, value in entity.items():
        if key in IDENTITY_ONLY_KEYS:
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        return True
    return False


@dataclass
class AssetContext:
    """What the catalog says about one asset. Testimony, not evidence."""

    urn: str
    found: bool
    name: str | None = None
    description: str | None = None
    owners: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "urn": self.urn,
            "found": self.found,
            "name": self.name,
            "description": self.description,
            "owners": self.owners,
            "source": "mcp-server-datahub:get_entities",
            "note": (
                "The catalog's own claim about this asset. Polygraph does not verify "
                "ownership or descriptions -- only lineage."
            )
            if self.found
            else (
                "The catalog returned no metadata for this URN. DataHub answers "
                "entities(urns:) for any syntactically valid URN, so a response is not "
                "evidence the asset is registered -- this one carried nothing but the "
                "URN itself."
            ),
        }


# --------------------------------------------------------------------------
# structural extraction
#
# Every extractor below walks the response rather than indexing a known path.
# The MCP Server passes GraphQL results through `clean_gql_response` and
# `clean_get_entities_response`, whose output shape is an implementation detail
# with no stability guarantee. A hard-coded path breaks silently on a server
# upgrade and returns None, which reads as "this asset has no owner" -- a
# confident wrong answer. A walk either finds the value or finds nothing, and
# finding nothing is reported.
# --------------------------------------------------------------------------

def _walk(node: Any):
    """Yield every dict in an arbitrarily nested structure, outermost first."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _collect_actor_urns(node: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def visit(n: Any) -> None:
        if isinstance(n, str):
            if ACTOR_URN_RE.match(n) and n not in seen:
                seen.add(n)
                found.append(n)
        elif isinstance(n, dict):
            for v in n.values():
                visit(v)
        elif isinstance(n, list):
            for item in n:
                visit(item)

    visit(node)
    return found


def extract_owners(entity: Any) -> list[str]:
    """Owning actors, from the entity's ownership subtree.

    Scoped to the ``ownership`` subtree rather than the whole entity: a
    ``lastModified.actor`` or a ``created.actor`` is also a corpuser URN, and
    reporting the person who last edited the metadata as the *owner* would be a
    quiet, plausible lie.
    """
    for node in _walk(entity):
        if "ownership" in node and node["ownership"] is not None:
            return _collect_actor_urns(node["ownership"])
    return []


def _first_str(node: Any, keys: tuple[str, ...], prefer_under: str = "properties") -> str | None:
    """First non-empty string under one of ``keys``, preferring ``properties``.

    ``properties.name`` is the registered name; a bare top-level ``name`` may be
    a rendered display string. Prefer the former, fall back to the latter.
    """
    for node_dict in _walk(node):
        sub = node_dict.get(prefer_under)
        if isinstance(sub, dict):
            for key in keys:
                value = sub.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    for node_dict in _walk(node):
        for key in keys:
            value = node_dict.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _index_by_urn(payload: Any) -> dict[str, dict]:
    """Map urn -> the entity dict that carries it.

    ``get_entities`` returns a list for a list input and a bare dict for a
    single-urn input, and the payload may be wrapped. Indexing structurally
    handles all three without branching on the caller's argument shape.
    """
    index: dict[str, dict] = {}
    for node in _walk(payload):
        urn = node.get("urn")
        if isinstance(urn, str) and urn.startswith("urn:li:") and urn not in index:
            # Only treat it as an entity record if it carries more than the urn;
            # bare {"urn": ...} references appear inside ownership and lineage.
            if len(node) > 1:
                index[urn] = node
    return index


def _kind(urn: str) -> str | None:
    m = URN_KIND_RE.match(urn)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def fetch_catalog_context(
    urns: list[str],
    gms: str | None = None,
    token: str | None = None,
) -> dict[str, AssetContext]:
    """Read the catalog's description and owners for each URN, via MCP.

    Every requested URN comes back in the result, including ones the catalog
    does not know -- with ``found=False``. Dropping them would make a missing
    asset indistinguishable from an asset with no owner.
    """
    if not urns:
        return {}

    try:
        out = dh_mcp.call_tools([("get_entities", {"urns": list(urns)})], gms, token)
    except DataHubMcpError:
        raise
    except Exception as e:  # noqa: BLE001 - surfaced with context, never swallowed
        raise CatalogContextError(
            "Could not read catalog context via DataHub's MCP Server.\n"
            + dh_mcp.explain_failure(e)
        ) from e

    index = _index_by_urn(out["results"].get("get_entities"))

    context: dict[str, AssetContext] = {}
    for urn in urns:
        entity = index.get(urn)
        # Absent from the response and present-but-empty are the same finding:
        # the catalog told us nothing about this asset. See _carries_metadata.
        if entity is None or not _carries_metadata(entity):
            context[urn] = AssetContext(urn=urn, found=False, raw=entity or {})
            continue
        context[urn] = AssetContext(
            urn=urn,
            found=True,
            name=_first_str(entity, ("name", "qualifiedName")),
            description=_first_str(entity, ("description",)),
            owners=extract_owners(entity),
            raw=entity,
        )
    return context


def search_catalog(
    query: str,
    gms: str | None = None,
    token: str | None = None,
    count: int = SEARCH_COUNT,
) -> dict[str, Any]:
    """Search the catalog for registered assets matching ``query``, via MCP.

    The question this exists to answer: Polygraph reports an UNDECLARED source,
    proven read at runtime. Is that asset registered in the catalog at all, and
    under what name? A hit means the catalog knows the asset but not the edge --
    a lineage gap. A miss means the asset is invisible to the catalog entirely,
    which is a different and usually worse problem.
    """
    try:
        out = dh_mcp.call_tools(
            [("search", {"query": query, "num_results": count})], gms, token
        )
    except DataHubMcpError:
        raise
    except Exception as e:  # noqa: BLE001
        raise CatalogContextError(
            "Could not search the catalog via DataHub's MCP Server.\n"
            + dh_mcp.explain_failure(e)
        ) from e

    index = _index_by_urn(out["results"].get("search"))
    hits = [
        {
            "urn": urn,
            "kind": _kind(urn),
            "name": _first_str(entity, ("name", "qualifiedName")),
        }
        for urn, entity in index.items()
        if _kind(urn) in INTERESTING_KINDS
    ]

    return {
        "query": query,
        "source": "mcp-server-datahub:search",
        "count": len(hits),
        "hits": sorted(hits, key=lambda h: h["urn"]),
        "note": (
            "Catalog registrations only. A hit means DataHub knows the asset; it says "
            "nothing about whether any lineage edge to it is declared or real."
        ),
    }
