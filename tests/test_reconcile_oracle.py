"""Phase 4 gate: the reconciler's verdicts must match the seed manifest exactly.

``demo/seed_manifest.json`` is the oracle. It states, for every edge, what the
catalog will declare and what verdict Polygraph must reach. This test runs the
real reconciler over a real captured ``observed_graph.json`` and asserts an
exact match -- no missing verdicts, no extra ones, no disagreements.

It deliberately does not need DataHub: the declared side is read from the
manifest, so the reconciliation logic is testable offline and in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polygraph.reconcile import DeclaredEdge, reconcile  # noqa: E402
from polygraph.urnmap import UrnMap  # noqa: E402

MANIFEST = json.loads((ROOT / "demo" / "seed_manifest.json").read_text(encoding="utf-8"))


def _declared_edges() -> list[DeclaredEdge]:
    return [
        DeclaredEdge(e["upstream"], e["downstream"], via="dataJob.inputDatasets")
        for e in MANIFEST["edges"]
        if e["declared"]
    ]


def _run_reconcile(observed_path: Path) -> dict:
    graph = json.loads(observed_path.read_text(encoding="utf-8"))
    urn_map = UrnMap.load(ROOT / "demo" / "urn_map.yaml")
    mapped, unmapped = urn_map.map_graph(graph)
    return reconcile(
        declared=_declared_edges(),
        observed_graph=graph,
        key_to_urn=mapped,
        unmapped_keys=unmapped,
        scope_downstream=set(MANIFEST["scope"]["downstream_urns"]),
    )


@pytest.mark.parametrize("mode", ["healthy", "buggy"])
def test_verdicts_match_manifest(mode: str) -> None:
    observed = ROOT / "runs" / mode / "observed_graph.json"
    if not observed.exists():
        pytest.skip(f"no capture for {mode}; run demo/pipeline.py --mode {mode} first")

    report = _run_reconcile(observed)
    actual = {(v["upstream"], v["downstream"]): v["verdict"] for v in report["verdicts"]}
    expected = {(e["upstream"], e["downstream"]): e["expected_verdict"] for e in MANIFEST["edges"]}

    assert actual == expected, (
        "reconciler disagrees with the oracle\n"
        f"  expected: {json.dumps({f'{k[0]} -> {k[1]}': v for k, v in expected.items()}, indent=2)}\n"
        f"  actual:   {json.dumps({f'{k[0]} -> {k[1]}': v for k, v in actual.items()}, indent=2)}"
    )


@pytest.mark.parametrize("mode", ["healthy", "buggy"])
def test_no_unmapped_or_skipped(mode: str) -> None:
    """Every observed node must be mapped. An unmapped node is a real defect:
    it means the pipeline touched an asset that urn_map.yaml does not know."""
    observed = ROOT / "runs" / mode / "observed_graph.json"
    if not observed.exists():
        pytest.skip(f"no capture for {mode}")
    report = _run_reconcile(observed)
    assert report["summary"]["unmapped_nodes"] == []
    assert report["summary"]["skipped_observed_edges"] == []


def test_discrepancies_are_detected() -> None:
    """The seeded catalog is deliberately wrong, so the run must be non-clean.
    This is what makes the CLI's non-zero exit code meaningful."""
    from polygraph.reconcile import has_discrepancies

    observed = ROOT / "runs" / "healthy" / "observed_graph.json"
    if not observed.exists():
        pytest.skip("no capture")
    assert has_discrepancies(_run_reconcile(observed)) is True
