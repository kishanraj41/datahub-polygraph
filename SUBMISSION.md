# Submission checklist

Deadline: **Aug 10, 2026 @ 5:00pm EDT** (4:00pm CT).
Track: Production ML Agents. Repo: https://github.com/kishanraj41/datahub-polygraph

Run `.\scripts\verify_all.ps1` before submitting. Exit 0 = ready. Exit 2 = only
human items outstanding. Exit 1 = a real defect.

## Code — done

- [x] Public repo, Apache-2.0 detectable in the About box
- [x] 54 tests, 0 skipped, hermetic (no DataHub required)
- [x] Fresh clone reproduces the README exactly (F1 to 1e-12)
- [x] Ring 1 — reconcile, tag write-back, incident report
- [x] Ring 2 — integrity score, MCP server, `polygraph ask`
- [x] Ring 3 — lineage proposals with revert, upstream PR drafted
- [x] `examples/` with 10 real artifacts, digests verifiable

## Yours — in the order I'd do them

- [ ] **Look at the DataHub UI.** Nobody has. Every gate reports tags landing and
      reads them back clean, but no human has seen one render. If they look wrong
      on screen, everything else is moot.
      - `polygraph:phantom` on `polygraph.demo.legacy_claims_archive`
      - `polygraph:undeclared-source` on `polygraph.demo.fee_schedule`
      - the incident document in the knowledge base
      - the integrity score as a structured property on the job

- [ ] **Register the MCP server** with Claude Desktop and ask one real question.
      Config ready at `docs/claude_desktop_config.example.json`. Gate 8 proves the
      tools work over stdio; it does not prove the config file is right.

- [ ] **Screenshots** into `examples/` — the two tags and the incident document.

- [ ] **Record the video.** `docs/VIDEO_SCRIPT.md`, 7 beats, under 3:00.
      Pre-flight is in the script. Upload to YouTube, **set to Public**.

- [ ] **Devpost submission.** Text ready in `docs/DEVPOST.md`.

- [ ] **Paper 2 SSRN URL** — replace `[KISHAN: Paper 2 SSRN URL]` in README.
      This is the only check `verify_all` still reports red.

- [ ] **GitHub About box** — description and topics. Path A of `publish.ps1`
      didn't set them.

## Optional

- [ ] Open the upstream PR (`docs/upstream/PR_DRAFT.md`). A real fix to a real
      silent bug, with a test. Your call whether before or after judging.

## If something breaks the day of

```powershell
.\scripts\run_gate1.ps1     # DataHub down
demo\seed_catalog.py        # catalog in a weird state — resets to 1/1/1
.\scripts\verify_all.ps1    # tells you what is actually wrong
```

## Numbers — use these, nothing else

| | |
| --- | --- |
| baseline F1 | 0.8282290279627164 |
| degraded F1 | 0.0000 |
| root cause | `filter`, impact 1.0 |
| integrity score | 0.3333 (precision 0.5, recall 0.5) |
| incident digest | `acbedff47da6255e6b69877f722e52c2421f711e560d8517919e04bfe12ee5d3` |
| owner | `urn:li:corpGroup:ml-platform-team` |
