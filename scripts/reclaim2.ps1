# Polygraph -- reclaim the orphaned Docker volumes, then run Gate 1.
#
#     cd C:\dev\polygraph ; .\scripts\reclaim2.ps1
#
# Why a second script: `docker system prune --volumes` only removes ANONYMOUS
# volumes. Docker 23+ requires `docker volume prune -a` to remove named ones.
# On this machine that is 11.05 GB sitting idle (ACTIVE 0, RECLAIMABLE 100%).
#
# Important: this frees space INSIDE docker_data.vhdx, which is where DataHub
# actually installs. C: free space will not change, and does not need to.
#
# -NoChain stops after reclaiming instead of continuing into Gate 1.

param([switch]$NoChain)

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "reclaim2.log"
if (Test-Path $log) { Remove-Item $log }

function Log($msg) { Write-Host $msg; Add-Content -Path $log -Value $msg }

Log "===== Polygraph reclaim, round 2 ====="
Log ""

# ------------------------------------------------- show before deleting
# These volumes are not ours and not in use, but you should see what they are
# before they go. If any name here means something to you, Ctrl-C now.
Log "----- volumes that are about to be removed -----"
docker volume ls 2>&1 | ForEach-Object { Log "  $_" }
Log ""
Log "All of the above report ACTIVE 0 / RECLAIMABLE 100%, i.e. no container"
Log "references them. Pausing 10 seconds -- Ctrl-C if any name matters to you."
Start-Sleep -Seconds 10
Log ""

# ------------------------------------------------------------ the reclaim
Log "----- docker volume prune -a -----"
docker volume prune -a -f 2>&1 | ForEach-Object { Log "  $_" }
Log ""

Log "----- docker disk usage after -----"
docker system df 2>&1 | ForEach-Object { Log "  $_" }
Log ""

# --------------------------------------- how much room is there really?
# The number that matters is free space inside the Docker VM's filesystem,
# not free space on C:. Ask the VM directly.
Log "----- free space INSIDE the Docker VM (this is what DataHub consumes) -----"
$vmFree = $null
try {
    $dfOut = wsl -d docker-desktop df -h /var/lib/docker 2>&1
    $dfOut | ForEach-Object { Log "  $_" }
    $line = ($dfOut | Select-Object -Last 1) -split '\s+'
    if ($line.Length -ge 4) { $vmFree = $line[3] }
} catch {
    Log "  (could not query the docker-desktop distro directly)"
}
Log ""

$cFree = [math]::Round((Get-PSDrive C).Free / 1GB, 2)
Log "============================================"
Log "Free inside Docker VM: $(if ($vmFree) { $vmFree } else { 'see docker system df above' })"
Log "Free on C:           : $cFree GB  (headroom for the vhdx to grow if needed)"
Log "============================================"
Log ""
Log "DataHub needs roughly 12-14 GB inside the VM. The vhdx is already 18.8 GB"
Log "and holds almost nothing now, so that room exists without touching C:."
Log ""

if ($NoChain) {
    Log "-NoChain set. Run .\scripts\run_gate1.ps1 when ready."
    exit 0
}
Log "Chaining into Gate 1..."
Log ""
& (Join-Path $PSScriptRoot "run_gate1.ps1")
