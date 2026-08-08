# Polygraph -- bring the search backend back up.
#
#     cd C:\dev\polygraph ; .\scripts\fix_search_backend.ps1
#
# WHAT WENT WRONG
#   datahub-opensearch-1 exited (127) while the rest of the stack stayed up for
#   45 hours. GMS resolves its search backend by the compose service alias
#   `search`, so with the container gone every search and lineage query fails:
#
#       java.net.UnknownHostException: search
#
#   That is also what "Root cause: search" meant in the point-in-time 500 --
#   `search` was the hostname, not a subsystem. GMS's own stack trace names
#   OpenSearch2SearchClientShim, so it had detected OpenSearch correctly all
#   along. The dialect-mismatch theory this repo briefly carried was wrong.
#
#   Nothing was lost: metadata lives in MySQL, which never went down. Only the
#   search and graph INDICES live in OpenSearch, and those are rebuildable.
#
# WHAT THIS DOES
#   Captures why OpenSearch died, starts it, waits for the cluster to go green,
#   restarts GMS so it re-resolves the host, then re-probes. Read the log before
#   assuming it worked.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "fix_search.log"
if (Test-Path $log) { Remove-Item $log }

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Die($msg) {
    Log ""; Log "################ STOPPED ################"; Log $msg
    Log "#########################################"; exit 1
}

# --- resolve names instead of hardcoding them -----------------------------
# The quickstart compose file pins no container_name, so compose names things
# <project>-<service>-<n>. Every script in this repo that said "datahub-gms"
# was addressing a container that does not exist.
function ContainerMatching($pattern) {
    $hit = docker ps -a --format "{{.Names}}" 2>$null | Select-String -Pattern $pattern | Select-Object -First 1
    if ($hit) { return $hit.ToString().Trim() }
    return $null
}

$search = ContainerMatching "opensearch|elasticsearch"
$gms    = ContainerMatching "gms"
if (-not $search) { Die "No search container exists at all. Run: .venv\Scripts\datahub.exe docker quickstart" }
if (-not $gms)    { Die "No GMS container found." }
Log "search container: $search"
Log "gms container:    $gms"

$project = (docker inspect $gms --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>$null).Trim()
$compose = (docker inspect $gms --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>$null).Trim()
Log "compose project:  $project"
Log "compose file:     $compose"

# --- why did it die? ------------------------------------------------------
# Answer this BEFORE restarting. If it was killed for disk or memory, starting
# it again just buys a few hours before the same failure lands mid-demo.
Log ""
Log "----- why $search exited (last 40 lines) -----"
docker logs --tail 40 $search 2>&1 | ForEach-Object { Log "  $_" }

Log ""
Log "----- resource headroom -----"
$freeGB = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
Log "  C: free: $freeGB GB"
if ($freeGB -lt 5) {
    Log "  LOW. OpenSearch refuses writes past its flood-stage watermark and can"
    Log "  exit. Free space first -- see scripts\reclaim.ps1 -- or it will die again."
}
docker system df 2>&1 | ForEach-Object { Log "  $_" }
docker info --format '  docker memory: {{.MemTotal}} bytes' 2>&1 | ForEach-Object { Log $_ }

# --- start it -------------------------------------------------------------
Log ""
Log "----- starting $search -----"
docker start $search 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) {
    Die "docker start failed. Read the exit logs above -- restarting will not fix a container that cannot boot."
}

# The quickstart does not publish 9200 to the host, so health has to be checked
# from inside the container. The OpenSearch image ships curl.
Log ""
Log "----- waiting for the cluster (up to 5 minutes) -----"
$ready = $false
for ($i = 1; $i -le 60; $i++) {
    $health = docker exec $search curl -s "http://localhost:9200/_cluster/health" 2>$null
    if ($LASTEXITCODE -eq 0 -and $health -match '"status"\s*:\s*"(green|yellow)"') {
        Log "  cluster: $health"
        $ready = $true; break
    }
    if ($i % 6 -eq 0) { Log "  ...still waiting ($($i * 5)s)" }
    Start-Sleep -Seconds 5
}
if (-not $ready) {
    Log ""
    Log "----- $search logs since start -----"
    docker logs --tail 40 $search 2>&1 | ForEach-Object { Log "  $_" }
    Die "OpenSearch did not become healthy. The logs above are the place to look."
}

# --- make GMS re-resolve the host ----------------------------------------
# GMS caches the failed DNS lookup and its connection pool; it does not
# reliably recover on its own once `search` has been unresolvable for hours.
Log ""
Log "----- restarting $gms so it re-resolves 'search' -----"
docker restart $gms 2>&1 | ForEach-Object { Log "  $_" }

Log "----- waiting for GMS on :8080 -----"
$gmsReady = $false
for ($i = 1; $i -le 72; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8080/config" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $gmsReady = $true; break }
    } catch { }
    if ($i % 6 -eq 0) { Log "  ...still waiting ($($i * 5)s)" }
    Start-Sleep -Seconds 5
}
if (-not $gmsReady) {
    docker logs --tail 40 $gms 2>&1 | ForEach-Object { Log "  $_" }
    Die "GMS did not come back within 6 minutes."
}
Log "GMS is up. Letting the GraphQL layer warm up (20s)..."
Start-Sleep -Seconds 20

# --- did it actually work? ------------------------------------------------
Log ""
Log "----- re-probing -----"
$py = Join-Path $repo ".venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
Push-Location $repo
$out = & $py "scripts\probe_gms.py" 2>&1 | Out-String
Pop-Location
Write-Host $out
Add-Content -Path $log -Value $out

$lineageOk = $out -match "1\. searchAcrossLineage[\s\S]{0,400}?OK\s"
$searchOk  = $out -match "4\. searchAcrossEntities[\s\S]{0,400}?OK\s"

Log ""
if ($lineageOk -and $searchOk) {
    Log "==================== SEARCH IS BACK ===================="
    Log "  Next:"
    Log "    .\scripts\run_gate10.ps1"
    Log "    http://localhost:9002   -- check search AND the Lineage tab"
    Log "======================================================="
    exit 0
}

if ($searchOk -and -not $lineageOk) {
    Log "Plain search works, lineage does not. NOW the point-in-time theory is"
    Log "worth testing -- that is the one case where it was ever the right answer."
    exit 1
}

Log "################ SEARCH STILL NOT ANSWERING ################"
Log "  The cluster is healthy but GMS's queries still fail. Most likely the"
Log "  indices were lost when OpenSearch died, and GMS is querying indices that"
Log "  no longer exist. Metadata is safe in MySQL; rebuild the indices with:"
Log ""
Log "    docker compose -p $project -f `"$compose`" up -d system-update-quickstart"
Log ""
Log "  Then re-run this script's probe:  .\scripts\probe_gms.ps1"
Log "###########################################################"
exit 1
