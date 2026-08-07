# Polygraph -- the real Phase 9 gate.
#
#     cd C:\dev\polygraph ; .\scripts\verify.ps1
#
# The earlier fresh-clone check was too weak: it ran the pipeline and the test
# suite, saw green, and moved on -- while five of the tests SILENTLY SKIPPED.
# The five that skipped were exactly the oracle tests that prove the verdicts
# are correct. Green with an unread skip count is indistinguishable from green.
#
# This version:
#   * clones the PUSHED repo, so it tests what a judge actually gets
#   * installs from requirements.txt only
#   * runs the FULL README quickstart against the live DataHub
#   * asserts the F1 the README publishes, to 1e-12
#   * asserts the verdict counts, not just "the command exited 0"
#   * FAILS if more tests skip than expected
#
# Needs DataHub running. Writes verify.log.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "verify.log"
if (Test-Path $log) { Remove-Item $log }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONHASHSEED = "0"

# A fresh clone has no runs/, so the two fixture-vs-live drift checks have
# nothing to compare against. Everything else must actually execute.
$EXPECTED_MAX_SKIPS = 2
$EXPECTED_MIN_PASSES = 46
$EXPECTED_F1 = 0.8282290279627164

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Die($msg) {
    Log ""; Log "############### VERIFY FAILED ###############"; Log $msg
    Log "############################################"
    if ($tmp -and (Test-Path $tmp)) { Log "Clone left at $tmp for inspection." }
    exit 1
}

# --- DataHub must be up; the README quickstart depends on it.
try {
    $r = Invoke-WebRequest -Uri "http://localhost:8080/config" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -ne 200) { throw }
} catch {
    Die "DataHub is not answering on :8080. Start it (scripts\run_gate1.ps1) and re-run."
}
Log "DataHub is up."

$remote = (git -C $repo remote get-url origin)
$tmp = Join-Path $env:TEMP ("polygraph-verify-" + [System.IO.Path]::GetRandomFileName().Substring(0,6))
Log "Cloning $remote"
git clone --quiet $remote $tmp 2>&1 | ForEach-Object { Log "  $_" }
if (-not (Test-Path (Join-Path $tmp "README.md"))) { Die "Clone failed or is empty." }

Push-Location $tmp

Log "----- clean venv from requirements.txt only -----"
python -m venv .venv 2>&1 | Out-Null
$py = Join-Path $tmp ".venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip 2>&1 | Out-Null
& $py -m pip install --quiet -r requirements.txt 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) { Pop-Location; Die "pip install failed in a clean environment." }
$env:PYTHONPATH = Join-Path $tmp "src"

function Step($label, $argList) {
    Log "----- $label -----"
    $out = & $py @argList 2>&1 | Out-String
    Write-Host $out; Add-Content -Path $log -Value $out
    if ($LASTEXITCODE -ne 0) { Pop-Location; Die "'$label' exited $LASTEXITCODE." }
    return $out
}

# ============================================ the README quickstart, verbatim
Step "README step 2: seed the catalog" @("demo\seed_catalog.py") | Out-Null
Step "README step 3: run the pipeline" @("demo\pipeline.py", "--mode", "healthy") | Out-Null

$m = Get-Content "runs\healthy\metrics.json" -Raw | ConvertFrom-Json
Log ("F1 from the clone = {0}" -f $m.f1)
if ([math]::Abs($m.f1 - $EXPECTED_F1) -gt 1e-12) {
    Pop-Location
    Die "Clone produced F1 = $($m.f1); the README publishes $EXPECTED_F1. The README would be lying to a judge."
}
Log "F1 matches the README exactly."

Step "README step 4: observe" @(
    "-m", "polygraph.cli", "observe",
    "--trace", "runs\healthy\trace.json",
    "--out", "runs\healthy\observed_graph.json", "--root", "."
) | Out-Null

Step "README step 5: reconcile" @(
    "-m", "polygraph.cli", "reconcile",
    "--observed", "runs\healthy\observed_graph.json",
    "--out-json", "examples\reconciliation_report.json",
    "--out-md", "examples\reconciliation_report.md",
    "--allow-discrepancies"
) | Out-Null

# Assert the actual verdicts, not merely that the command succeeded.
$rep = Get-Content "examples\reconciliation_report.json" -Raw | ConvertFrom-Json
Log ("verdicts: VERIFIED={0} PHANTOM={1} UNDECLARED={2}" -f `
    $rep.summary.VERIFIED, $rep.summary.PHANTOM, $rep.summary.UNDECLARED)
if ($rep.summary.VERIFIED -ne 1 -or $rep.summary.PHANTOM -ne 1 -or $rep.summary.UNDECLARED -ne 1) {
    Pop-Location; Die "Expected exactly 1/1/1. The demo's headline claim does not reproduce."
}
$undeclared = @($rep.verdicts | Where-Object { $_.verdict -eq "UNDECLARED" })
if ($undeclared[0].upstream -notlike "*fee_schedule*") {
    Pop-Location; Die "The UNDECLARED verdict is not on fee_schedule. The demo story does not reproduce."
}
Log "Verdicts reproduce, and the shadow input is the expected one."

Step "README step 6: writeback" @(
    "-m", "polygraph.cli", "writeback",
    "--report", "examples\reconciliation_report.json",
    "--document", "examples\reconciliation_report.md"
) | Out-Null

Step "incident path" @("demo\pipeline.py", "--mode", "buggy") | Out-Null
Step "observe (buggy)" @(
    "-m", "polygraph.cli", "observe", "--trace", "runs\buggy\trace.json",
    "--out", "runs\buggy\observed_graph.json", "--root", ".", "--mode", "buggy"
) | Out-Null
Step "incident" @("-m", "polygraph.cli", "incident") | Out-Null
Step "integrity score" @("-m", "polygraph.cli", "score") | Out-Null

# ==================================================== tests, with skip policing
Log "----- test suite (skips are policed) -----"
$out = & $py -m pytest tests -q -rs 2>&1 | Out-String
Write-Host $out; Add-Content -Path $log -Value $out

$passed = 0; $skipped = 0; $failed = 0
if ($out -match '(\d+) passed')  { $passed  = [int]$Matches[1] }
if ($out -match '(\d+) skipped') { $skipped = [int]$Matches[1] }
if ($out -match '(\d+) failed')  { $failed  = [int]$Matches[1] }
Log "passed=$passed skipped=$skipped failed=$failed"

if ($failed -gt 0) { Pop-Location; Die "$failed test(s) failed in a fresh clone." }
if ($skipped -gt $EXPECTED_MAX_SKIPS) {
    Pop-Location
    Die @"
$skipped tests skipped; at most $EXPECTED_MAX_SKIPS is expected.

A skipped test reads as green but proves nothing. This exact failure already
happened once: the oracle tests skipped in a fresh clone because no capture
existed, so "9 passed, 5 skipped" looked fine while nothing about the verdicts
had been checked. Find out which tests skipped (listed above) before shipping.
"@
}
if ($passed -lt $EXPECTED_MIN_PASSES) {
    Pop-Location
    Die "Only $passed tests passed; at least $EXPECTED_MIN_PASSES expected. Tests may have been silently deselected."
}
Log "Test suite ran with no unexpected skips."

# ============================================ digest verification, as a judge would
Log "----- verifying the published sha-256 the way a judge would -----"
$incidentPath = Join-Path $tmp "examples\incident_report.md"
$bytes = [System.IO.File]::ReadAllBytes($incidentPath)
$sha = [System.BitConverter]::ToString(
    [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
).Replace("-", "").ToLower()
Log "examples\incident_report.md sha256 = $sha"
for ($i = 0; $i -lt $bytes.Length - 1; $i++) {
    if ($bytes[$i] -eq 13 -and $bytes[$i+1] -eq 10) {
        Pop-Location; Die "incident_report.md in the clone contains CRLF; its published digest will not verify."
    }
}
Log "No CRLF. Digest is verifiable."

Pop-Location
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Log ""
Log "==================== VERIFIED ===================="
Log "  A clean clone, following only the README, reproduces:"
Log "    - F1 $EXPECTED_F1 exactly"
Log "    - verdicts 1 VERIFIED / 1 PHANTOM / 1 UNDECLARED"
Log "    - the shadow input on fee_schedule"
Log "    - the incident, the score, and the write-back"
Log "    - $passed tests passing, $skipped skipped (policed)"
Log "  PHASE 9 GATE: GREEN"
Log "================================================="
