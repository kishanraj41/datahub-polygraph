# Polygraph -- disk triage.
#
#     cd C:\dev\polygraph ; .\scripts\free_space.ps1
#
# Read-only. This deletes NOTHING. It reports where the space went and prints
# the exact reclaim commands for each candidate, so you decide what goes.
#
# Writes to freespace.log alongside the console.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "freespace.log"
if (Test-Path $log) { Remove-Item $log }

function Log($msg) { Write-Host $msg; Add-Content -Path $log -Value $msg }

function SizeOf($path) {
    if (-not (Test-Path $path)) { return $null }
    try {
        $b = (Get-ChildItem $path -Recurse -Force -ErrorAction SilentlyContinue |
              Measure-Object -Property Length -Sum).Sum
        if ($null -eq $b) { return 0 }
        return [math]::Round($b / 1GB, 2)
    } catch { return $null }
}

$free = [math]::Round((Get-PSDrive C).Free / 1GB, 2)
Log "===== Polygraph disk triage ====="
Log "Free on C: $free GB   (DataHub quickstart needs ~15 GB)"
Log ""

# ---------------------------------------------------------------- all drives
Log "----- drives -----"
Get-PSDrive -PSProvider FileSystem | ForEach-Object {
    if ($_.Used -ne $null) {
        $f = [math]::Round($_.Free / 1GB, 1); $u = [math]::Round($_.Used / 1GB, 1)
        Log ("  {0}:  free {1} GB   used {2} GB" -f $_.Name, $f, $u)
    }
}
Log ""

# ------------------------------------------------------------ docker's usage
Log "----- docker disk usage -----"
try { docker system df 2>&1 | ForEach-Object { Log "  $_" } } catch { Log "  (docker not reachable)" }
Log ""

# ----------------------------------------------------- WSL2 virtual disks
Log "----- WSL2 virtual disks (these are usually the biggest single files) -----"
$vhdxRoots = @(
    "$env:LOCALAPPDATA\Docker\wsl",
    "$env:LOCALAPPDATA\wsl",
    "$env:LOCALAPPDATA\Packages"
)
foreach ($root in $vhdxRoots) {
    if (Test-Path $root) {
        Get-ChildItem $root -Recurse -Filter *.vhdx -Force -ErrorAction SilentlyContinue |
            Sort-Object Length -Descending | Select-Object -First 8 | ForEach-Object {
                Log ("  {0,8:N1} GB  {1}" -f ($_.Length / 1GB), $_.FullName)
            }
    }
}
Log ""

# ------------------------------------------------------- usual dev-box hogs
Log "----- common space consumers -----"
$candidates = [ordered]@{
    "VirtualBox VMs"      = "$env:USERPROFILE\VirtualBox VMs"
    "minikube"            = "$env:USERPROFILE\.minikube"
    "conda pkgs cache"    = "$env:USERPROFILE\miniconda3\pkgs"
    "conda envs"          = "$env:USERPROFILE\miniconda3\envs"
    "pip cache"           = "$env:LOCALAPPDATA\pip\Cache"
    "npm cache"           = "$env:APPDATA\npm-cache"
    "Windows temp"        = "$env:LOCALAPPDATA\Temp"
    "Downloads"           = "$env:USERPROFILE\Downloads"
    "rudriq .venv"        = "$env:USERPROFILE\OneDrive\Documents\AI\rudriq\.venv"
    "rudriq .venv-full"   = "$env:USERPROFILE\OneDrive\Documents\AI\rudriq\.venv-full"
    "autolineage paper data" = "$env:USERPROFILE\OneDrive\Documents\AI\autolineage\paper\data"
}
$rows = @()
foreach ($name in $candidates.Keys) {
    $gb = SizeOf $candidates[$name]
    if ($null -ne $gb -and $gb -gt 0.05) {
        $rows += [pscustomobject]@{ Name = $name; GB = $gb; Path = $candidates[$name] }
    }
}
$rows | Sort-Object GB -Descending | ForEach-Object {
    Log ("  {0,8:N2} GB  {1}  --  {2}" -f $_.GB, $_.Name, $_.Path)
}
Log ""

# --------------------------------------------------------- reclaim options
Log "===== reclaim options, safest first ====="
Log ""
Log "1. Docker images and build cache you are not using (safe, instant):"
Log "     docker system prune -a --volumes"
Log "   Removes every image, container and volume not currently in use. DataHub"
Log "   is not running yet, so nothing of ours is lost."
Log ""
Log "2. Package caches (safe, they re-download on demand):"
Log "     & `"$env:USERPROFILE\miniconda3\Scripts\conda.exe`" clean --all -y"
Log "     python -m pip cache purge"
Log ""
Log "3. Windows cleanup (safe):"
Log "     cleanmgr /d C"
Log "   Tick 'Previous Windows installations' and 'Delivery Optimization' if offered."
Log ""
Log "4. VirtualBox VMs and minikube -- check the sizes above. These are often"
Log "   tens of GB and are the fastest way to free real space, but only you know"
Log "   whether you still need them. Nothing here deletes them."
Log ""
Log "5. If another drive has room, move Docker's data root instead of deleting:"
Log "     Docker Desktop -> Settings -> Resources -> Advanced -> Disk image location"
Log "   Point it at the other drive and Apply. Docker restarts and migrates."
Log ""
Log "After reclaiming, re-run:  .\scripts\run_gate1.ps1"
Log ""
Log "Log written to $log"
