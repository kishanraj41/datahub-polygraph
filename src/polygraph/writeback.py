"""Write verdicts back into DataHub as tags, and the report as a document.

Two correctness rules govern everything here.

**Merge, never clobber.** ``GlobalTagsClass`` is a whole-aspect write: emitting
it replaces every tag on the asset. Naively writing ``[polygraph:phantom]``
would silently delete a team's PII or tier tags. So every write reads the
existing aspect first and unions.

**Polygraph owns its own namespace and nothing else.** On each run, previously
applied ``polygraph:*`` tags are removed before the new ones are added, so an
edge whose verdict changes from PHANTOM to VERIFIED does not end up carrying
both. Tags outside the ``polygraph:`` prefix are never touched.

Which asset gets the tag: the verdict is a statement about an *edge*, but tags
attach to entities. Polygraph tags the **upstream dataset**, because that is the
asset whose trustworthiness the verdict describes and the one a person browsing
the catalog would be looking at.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph
from datahub.metadata.schema_classes import (
    GlobalTagsClass,
    TagAssociationClass,
    TagPropertiesClass,
)

from .reconcile import TAG_FOR_VERDICT

TAG_PREFIX = "polygraph:"
INCIDENT_TAG = "polygraph:incident"


def tag_urn(name: str) -> str:
    return f"urn:li:tag:{name}"


@dataclass
class WriteResult:
    urn: str
    added: list[str]
    removed: list[str]
    preserved: list[str]


def _current_tags(graph: DataHubGraph, urn: str) -> list[str]:
    aspect = graph.get_aspect(urn, GlobalTagsClass)
    if not aspect or not aspect.tags:
        return []
    return [t.tag for t in aspect.tags]


def apply_tags(
    graph: DataHubGraph,
    urn: str,
    polygraph_tags: Iterable[str],
    dry_run: bool = False,
) -> WriteResult:
    """Set this asset's ``polygraph:*`` tags, leaving every other tag alone."""
    wanted = {tag_urn(t) for t in polygraph_tags}
    existing = _current_tags(graph, urn)

    # Anything outside the polygraph namespace is preserved untouched.
    preserved = [t for t in existing if not t.startswith(f"urn:li:tag:{TAG_PREFIX}")]
    stale = [t for t in existing if t.startswith(f"urn:li:tag:{TAG_PREFIX}") and t not in wanted]
    added = [t for t in wanted if t not in existing]

    final = sorted(set(preserved) | wanted)

    if not dry_run:
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=t) for t in final]),
            )
        )

    return WriteResult(urn=urn, added=sorted(added), removed=sorted(stale), preserved=sorted(preserved))


def ensure_tag_entities(graph: DataHubGraph, descriptions: dict[str, str]) -> None:
    """Create tag entities so they render with a description, not as bare strings."""
    for name, desc in descriptions.items():
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=tag_urn(name),
                aspect=TagPropertiesClass(name=name, description=desc),
            )
        )


def tags_for_report(report: dict[str, Any]) -> dict[str, set[str]]:
    """Map each asset URN to the polygraph tags its verdicts imply.

    An asset can legitimately carry more than one verdict if it feeds several
    jobs, so this accumulates rather than overwrites.
    """
    per_asset: dict[str, set[str]] = {}
    for v in report["verdicts"]:
        tag = TAG_FOR_VERDICT.get(v["verdict"])
        if not tag:
            continue
        per_asset.setdefault(v["upstream"], set()).add(tag)
    return per_asset


def write_verdicts(
    graph: DataHubGraph, report: dict[str, Any], dry_run: bool = False
) -> list[WriteResult]:
    results = []
    for urn, tags in sorted(tags_for_report(report).items()):
        results.append(apply_tags(graph, urn, tags, dry_run=dry_run))
    if not dry_run:
        graph.flush()
    return results


def verify_written(graph: DataHubGraph, report: dict[str, Any]) -> dict[str, Any]:
    """Read every tagged asset back. A write that did not land is a failed gate."""
    expected = tags_for_report(report)
    problems = []
    confirmed = {}
    for urn, tags in sorted(expected.items()):
        actual = set(_current_tags(graph, urn))
        want = {tag_urn(t) for t in tags}
        missing = sorted(want - actual)
        if missing:
            problems.append({"urn": urn, "missing": missing, "actual": sorted(actual)})
        else:
            confirmed[urn] = sorted(want)
    return {"confirmed": confirmed, "problems": problems, "ok": not problems}


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def publish_document(
    graph: DataHubGraph,
    doc_id: str,
    title: str,
    markdown: str,
    related_asset_urns: Iterable[str] = (),
    custom_properties: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Publish a report into DataHub's knowledge base.

    Uses the SDK's Document entity, which is the same surface the MCP server's
    ``save_document`` tool writes through. The sha-256 of the exact markdown is
    stored as a custom property so the document in the catalog can be checked
    against the file in ``examples/`` byte for byte.
    """
    from datahub.sdk.document import Document

    digest = sha256_of(markdown)
    props = {k: str(v) for k, v in (custom_properties or {}).items()}
    props["polygraph_sha256"] = digest

    doc = Document.create_document(
        id=doc_id,
        title=title,
        text=markdown,
        status="PUBLISHED",
        related_assets=list(related_asset_urns) or None,
        custom_properties=props,
    )

    for mcp in doc.as_mcps():
        graph.emit_mcp(mcp)
    graph.flush()

    return {"urn": str(doc.urn), "sha256": digest, "related_assets": list(related_asset_urns)}
