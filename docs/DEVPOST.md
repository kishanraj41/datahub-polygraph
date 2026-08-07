# Devpost submission text

Paste into the Devpost form. Track: **Production ML Agents**.

---

## Tagline

A lie detector for data catalogs — Polygraph runs your pipeline, proves what it
actually reads, and writes the verdicts back into DataHub.

---

## Inspiration

Lineage in a data catalog is testimony, not evidence. Someone wrote it down — by
hand, from a DAG, or from a SQL parser — and then the code changed and the
testimony didn't. A stale edge looks identical to a correct one. A missing edge
looks like nothing at all.

There's no mechanism in any catalog for being wrong out loud. So data scientists
make decisions on lineage that has quietly drifted, and the first symptom is a
model that broke for reasons nobody can trace.

I wanted DataHub to be able to catch itself.

## What it does

Polygraph reads the declared ML lineage from DataHub, runs the real pipeline
under runtime capture, reconciles the two edge by edge, and writes verdicts back
into the catalog as tags:

- **`polygraph:verified`** — declared, and runtime proves data flowed along it
- **`polygraph:phantom`** — declared, but nothing flowed in the captured run
- **`polygraph:undeclared-source`** — runtime proves an edge the catalog never mentioned

When a run's model quality collapses, it goes further: it compares the degraded
capture against a healthy baseline, ranks every operation by deviation, names
the responsible one, resolves the owning team from DataHub ownership, and
publishes a sha-256-verified incident report into the knowledge base.

In the demo, a fraud model's catalog entry declares an archive that hasn't been
read since a refactor, and omits a fee schedule that gets merged into the
training set on every run. Polygraph finds both and tags them.

## How I built it

**AutoLineage** ([SSRN 6683825](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6683825))
is my open-source capture library — 239 import-time hooks over pandas and
scikit-learn, no code changes required. Polygraph depends on it from PyPI.

**DataHub** provides both sides of the reconciliation: `dataJobInputOutput` for
the declared claim, and tags, ownership and documents for the write-back. All
via `acryl-datahub` 1.7.0 against an OSS quickstart instance.

The interesting engineering is the bridge. AutoLineage records at *operation*
granularity — dataframe version to dataframe version. DataHub declares at
*dataset* granularity. Three non-obvious decisions made that work:

1. **Anchors.** A node is catalog-visible if it's a file read, a file write, or
   a fitted model. Two anchors form an edge when a path connects them through no
   other anchor.
2. **Union of paths, not shortest path.** AutoLineage links
   `LogisticRegression.fit` straight back to an early hub node, so the shortest
   route from source to model skips the filter, merge and split entirely. Those
   operations really ran. Shortest-path would have left the incident report with
   nothing to name.
3. **Self-loops carry the payload.** When a transform doesn't change dataframe
   identity, AutoLineage emits `parent_id == child_id`. The planted bug *is* one
   of those. Discarding them is right for topology and fatal for diagnosis.

## Challenges

**A silent capture failure.** AutoLineage's IO hooks test
`isinstance(path, str)`, so `pd.read_csv(Path(...))` records nothing at all — no
error, no file lineage, just an empty graph. Found it by noticing every
`filepath` was null. It's an upstream bug and it's in my Limitations section.

**Polygraph almost corrupted the catalog it was auditing.** DataHub's
`GlobalTagsClass` is a whole-aspect write: emitting `[polygraph:phantom]`
replaces *every* tag on the asset. In a real catalog that silently deletes
someone's PII or tier tags. Every write now reads first, preserves everything
outside the `polygraph:` namespace, and removes only stale polygraph tags.

**My own integrity claim was false.** The incident report says its sha-256 lets
you verify the catalog copy against the shipped file byte for byte. On Windows,
`Path.write_text` translates `\n` to `\r\n`, so the file on disk hashed
differently from the digest published to DataHub. Caught it by hashing the
artifact and comparing. Fixed with LF-pinned writes, `.gitattributes`, and a
regression test that fails if CRLF ever reaches an artifact whose hash is
published.

## What I learned

Reconciliation forces you to be precise about what a claim actually means. Early
on I was going to reconcile output edges too — until I realised AutoLineage
can't link a numpy `predict()` output into a newly constructed DataFrame, so a
declared `job → predictions` edge would come back `PHANTOM`. That would have
been a tool limitation reported as a stale catalog edge. Polygraph now scopes to
inputs, which is also what its tags actually claim: `undeclared-source`.

The same instinct drove attributing incidents to the *job* rather than a
dataset. The offending `filter` sits on the path from two different sources.
Picking one would look more impressive and would be a guess presented as a
finding.

## Ask an agent, not the catalog

Polygraph also ships as an **MCP server**. `mcp-server-datahub` lets an agent read
what the catalog claims; Polygraph's server lets the same agent read what the
runtime proved. Six tools — `can_i_trust`, `get_integrity_score`,
`list_undeclared_sources`, `list_phantom_edges`, `get_incident_report`,
`explain_verdict_semantics`.

The constraint I cared most about: **absent evidence must never read as a clean
bill of health.** Every tool returns `evidence_available`, and asked about an
asset Polygraph has not examined, `can_i_trust` says so in those words. A test
asserts the tool returns no edge list at all in that case, because an empty list
reads as "nothing wrong" — which is the exact failure mode this project exists
to complain about.

## Lineage Integrity Score

Written to DataHub as structured properties. The obvious formula —
verified-over-declared — scores a catalog **1.0** when it declares one correct
edge and misses five real ones. Lineage drift is overwhelmingly a *recall*
problem, so the score is the Jaccard index of declared vs observed edges, with
precision and recall reported separately because they point at different fixes.
The demo job scores **0.3333**.

I also dropped a planned confidence weighting: DataHub OSS exposes no per-edge
confidence on `dataJobInputOutput`, so there was nothing to weight by. Inventing
a plausible-looking weight would have been the easy path.

## What's next

- **Proposed lineage** — emit undeclared edges back as suggestions with human approval
- **Upstream PR** to AutoLineage for the `pathlib.Path` capture bug
- **Multi-run evidence** — aggregate captures so conditional edges stop looking phantom

## Built with

`python` · `datahub` · `acryl-datahub` · `autolineage` · `mcp` · `fastmcp` ·
`pandas` · `scikit-learn` · `docker`

## Try it out

- Repo: https://github.com/kishanraj41/datahub-polygraph
- License: Apache 2.0
- Sample outputs: [`examples/`](https://github.com/kishanraj41/datahub-polygraph/tree/main/examples)
