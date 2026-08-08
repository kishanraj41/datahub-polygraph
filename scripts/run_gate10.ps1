# Polygraph -- Gate 10: reach DataHub through DataHub's own MCP Server.
#
#     cd C:\dev\polygraph ; .\scripts\run_gate10.ps1
#
# The hackathon requires a project to use "the open-source platform together
# with at least one of: the MCP Server, Agent Context Kit, DataHub Skills, or
# Analytics Agent". Polygraph ships its OWN MCP server, which is not the same
# thing. This gate is the evidence that `mcp-server-datahub` is on a real path.
#
# TWO PARTS, because they have different dependencies:
#
#   10a  catalog context via get_entities + search.
#        Neither touches searchAcrossLineage. This is the gate that must be
#        green -- it is what satisfies the requirement.
#
#   10b  declared lineage via get_lineage.
#        Resolves to searchAcrossLineage, which 500s on a stock OSS quickstart
#        (GMS speaks the Elasticsearch dialect to an OpenSearch backend). If it
#        fails with THAT signature, it is reported as environment-blocked with
#        the fix command. Any OTHER failure is red -- a known bug is an excuse
#        for exactly one error message and no others.
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

# The server must be importable by the SAME interpreter Polygraph runs under --
# that is how dh_mcp launches it (python -m mcp_server_datahub).
& $py -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('mcp_server_datahub') else 1)"
if ($LASTEXITCODE -ne 0) {
    Die @"
mcp_server_datahub is not importable by $py

    .venv\Scripts\pip.exe install -r requirements.txt

It is already pinned in requirements.txt; this only fails if the venv predates it.
"@
}
Log "mcp_server_datahub is importable by the venv interpreter."

Push-Location $repo

# =========================================================== GATE 10a
Log ""
Log "===================== GATE 10a ====================="
$aOut = & $py "scripts\gate10_catalog_smoke.py" 2>&1 | Out-String
Write-Host $aOut; Add-Content -Path $log -Value $aOut
$aPassed = ($LASTEXITCODE -eq 0)

if (-not $aPassed) {
    Pop-Location
    Die @"
Catalog context could not be read through DataHub's MCP Server.

This is the part that must work -- it uses get_entities and search, neither of
which goes near the lineage resolver. Diagnose with:
    .\scripts\probe_gms.ps1
"@
}
Log "GATE 10a GREEN."

# =========================================================== GATE 10b
Log ""
Log "===================== GATE 10b ====================="
Log "Declared lineage via get_lineage -> searchAcrossLineage."

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
    return @{ ExitCode = $LASTEXITCODE; Output = $out; Path = $jsonPath }
}

$sdkRun = ReconcileVia "sdk" "$env:TEMP\pg_sdk.json"
if ($sdkRun.ExitCode -ne 0 -or -not (Test-Path $sdkRun.Path)) {
    Pop-Location
    Die "The SDK path failed. That is the oracle -- nothing downstream is meaningful.`n$($sdkRun.Output)"
}
$sdk = Get-Content $sdkRun.Path -Raw | ConvertFrom-Json

$mcpRun = ReconcileVia "mcp" "$env:TEMP\pg_mcp.json"

if ($mcpRun.ExitCode -ne 0 -or -not (Test-Path $mcpRun.Path)) {

    # The one failure a known bug is allowed to produce, and only this one.
    if ($mcpRun.Output -match "PointInTime") {
        Log ""
        Log "############ GATE 10b: ENVIRONMENT-BLOCKED ############"
        Log "  GMS returned 500 on searchAcrossLineage: point-in-time"
        Log "  creation failed. GMS is speaking the Elasticsearch dialect"
        Log "  to an OpenSearch backend."
        Log ""
        Log "  This is NOT Polygraph and NOT the MCP Server -- the same"
        Log "  query backs DataHub's own UI Lineage tab, which is"
        Log "  therefore also broken on this stack."
        Log ""
        Log "  Diagnose:  .\scripts\probe_gms.ps1"
        Log "  Fix:       .\scripts\fix_gms_search.ps1"
        Log "  Explained: docs\DATAHUB_MCP.md"
        Log "######################################################"
        Log ""
        Log "================ GATE 10: GREEN (PARTIAL) ================"
        Log "  10a GREEN -- DataHub reached through its own MCP Server"
        Log "               (get_entities + search)."
        Log "  10b BLOCKED by the GMS bug above, not by our code."
        Log ""
        Log "  The eligibility requirement is met by 10a."
        Log "  Run fix_gms_search.ps1 then re-run this gate for a full green."
        Log "========================================================="
        Pop-Location
        exit 0
    }

    Pop-Location
    Die @"
reconcile --declared-via mcp failed, and NOT with the known point-in-time
signature. An unrecognised failure does not get the benefit of a known bug.

$($mcpRun.Output)
"@
}

$mcp = Get-Content $mcpRun.Path -Raw | ConvertFrom-Json

# --- the comparison that matters -----------------------------------------
Log ""
Log "----- comparing the two paths -----"
Log ("SDK : V={0} P={1} U={2}" -f $sdk.summary.VERIFIED, $sdk.summary.PHANTOM, $sdk.summary.UNDECLARED)
Log ("MCP : V={0} P={1} U={2}" -f $mcp.summary.VERIFIED, $mcp.summary.PHANTOM, $mcp.summary.UNDECLARED)

# Compare verdict-by-verdict, not just the totals. Matching totals with
# differing edges is worse than a plain mismatch -- a summary check passes it.
$sdkPairs = ($sdk.verdicts | ForEach-Object { "$($_.verdict)|$($_.upstream)|$($_.downstream)" } | Sort-Object) -join "`n"
$mcpPairs = ($mcp.verdicts | ForEach-Object { "$($_.verdict)|$($_.upstream)|$($_.downstream)" } | Sort-Object) -join "`n"
if ($sdkPairs -ne $mcpPairs) {
    Pop-Location
    Die @"
The two paths disagree on the per-edge verdicts.

The SDK reads the dataJobInputOutput aspect directly and is the oracle. A
divergence means either the MCP response parser is picking up the wrong URNs, or
DataHub's agent-facing lineage API genuinely reports something different from the
stored aspect. Both are real findings -- do not ship until you know which.

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
Log "==================== GATE 10: GREEN (FULL) ===================="
Log "  10a  catalog context via get_entities + search"
Log "  10b  declared lineage via get_lineage, verdicts identical to"
Log "       the SDK oracle, per edge"
Log ""
Log "  Also check the UI Lineage tab now -- same resolver:"
Log "  http://localhost:9002/tasks/urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)/Lineage"
Log "=============================================================="
