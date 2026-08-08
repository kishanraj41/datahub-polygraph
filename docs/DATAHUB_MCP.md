# Polygraph and DataHub's MCP Server

Polygraph reaches DataHub two ways: the `acryl-datahub` SDK, and **DataHub's own
MCP Server** (`mcp-server-datahub`), launched as a stdio subprocess exactly the
way an agent client launches it.

This document records what works, what does not, and why — including a
server-side bug that took a day to isolate and that also breaks DataHub's own
UI.

---

## What Polygraph calls, and where

| DataHub MCP tool | GraphQL resolver | Used by | Needs the search backend? |
|---|---|---|---|
| `get_entities` | `entities(urns:)` | `polygraph catalog`, `polygraph ask --llm` | no — reads from MySQL |
| `search` | `searchAcrossEntities` | `polygraph catalog --search`, `polygraph ask --llm` | yes |
| `get_lineage` | `searchAcrossLineage` | `polygraph reconcile --declared-via mcp` | yes, plus point-in-time |

The last column is the one that matters when something breaks. A stack whose
search backend is missing still answers `/config`, still serves entity reads, and
still passes every gate in this project except the two that need search — which
is exactly how it stays undiagnosed.

`src/polygraph/dh_mcp.py` holds the transport. `catalog_mcp.py` and
`declared_mcp.py` are the two callers.

---

## The blocker: point-in-time creation fails on the OSS quickstart

`get_lineage` returns HTTP 500:

```
Failed to generate PointInTime Identifier.. Root cause: search
path: ['searchAcrossLineage']
extensions: {code: 500, type: SERVER_ERROR}
```

### It is not a Polygraph bug, and not an MCP Server bug

The MCP Server sends an ordinary `searchAcrossLineage` query — degree filter,
`count`, `skipHighlighting`, nothing exotic. It is the same query **DataHub's own
UI Lineage tab** sends. Reproduced independently of both, straight against
GraphQL, by `scripts/probe_gms.py`.

### Two candidate causes, and the test that separates them

Recorded honestly, because the first diagnosis here was stated with more
confidence than the evidence supported.

**Candidate A — dialect mismatch.** Two GMS defaults collide with how the
quickstart is packaged: `ELASTICSEARCH_IMPLEMENTATION` defaults to
`elasticsearch` and the quickstart compose file does not set it, while
`ELASTICSEARCH_SEARCH_GRAPH_POINT_IN_TIME_CREATION_ENABLED` defaults to `true`,
so every graph query creates a point-in-time snapshot. If the backend is
OpenSearch, GMS is calling the wrong endpoint: Elasticsearch exposes
point-in-time at `POST /<index>/_pit`, OpenSearch at
`POST /_search/point_in_time`.

**Candidate B — the search backend is not reachable at all.** GMS answers
`/config` and serves entity reads regardless, because those come from MySQL. A
stack with no running search container looks healthy from the outside and fails
every search and lineage query.

**The discriminator: `searchAcrossEntities`.** Ordinary search uses no
point-in-time and no graph traversal.

| `searchAcrossLineage` | `searchAcrossEntities` | Diagnosis |
|---|---|---|
| 500 (PointInTime) | works | Candidate A — apply the fix below |
| 500 (PointInTime) | also fails | Candidate B — the backend is down; the PIT error is a symptom |
| works | works | nothing to fix |

`scripts/probe_gms.py` runs both and prints the reading.
`scripts/stack_status.ps1` lists every container, running or stopped, and
checks `:9200`.

Applying the Candidate A fix to a Candidate B stack changes a setting on a
service whose actual problem is a missing peer, and wastes a GMS restart.

### Fix for Candidate A

```powershell
.\scripts\probe_gms.ps1          # read-only diagnosis, changes nothing
.\scripts\fix_gms_search.ps1     # applies ELASTICSEARCH_IMPLEMENTATION=opensearch
.\scripts\fix_gms_search.ps1 -Mode revert
```

`fix_gms_search.ps1` layers `docker/gms-search-override.yml` on top of the
generated quickstart compose file and recreates **only** `datahub-gms`, with
`--no-deps`. No volume is touched; the seeded catalog survives. Nothing the
quickstart wrote is edited.

If the dialect fix alone does not clear it, `-Mode both` additionally sets
`ELASTICSEARCH_SEARCH_GRAPH_POINT_IN_TIME_CREATION_ENABLED=false`
(`docker/gms-nopit-override.yml`). That is the blunter fix: point-in-time gives
a lineage query a consistent view of the index while it pages, so disabling it
trades a correctness guarantee for availability. Fine for a four-dataset demo
catalog; not equivalent to the dialect fix, and not presented as such.

### Consequence for the default

`polygraph reconcile` defaults to `--declared-via sdk`, **not** `mcp`. The
default has to be the path that works on a clean clone against a stock
quickstart, because that is what a judge will run. `--declared-via mcp` is
available and gate-tested, and needs the environment fix above.

---

## A separate finding: the MCP Server cannot report declared job inputs at all

Independent of the point-in-time bug, `get_entities` **cannot** substitute for
`get_lineage` here.

`DataJob` appears in four places in `mcp_server_datahub/gql/entity_details.gql`.
Every one of them selects only:

```
urn  type  dataFlow  jobId  ownership  properties  tags  glossaryTerms  structuredProperties
```

A grep for `inputOutput`, `inputDatasets` or `outputDatasets` across the whole
package returns nothing. The MCP Server never asks DataHub for a data job's
declared inputs and outputs, even though GMS's GraphQL schema exposes
`DataJob.inputOutput`.

So an agent using DataHub's MCP Server can see *that* a job exists, who owns it
and what tags it carries — but cannot ask it what the job is declared to read.
`scripts/probe_gms.py` probe #3 verifies GMS answers that field, which places
the gap in the MCP Server's query rather than in DataHub.

This is a one-field change upstream. Draft in `docs/upstream/`.

---

## Two servers in one agent loop

`polygraph ask --llm` holds tools from both servers at once:

- Polygraph's six tools return **evidence** — what a runtime capture proved.
- `datahub_get_entities` and `datahub_search` return **testimony** — what the
  catalog claims, read through DataHub's MCP Server.

The system prompt requires the model to say "Polygraph observed" for one and
"the catalog says" for the other. The gap between those two is the entire
subject of the project, so blurring them in the answer would defeat it.

Polygraph's **own** MCP server does not re-advertise DataHub's tools —
`test_polygraph_does_not_readvertise_datahubs_tools` enforces that. A client
reading Polygraph's tool list must not mistake catalog testimony for Polygraph
evidence.

The question this composition exists for: Polygraph reports an `UNDECLARED`
source, proven read at runtime. Is that asset registered in the catalog at all,
under some other name? A hit means the catalog knows the asset but not the edge.
A miss means the asset is invisible to the catalog entirely — worse, and a
different fix.

---

## Two bugs of our own, found by running it

Recorded because the errors they produced were both misleading, and the
misleading part is the reusable lesson.

**1. The subprocess was launched without credentials.** `server_env` set
`DATAHUB_GMS_URL` only when a caller passed one explicitly. `gate10_catalog_smoke.py`
did not, so DataHub's MCP Server started with nothing, hit `MissingConfigError`
inside `DataHubClient.from_env()`, and died before serving a single tool. The
stdio transport reported that as:

```
mcp.shared.exceptions.McpError: Connection closed
```

An infrastructure failure wearing a protocol failure's clothes, four layers from
the cause. Fixed two ways: `dh_mcp.resolve_gms()` now always produces a URL
(argument → environment → `~/.datahubenv` → quickstart default) and always sets
it on the subprocess, and `dh_mcp.preflight()` checks GMS answers *before*
launching, so "GMS is down" keeps saying so. `tests/test_dh_mcp.py` is the
regression.

**2. The probe's own GraphQL was wrong.** Probe #2 reported

```
Validation error (FieldUndefined@[entities/ownership/owners/owner/urn])
: Field 'urn' in type 'OwnerType' is undefined
```

which reads like a broken server and is a broken query: `OwnerType` is a union of
`CorpUser` and `CorpGroup`, so `urn` has to be selected inside an inline fragment
per concrete type. The MCP Server sends its own, correct query — so this said
nothing about whether `get_entities` works. The probe now says so in its output
rather than letting a reader conclude the catalog is unreachable.
