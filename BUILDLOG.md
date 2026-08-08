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

## 2026-08-08 — Phase 10: DataHub's MCP Server, and a GMS bug found the hard way

Eligibility gap, found by re-reading the rules: the hackathon requires the OSS
platform **together with** at least one of the MCP Server, Agent Context Kit,
DataHub Skills or the Analytics Agent. Polygraph shipped its own MCP server and
had `mcp-server-datahub` in `requirements.txt` — but never imported it. Shipping
an MCP server is not the same as using DataHub's.

First attempt read declared lineage through `get_lineage`. Two reds.

**Red 1 — `[WinError 2]`.** Launching by bare console-script name fails on
Windows: `CreateProcess` does not search PATH the way a shell does, and a venv's
`Scripts` directory is not on the subprocess PATH. Fixed by launching as
`sys.executable -m mcp_server_datahub`, which also guarantees the server runs in
the same venv, so it sees the same credentials and the same pinned SDK.

**Red 2 — a 500 from GMS.**

```
Failed to generate PointInTime Identifier.. Root cause: search
path: ['searchAcrossLineage']
```

Isolated it by reading the MCP Server's source rather than guessing: its
`get_lineage` sends an ordinary `searchAcrossLineage` query — degree filter,
count, skipHighlighting — which is the same query **DataHub's own UI Lineage
tab** sends. So the fault is neither ours nor the MCP Server's.

Root cause, from DataHub's env-var reference: `ELASTICSEARCH_IMPLEMENTATION`
defaults to `elasticsearch` and the quickstart compose file does not set it,
while `ELASTICSEARCH_SEARCH_GRAPH_POINT_IN_TIME_CREATION_ENABLED` defaults to
`true`. The quickstart's backend is OpenSearch. GMS sends the Elasticsearch
`_pit` call to a server that answers point-in-time on a different endpoint.

`scripts/probe_gms.ps1` reproduces it straight against GraphQL, independent of
both clients. `scripts/fix_gms_search.ps1` layers a compose override that sets
`ELASTICSEARCH_IMPLEMENTATION=opensearch` and recreates **only** `datahub-gms`
with `--no-deps` — no volume touched, catalog survives, `-Mode revert` undoes it.

**The design change this forced, and why it is better.** Checked whether
`get_entities` could supply declared inputs instead. It cannot: `inputOutput`,
`inputDatasets` and `outputDatasets` appear in **none** of the MCP Server's
GraphQL documents, though GMS exposes `DataJob.inputOutput` (probe #3 verifies
this). That is a gap in the MCP Server, worth an upstream PR.

So the integration was rebuilt around the two tools that do not touch the broken
resolver — `get_entities` and `search` — as `src/polygraph/catalog_mcp.py`, and
both are wired into the `ask --llm` agent loop alongside Polygraph's own six.
That composition is more interesting than the original plan: Polygraph's tools
return **evidence** (what a capture proved), DataHub's return **testimony** (what
the catalog claims), and the system prompt requires the model to keep them
distinguishable. The gap between the two is the entire subject of the project.

`reconcile` now defaults to `--declared-via sdk`. The default has to work on a
clean clone against a stock quickstart; `--declared-via mcp` stays, gate-tested,
and needs the environment fix.

Gate 10 split accordingly: **10a** (catalog context, must be green) and **10b**
(declared lineage, may be environment-blocked). 10b is allowed to report
"blocked" for exactly one error signature — anything else is red. A known bug is
an excuse for one message and no others.

Transport extracted to `src/polygraph/dh_mcp.py` so `declared_mcp` and
`catalog_mcp` cannot drift on how they launch and configure the server.

Extraction is structural everywhere — walking payloads rather than indexing
fixed paths. Owner extraction is scoped to the `ownership` subtree specifically:
`lastModified.actor` and `created.actor` are also corpuser URNs, and reporting
the last editor as the owner would be a quiet, plausible lie. A URN the catalog
does not know comes back `found: false` rather than vanishing, because otherwise
"no owner" and "does not exist" become the same answer.

78 tests passing, 2 skipped (the live-capture oracle pair, which run on the
box with DataHub).

## 2026-08-08 — the search container was dead, and I diagnosed it wrong first

`searchAcrossLineage` returned a 500: `Failed to generate PointInTime
Identifier.. Root cause: search`. I read DataHub's env-var reference, found that
`ELASTICSEARCH_IMPLEMENTATION` defaults to `elasticsearch` while the quickstart
runs OpenSearch, and that graph queries create a point-in-time snapshot by
default — and wrote that up as the cause, with a fix script and two compose
overrides. Confidently. In the README.

It was wrong. `datahub-opensearch-1` had exited (127) while everything else
stayed up for 45 hours, and GMS could not resolve the compose alias `search`:

```
java.net.UnknownHostException: search
    at ...OpenSearch2SearchClientShim.search(...)
```

`Root cause: search` was the **hostname**. And `OpenSearch2SearchClientShim` in
GMS's own stack trace says it had detected OpenSearch correctly all along, so
the dialect theory was contradicted by evidence that was already on screen.

What actually went wrong in my reasoning: I reasoned from documentation to a
plausible mechanism, and never checked the cheapest fact that would have settled
it — whether the container was running. The original probe did grep `docker ps`
and did report "no opensearch/elasticsearch container found", and I treated that
as a naming quirk rather than as the answer.

Two changes so the next one lands faster:

* **`searchAcrossEntities` is now a probe, and the discriminator.** Ordinary
  search uses no point-in-time. It was failing too, which no dialect theory
  explains. One extra query would have killed the wrong hypothesis immediately.
* **`scripts/stack_status.ps1`** — container-level facts before service-level
  theories. Every container running *or stopped*, compose projects, which
  container owns each port, and whether `container_name` is pinned.

That last one found a second latent bug: the quickstart pins no
`container_name`, so containers are `datahub-<service>-quickstart-1`, and every
script here that said `docker ... datahub-gms` was addressing nothing. Including
`run_gate1.ps1`'s log dump, which would have printed nothing at the exact moment
it was needed. Now resolved by pattern.

The fix scripts written for the wrong diagnosis are deleted, not kept "just in
case". A repo that ships a remedy for a cause that never existed teaches its next
reader the wrong lesson. What is kept is the account of being wrong, in
`docs/DATAHUB_MCP.md`.

Also fixed, from the same run: `server_env` set `DATAHUB_GMS_URL` only when a
caller passed one, so the MCP subprocess started with no credentials, died on
`MissingConfigError`, and surfaced as `McpError: Connection closed` — an
infrastructure failure wearing a protocol failure's clothes. `resolve_gms()` now
always produces a URL and `preflight()` checks GMS before launching.
`tests/test_dh_mcp.py` holds the regressions. 89 tests, 2 skipped.

Worth stating plainly: nothing about the demo path broke during any of this.
`verify.ps1` went green from a fresh clone with OpenSearch dead — F1
`0.8282290279627164`, verdicts 1/1/1, digest `acbedff4…` — because every step of
it reads aspects from MySQL. That is a real property of the design, not luck,
but it is also exactly why the outage went unnoticed for 17 hours.
