# Polygraph and DataHub's MCP Server

Polygraph reaches DataHub two ways: the `acryl-datahub` SDK, and **DataHub's own
MCP Server** (`mcp-server-datahub`), launched as a stdio subprocess exactly the
way an agent client launches it.

This document records what works, what does not, and why — including a failure
that took most of a day to isolate, and a wrong diagnosis that is kept here on
purpose because the way it was wrong is the useful part.

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

## The blocker: a dead search container, misread as a config problem

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

### Root cause: the search container was dead

`datahub-opensearch-1` had **exited (127)** while the rest of the stack stayed up
for 45 hours. GMS reaches its search backend through the compose service alias
`search`, so with the container gone:

```
java.net.UnknownHostException: search
    at ...OpenSearch2SearchClientShim.search(OpenSearch2SearchClientShim.java:312)
    at ...ESSearchDAO.lambda$executeAndExtract$4(ESSearchDAO.java:259)
```

`Root cause: search` in the original 500 was the **hostname**. It was never a
subsystem name, and reading it as one cost most of a day.

A stack in this state looks healthy from every angle that matters to a casual
check: GMS answers `/config`, reports `healthy` to Docker, serves every entity
read, and passes every gate in this project except the two that need search.
Metadata lives in MySQL; only the search and graph *indices* live in OpenSearch.

### What this ruled out, recorded because it was asserted first

This document previously claimed the cause was a dialect mismatch —
`ELASTICSEARCH_IMPLEMENTATION` defaulting to `elasticsearch` against an
OpenSearch backend. **That was wrong**, and the evidence against it was already
in the stack trace: `OpenSearch2SearchClientShim` is the OpenSearch client. GMS
1.7 auto-detects the engine and had detected it correctly the whole time.

The mistake was reasoning from documentation (`ELASTICSEARCH_IMPLEMENTATION`
defaults to `elasticsearch`; the quickstart runs OpenSearch; therefore mismatch)
without checking the one fact that would have settled it — whether the search
container was running. Two things would have caught it sooner, and both are now
in the probe:

* **`searchAcrossEntities` as a discriminator.** Ordinary search uses no
  point-in-time. It failed too, which no dialect theory explains.
* **Container-level facts before service-level theories.**
  `scripts/stack_status.ps1` lists every container running *or stopped*. The
  original probe only grepped `docker ps` for images, and reported
  "no opensearch container found" as a footnote rather than as the answer.

The fix scripts written for the wrong diagnosis (`fix_gms_search.ps1`,
`docker/gms-*.yml`) have been deleted rather than kept "just in case". Shipping a
remedy for a cause that did not exist is how a repo teaches its next reader the
wrong lesson.

### Fix

```powershell
.\scripts\stack_status.ps1        # read-only: what is actually running
.\scripts\fix_search_backend.ps1  # capture why it died, start it, restart GMS, re-probe
```

`fix_search_backend.ps1` reads the dead container's logs *before* restarting it —
a container killed for disk or memory will die again, and finding that out
mid-demo is worse than finding it out now. If the indices did not survive, it
prints the `system-update` command that rebuilds them from MySQL.

### Consequence for the default

`polygraph reconcile` defaults to `--declared-via sdk`, **not** `mcp`. The
default has to survive a partly-degraded stack: the SDK path reads the
`dataJobInputOutput` aspect from MySQL and kept working throughout the outage,
while every search-backed path was down. That is not a preference for the SDK
as an interface — it is a preference for the default that still answers when
something is broken.

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
