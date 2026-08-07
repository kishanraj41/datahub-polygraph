# Polygraph -- Gates 2 through 5, end to end.
#
#     cd C:\dev\polygraph ; .\scripts\run_gate2.ps1
#
#   Gate 2  seed the catalog with deliberately imperfect lineage
#   Gate 3  run the pipeline under AutoLineage capture -> observed graph
#   Gate 4  reconcile declared vs observed -> verdicts
#   Gate 5  write verdicts back as tags + publish the report as a document
#
# Stops at the first red gate. Nothing here pulls images, so it runs in about a
# minute. Writes gate2.log.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "gate2.log"
if (Test-Path $log) { Remove-Item $log }

# The DataHub CLI prints check marks. A cp1252 console cannot encode them, which
# produced two UnicodeEncodeError tracebacks in Gate 1 *after* the work had
# already succeeded. Forcing UTF-8 stops cosmetic noise from looking like
# failure -- and stops it from masking a real one.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONHASHSEED = "0"
$env:PYTHONPATH = Join-Path $repo "src"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Die($gate, $msg) {
    Log ""
    Log "################ $gate RED ################"
    Log $msg
    Log "##########################################"
    exit 1
}

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Die "SETUP" "No venv at $py. Run .\scripts\run_gate1.ps1 first." }

Push-Location $repo

function Step($gate, $label, $argList) {
    Log "----- $gate : $label -----"
    Add-Content -Path $log -Value ("$ python " + ($argList -join " "))
    $out = & $py @argList 2>&1 | Out-String
    Write-Host $out; Add-Content -Path $log -Value $out
    if ($LASTEXITCODE -ne 0) {
        Die $gate "'$label' exited $LASTEXITCODE. Output is above and in $log."
    }
}

# ===================================================== GATE 2: seed catalog
Step "GATE 2" "seed the catalog" @("demo\seed_catalog.py", "--gms", "http://localhost:8080")

# ============================================= GATE 3: capture + observe
# Fresh capture each run so the numbers in the report are from this run.
if (Test-Path (Join-Path $repo "runs")) { Remove-Item (Join-Path $repo "runs") -Recurse -Force }
Step "GATE 3" "run pipeline (healthy)" @("demo\pipeline.py", "--mode", "healthy")
Step "GATE 3" "export observed graph" @(
    "-m", "polygraph.cli", "observe",
    "--trace", "runs\healthy\trace.json",
    "--out",   "runs\healthy\observed_graph.json",
    "--root",  ".",
    "--mode",  "healthy"
)

# ================================================== GATE 4: reconcile
# reconcile exits 2 when discrepancies exist -- which is the expected, correct
# outcome here, because the seeded catalog is deliberately wrong. Allow it and
# assert on the verdict counts instead.
Log "----- GATE 4 : reconcile declared vs observed -----"
$out = & $py -m polygraph.cli reconcile `
    --observed "runs\healthy\observed_graph.json" `
    --urn-map  "demo\urn_map.yaml" `
    --gms      "http://localhost:8080" `
    --out-json "examples\reconciliation_report.json" `
    --out-md   "examples\reconciliation_report.md" `
    --allow-discrepancies 2>&1 | Out-String
Write-Host $out; Add-Content -Path $log -Value $out
if ($LASTEXITCODE -ne 0) { Die "GATE 4" "reconcile exited $LASTEXITCODE" }

# The oracle: one of each verdict, exactly as seed_manifest.json specifies.
$report = Get-Content "examples\reconciliation_report.json" -Raw | ConvertFrom-Json
$v = $report.summary.VERIFIED; $p = $report.summary.PHANTOM; $u = $report.summary.UNDECLARED
Log "verdicts -- VERIFIED=$v PHANTOM=$p UNDECLARED=$u"
if ($v -lt 1 -or $p -lt 1 -or $u -lt 1) {
    Die "GATE 4" @"
Expected at least one of each verdict against the seeded catalog.
Got VERIFIED=$v PHANTOM=$p UNDECLARED=$u.

If PHANTOM is 0, the seed did not include legacy_claims_archive as an input.
If UNDECLARED is 0, the pipeline did not read fee_schedule.csv, or urn_map.yaml
does not map it. Check examples\reconciliation_report.md for unmapped nodes.
"@
}

Step "GATE 4" "oracle test" @("-m", "pytest", "tests", "-q")

# ============================================ GATE 5: write back to DataHub
Step "GATE 5" "apply tags and publish the report" @(
    "-m", "polygraph.cli", "writeback",
    "--report",   "examples\reconciliation_report.json",
    "--document", "examples\reconciliation_report.md",
    "--gms",      "http://localhost:8080"
)

Pop-Location

Log ""
Log "==================== GATES 2-5 GREEN ===================="
Log "Look at these in the UI (login datahub / datahub):"
Log "  phantom tag     http://localhost:9002/dataset/urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.legacy_claims_archive,PROD)"
Log "  undeclared tag  http://localhost:9002/dataset/urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.fee_schedule,PROD)"
Log "  lineage         http://localhost:9002/tasks/urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)/Lineage"
Log "  the document    search 'Polygraph reconciliation' in the UI"
Log "========================================================"
