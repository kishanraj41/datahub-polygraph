"""Gate 10a: prove Polygraph really reaches DataHub through DataHub's MCP Server.

The hackathon requires the open-source platform *together with* at least one of
the MCP Server, Agent Context Kit, DataHub Skills or the Analytics Agent.
Polygraph ships its own MCP server, which is not the same thing. This gate is
the evidence that `mcp-server-datahub` is on a real code path.

It asserts against facts the seeder established, not against "the call returned
something":

* the training job's owner comes back as the group demo/seed_catalog.py set
* `fee_schedule` -- the undeclared source -- is findable by catalog search
* a URN that does not exist comes back marked missing rather than dropped

Deliberately uses only `get_entities` and `search`. Neither touches
`searchAcrossLineage`, so this gate is independent of the point-in-time bug
documented in docs/DATAHUB_MCP.md.

    python scripts/gate10_catalog_smoke.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polygraph import catalog_mcp, dh_mcp  # noqa: E402

JOB = "urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"
RAW = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.raw_claims,PROD)"
FEE = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.fee_schedule,PROD)"
GHOST = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.does_not_exist,PROD)"
EXPECTED_OWNER = "urn:li:corpGroup:ml-platform-team"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    if not ok:
        failures.append(label)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gms", default=None, help="GMS URL. Resolved automatically if omitted.")
    ap.add_argument("--token", default=None)
    args = ap.parse_args()

    gms, _ = dh_mcp.resolve_gms(args.gms, args.token)
    print("Gate 10a -- catalog context through DataHub's MCP Server")
    print("=" * 66)
    # Printed because the previous red was a credential resolution failure that
    # surfaced four layers away as "Connection closed". Show the input.
    print(f"GMS: {gms}")
    print(f"server: {' '.join(dh_mcp.resolve_server_command())}")

    print("\n1. get_entities on the job, a real dataset, and one that does not exist")
    context = catalog_mcp.fetch_catalog_context([JOB, RAW, FEE, GHOST], args.gms, args.token)

    check("every requested URN is accounted for", set(context) == {JOB, RAW, FEE, GHOST})
    check("the training job was found", context[JOB].found)
    check("raw_claims was found", context[RAW].found)
    check("fee_schedule was found", context[FEE].found)
    check(
        "a nonexistent URN is reported missing, not dropped",
        GHOST in context and context[GHOST].found is False,
    )
    check(
        "the job's owner matches what the seeder set",
        EXPECTED_OWNER in context[JOB].owners,
        f"owners: {context[JOB].owners}",
    )
    check(
        "raw_claims carries the description the seeder wrote",
        bool(context[RAW].description),
        f"description: {(context[RAW].description or '')[:80]}",
    )

    print("\n2. search for the undeclared source")
    hits = catalog_mcp.search_catalog("/q fee+schedule", args.gms, args.token)
    urns = {h["urn"] for h in hits["hits"]}
    check(
        "the undeclared source is findable in the catalog",
        FEE in urns,
        f"{hits['count']} hit(s): {sorted(urns)}",
    )
    check("search results are labelled with their source", hits["source"].startswith("mcp-server-datahub"))

    out = Path("examples/catalog_context.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "source": "mcp-server-datahub",
                "tools_used": ["get_entities", "search"],
                "assets": [c.to_dict() for c in context.values()],
                "search": hits,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"\nWritten: {out}")

    print("\n" + "=" * 66)
    if failures:
        print(f"GATE 10a: FAIL -- {len(failures)} check(s) failed")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GATE 10a: PASS -- DataHub reached through its own MCP Server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
