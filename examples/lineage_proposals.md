# Polygraph lineage proposals

Job: `urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)`

Polygraph's tags and scores are additive and namespaced. These proposals are different: they change what the catalog **claims**. Nothing here is applied without `--approve`, and removals need `--remove-phantom` on top of that.

## Proposed changes

| Action | Edge | Needs extra flag | Rationale |
| --- | --- | :-: | --- |
| `ADD_INPUT` | `polygraph.demo.fee_schedule` → `train_fraud_model` | no | Runtime capture recorded data flowing along this edge through 5 operation(s), but the catalog does not declare it. |
| `REMOVE_INPUT` | `polygraph.demo.legacy_claims_archive` → `train_fraud_model` | yes | The catalog declares this edge but the captured run recorded no data flowing along it. |

## Safety

**`ADD_INPUT` polygraph.demo.fee_schedule**

Additive and evidence-backed. The edge demonstrably exists; declaring it can only make the catalog more accurate.

**`REMOVE_INPUT` polygraph.demo.legacy_claims_archive**

NOT SAFE FROM ONE RUN. A conditional edge whose branch was not taken during capture is indistinguishable from a dead one. Removing a real-but-conditional edge would make the catalog less true — the exact failure Polygraph exists to catch. Confirm across several runs, or ask the owning team, before approving.

## Resulting declared inputs

| | inputs |
| --- | --- |
| before | `polygraph.demo.legacy_claims_archive`, `polygraph.demo.raw_claims` |
| after | `polygraph.demo.fee_schedule`, `polygraph.demo.legacy_claims_archive`, `polygraph.demo.raw_claims` |

## Not applied

- `REMOVE_INPUT` polygraph.demo.legacy_claims_archive — removal requires --remove-phantom

## Why removals are gated harder than additions

Adding an undeclared source is backed by positive evidence: the runtime recorded data flowing along that edge. Declaring it can only make the catalog more accurate.

Removing a phantom edge is backed by *absence* of evidence from a single run. A conditional edge whose branch was not taken looks identical to a dead one. Deleting a real edge on that basis would be Polygraph making exactly the kind of confident-but-wrong claim it was built to detect, so it requires a separate explicit flag and a human who has looked at more than one run.
