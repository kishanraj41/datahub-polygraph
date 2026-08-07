# Polygraph lineage integrity scores

| Asset | Score | Precision | Recall | Verified | Phantom | Undeclared |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `train_fraud_model` | **0.3333** | 0.5 | 0.5 | 1 | 1 | 1 |

## Interpretation

- `urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)` — Not trustworthy: 1 declared edge(s) carried no data in this run; 1 real source(s) are missing from the catalog.

## How the score is defined

`LIS = |declared ∩ observed| / |declared ∪ observed|` (Jaccard index).

The naive alternative — verified over declared — scores a catalog 1.0 when it declares one correct edge and misses five real ones. Lineage drift is mostly a recall problem, so the headline number has to punish omissions as hard as it punishes stale claims. Precision and recall are reported separately because they point at different fixes.

Unweighted: DataHub OSS exposes no per-edge confidence on `dataJobInputOutput`, so there is nothing to weight by. No weight was invented.
