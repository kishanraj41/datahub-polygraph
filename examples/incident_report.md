# incident_515d772c4624: F1 collapse in `fraud_scoring`

## What happened

| | baseline | degraded | delta |
| --- | ---: | ---: | ---: |
| **f1** | 0.8282 | 0.0000 | -0.8282 |
| rows after filter | 5994 | 300 | |
| filter quantile | 0.999 | 0.05 | |

Both rows come from `metrics.json` files written by real runs of `demo/pipeline.py`. Neither number is illustrative.

## Root cause

AutoLineage's analyzer localises the collapse to the **`filter`** operation (impact score 1.0).

Full ranking, so the confidence is visible rather than asserted:

| Operation | Metric | Severity | Deviation |
| --- | --- | --- | ---: |
| `filter` | row_delta | critical | 94900.0 |
| `train_test_split` | row_delta | critical | 95.0 |
| `f1_score` | f1_score | critical | 100.0 |
| `precision_score` | precision_score | critical | 100.0 |
| `recall_score` | recall_score | critical | 100.0 |

## Where it sits in the lineage

The `filter` operation was recorded on the path between these assets, according to runtime capture:

| Upstream | Downstream | Operations recorded |
| --- | --- | --- |
| `file:demo/data/fee_schedule.csv` | `file:runs/buggy/predictions.csv` | filter → merge → drop → select → train_test_split → assign |
| `file:demo/data/fee_schedule.csv` | `model:LogisticRegression` | filter → merge → drop → select → LogisticRegression.fit |
| `file:demo/data/raw_claims.csv` | `file:runs/buggy/predictions.csv` | filter → concat → merge → drop → select → train_test_split → assign |
| `file:demo/data/raw_claims.csv` | `model:LogisticRegression` | filter → concat → merge → drop → select → LogisticRegression.fit |

Polygraph attributes the incident to the **job**, not to any single upstream dataset. The operation lies on the path from more than one source, and picking one would be a guess presented as a finding.

## Ownership

Affected job: `urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)`  
Owners (from DataHub): `urn:li:corpGroup:ml-platform-team`

## Verification

The sha-256 of this document is stored as the `polygraph_sha256` custom property on the DataHub document, so the catalog copy can be checked against the file in `examples/` byte for byte.

## What this does not tell you

- The analyzer ranks **row-count and column-count deviations**. A bug that preserves both shapes -- a unit error scaling a column by 1000, say -- would not appear here at all.
- Localisation is to an *operation*, not a line number.
- The baseline is a single captured run with a fixed seed, not a distribution.
