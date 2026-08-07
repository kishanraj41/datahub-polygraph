"""``polygraph`` command line.

    polygraph observe    trace.json -> observed_graph.json
    polygraph reconcile  declared (DataHub) vs observed -> verdicts, JSON + Markdown
    polygraph writeback  verdicts -> tags + a document in DataHub

``reconcile`` exits non-zero when discrepancies exist, so it drops straight into
CI as a lineage-drift check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import declared as declared_mod
from . import incident as incident_mod
from . import reconcile as rec
from . import score as score_mod
from . import writeback as wb
from .fsutil import write_text_lf
from .observed import export
from .urnmap import UrnMap

DEFAULT_GMS = "http://localhost:8080"
DEFAULT_JOB = "urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"

TAG_DESCRIPTIONS = {
    "polygraph:verified": "Runtime capture confirmed data flows along this declared edge.",
    "polygraph:phantom": "Declared in the catalog, but no data flowed along it in the captured run.",
    "polygraph:undeclared-source": (
        "Runtime capture proved this asset is read, but the catalog never declared it."
    ),
    "polygraph:incident": "Implicated in a model-quality incident by Polygraph.",
}


def cmd_observe(args: argparse.Namespace) -> int:
    graph = export(
        Path(args.trace),
        Path(args.out),
        Path(args.root),
        run_metadata={"mode": args.mode} if args.mode else None,
    )
    print(json.dumps({"nodes": len(graph["nodes"]), "edges": len(graph["edges"]), "out": args.out}, indent=2))
    for e in graph["edges"]:
        print(f"  {e['upstream']} -> {e['downstream']}")
        print(f"      {' > '.join(e['operations'])}")
    return 0


def _build_report(args: argparse.Namespace) -> dict:
    observed = json.loads(Path(args.observed).read_text(encoding="utf-8"))
    urn_map = UrnMap.load(args.urn_map)
    mapped, unmapped = urn_map.map_graph(observed)

    graph = declared_mod.connect(args.gms, args.token)
    lineage = declared_mod.fetch_declared(graph, args.job)

    return rec.reconcile(
        declared=lineage.edges,
        observed_graph=observed,
        key_to_urn=mapped,
        unmapped_keys=unmapped,
        scope_downstream={args.job},
    )


def cmd_reconcile(args: argparse.Namespace) -> int:
    report = _build_report(args)

    json_path = Path(args.out_json)
    md_path = Path(args.out_md)
    rec.write_report(report, json_path, md_path, title="Polygraph reconciliation")

    s = report["summary"]
    print(rec.to_markdown(report))
    print(f"\nWrote {json_path} and {md_path}")

    if rec.has_discrepancies(report):
        print(
            f"\nDISCREPANCIES: {s[rec.PHANTOM]} phantom, {s[rec.UNDECLARED]} undeclared",
            file=sys.stderr,
        )
        return 0 if args.allow_discrepancies else 2
    print("\nNo discrepancies. Catalog and runtime agree.")
    return 0


def cmd_writeback(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    graph = declared_mod.connect(args.gms, args.token)

    wb.ensure_tag_entities(graph, TAG_DESCRIPTIONS)
    results = wb.write_verdicts(graph, report, dry_run=args.dry_run)

    for r in results:
        print(f"{r.urn}")
        print(f"   added:     {r.added or '-'}")
        print(f"   removed:   {r.removed or '-'}")
        print(f"   preserved: {r.preserved or '-'}")

    if args.dry_run:
        print("\nDry run; nothing was written.")
        return 0

    check = wb.verify_written(graph, report)
    print("\n" + json.dumps(check, indent=2))
    if not check["ok"]:
        print("\nGATE 5: FAIL -- tags did not land", file=sys.stderr)
        return 1

    doc = None
    if args.document:
        # newline="" keeps the bytes as written, so the digest published to
        # DataHub matches the file a judge can hash themselves.
        with open(args.document, encoding="utf-8", newline="") as fh:
            markdown = fh.read()
        doc = wb.publish_document(
            graph,
            doc_id=args.document_id,
            title="Polygraph reconciliation: fraud_scoring",
            markdown=markdown,
            related_asset_urns=sorted(wb.tags_for_report(report)),
            custom_properties={
                "verified": str(report["summary"][rec.VERIFIED]),
                "phantom": str(report["summary"][rec.PHANTOM]),
                "undeclared": str(report["summary"][rec.UNDECLARED]),
            },
        )
        print("\ndocument: " + json.dumps(doc, indent=2))

    print("\nGATE 5: PASS")
    print("UI: http://localhost:9002")
    return 0


def cmd_incident(args: argparse.Namespace) -> int:
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    degraded = json.loads(Path(args.degraded).read_text(encoding="utf-8"))
    observed = json.loads(Path(args.observed).read_text(encoding="utf-8"))

    # --dry-run must not require DataHub: it exists so the report can be
    # rendered and reviewed offline before anything is published.
    graph = None
    owners: list[str] = []
    if not args.dry_run:
        graph = declared_mod.connect(args.gms, args.token)
        owners = declared_mod.owners_of(graph, args.job)

    inc = incident_mod.build_incident(
        baseline_metrics=baseline,
        degraded_metrics=degraded,
        observed_graph=observed,
        job_urn=args.job,
        owners=owners,
        metric=args.metric,
    )

    out_md = Path(args.out_md)
    write_text_lf(out_md, inc.markdown)

    print(inc.markdown)
    print(f"incident_id : {inc.incident_id}")
    print(f"sha256      : {inc.sha256}")
    print(f"written     : {out_md}")

    if args.dry_run:
        print("\nDry run; nothing written to DataHub.")
        return 0

    # Tag the affected job, preserving every non-polygraph tag.
    wb.ensure_tag_entities(graph, TAG_DESCRIPTIONS)
    for urn in inc.affected_urns:
        existing_polygraph = {
            t.split("urn:li:tag:")[-1]
            for t in wb._current_tags(graph, urn)
            if t.startswith("urn:li:tag:polygraph:")
        }
        result = wb.apply_tags(graph, urn, existing_polygraph | {wb.INCIDENT_TAG})
        print(f"\n{result.urn}\n   added: {result.added or '-'}   preserved: {result.preserved or '-'}")
    graph.flush()

    doc = wb.publish_document(
        graph,
        doc_id=inc.incident_id,
        title=f"Polygraph incident: {inc.metric.upper()} collapse in fraud_scoring",
        markdown=inc.markdown,
        related_asset_urns=inc.affected_urns,
        custom_properties={
            "metric": inc.metric,
            "baseline": f"{inc.baseline_value:.4f}",
            "degraded": f"{inc.degraded_value:.4f}",
            "root_operation": str(inc.root_operation),
            "impact_score": str(inc.impact_score),
        },
    )
    print("\ndocument: " + json.dumps(doc, indent=2))

    if doc["sha256"] != inc.sha256:
        print("\nGATE 6: FAIL -- published digest does not match the local file", file=sys.stderr)
        return 1

    check = {}
    for urn in inc.affected_urns:
        check[urn] = sorted(wb._current_tags(graph, urn))
    print("\ntags now on affected assets: " + json.dumps(check, indent=2))
    if not any(wb.INCIDENT_TAG in t for tags in check.values() for t in tags):
        print("\nGATE 6: FAIL -- incident tag did not land", file=sys.stderr)
        return 1

    print("\nGATE 6: PASS")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    scores = score_mod.score_all_consumers(report)

    markdown = score_mod.to_markdown(scores)
    write_text_lf(Path(args.out_md), markdown)
    print(markdown)

    if args.dry_run:
        print("Dry run; nothing written to DataHub.")
        return 0

    graph = declared_mod.connect(args.gms, args.token)
    score_mod.define_properties(graph)
    score_mod.write_scores(graph, scores)

    check = score_mod.verify_scores(graph, scores)
    print(json.dumps(check, indent=2))
    if not check["ok"]:
        print("\nGATE 7: FAIL -- scores did not land", file=sys.stderr)
        return 1

    print("\nGATE 7: PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="polygraph", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    o = sub.add_parser("observe", help="AutoLineage trace -> observed graph")
    o.add_argument("--trace", required=True)
    o.add_argument("--out", required=True)
    o.add_argument("--root", default=".")
    o.add_argument("--mode", default=None)
    o.set_defaults(func=cmd_observe)

    r = sub.add_parser("reconcile", help="declared vs observed -> verdicts")
    r.add_argument("--observed", required=True)
    r.add_argument("--urn-map", default="demo/urn_map.yaml")
    r.add_argument("--gms", default=DEFAULT_GMS)
    r.add_argument("--token", default=None)
    r.add_argument("--job", default=DEFAULT_JOB)
    r.add_argument("--out-json", default="examples/reconciliation_report.json")
    r.add_argument("--out-md", default="examples/reconciliation_report.md")
    r.add_argument(
        "--allow-discrepancies",
        action="store_true",
        help="exit 0 even when phantom/undeclared edges are found (default: exit 2)",
    )
    r.set_defaults(func=cmd_reconcile)

    w = sub.add_parser("writeback", help="apply verdict tags and publish the report")
    w.add_argument("--report", default="examples/reconciliation_report.json")
    w.add_argument("--document", default=None, help="markdown file to publish as a document")
    w.add_argument("--document-id", default="polygraph_reconciliation_fraud_scoring")
    w.add_argument("--gms", default=DEFAULT_GMS)
    w.add_argument("--token", default=None)
    w.add_argument("--dry-run", action="store_true")
    w.set_defaults(func=cmd_writeback)

    i = sub.add_parser("incident", help="metric collapse -> root cause -> incident document")
    i.add_argument("--baseline", default="runs/healthy/metrics.json")
    i.add_argument("--degraded", default="runs/buggy/metrics.json")
    i.add_argument("--observed", default="runs/buggy/observed_graph.json")
    i.add_argument("--metric", default="f1")
    i.add_argument("--gms", default=DEFAULT_GMS)
    i.add_argument("--token", default=None)
    i.add_argument("--job", default=DEFAULT_JOB)
    i.add_argument("--out-md", default="examples/incident_report.md")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=cmd_incident)

    sc = sub.add_parser("score", help="lineage integrity score -> structured properties")
    sc.add_argument("--report", default="examples/reconciliation_report.json")
    sc.add_argument("--out-md", default="examples/integrity_scores.md")
    sc.add_argument("--gms", default=DEFAULT_GMS)
    sc.add_argument("--token", default=None)
    sc.add_argument("--dry-run", action="store_true")
    sc.set_defaults(func=cmd_score)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
