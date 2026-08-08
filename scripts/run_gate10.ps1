# Polygraph -- Gate 10: read declared lineage through DataHub's MCP Server.
#
#     cd C:\dev\polygraph ; .\scripts\run_gate10.ps1
#
# The hackathon requires a project to use "the open-source platform together
# with at least one of: the MCP Server, Agent Context Kit, DataHub Skills, or
# Analytics Agent". Until now Polygraph talked to DataHub only through the
# acryl-datahub SDK and shipped its OWN MCP server -- which is not the same
# thing. `reconcile --declared-via mcp` reads the catalog's claim through
# DataHub's MCP Server, launched as a stdio subprocess.
#
# This gate proves the two paths agree. The SDK path reads the aspect directly
# and is the known-good oracle; if the MCP path disagrees, that is either a bug
# in the parser or a real difference between the stored aspect and the
# agent-facing API. Both matter, so any divergence is red.
#
# Needs DataHub running and the catalog seeded.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "gate10.log"
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
    Log ""; Log "################ GATE 10 RED ################"; Log $msg
    Log "############################################"; exit 1
}

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Die "No venv. Run scripts\run_gate1.ps1 first." }

try {
    $r = Invoke-WebRequest -Uri "http://localhost:8080/config" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -ne 200) { throw }
} catch { Die "DataHub is not answering on :8080." }
Log "DataHub is up."

Push-Location $repo

# mcp-server-datahub must be on PATH inside the venv.
$server = Join-Path $repo ".venv\Scripts\mcp-server-datahub.exe"
if (-not (Test-Path $server)) {
    Die @"
mcp-server-datahub is not installed in the venv.

    .venv\Scripts\pip.exe install -r requirements.txt

It is already pinned in requirements.txt; this only fails if the venv predates it.
"@
}
Log "mcp-server-datahub found at $server"

# The healthy capture is what both paths reconcile against.
if (-not (Test-Path "runs\healthy\observed_graph.json")) {
    Log "No healthy capture; regenerating."
    & $py demo\pipeline.py --mode healthy 2>&1 | Out-Null
    & $py -m polygraph.cli observe --trace "runs\healthy\trace.json" `
        --out "runs\healthy\observed_graph.json" --root "." --mode healthy 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Pop-Location; Die "Could not regenerate the healthy capture." }
}

function ReconcileVia($via, $jsonPath) {
    Log "----- reconcile --declared-via $via -----"
    if (Test-Path $jsonPath) { Remove-Item $jsonPath -Force }
    $md = [System.IO.Path]::ChangeExtension($jsonPath, ".md")
    $out = & $py -m polygraph.cli reconcile `
        --observed "runs\healthy\observed_graph.json" `
        --declared-via $via `
        --out-json $jsonPath --out-md $md --allow-discrepancies 2>&1 | Out-String
    Write-Host $out; Add-Content -Path $log -Value $out
    if ($LASTEXITCODE -ne 0) { Pop-Location; Die "reconcile --declared-via $via exited $LASTEXITCODE.`n$out" }
    if (-not (Test-Path $jsonPath)) { Pop-Location; Die "reconcile --declared-via $via wrote no report." }
    return (Get-Content $jsonPath -Raw | ConvertFrom-Json)
}

$sdk = ReconcileVia "sdk" "$env:TEMP\pg_sdk.json"
$mcp = ReconcileVia "mcp" "$env:TEMP\pg_mcp.json"

# --- the comparison that matters -----------------------------------------
Log ""
Log "----- comparing the two paths -----"
Log ("SDK : V={0} P={1} U={2}" -f $sdk.summary.VERIFIED, $sdk.summary.PHANTOM, $sdk.summary.UNDECLARED)
Log ("MCP : V={0} P={1} U={2}" -f $mcp.summary.VERIFIED, $mcp.summary.PHANTOM, $mcp.summary.UNDECLARED)

if ($sdk.summary.VERIFIED   -ne $mcp.summary.VERIFIED -or
    $sdk.summary.PHANTOM    -ne $mcp.summary.PHANTOM  -or
    $sdk.summary.UNDECLARED -ne $mcp.summary.UNDECLARED) {
    Pop-Location
    Die @"
The two paths disagree on the verdict counts.

The SDK reads the dataJobInputOutput aspect directly and is the oracle. A
divergence means either the MCP response parser is picking up the wrong URNs, or
DataHub's agent-facing lineage API genuinely reports something different from the
stored aspect. Both are real findings -- do not ship until you know which.

Reports: $env:TEMP\pg_sdk.json and $env:TEMP\pg_mcp.json
"@
}

# Compare verdict-by-verdict, not just the totals.
$sdkPairs = ($sdk.verdicts | ForEach-Object { "$($_.verdict)|$($_.upstream)|$($_.downstream)" } | Sort-Object) -join "`n"
$mcpPairs = ($mcp.verdicts | ForEach-Object { "$($_.verdict)|$($_.upstream)|$($_.downstream)" } | Sort-Object) -join "`n"
if ($sdkPairs -ne $mcpPairs) {
    Pop-Location
    Die @"
Verdict counts match but the per-edge verdicts differ. Totals agreeing while
edges disagree is worse than a plain mismatch -- a summary check would have
passed this.

SDK:
$sdkPairs

MCP:
$mcpPairs
"@
}
Log "Per-edge verdicts are identical across both paths."

if ($mcp.declared_source -notlike "*MCP Server*") {
    Pop-Location; Die "The MCP run did not record declared_source as the MCP Server. Got: $($mcp.declared_source)"
}
Log "Report records declared_source = $($mcp.declared_source)"

Pop-Location

Log ""
Log "==================== GATE 10 GREEN ===================="
Log "  Declared lineage read through DataHub's MCP Server,"
Log "  verdicts identical to the SDK oracle, per edge."
Log ""
Log "  reconcile now defaults to --declared-via mcp."
Log "======================================================"
