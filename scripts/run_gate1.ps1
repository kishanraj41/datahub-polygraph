# Polygraph -- Gate 1: stand up DataHub and prove a mutation round-trip.
#
#     cd C:\dev\polygraph ; .\scripts\run_gate1.ps1
#
# Writes everything to gate1.log. Safe to re-run; it skips work already done.
#
# The first successful run takes 10-25 minutes, nearly all of it pulling ~8 GB
# of DataHub images.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "gate1.log"
if (Test-Path $log) { Remove-Item $log }

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line
}
function Run($label, $cmd) {
    Log "----- $label -----"
    Add-Content -Path $log -Value "`$ $cmd"
    try { $out = Invoke-Expression "$cmd 2>&1" | Out-String } catch { $out = "EXCEPTION: $_" }
    Write-Host $out
    Add-Content -Path $log -Value $out
}
function Die($msg) {
    Log ""
    Log "################ GATE 1 STOPPED ################"
    Log $msg
    Log "###############################################"
    exit 1
}

Log "Polygraph Gate 1. Repo: $repo"

# ============================================================== 1. preflight
Run "docker version" "docker --version"
Run "wsl status"     "wsl --status"
Run "python version" "python --version"

# --- Docker daemon. This is the step that failed the first time, so it is now
# --- a hard gate with a readiness poll instead of a cascade of tracebacks.
Log "----- waiting for the Docker daemon -----"
$dockerReady = $false
for ($i = 1; $i -le 60; $i++) {
    docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
    if ($i -eq 1) { Log "Docker daemon not up yet. Waiting (up to 5 minutes)..." }
    if ($i % 6 -eq 0) { Log "  ...still waiting ($($i * 5)s)" }
    Start-Sleep -Seconds 5
}
if (-not $dockerReady) {
    Die @"
The Docker daemon never came up.

Open Docker Desktop and wait for the whale icon to stop animating, then re-run
this script. If Docker Desktop is already open, check that it finished starting
the WSL2 backend -- 'docker info' must succeed before DataHub can do anything.
"@
}
Run "docker daemon" "docker info --format 'server={{.ServerVersion}} mem={{.MemTotal}} cpus={{.NCPU}}'"

# --- memory: DataHub quickstart needs roughly 8 GB allocated to Docker.
$memBytes = (docker info --format '{{.MemTotal}}' 2>$null)
if ($memBytes -match '^\d+$') {
    $memGB = [math]::Round([int64]$memBytes / 1GB, 1)
    Log "Docker memory allocation: $memGB GB"
    if ($memGB -lt 7.0) {
        Die @"
Docker has only $memGB GB allocated. DataHub quickstart runs Elasticsearch,
Kafka, MySQL, GMS and the frontend; below ~7 GB the containers OOM partway
through startup and leave a half-broken stack.

Fix: Docker Desktop -> Settings -> Resources -> Memory, raise to 8 GB, Apply &
Restart, then re-run this script.
"@
    }
}

# --- disk. The number that matters is free space INSIDE docker_data.vhdx,
# --- not free space on C:. DataHub installs into the Docker VM's filesystem;
# --- C: only matters as headroom for the vhdx to grow if the VM runs out.
# --- Gating on C: alone was wrong and blocked a machine that had plenty of
# --- usable room inside an already-large vhdx.
$freeGB = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
Log "Free on C: $freeGB GB (headroom for vhdx growth)"

$vmFreeGB = $null
try {
    $dfOut = wsl -d docker-desktop df -BG /var/lib/docker 2>&1
    $line = ($dfOut | Select-Object -Last 1) -split '\s+'
    if ($line.Length -ge 4 -and $line[3] -match '^(\d+)G') { $vmFreeGB = [int]$Matches[1] }
} catch { }

if ($null -ne $vmFreeGB) {
    Log "Free inside the Docker VM: $vmFreeGB GB  <-- this is what DataHub consumes"
    if ($vmFreeGB -lt 12) {
        Die @"
Only $vmFreeGB GB free inside the Docker VM. DataHub needs roughly 12-14 GB.

Try this first -- it removes named volumes that 'docker system prune --volumes'
leaves behind (Docker 23+ needs -a for named volumes):

    .\scripts\reclaim2.ps1

If that is not enough, free space on C: as well so the vhdx can grow, or move
Docker's disk image to another drive:
  Docker Desktop -> Settings -> Resources -> Advanced -> Disk image location
"@
    }
} else {
    Log "NOTE: could not measure free space inside the Docker VM; falling back to C: only."
}

# C: is only a growth buffer now, so the bar is much lower than before.
if ($freeGB -lt 5) {
    Die "Only $freeGB GB free on C:. Even with room inside the VM, the vhdx needs some headroom to grow. Free ~5 GB and re-run."
}

# ========================================================== 2. venv and deps
$venv = Join-Path $repo ".venv"
if (-not (Test-Path $venv)) { Run "create venv" "python -m venv `"$venv`"" }
$py  = Join-Path $venv "Scripts\python.exe"
$pip = Join-Path $venv "Scripts\pip.exe"

Run "upgrade pip"  "& `"$py`" -m pip install --upgrade pip --quiet"
# Pinned via requirements.txt so the F1 numbers a judge reproduces match the
# numbers in the README.
Run "install deps" "& `"$pip`" install --quiet -r `"$repo\requirements.txt`""
Run "installed versions" "& `"$pip`" list | Select-String -Pattern 'acryl-datahub|mcp-server-datahub|autolineage|^pandas|scikit-learn|^numpy'"

# ============================================================ 3. quickstart
$datahub = Join-Path $venv "Scripts\datahub.exe"
Log "Starting DataHub quickstart. This is the long step -- go do something else."
Run "datahub quickstart" "& `"$datahub`" docker quickstart --dump-logs-on-failure"
Run "containers" "docker ps --format '{{.Names}}`t{{.Status}}'"

# --- wait for GMS to actually answer before trying to authenticate.
Log "----- waiting for GMS on :8080 -----"
$gmsReady = $false
for ($i = 1; $i -le 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8080/config" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $gmsReady = $true; break }
    } catch { }
    if ($i % 6 -eq 0) { Log "  ...still waiting ($($i * 5)s)" }
    Start-Sleep -Seconds 5
}
if (-not $gmsReady) {
    # Resolved by pattern: the quickstart pins no container_name, so the real
    # name is <project>-<service>-<n>. Hardcoding "datahub-gms" meant this
    # diagnostic silently printed nothing at the one moment it was needed.
    $gmsName = (docker ps -a --format "{{.Names}}" 2>$null | Select-String -Pattern "gms" | Select-Object -First 1)
    if ($gmsName) {
        Run "gms container logs" "docker logs --tail 60 $($gmsName.ToString().Trim())"
    } else {
        Log "No container matching 'gms' exists. Run scripts\stack_status.ps1."
    }
    Run "all containers" "docker ps -a --format '{{.Names}}`t{{.State}}`t{{.Status}}'"
    Die "GMS never answered on http://localhost:8080/config. The container logs above are the place to look."
}
Log "GMS is up."

# ================================================================== 4. auth
Run "datahub init" "& `"$datahub`" init --username datahub --password datahub"

# ============================================================ 5. smoke test
Run "gate 1 smoke test" "& `"$py`" `"$PSScriptRoot\gate1_smoke.py`""

Log ""
Log "Gate 1 script finished. Log: $log"
Log "UI: http://localhost:9002   (login datahub / datahub)"
