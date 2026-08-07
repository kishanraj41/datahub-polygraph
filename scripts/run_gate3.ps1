# Polygraph -- Gate 6: the incident path.
#
#     cd C:\dev\polygraph ; .\scripts\run_gate3.ps1
#
# Runs the healthy pipeline to save a baseline fingerprint, runs the buggy
# variant (one changed line: the filter quantile), asks AutoLineage's analyzer
# which operation is responsible, then writes an incident document into
# DataHub's knowledge base and tags the affected job.
#
# Takes about a minute. Writes gate3.log.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "gate3.log"
if (Test-Path $log) { Remove-Item $log }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONHASHSEED = "0"
$env:PYTHONPATH = Join-Path $repo "src"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Die($msg) {
    Log ""; Log "################ GATE 6 RED ################"; Log $msg
    Log "###########################################"; exit 1
}

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Die "No venv at $py. Run .\scripts\run_gate1.ps1 first." }

Push-Location $repo

function Step($label, $argList) {
    Log "----- $label -----"
    Add-Content -Path $log -Value ("$ python " + ($argList -join " "))
    $out = & $py @argList 2>&1 | Out-String
    Write-Host $out; Add-Content -Path $log -Value $out
    if ($LASTEXITCODE -ne 0) { Die "'$label' exited $LASTEXITCODE. Output above and in $log." }
}

# Fresh captures both times, so the incident numbers come from this run.
if (Test-Path (Join-Path $repo "runs")) { Remove-Item (Join-Path $repo "runs") -Recurse -Force }

# --- healthy: establishes the baseline fingerprint the analyzer compares to.
Step "healthy run (saves baseline fingerprint)" @("demo\pipeline.py", "--mode", "healthy")

# --- buggy: one changed line. Loads the baseline, detects anomalies, localises.
Step "buggy run (metric collapse + RCA)" @("demo\pipeline.py", "--mode", "buggy")

# --- observe the healthy run too. run_gate9 and verify_all both reconcile
# --- against runs\healthy\observed_graph.json, and this script wipes runs/ on
# --- entry -- so not exporting it here leaves the next gate with nothing to read.
Step "observe (healthy)" @(
    "-m", "polygraph.cli", "observe",
    "--trace", "runs\healthy\trace.json",
    "--out",   "runs\healthy\observed_graph.json",
    "--root",  ".", "--mode", "healthy"
)

# --- observed graph for the degraded run, so the incident can say where in the
# --- lineage the offending operation sits.
Step "observe (buggy)" @(
    "-m", "polygraph.cli", "observe",
    "--trace", "runs\buggy\trace.json",
    "--out",   "runs\buggy\observed_graph.json",
    "--root",  ".", "--mode", "buggy"
)

# --- sanity: the collapse must actually have happened, and RCA must have fired.
$base = Get-Content "runs\healthy\metrics.json" -Raw | ConvertFrom-Json
$bad  = Get-Content "runs\buggy\metrics.json"   -Raw | ConvertFrom-Json
Log ("baseline F1 = {0}   degraded F1 = {1}" -f $base.f1, $bad.f1)
if ($null -eq $bad.root_cause) {
    Die @"
The buggy run produced no root cause.

Most likely the baseline fingerprint was missing, so the analyzer had nothing to
compare against. Check runs\baseline_fingerprint.json exists and that the
healthy run reported 'fingerprint_saved'.
"@
}
Log ("root cause  = {0}  (impact {1})" -f $bad.root_cause.operation, $bad.root_cause.impact)
if ($bad.f1 -ge $base.f1) {
    Die "Expected the buggy run's F1 to be lower than baseline. Got $($bad.f1) vs $($base.f1)."
}

# --- publish the incident.
Step "incident -> DataHub knowledge base" @(
    "-m", "polygraph.cli", "incident",
    "--baseline", "runs\healthy\metrics.json",
    "--degraded", "runs\buggy\metrics.json",
    "--observed", "runs\buggy\observed_graph.json",
    "--out-md",   "examples\incident_report.md",
    "--gms",      "http://localhost:8080"
)

Pop-Location

Log ""
Log "==================== GATE 6 GREEN ===================="
Log "In the UI (login datahub / datahub):"
Log "  search the knowledge base for 'Polygraph incident'"
Log "  the job now carries polygraph:incident:"
Log "    http://localhost:9002/tasks/urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"
Log ""
Log "RING 1 COMPLETE once you have eyes on the above."
Log "====================================================="
