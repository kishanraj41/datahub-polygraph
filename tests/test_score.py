"""The integrity score must punish omissions as hard as stale claims."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polygraph.score import score_consumer  # noqa: E402

JOB = "urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"


def _report(verdicts):
    return {"verdicts": [{"downstream": JOB, "upstream": f"ds{i}", "verdict": v}
                         for i, v in enumerate(verdicts)]}


def test_perfect_agreement_scores_one():
    s = score_consumer(_report(["VERIFIED", "VERIFIED"]), JOB)
    assert s.score == 1.0 and s.precision == 1.0 and s.recall == 1.0


def test_naive_precision_would_be_perfect_but_score_is_not():
    """One correct declared edge, five undeclared sources.

    verified/declared = 1/1 = 1.0 -- the naive score calls this a perfect
    catalog while five real inputs are missing. The Jaccard score must not.
    """
    s = score_consumer(_report(["VERIFIED"] + ["UNDECLARED"] * 5), JOB)
    assert s.precision == 1.0, "precision is genuinely perfect here"
    assert s.recall < 0.2
    assert s.score < 0.2, "the headline score must reflect the missing sources"


def test_phantom_and_undeclared_both_reduce_the_score():
    baseline = score_consumer(_report(["VERIFIED", "VERIFIED"]), JOB).score
    with_phantom = score_consumer(_report(["VERIFIED", "VERIFIED", "PHANTOM"]), JOB).score
    with_undeclared = score_consumer(_report(["VERIFIED", "VERIFIED", "UNDECLARED"]), JOB).score
    assert with_phantom < baseline
    assert with_undeclared < baseline
    assert with_phantom == with_undeclared, "both failure modes should cost the same"


def test_empty_catalog_entry_scores_zero_not_one():
    """A job declaring nothing has proven nothing. Scoring it 1.0 for making no
    false claims would reward the exact failure Polygraph exists to catch."""
    s = score_consumer(_report([]), JOB)
    assert s.score == 0.0 and s.precision == 0.0 and s.recall == 0.0


def test_matches_the_real_demo_report():
    import json
    report = json.loads((ROOT / "examples" / "reconciliation_report.json").read_text(encoding="utf-8"))
    s = score_consumer(report, JOB)
    assert (s.verified, s.phantom, s.undeclared) == (1, 1, 1)
    assert s.score == 0.3333
    assert s.precision == 0.5 and s.recall == 0.5
