"""Gate 1 smoke test: prove a mutation round-trip against the running DataHub.

Polygraph's entire write-back path depends on three things being true of the
*running* server, not of the docs:

1. GMS is reachable and reports an OSS version >= 1.4.0 (below that, the MCP
   server hides ``add_tags`` / ``save_document`` -- see
   ``mcp_server_datahub/version_requirements.py``).
2. A dataset can be created via the SDK.
3. A tag can be applied to it and read back.

If any of these fail, nothing downstream is worth building. Exit code is 0 only
when all three pass.
"""

from __future__ import annotations

import json
import sys
import traceback

GMS = "http://localhost:8080"
TEST_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.smoke_test,PROD)"
)
TEST_TAG = "polygraph:smoke-test"

results: dict[str, object] = {}


def fail(step: str, err: object) -> None:
    results[step] = {"ok": False, "error": str(err)[:400]}
    print(json.dumps(results, indent=2))
    print("\nGATE 1: FAIL", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    # --- 1. connect + version ------------------------------------------------
    try:
        from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

        graph = DataHubGraph(DatahubClientConfig(server=GMS))
        cfg = graph.server_config
        is_cloud = bool(getattr(cfg, "is_datahub_cloud", False))
        parsed = getattr(cfg, "parsed_version", None)
        results["connect"] = {
            "ok": True,
            "gms": GMS,
            "is_cloud": is_cloud,
            "parsed_version": list(parsed) if parsed else None,
        }
        # The MCP mutation tools require OSS >= 1.4.0.
        if not is_cloud and parsed and tuple(parsed[:3]) < (1, 4, 0):
            fail(
                "version_gate",
                f"OSS {parsed} is below 1.4.0; add_tags/save_document will be hidden "
                "by the MCP server's version filter.",
            )
        results["version_gate"] = {"ok": True}
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        fail("connect", e)

    # --- 2. create a dataset -------------------------------------------------
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import DatasetPropertiesClass

        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=TEST_URN,
                aspect=DatasetPropertiesClass(
                    name="polygraph.smoke_test",
                    description="Created by Polygraph gate 1. Safe to delete.",
                ),
            )
        )
        graph.flush()
        results["create_dataset"] = {"ok": True, "urn": TEST_URN}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        fail("create_dataset", e)

    # --- 3. tag round-trip ---------------------------------------------------
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.metadata.schema_classes import (
            GlobalTagsClass,
            TagAssociationClass,
            TagPropertiesClass,
        )

        tag_urn = f"urn:li:tag:{TEST_TAG}"
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=tag_urn,
                aspect=TagPropertiesClass(name=TEST_TAG, description="Polygraph gate 1 probe."),
            )
        )
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=TEST_URN,
                aspect=GlobalTagsClass(tags=[TagAssociationClass(tag=tag_urn)]),
            )
        )
        graph.flush()

        read_back = graph.get_tags(TEST_URN)
        applied = [t.tag for t in (read_back.tags if read_back else [])]
        if tag_urn not in applied:
            fail("tag_roundtrip", f"tag not found after write; got {applied}")
        results["tag_roundtrip"] = {"ok": True, "tags": applied}
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        fail("tag_roundtrip", e)

    print(json.dumps(results, indent=2))
    print(f"\nGATE 1: PASS")
    print(f"Check the UI:  http://localhost:9002/dataset/{TEST_URN}")


if __name__ == "__main__":
    main()
