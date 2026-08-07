"""Polygraph demo pipeline — a fraud-scoring pipeline under AutoLineage capture.

Two things make this pipeline useful as a Polygraph demo:

1. It reads a *side input* (``fee_schedule.csv``) that the DataHub catalog will
   not declare. Runtime capture proves the edge exists -> ``UNDECLARED``.
2. It has a ``--mode buggy`` variant in which exactly one line changes -- the
   quantile in the amount filter -- which collapses F1. This reproduces the
   planted-bug "filter" case from the AutoLineage paper's benchmark suite
   (``benchmarks/planted_bugs/pipeline.py``), with the data materialised to
   real CSV files so that file-level lineage anchors exist.

Data is synthetic and generated deterministically (seed=0). Nothing here is
downloaded and nothing is fabricated: every number Polygraph reports comes from
an actual run of this file.

Usage:
    python demo/pipeline.py --mode healthy --outdir runs/healthy
    python demo/pipeline.py --mode buggy   --outdir runs/buggy
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Import-time hooks. Must precede the pandas/sklearn imports that follow so the
# wrapped callables are what this module binds to.
import autolineage.auto  # noqa: F401  (side effect: installs 239 hooks)
from autolineage.auto import get_tracker
from autolineage.core.analyzer import LineageAnalyzer

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

SEED = 0
N_ROWS = 6000
REGIONS = ["north", "south", "east", "west"]


def materialise_inputs(data_dir: Path) -> tuple[Path, Path]:
    """Write the two source CSVs. Deterministic; safe to re-run."""
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_path = data_dir / "raw_claims.csv"
    fee_path = data_dir / "fee_schedule.csv"

    if not raw_path.exists():
        rng = np.random.default_rng(SEED)
        amount = rng.exponential(50, N_ROWS)
        region = rng.choice(REGIONS, N_ROWS)
        # Label concentrated in high-amount rows, with 5% label noise.
        y = ((amount > np.quantile(amount, 0.80)) ^ (rng.random(N_ROWS) < 0.05)).astype(int)
        pd.DataFrame(
            {"claim_id": np.arange(N_ROWS), "amount": amount, "region": region, "y": y}
        ).to_csv(str(raw_path), index=False)

    if not fee_path.exists():
        # The shadow input: small, real, and deliberately left out of the catalog.
        pd.DataFrame({"region": REGIONS, "region_fee": [1.0, 2.0, 3.0, 4.0]}).to_csv(str(fee_path), index=False)

    return raw_path, fee_path


def run(mode: str, outdir: Path, data_dir: Path, fingerprint: Path | None = None) -> dict:
    raw_path, fee_path = materialise_inputs(data_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    # --- read: two file anchors enter the lineage graph here -----------------
    claims = pd.read_csv(str(raw_path))
    fees = pd.read_csv(str(fee_path))  # <- undeclared in the catalog, real at runtime

    # --- join the side input ------------------------------------------------
    df = claims.merge(fees, on="region", how="left")

    # --- THE PLANTED BUG ----------------------------------------------------
    # One line. Healthy keeps everything below the 99.9th percentile (a sane
    # outlier trim). Buggy keeps only the bottom 5%, which discards nearly every
    # positive label because the label is concentrated in high-amount rows.
    cutoff = 0.05 if mode == "buggy" else 0.999
    df = df[df["amount"] <= df["amount"].quantile(cutoff)]

    # --- features -----------------------------------------------------------
    features = pd.get_dummies(
        df.drop(columns=["y", "claim_id"]), columns=["region"], drop_first=True, dtype=float
    )
    features = features.drop(columns=[c for c in features.columns if features[c].dtype == object])
    labels = df["y"]

    X_train, X_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.3, random_state=SEED
    )
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    model = LogisticRegression(max_iter=200).fit(X_train_s, y_train)
    preds = model.predict(X_test_s)

    metrics = {
        "mode": mode,
        "rows_after_filter": int(len(df)),
        "filter_quantile": cutoff,
        "f1": float(f1_score(y_test, preds, zero_division=0)),
        "precision": float(precision_score(y_test, preds, zero_division=0)),
        "recall": float(recall_score(y_test, preds, zero_division=0)),
        "accuracy": float(accuracy_score(y_test, preds)),
    }

    # --- write: the terminal file anchor ------------------------------------
    # Derive the output frame from X_test (a tracked object) rather than
    # constructing a fresh DataFrame from raw arrays. A fresh DataFrame has no
    # captured provenance, which would silently break the model -> predictions
    # edge. See "Limitations" in the README.
    pred_path = outdir / "predictions.csv"
    predictions = X_test.assign(y_true=y_test.to_numpy(), y_pred=preds)
    predictions.to_csv(str(pred_path), index=False)

    # --- persist the raw capture for the observed-graph exporter -------------
    tracker = get_tracker()
    graph = tracker.get_full_graph()
    with open(outdir / "trace.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(graph, fh, indent=2, default=str)

    # --- anomaly detection and root-cause localisation -----------------------
    # The healthy run saves a fingerprint of its own shape. The buggy run loads
    # that baseline and asks AutoLineage which operation is responsible for the
    # metric moving. Nothing here is simulated: the ranking comes from the
    # analyzer comparing two real captures.
    analyzer = LineageAnalyzer(tracker)
    if fingerprint is not None:
        if mode == "healthy":
            analyzer.save_fingerprint(str(fingerprint))
            metrics["fingerprint_saved"] = str(fingerprint)
        elif fingerprint.exists():
            analyzer.load_baseline(str(fingerprint))
            anomalies = analyzer.detect_anomalies()
            metrics["anomalies"] = [
                {
                    "operation": a.operation,
                    "metric": a.metric,
                    "severity": a.severity,
                    "deviation": round(float(a.deviation), 2),
                }
                for a in anomalies
            ]
            root = None
            for metric_name in ("f1_score", "accuracy_score"):
                try:
                    r = analyzer.localize_root_cause(metric_name)
                except Exception:  # noqa: BLE001 - analyzer raises on missing metric
                    continue
                if r:
                    root = {
                        "metric": metric_name,
                        "operation": r.root_operation,
                        "impact": round(float(r.impact_score), 3),
                    }
                    break
            metrics["root_cause"] = root
        else:
            metrics["fingerprint_missing"] = str(fingerprint)

    with open(outdir / "metrics.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(metrics, fh, indent=2)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["healthy", "buggy"], default="healthy")
    parser.add_argument("--outdir", default=None)
    parser.add_argument("--data-dir", default="demo/data")
    parser.add_argument(
        "--fingerprint",
        default="runs/baseline_fingerprint.json",
        help="healthy mode saves the baseline here; buggy mode loads it for RCA",
    )
    args = parser.parse_args()

    outdir = Path(args.outdir or f"runs/{args.mode}")
    metrics = run(args.mode, outdir, Path(args.data_dir), Path(args.fingerprint))
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONHASHSEED", "0")
    main()
