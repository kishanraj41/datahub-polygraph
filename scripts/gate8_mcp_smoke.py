"""Gate 8: exercise every MCP tool against the live DataHub, over real stdio.

The unit tests cover the three tools that read local files. The three that read
DataHub -- ``can_i_trust``, ``get_integrity_score``, ``get_incident_report`` --
were never executed against a running GMS, only written against the SDK's type
signatures. ``get_incident_report`` in particular reaches into
``DocumentInfoClass.contents.text``, an attribute path inferred from a
constructor signature rather than observed on a real response.

This launches the server exactly as Claude Desktop does -- a subprocess speaking
stdio -- and calls all six tools with real URNs. Exit code 0 only if every tool
returns evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

JOB = "urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"
FEE = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.fee_schedule,PROD)"
ARCHIVE = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.legacy_claims_archive,PROD)"
UNKNOWN = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.does_not_exist,PROD)"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")


async def run() -> int:
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "polygraph.mcp_server"],
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "POLYGRAPH_GMS": os.environ.get("POLYGRAPH_GMS", "http://localhost:8080"),
            "POLYGRAPH_REPORT": str(ROOT / "examples" / "reconciliation_report.json"),
        },
    )

    async with Client(transport) as c:
        tools = {t.name for t in await c.list_tools()}
        check("stdio handshake", len(tools) == 6, f"{len(tools)} tools advertised")

        # ---- can_i_trust on a tagged asset (LIVE DataHub read) --------------
        r = (await c.call_tool("can_i_trust", {"asset_urn": FEE})).data
        check(
            "can_i_trust(fee_schedule)",
            r.get("evidence_available") is True and "UNDECLARED" in r.get("verdicts", []),
            f"verdicts={r.get('verdicts')}",
        )

        r = (await c.call_tool("can_i_trust", {"asset_urn": ARCHIVE})).data
        check(
            "can_i_trust(legacy_archive)",
            r.get("evidence_available") is True and "PHANTOM" in r.get("verdicts", []),
            f"verdicts={r.get('verdicts')}",
        )

        # ---- the honesty case: an asset Polygraph never examined ------------
        r = (await c.call_tool("can_i_trust", {"asset_urn": UNKNOWN})).data
        no_false_clean = (
            r.get("evidence_available") is False
            and "clean bill of health" in r.get("answer", "").lower()
        ) or (r.get("evidence_available") is False and "does not exist" in r.get("answer", ""))
        check(
            "can_i_trust(unknown) refuses to imply health",
            no_false_clean,
            r.get("answer", "")[:90],
        )

        # ---- integrity score (LIVE structured-property read) ----------------
        r = (await c.call_tool("get_integrity_score", {"job_urn": JOB})).data
        check(
            "get_integrity_score",
            r.get("evidence_available") is True
            and abs(float(r.get("lineage_integrity_score", -1)) - 0.3333) < 1e-6,
            f"score={r.get('lineage_integrity_score')} diagnosis={r.get('diagnosis', '')[:50]}",
        )

        # ---- incident document (LIVE document read; riskiest attribute path)
        r = (await c.call_tool("get_incident_report", {})).data
        md = r.get("markdown") or ""
        check(
            "get_incident_report",
            r.get("evidence_available") is True and "filter" in md and len(md) > 500,
            f"{len(md)} chars, sha256={str(r.get('sha256'))[:16]}, root={r.get('root_operation')}",
        )

        # ---- the local-file tools, over stdio this time ---------------------
        r = (await c.call_tool("list_undeclared_sources", {})).data
        check(
            "list_undeclared_sources",
            r.get("count") == 1 and "fee_schedule" in r["edges"][0]["upstream"],
            f"count={r.get('count')}",
        )

        r = (await c.call_tool("list_phantom_edges", {})).data
        check(
            "list_phantom_edges",
            r.get("count") == 1 and "legacy_claims_archive" in r["edges"][0]["upstream"],
            f"count={r.get('count')}",
        )

        r = (await c.call_tool("explain_verdict_semantics", {})).data
        check(
            "explain_verdict_semantics",
            all("does_not_mean" in r[v] for v in ("VERIFIED", "PHANTOM", "UNDECLARED")),
            "all three verdicts state their limits",
        )

    failed = [n for n, ok, _ in results if not ok]
    print()
    if failed:
        print(f"GATE 8: FAIL -- {len(failed)} tool(s): {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"GATE 8: PASS -- all {len(results)} checks green over real stdio against live DataHub")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
