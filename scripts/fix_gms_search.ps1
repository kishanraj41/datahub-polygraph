# Polygraph -- fix the GMS point-in-time failure that reds Gate 10.
#
#     cd C:\dev\polygraph ; .\scripts\fix_gms_search.ps1
#     cd C:\dev\polygraph ; .\scripts\fix_gms_search.ps1 -Mode nopit
#     cd C:\dev\polygraph ; .\scripts\fix_gms_search.ps1 -Mode revert
#
# WHAT IT CHANGES
#   Recreates ONE container -- datahub-gms -- with one extra environment
#   variable layered on by a compose override file. It does not touch the
#   generated quickstart compose file, the other containers, or any volume.
#   MySQL and OpenSearch keep their data, so the seeded catalog survives.
#
# WHY
#   The quickstart runs OpenSearch; GMS defaults to the Elasticsearch dialect
#   and creates a point-in-time snapshot for every graph query. The ES `_pit`
#   endpoint does not exist on OpenSearch. See docker/gms-search-override.yml.
#
# REVERTING
#   -Mode revert recreates datahub-gms from the quickstart file alone.
#
# Takes 1-3 minutes, nearly all of it waiting for GMS to come back up.

param(
    [ValidateSet("opensearch", "nopit", "both", "revert")]
    [string]$Mode = "opensearch"
)

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "fix_gms.log"
if (Test-Path $log) { Remove-Item $log }

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Die($msg) {
    Log ""; Log "################ FIX ABORTED ################"; Log $msg
    Log "#############################################"; exit 1
}

Log "Mode: $Mode"

# ------------------------------------------------------- locate the stack
$project = docker inspect datahub-gms --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>&1
if ($LASTEXITCODE -ne 0 -or -not $project) {
    Die @"
Could not find a running datahub-gms container.

Start the stack first:
    .\scripts\run_gate1.ps1
"@
}
$project = $project.Trim()
Log "Compose project: $project"

$composeFile = docker inspect datahub-gms `
    --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>&1
if ($LASTEXITCODE -ne 0 -or -not $composeFile) {
    Die "datahub-gms carries no compose config_files label. Was it started outside the quickstart?"
}
$composeFile = $composeFile.Trim()
Log "Base compose file: $composeFile"
if (-not (Test-Path $composeFile)) {
    Die "The compose file the container names does not exist on disk: $composeFile"
}

# ------------------------------------------------------ build the -f chain
$files = @("-f", $composeFile)
switch ($Mode) {
    "opensearch" { $files += @("-f", (Join-Path $repo "docker\gms-search-override.yml")) }
    "nopit"      { $files += @("-f", (Join-Path $repo "docker\gms-nopit-override.yml")) }
    "both"       {
        $files += @("-f", (Join-Path $repo "docker\gms-search-override.yml"))
        $files += @("-f", (Join-Path $repo "docker\gms-nopit-override.yml"))
    }
    "revert"     { }   # base file only
}
for ($i = 1; $i -lt $files.Count; $i += 2) {
    if (-not (Test-Path $files[$i])) { Die "Override file missing: $($files[$i])" }
}

# ------------------------------------------------------------- recreate GMS
# --no-deps is what keeps this surgical: only datahub-gms is touched. Without
# it compose would also recreate mysql and opensearch, and a failed recreate
# there is a much worse day than a failed recreate of a stateless service.
Log ""
Log "----- recreating datahub-gms (only this container) -----"
$argv = @("compose", "-p", $project) + $files + @("up", "-d", "--no-deps", "--force-recreate", "datahub-gms")
Log "  docker $($argv -join ' ')"
& docker @argv 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Die @"
docker compose could not recreate datahub-gms.

Nothing is lost -- the volumes are untouched. Bring the stack back with:
    .\scripts\fix_gms_search.ps1 -Mode revert
or
    .venv\Scripts\datahub.exe docker quickstart
"@
}

# ------------------------------------------------------------ wait for GMS
Log ""
Log "----- waiting for GMS to answer on :8080 -----"
$ready = $false
for ($i = 1; $i -le 72; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8080/config" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    if ($i % 6 -eq 0) { Log "  ...still waiting ($($i * 5)s)" }
    Start-Sleep -Seconds 5
}
if (-not $ready) {
    Log ""
    Log "----- last 60 lines of datahub-gms -----"
    docker logs --tail 60 datahub-gms 2>&1 | ForEach-Object { Log "  $_" }
    Die @"
GMS did not come back within 6 minutes. The logs above are the place to look.

To undo:
    .\scripts\fix_gms_search.ps1 -Mode revert
"@
}
Log "GMS is up."

# GMS answers /config before its GraphQL layer is fully warm; probing too early
# reports a failure that would have passed 20 seconds later.
Log "Letting the GraphQL layer warm up (20s)..."
Start-Sleep -Seconds 20

# ----------------------------------------------------------------- re-probe
Log ""
Log "----- re-probing -----"
$py = Join-Path $repo ".venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
Push-Location $repo
$out = & $py "scripts\probe_gms.py" 2>&1 | Out-String
Pop-Location
Write-Host $out
Add-Content -Path $log -Value $out

if ($out -match "1\. searchAcrossLineage[\s\S]{0,400}?OK\s") {
    Log ""
    Log "==================== LINEAGE QUERIES WORK ===================="
    Log "  Next:  .\scripts\run_gate10.ps1"
    Log "  Also check the UI Lineage tab -- it uses the same resolver:"
    Log "  http://localhost:9002/tasks/urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)/Lineage"
    Log "============================================================="
    exit 0
}

Log ""
Log "################ STILL FAILING ################"
if ($Mode -eq "opensearch") {
    Log "The dialect fix did not clear it. Try the blunter one:"
    Log "    .\scripts\fix_gms_search.ps1 -Mode both"
} elseif ($Mode -eq "revert") {
    Log "Reverted. The failure is back, as expected."
} else {
    Log "Neither fix cleared it. Paste this log -- do not keep restarting GMS."
}
Log "###############################################"
exit 1
