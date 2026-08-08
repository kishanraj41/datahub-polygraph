# Polygraph -- diagnose the Gate 10 red. READ-ONLY. Changes nothing.
#
#     cd C:\dev\polygraph ; .\scripts\probe_gms.ps1
#
# Gate 10 failed with a 500 from GMS, not from our code:
#     Failed to generate PointInTime Identifier.. Root cause: search
#     path: ['searchAcrossLineage']
#
# Before changing anything, establish which GraphQL surfaces this stack can
# actually serve. This inspects the containers and then probes four queries.
#
# Takes about 30 seconds. Writes probe.log and probe_gms.json.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "probe.log"
if (Test-Path $log) { Remove-Item $log }

function Log($msg) { Write-Host $msg; Add-Content -Path $log -Value $msg }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

Log "===== Polygraph GMS probe (read-only) ====="
Log ""

# ------------------------------------------------------------ which engine?
Log "----- search engine container -----"
$engine = docker ps --format "{{.Names}}`t{{.Image}}" 2>&1 |
          Select-String -Pattern "opensearch|elasticsearch"
if ($engine) {
    $engine | ForEach-Object { Log "  $_" }
} else {
    Log "  (no opensearch/elasticsearch container found in docker ps)"
}
Log ""

# ------------------------------------------------- GMS's relevant env vars
# These two decide whether GMS speaks the right point-in-time dialect.
#   ELASTICSEARCH_IMPLEMENTATION                              default: elasticsearch
#   ELASTICSEARCH_SEARCH_GRAPH_POINT_IN_TIME_CREATION_ENABLED default: true
# The quickstart runs OpenSearch and sets neither -- which is the suspect.
Log "----- datahub-gms env (search-related) -----"
$gmsEnv = docker inspect datahub-gms --format '{{range .Config.Env}}{{println .}}{{end}}' 2>&1
if ($LASTEXITCODE -eq 0) {
    $hits = $gmsEnv | Select-String -Pattern "ELASTICSEARCH|OPENSEARCH|POINT_IN_TIME"
    if ($hits) { $hits | ForEach-Object { Log "  $_" } } else { Log "  (none set)" }

    foreach ($v in @("ELASTICSEARCH_IMPLEMENTATION",
                     "ELASTICSEARCH_SEARCH_GRAPH_POINT_IN_TIME_CREATION_ENABLED")) {
        $set = $gmsEnv | Select-String -Pattern "^$v="
        if ($set) { Log "  [set]    $v" } else { Log "  [DEFAULT] $v  <- not set on the container" }
    }
} else {
    Log "  could not inspect datahub-gms: $gmsEnv"
}
Log ""

# ------------------------------------------------ where is the compose file?
Log "----- quickstart compose file -----"
$composeCandidates = @(
    (Join-Path $env:USERPROFILE ".datahub\quickstart\docker-compose.yml"),
    (Join-Path $env:USERPROFILE ".datahub\quickstart\docker-compose.yaml")
)
$compose = $composeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($compose) { Log "  $compose" } else { Log "  not found in $env:USERPROFILE\.datahub\quickstart" }
Log ""

# ----------------------------------------------------------- graphql probes
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Log "No venv at $py. Run scripts\run_gate1.ps1 first."; exit 1 }

Push-Location $repo
$out = & $py "scripts\probe_gms.py" 2>&1 | Out-String
Pop-Location
Write-Host $out
Add-Content -Path $log -Value $out

Log ""
Log "Probe log: $log"
Log "Nothing was changed. Paste the output back."
