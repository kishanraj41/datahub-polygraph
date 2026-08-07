# Polygraph — demo video script

**Target: 2:40. Hard ceiling 3:00** (Devpost disqualifies over 3:00).

Record at 1920×1080. Browser zoom 110% so tags are legible when Devpost
downscales. Have DataHub already running and seeded **before** you hit record —
the quickstart pull is not part of the story.

**Pre-flight, off camera:**

```powershell
.\scripts\run_gate1.ps1      # only if DataHub is not already up
.\scripts\run_gate2.ps1      # seeds the catalog and writes verdicts
```

Then **undo** the write-back so the tags land live on camera:

```powershell
.venv\Scripts\python.exe demo\seed_catalog.py      # re-seeds clean
```

Have two windows ready: a terminal (dark theme, large font) and Chrome on
<http://localhost:9002>, already logged in.

---

## Beat 1 — Cold open (0:00–0:20)

**Screen:** DataHub lineage view for `train_fraud_model`. Clean graph, two
inputs, one output. Nothing alarming.

**Voiceover:**

> This is a fraud model's lineage in DataHub. Two declared inputs, one output.
> It looks correct.
>
> One of these edges hasn't been real since a refactor last year. And a file
> this model reads on every single run isn't on this diagram at all.
>
> The catalog has no way to tell you that. It's testimony, not evidence.

**Direction:** Do not move the mouse during the last line. Let the clean graph
sit there.

---

## Beat 2 — The claim (0:20–0:40)

**Screen:** Terminal. Show `demo/seed_manifest.json` briefly, then the pipeline
source with the `read_csv` calls visible.

**Voiceover:**

> Polygraph doesn't ask the catalog. It runs the pipeline and watches.
>
> AutoLineage installs 239 import-time hooks over pandas and scikit-learn. No
> code changes, no decorators. Every read, every transform, every fit gets
> recorded.

---

## Beat 3 — The run (0:40–1:15)

**Screen:** Terminal. Type and run, letting output scroll:

```
python demo\pipeline.py --mode healthy
python -m polygraph.cli observe --trace runs\healthy\trace.json --out runs\healthy\observed_graph.json --root .
python -m polygraph.cli reconcile --observed runs\healthy\observed_graph.json
```

**Voiceover:**

> The pipeline runs. Polygraph reduces the operation-level capture to
> dataset-level lineage, then compares it edge by edge against what DataHub
> declares.
>
> Three verdicts. Verified — declared and proven. Phantom — declared, but
> nothing flowed. Undeclared — the runtime proves an edge the catalog never
> mentioned.

**Direction:** Pause on the verdict table. It's the densest information in the
video. Give it three full seconds.

---

## Beat 4 — Verdicts bloom in the UI (1:15–1:55)

**Screen:** Terminal, then cut to Chrome.

```
python -m polygraph.cli writeback --report examples\reconciliation_report.json --document examples\reconciliation_report.md
```

Then in Chrome, in this order:

1. `polygraph.demo.legacy_claims_archive` → **refresh** → `polygraph:phantom` appears
2. `polygraph.demo.fee_schedule` → `polygraph:undeclared-source`
3. Back to the lineage view

**Voiceover:**

> Now it writes the verdicts back where people actually look.
>
> The archive nobody reads: tagged phantom. The fee schedule nobody declared:
> tagged undeclared-source.
>
> Polygraph only ever touches its own namespace. Every other tag on these assets
> is read first and preserved — a tool that accuses your catalog of lying can't
> be the thing that corrupts it.

**Direction:** The refresh revealing the tag is the money shot. Make sure the
tag chip is clearly visible and centered.

---

## Beat 5 — The incident kicker (1:55–2:35)

**Screen:** Terminal.

```
python demo\pipeline.py --mode buggy
python -m polygraph.cli observe --trace runs\buggy\trace.json --out runs\buggy\observed_graph.json --root . --mode buggy
python -m polygraph.cli incident
```

**Voiceover:**

> Now change one line. A filter quantile goes from point nine nine nine to point
> zero five.
>
> F1 drops from 0.83 to zero.
>
> Polygraph compares the two captures, ranks every operation by deviation, and
> names the filter — impact score one point zero, three orders of magnitude
> above anything else. It resolves the owning team from DataHub. And it writes
> an incident report into the knowledge base, hash-verified.

**Screen:** Cut to Chrome, DataHub knowledge base, the incident document open.
Scroll slowly through the metrics table and the ownership line.

---

## Beat 6 — Close (2:35–2:50)

**Screen:** The incident document, showing the "What this does not tell you"
section.

**Voiceover:**

> Every number you just saw came from a real run. The repo ships the artifacts
> and the hashes so you can check.
>
> And it tells you what it can't see — shape-preserving bugs, conditional
> branches, single-run evidence. A lie detector that overclaims is just another
> liar.
>
> Polygraph. Apache 2.0.

---

## Shot list checklist

- [ ] DataHub running and seeded before recording
- [ ] Write-back undone so tags land on camera
- [ ] Terminal font ≥ 16pt, dark theme
- [ ] Browser zoom 110%
- [ ] No personal info in browser tabs, bookmarks bar hidden
- [ ] Total runtime under 3:00 — **check before uploading**
- [ ] Upload to YouTube, **set visibility to Public** (Devpost requires it)

## Things not to say

- Do not say the demo uses the Kaggle credit-card dataset. It uses synthetic
  data. The README is explicit; the video must be too if it mentions data at all.
- Do not claim Polygraph verifies lineage "for all runs". It verifies the
  captured run.
- Do not present the integrity score or `polygraph ask` — those are Ring 2 and
  are not built.
