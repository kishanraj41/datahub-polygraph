"""Phase 4 gate: the reconciler's verdicts must match the seed manifest exactly.

``demo/seed_manifest.json`` is the oracle. It states, for every edge, what the
catalog will declare and what verdict Polygraph must reach. These tests run the
real reconciler and assert an exact match -- no missing verdicts, no extra ones,
no disagreements.

**These tests never skip.** They used to fall back to ``pytest.skip`` when no
capture existed, which meant a fresh clone reported "9 passed, 5 skipped" and
the five that skipped were exactly the ones proving the verdicts are right.
Green with a skip count nobody reads is indistinguishable from green. So the
captures are committed as fixtures in ``tests/fixtures/`` and the tests run
hermetically: no DataHub, no prior pipeline run, no network.

``test_fixtures_match_live_capture`` closes the loop the other way -- if a real
run is present, its graph must equal the committed fixture, so the fixtures
cannot rot silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from polygraph.reconcile import DeclaredEdge, has_discrepancies, reconcile  # noqa: E402
from polygraph.urnmap import UrnMap  # noqa: E402

MANIFEST = json.loads((ROOT / "demo" / "seed_manifest.json").read_text(encoding="utf-8"))
MODES = ["healthy", "buggy"]


def _declared_edges() -> list[DeclaredEdge]:
    return [
        DeclaredEdge(e["upstream"], e["downstream"], via="dataJob.inputDatasets")
        for e in MANIFEST["edges"]
        if e["declared"]
    ]


def _load_graph(mode: str) -> dict:
    """Committed fixture. Deliberately not the live run -- these must be hermetic."""
    path = FIXTURES / f"observed_graph_{mode}.json"
    assert path.exists(), (
        f"missing fixture {path}. Regenerate with:\n"
        f"  python demo/pipeline.py --mode {mode}\n"
        f"  python -m polygraph.cli observe --trace runs/{mode}/trace.json "
        f"--out tests/fixtures/observed_graph_{mode}.json --root ."
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _run_reconcile(graph: dict) -> dict:
    urn_map = UrnMap.load(ROOT / "demo" / "urn_map.yaml")
    mapped, unmapped = urn_map.map_graph(graph)
    return reconcile(
        declared=_declared_edges(),
        observed_graph=graph,
        key_to_urn=mapped,
        unmapped_keys=unmapped,
        scope_downstream=set(MANIFEST["scope"]["downstream_urns"]),
    )


@pytest.mark.parametrize("mode", MODES)
def test_verdicts_match_manifest(mode: str) -> None:
    report = _run_reconcile(_load_graph(mode))
    actual = {(v["upstream"], v["downstream"]): v["verdict"] for v in report["verdicts"]}
    expected = {(e["upstream"], e["downstream"]): e["expected_verdict"] for e in MANIFEST["edges"]}

    assert actual == expected, (
        "reconciler disagrees with the oracle\n"
        f"  expected: {json.dumps({f'{k[0]} -> {k[1]}': v for k, v in expected.items()}, indent=2)}\n"
        f"  actual:   {json.dumps({f'{k[0]} -> {k[1]}': v for k, v in actual.items()}, indent=2)}"
    )


@pytest.mark.parametrize("mode", MODES)
def test_no_unmapped_or_skipped(mode: str) -> None:
    """An unmapped observed node means the pipeline touched an asset that
    urn_map.yaml does not know about. That is a real defect, not a warning."""
    report = _run_reconcile(_load_graph(mode))
    assert report["summary"]["unmapped_nodes"] == []
    assert report["summary"]["skipped_observed_edges"] == []


@pytest.mark.parametrize("mode", MODES)
def test_discrepancies_are_detected(mode: str) -> None:
    """The seeded catalog is deliberately wrong, so every run must be non-clean.
    This is what makes the CLI's non-zero exit code meaningful in CI."""
    assert has_discrepancies(_run_reconcile(_load_graph(mode))) is True


@pytest.mark.parametrize("mode", MODES)
def test_undeclared_edge_names_the_shadow_input(mode: str) -> None:
    """The headline finding, asserted directly rather than inferred from counts."""
    report = _run_reconcile(_load_graph(mode))
    undeclared = [v for v in report["verdicts"] if v["verdict"] == "UNDECLARED"]
    assert len(undeclared) == 1
    assert "fee_schedule" in undeclared[0]["upstream"]
    assert undeclared[0]["operations"], "an undeclared edge with no operations is not evidence"


@pytest.mark.parametrize("mode", MODES)
def test_fixtures_match_live_capture(mode: str) -> None:
    """If a real run is present, the fixture must equal it.

    Without this, the fixtures could drift from what the pipeline actually
    produces and every test above would keep passing against a stale snapshot.
    Skips only when no live run exists -- which is the one case where there is
    genuinely nothing to compare.
    """
    live_path = ROOT / "runs" / mode / "observed_graph.json"
    if not live_path.exists():
        pytest.skip(f"no live capture for {mode}; fixture correctness checked elsewhere")

    live = json.loads(live_path.read_text(encoding="utf-8"))
    fixture = _load_graph(mode)

    def edges(g):
        return sorted((e["upstream"], e["downstream"], tuple(e["operations"])) for e in g["edges"])

    assert edges(live) == edges(fixture), (
        f"tests/fixtures/observed_graph_{mode}.json has drifted from what the pipeline "
        "now produces. Regenerate it."
    )
