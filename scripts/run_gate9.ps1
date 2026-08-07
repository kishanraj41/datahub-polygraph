# Polygraph -- Gate 9: Ring 3, lineage proposals with human approval.
#
#     cd C:\dev\polygraph ; .\scripts\run_gate9.ps1
#
# This is the only part of Polygraph that changes what the CATALOG CLAIMS.
# Everything else writes into the polygraph: namespace and is additive.
#
# The script runs three phases:
#   1. dry run    -- nothing applied, proposals written to examples/
#   2. approve    -- applies the ADDITION only (fee_schedule), verifies, snapshots revert
#   3. revert     -- restores the original lineage and verifies the restore
#
# It deliberately does NOT exercise --remove-phantom. Removing the archive edge
# would leave your demo catalog in a state where the PHANTOM verdict no longer
# reproduces, and phantom removal is not safe from a single run anyway. Run that
# by hand if you want to see it.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "gate9.log"
if (Test-Path $log) { Remove-Item $log }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = Join-Path $repo "src"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Die($msg) {
    Log ""; Log "################ GATE 9 RED ################"; Log $msg
    Log "###########################################"; exit 1
}

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Die "No venv. Run scripts\run_gate1.ps1 first." }

try {
    $r = Invoke-WebRequest -Uri "http://localhost:8080/config" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -ne 200) { throw }
} catch { Die "DataHub is not answering on :8080." }

Push-Location $repo
$revert = "examples\lineage_revert.json"

# run_gate3.ps1 only runs `observe` for the buggy mode, so the healthy observed
# graph is not guaranteed to exist. Gate 9 needs it to re-reconcile after each
# phase. Regenerate rather than assuming.
if (-not (Test-Path "runs\healthy\observed_graph.json")) {
    Log "runs\healthy\observed_graph.json missing; regenerating the healthy capture."
    & $py demo\pipeline.py --mode healthy 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Pop-Location; Die "Could not run the healthy pipeline." }
    & $py -m polygraph.cli observe --trace "runs\healthy\trace.json" `
        --out "runs\healthy\observed_graph.json" --root "." --mode healthy 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Pop-Location; Die "Could not export the healthy observed graph." }
    Log "Healthy capture regenerated."
}

function Reconcile($jsonPath) {
    <#
      Reconcile and return the parsed report. Fails loudly if the command did
      not succeed or did not write the file, instead of parsing a missing file
      and reporting the resulting empty value as a product fault -- which is
      exactly what the first version of this script did.
    #>
    if (Test-Path $jsonPath) { Remove-Item $jsonPath -Force }
    $mdPath = [System.IO.Path]::ChangeExtension($jsonPath, ".md")
    $o = & $py -m polygraph.cli reconcile --observed "runs\healthy\observed_graph.json" `
        --out-json $jsonPath --out-md $mdPath --allow-discrepancies 2>&1 | Out-String
    Add-Content -Path $log -Value $o
    if ($LASTEXITCODE -ne 0) {
        Pop-Location; Die "reconcile exited $LASTEXITCODE. This is a harness or tool failure, not a catalog change.`n$o"
    }
    if (-not (Test-Path $jsonPath)) {
        Pop-Location; Die "reconcile reported success but wrote no report at $jsonPath. Harness failure.`n$o"
    }
    $parsed = Get-Content $jsonPath -Raw | ConvertFrom-Json
    if ($null -eq $parsed.summary.UNDECLARED) {
        Pop-Location; Die "reconcile report at $jsonPath has no summary. Harness failure, not a catalog change."
    }
    return $parsed
}

function Run($label, $argList) {
    Log "----- $label -----"
    $out = & $py @argList 2>&1 | Out-String
    Write-Host $out; Add-Content -Path $log -Value $out
    if ($LASTEXITCODE -ne 0) { Pop-Location; Die "'$label' exited $LASTEXITCODE." }
    return $out
}

# ---------------------------------------------------------------- 1. dry run
$out = Run "phase 1: dry run (nothing applied)" @(
    "-m","polygraph.cli","propose","--report","examples\reconciliation_report.json"
)
if ($out -notmatch "NOTHING APPLIED") { Pop-Location; Die "Dry run did not announce that nothing was applied." }
if ($out -notmatch "requires --remove-phantom") { Pop-Location; Die "Dry run did not gate the phantom removal." }
Log "Dry run applied nothing and gated the removal, as intended."

# Confirm the catalog is genuinely untouched.
$b = Reconcile "$env:TEMP\pg_before.json"
if ($b.summary.UNDECLARED -ne 1) { Pop-Location; Die "Dry run changed the catalog: UNDECLARED is $($b.summary.UNDECLARED), expected 1." }
Log "Catalog verified unchanged after the dry run."

# ---------------------------------------------------------------- 2. approve
Run "phase 2: approve the addition" @(
    "-m","polygraph.cli","propose","--report","examples\reconciliation_report.json","--approve"
) | Out-Null

if (-not (Test-Path $revert)) { Pop-Location; Die "No revert snapshot was written. Refusing to trust an irreversible change." }
Log "Revert snapshot exists at $revert"

# The real proof: the undeclared source should now be declared, so reconciling
# again must show UNDECLARED drop to 0 and VERIFIED rise.
$a = Reconcile "$env:TEMP\pg_after.json"
Log ("after approve -- VERIFIED={0} PHANTOM={1} UNDECLARED={2}" -f $a.summary.VERIFIED, $a.summary.PHANTOM, $a.summary.UNDECLARED)
if ($a.summary.UNDECLARED -ne 0) {
    Pop-Location; Die "Expected UNDECLARED=0 after declaring the shadow input, got $($a.summary.UNDECLARED)."
}
if ($a.summary.VERIFIED -ne 2) {
    Pop-Location; Die "Expected VERIFIED=2 after the addition, got $($a.summary.VERIFIED)."
}
if ($a.summary.PHANTOM -ne 1) {
    Pop-Location; Die "PHANTOM should still be 1 -- the removal was never approved. Got $($a.summary.PHANTOM)."
}
Log "The shadow input is now declared; the phantom edge is untouched. Correct."

# ---------------------------------------------------------------- 3. revert
Run "phase 3: revert" @("-m","polygraph.cli","propose","--revert",$revert) | Out-Null

$rv = Reconcile "examples\reconciliation_report.json"
Log ("after revert -- VERIFIED={0} PHANTOM={1} UNDECLARED={2}" -f $rv.summary.VERIFIED, $rv.summary.PHANTOM, $rv.summary.UNDECLARED)
if ($rv.summary.VERIFIED -ne 1 -or $rv.summary.PHANTOM -ne 1 -or $rv.summary.UNDECLARED -ne 1) {
    Pop-Location
    Die @"
Revert did not restore the original 1/1/1 state.

Your demo catalog is now in a modified state, which will break the video and the
verify gate. Re-seed it:
    .venv\Scripts\python.exe demo\seed_catalog.py
"@
}
Log "Revert restored the catalog exactly. Demo state is intact."

Pop-Location

Log ""
Log "==================== GATE 9 GREEN ===================="
Log "  Proposals are dry-run by default, additions are evidence-gated,"
Log "  removals need a second explicit flag, and every applied change"
Log "  is reversible and was reverted cleanly."
Log ""
Log "  Upstream PR draft (NOT opened, for your review):"
Log "    docs\upstream\PR_DRAFT.md"
Log "    docs\upstream\autolineage-pathlib-fix.patch"
Log "====================================================="
