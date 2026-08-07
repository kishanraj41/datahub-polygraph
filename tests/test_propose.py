"""Lineage proposals must be gated asymmetrically and must be reversible.

Adding an undeclared source is backed by positive evidence. Removing a phantom
edge is backed by absence of evidence from one run, which cannot distinguish a
dead edge from a conditional one whose branch was not taken. Treating those two
as equally safe would have Polygraph making exactly the confident-but-wrong
claim it exists to detect.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polygraph.propose import (  # noqa: E402
    ADD,
    REMOVE,
    build_proposals,
    plan,
    to_markdown,
    write_revert,
)

JOB = "urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"
RAW = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.raw_claims,PROD)"
FEE = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.fee_schedule,PROD)"
ARCHIVE = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.legacy_claims_archive,PROD)"

REPORT = json.loads((ROOT / "examples" / "reconciliation_report.json").read_text(encoding="utf-8"))
CURRENT = [RAW, ARCHIVE]


def test_undeclared_becomes_an_add_and_phantom_becomes_a_remove():
    props = build_proposals(REPORT)
    by_action = {p.action: p for p in props}
    assert by_action[ADD].upstream == FEE
    assert by_action[REMOVE].upstream == ARCHIVE


def test_removals_require_an_extra_flag_but_additions_do_not():
    props = build_proposals(REPORT)
    add = next(p for p in props if p.action == ADD)
    rem = next(p for p in props if p.action == REMOVE)
    assert add.requires_extra_flag is False
    assert rem.requires_extra_flag is True


def test_default_plan_adds_but_never_removes():
    """Without --remove-phantom, the declared archive edge must survive."""
    props = build_proposals(REPORT)
    p = plan(props, CURRENT, JOB, allow_removals=False)
    assert FEE in p["after"], "the evidence-backed addition should apply"
    assert ARCHIVE in p["after"], "a phantom edge must not be deleted by default"
    assert any("--remove-phantom" in s["why"] for s in p["skipped"])


def test_removal_only_happens_with_explicit_opt_in():
    props = build_proposals(REPORT)
    p = plan(props, CURRENT, JOB, allow_removals=True)
    assert ARCHIVE not in p["after"]
    assert FEE in p["after"]


def test_plan_is_idempotent():
    """Re-running against an already-corrected catalog must be a no-op."""
    props = build_proposals(REPORT)
    once = plan(props, CURRENT, JOB, allow_removals=False)
    twice = plan(props, once["after"], JOB, allow_removals=False)
    assert twice["changed"] is False
    assert twice["after"] == once["after"]


def test_proposals_for_other_jobs_are_not_applied():
    props = build_proposals(REPORT)
    p = plan(props, CURRENT, "urn:li:dataJob:(urn:li:dataFlow:(other,flow,PROD),other_job)")
    assert p["changed"] is False
    assert all("different job" in s["why"] for s in p["skipped"])


def test_removal_safety_text_names_the_conditional_edge_problem():
    """The danger has to be stated where someone about to approve will read it."""
    props = build_proposals(REPORT)
    rem = next(p for p in props if p.action == REMOVE)
    assert "conditional" in rem.safety.lower()
    assert "one run" in rem.safety.lower()

    md = to_markdown(props, plan(props, CURRENT, JOB))
    assert "gated harder than additions" in md


def test_revert_snapshot_round_trips(tmp_path):
    path = tmp_path / "revert.json"
    write_revert(path, JOB, CURRENT, ["urn:li:dataset:(x,out,PROD)"])
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert snap["job"] == JOB
    assert sorted(snap["inputDatasets"]) == sorted(CURRENT)
    assert b"\r\n" not in path.read_bytes()
