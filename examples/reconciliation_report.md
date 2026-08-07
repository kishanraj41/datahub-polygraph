# Polygraph reconciliation

Generated: `2026-08-06T23:00:58+00:00`  
Run: `{"mode": "healthy"}`

## Summary

| Verdict | Count |
| --- | ---: |
| VERIFIED | 1 |
| PHANTOM | 1 |
| UNDECLARED | 1 |

Declared edges: 2 · observed edges: 2

## Edges

| Verdict | Upstream | Downstream | Operations observed |
| --- | --- | --- | --- |
| `PHANTOM` | `polygraph.demo.legacy_claims_archive` | `train_fraud_model` | — |
| `UNDECLARED` | `polygraph.demo.fee_schedule` | `train_fraud_model` | filter → merge → drop → select → LogisticRegression.fit |
| `VERIFIED` | `polygraph.demo.raw_claims` | `train_fraud_model` | filter → concat → merge → drop → select → LogisticRegression.fit |

## What these verdicts do and do not mean

- `VERIFIED` is evidence from **the captured run**, not a proof about all runs.
- `PHANTOM` means nothing flowed along a declared edge **in this run**. A genuinely conditional edge on a branch that was not taken will look phantom.
- `UNDECLARED` means the runtime proved an edge the catalog never mentioned.
