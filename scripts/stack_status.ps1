# Polygraph -- what is actually running. READ-ONLY. Changes nothing.
#
#     cd C:\dev\polygraph ; .\scripts\stack_status.ps1
#
# WHY THIS EXISTS
#   probe_gms.ps1 reported "no opensearch/elasticsearch container found" and
#   `docker inspect datahub-gms` returned "No such object" -- while GMS was
#   answering happily on :8080. Those cannot all be true of a healthy stack, so
#   the container names this project assumed are wrong, or containers are
#   stopped, or both.
#
#   Every earlier script here addresses containers by the literal name
#   `datahub-gms`. Compose v2 names containers `<project>-<service>-<index>`
#   unless the compose file pins `container_name:`. If DataHub 1.7 dropped those
#   pins, every one of those references is broken -- including the log-dumping
#   in run_gate1.ps1, which would have failed silently at the worst moment.
#
# Writes stack.log.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "stack.log"
if (Test-Path $log) { Remove-Item $log }

function Log($msg) { Write-Host $msg; Add-Content -Path $log -Value $msg }
function Section($t) { Log ""; Log ("----- " + $t + " -----") }

Log "===== Polygraph stack status (read-only) ====="

Section "docker context / daemon"
docker context show 2>&1 | ForEach-Object { Log "  context: $_" }
docker info --format 'server={{.ServerVersion}} containers={{.Containers}} running={{.ContainersRunning}} stopped={{.ContainersStopped}}' 2>&1 |
    ForEach-Object { Log "  $_" }

Section "docker ps -a  (EVERY container, running or not)"
$all = docker ps -a --format "{{.Names}}`t{{.State}}`t{{.Status}}`t{{.Image}}" 2>&1
if ($LASTEXITCODE -eq 0 -and $all) {
    $all | ForEach-Object { Log "  $_" }
} else {
    Log "  (docker ps -a returned nothing: $all)"
}

Section "compose projects"
docker compose ls -a 2>&1 | ForEach-Object { Log "  $_" }

Section "who is listening on the DataHub ports"
foreach ($port in 8080, 9002, 9200, 3306) {
    $owner = docker ps -a --filter "publish=$port" --format "{{.Names}} ({{.State}})" 2>&1
    if ($owner) { Log ("  {0,-5} -> {1}" -f $port, ($owner -join ', ')) }
    else        { Log ("  {0,-5} -> no container publishes this port" -f $port) }
}

Section "services declared in the quickstart compose file"
$compose = Join-Path $env:USERPROFILE ".datahub\quickstart\docker-compose.yml"
if (Test-Path $compose) {
    Log "  file: $compose"
    # Service keys sit at exactly two spaces of indent under `services:`.
    $inServices = $false
    Get-Content $compose | ForEach-Object {
        if ($_ -match '^services:\s*$') { $inServices = $true; return }
        if ($inServices -and $_ -match '^\S') { $inServices = $false }
        if ($inServices -and $_ -match '^  ([A-Za-z0-9_.-]+):\s*$') { Log "    service: $($Matches[1])" }
    }
    Log ""
    Log "  container_name pins (if absent, compose prefixes the project name):"
    $pins = Select-String -Path $compose -Pattern 'container_name:' 2>&1
    if ($pins) { $pins | ForEach-Object { Log "    $($_.Line.Trim())" } }
    else       { Log "    NONE -- so containers are named <project>-<service>-<n>" }
} else {
    Log "  not found at $compose"
}

Section "search backend health, if one is up"
$searchC = (docker ps -a --format "{{.Names}}`t{{.Image}}" 2>&1 |
            Select-String -Pattern "opensearch|elastic|search")
if ($searchC) {
    $searchC | ForEach-Object { Log "  $_" }
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:9200/_cluster/health" -UseBasicParsing -TimeoutSec 5
        Log "  cluster health: $($r.Content)"
    } catch {
        Log "  :9200 did not answer -- $($_.Exception.Message)"
    }
} else {
    Log "  NO search container exists at all, not even stopped."
    Log "  GMS cannot serve search or lineage without one. Entity reads still work"
    Log "  because they come from MySQL, which is why the demo path passes."
}

Section "GMS container logs, whatever it is called"
$gms = (docker ps -a --format "{{.Names}}" 2>&1 | Select-String -Pattern "gms").ToString()
if ($gms) {
    $gms = $gms.Trim()
    Log "  container: $gms"
    docker logs --tail 30 $gms 2>&1 | ForEach-Object { Log "    $_" }
} else {
    Log "  no container with 'gms' in its name -- yet something answers :8080."
    Log "  Check whether DataHub is running outside Docker, or in another context."
}

Log ""
Log "Written to $log. Paste it back."
