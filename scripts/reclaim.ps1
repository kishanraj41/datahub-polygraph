# Polygraph -- reclaim disk, then run Gate 1 automatically if it worked.
#
#     cd C:\dev\polygraph ; .\scripts\reclaim.ps1
#
# Only touches caches that re-download on demand:
#   * conda package cache      (~9.6 GB on this machine)
#   * pip wheel cache          (~3.6 GB)
#   * unused Docker images     (frees space INSIDE docker_data.vhdx)
#
# It does NOT touch Downloads, VirtualBox VMs, WSL distros, or any project
# files. Those stay your call.
#
# Add -NoChain to stop after reclaiming instead of continuing into Gate 1.

param([switch]$NoChain)

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "reclaim.log"
if (Test-Path $log) { Remove-Item $log }

function Log($msg) { Write-Host $msg; Add-Content -Path $log -Value $msg }
function FreeGB { [math]::Round((Get-PSDrive C).Free / 1GB, 2) }

$before = FreeGB
Log "===== Polygraph reclaim ====="
Log "Free on C: before: $before GB"
Log ""

# ---------------------------------------------------------------- conda
$conda = "$env:USERPROFILE\miniconda3\Scripts\conda.exe"
if (Test-Path $conda) {
    Log "----- conda clean --all (package cache; environments are untouched) -----"
    & $conda clean --all -y 2>&1 | ForEach-Object { Log "  $_" }
    Log "  free now: $(FreeGB) GB"
} else {
    Log "----- conda not found at $conda, skipping -----"
}
Log ""

# ------------------------------------------------------------------ pip
Log "----- pip cache purge -----"
$venvPy = Join-Path $repo ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }
& $py -m pip cache purge 2>&1 | ForEach-Object { Log "  $_" }
# The venv's pip has its own cache dir only if configured; clear the user one too.
if (Test-Path "$env:LOCALAPPDATA\pip\Cache") {
    Remove-Item "$env:LOCALAPPDATA\pip\Cache\*" -Recurse -Force -ErrorAction SilentlyContinue
    Log "  cleared $env:LOCALAPPDATA\pip\Cache"
}
Log "  free now: $(FreeGB) GB"
Log ""

# --------------------------------------------------------------- docker
# NOTE: this frees space *inside* docker_data.vhdx (18.8 GB on this machine).
# WSL2 .vhdx files never shrink on their own, so this will NOT show up as free
# space on C:. It still matters: it gives DataHub room to write without forcing
# the vhdx to grow further into what little C: has left.
Log "----- docker system prune -a --volumes -----"
Log "  (frees space inside docker_data.vhdx; C: free will not move -- expected)"
docker system prune -a --volumes -f 2>&1 | ForEach-Object { Log "  $_" }
Log ""
Log "----- docker disk usage after prune -----"
docker system df 2>&1 | ForEach-Object { Log "  $_" }
Log ""

$after = FreeGB
$freed = [math]::Round($after - $before, 2)
Log "============================================"
Log "Free on C: before: $before GB"
Log "Free on C: after:  $after GB   (reclaimed $freed GB)"
Log "============================================"
Log ""

if ($after -lt 15) {
    Log "Still under 15 GB. The remaining big items are yours to judge:"
    Log ""
    Log "  12.78 GB  C:\Users\kisha\Downloads"
    Log "  10.30 GB  C:\Users\kisha\VirtualBox VMs"
    Log "   6.30 GB  Ubuntu WSL distro (AppData\Local\Packages\CanonicalGroupLimited.Ubuntu_*)"
    Log ""
    Log "Or move Docker's disk image to another drive:"
    Log "  Docker Desktop -> Settings -> Resources -> Advanced -> Disk image location"
    Log ""
    Log "Then run:  .\scripts\run_gate1.ps1"
    exit 1
}

Log "Target met. Free space is $after GB."
if ($NoChain) {
    Log "-NoChain set; stopping here. Run .\scripts\run_gate1.ps1 when ready."
    exit 0
}
Log "Chaining straight into Gate 1..."
Log ""
& (Join-Path $PSScriptRoot "run_gate1.ps1")
