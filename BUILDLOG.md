# BUILDLOG

## 2026-08-05/06 — Phase 0: inventory

**What.** Verified the hackathon, the dependency surface, and the claimed prior work.

**Result.** Three corrections to the brief:

1. The fraud pipeline is in **AutoLineage**, not RudriQ. RudriQ is an LLM/RAG
   observability product (groundedness, coherence, drift, OTel ingest) with no
   pandas/sklearn lineage code. Decision A settled: Polygraph never touches RudriQ.
2. **"F1 0.984 → 0.000" does not exist.** Real numbers: synthetic planted-bug
   baseline F1 = 0.8282; credit-card RandomForest F1 = 0.8333, AUC-ROC 0.9871,
   accuracy 0.9995. The 0.984 appears to conflate AUC with F1.
3. `mcp-server-datahub` 0.6.0 gates every tool we need at `oss>=1.4.0`
   (`add_tags`, `save_document`, `add_structured_properties`, `add_owners`), so
   the write-back path is viable on open-source DataHub. Only
   `get_dataset_assertions` is Cloud-only.

Environment: the cloud build container has Docker but **all registries are
403-blocked**, so DataHub cannot run there. Computer-use cannot type into a
terminal (platform restriction: "click" tier only). Settled on script-drop:
code is written to the Windows box, one command per gate, logs read back.

## 2026-08-06 — Phase 3: observed exporter

Three problems solved, each verified against real captures:

- AutoLineage's IO hooks test `isinstance(path, str)`, so `pathlib.Path`
  arguments capture **nothing**. Silent. Upstream bug; noted for Ring 3.
- Shortest-path between anchors is misleading — `LogisticRegression.fit` links
  back to an early hub, skipping filter/merge/split. Switched to union-of-paths
  ordered by capture timestamp.
- Self-loop records (`parent_id == child_id`) carry the planted bug itself.
  First version discarded them as topology-free. Now collected as in-place ops.

Added a temporal guard: write anchors sit on the node they were written *from*,
which produced a backwards `predictions → model` edge until edges were required
to respect capture timestamps.

## 2026-08-06 — Phases 2, 4, 5: seed, reconcile, write-back

Gate 2-5 green on first run. Verdicts: VERIFIED=1, PHANTOM=1, UNDECLARED=1,
matching `seed_manifest.json` exactly.

`GlobalTagsClass` is a whole-aspect write — naive emission would delete every
other tag on an asset. `apply_tags` reads first, preserves everything outside
the `polygraph:` namespace, removes only stale polygraph tags.

Dependency pinning added after the Windows env resolved pandas 3.0.5 /
scikit-learn 1.9.0 against the cloud's 3.0.2 / 1.8.0. With pins, F1 reproduces
to all 16 digits on both machines: `0.8282290279627164`.

## 2026-08-06 — Phase 6: incident path

Gate 6 green. F1 0.8282 → 0.0000, root cause `filter`, impact 1.0, owner
resolved live to `urn:li:corpGroup:ml-platform-team`.

Design calls: publish the **full anomaly ranking** rather than just the winner;
attribute the incident to the **job**, not a dataset (the offending `filter`
sits on the path from two sources — naming one would be a guess presented as a
finding); ship a "what this does not tell you" section.

## 2026-08-07 — integrity bug found in our own verification claim

The incident report claims its sha-256 lets you verify the catalog copy against
`examples/` byte for byte. **That was false on Windows.** `Path.write_text`
translates `\n` to `\r\n`, so the file on disk hashed `b32a1e40...` while the
digest published to DataHub was `743b60da...`.

Fixed by routing every artifact write through `fsutil.write_text_lf`, adding
`.gitattributes` with LF pinned, and adding `tests/test_digest_integrity.py`
which fails if CRLF ever reaches an artifact whose hash is published.
