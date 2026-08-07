"""The published sha-256 must match the bytes on disk, on every platform.

Polygraph writes an incident document into DataHub and stores the sha-256 of
the markdown as a custom property, claiming the catalog copy can be verified
against the file in ``examples/`` byte for byte.

That claim was false on Windows. ``Path.write_text`` translates ``\\n`` to
``\\r\\n``, so the file on disk hashed to ``b32a1e40...`` while the digest
published to DataHub was ``743b60da...`` -- computed over the in-memory string.
The verification story would have failed the first time a judge tried it, on
the same OS the demo runs on.

These tests pin the invariant.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polygraph.fsutil import write_json_lf, write_text_lf  # noqa: E402
from polygraph.incident import build_incident  # noqa: E402


def test_write_text_lf_never_emits_crlf(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    text = "# title\n\nline one\nline two\n"
    write_text_lf(target, text)

    raw = target.read_bytes()
    assert b"\r\n" not in raw, "CRLF leaked into an artifact whose hash is published"
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_write_json_lf_never_emits_crlf(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    write_json_lf(target, {"a": 1, "b": ["x", "y"]})
    assert b"\r\n" not in target.read_bytes()
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": ["x", "y"]}


def test_incident_digest_matches_file_on_disk(tmp_path: Path) -> None:
    """End to end: build an incident, write it, hash the file, compare."""
    baseline = {"mode": "healthy", "f1": 0.8282290279627164, "rows_after_filter": 5994,
                "filter_quantile": 0.999}
    degraded = {
        "mode": "buggy", "f1": 0.0, "rows_after_filter": 300, "filter_quantile": 0.05,
        "anomalies": [
            {"operation": "filter", "metric": "row_delta", "severity": "critical",
             "deviation": 94900.0}
        ],
        "root_cause": {"metric": "f1_score", "operation": "filter", "impact": 1.0},
    }
    observed = {
        "edges": [
            {
                "upstream": "file:demo/data/raw_claims.csv",
                "downstream": "model:LogisticRegression",
                "operations": ["filter", "merge", "LogisticRegression.fit"],
            }
        ]
    }

    inc = build_incident(
        baseline_metrics=baseline,
        degraded_metrics=degraded,
        observed_graph=observed,
        job_urn="urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)",
        owners=["urn:li:corpGroup:ml-platform-team"],
    )

    out = tmp_path / "incident_report.md"
    write_text_lf(out, inc.markdown)

    on_disk = hashlib.sha256(out.read_bytes()).hexdigest()
    assert on_disk == inc.sha256, (
        "the digest Polygraph publishes to DataHub does not match the file it wrote; "
        "the 'verify byte for byte' claim in the report would be false"
    )


def test_shipped_examples_are_lf_and_utf8() -> None:
    """Artifacts committed to examples/ are what judges will hash. Pin them."""
    examples = ROOT / "examples"
    if not examples.exists():
        return
    for path in sorted(examples.glob("*")):
        if path.suffix not in {".md", ".json"}:
            continue
        raw = path.read_bytes()
        raw.decode("utf-8")  # raises if not valid UTF-8
        assert b"\r\n" not in raw, f"{path.name} contains CRLF; its published digest will not match"
