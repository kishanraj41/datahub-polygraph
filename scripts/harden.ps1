# Polygraph -- harden the published repo, then run Gate 7 (integrity scores).
#
#     cd C:\dev\polygraph ; .\scripts\harden.ps1
#
# Three things:
#
#   1. Removes the *.log files from the repo AND from git history. They went
#      public in the first push. freespace.log in particular lists your
#      Downloads size, VirtualBox VM sizes and home directory paths -- noise at
#      best, mild information leak at worst. There is exactly one commit, so
#      amending it removes them from history entirely rather than leaving them
#      one `git log -p` away.
#
#   2. Fresh-clone test: clones the pushed repo into a temp directory, installs
#      from requirements.txt, and runs the pipeline and tests using ONLY what a
#      judge would get. This is the Phase 9 gate.
#
#   3. Gate 7: computes lineage integrity scores and writes them to DataHub as
#      structured properties.

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "harden.log"
if (Test-Path $log) { Remove-Item $log }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONHASHSEED = "0"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Die($msg) {
    Log ""; Log "############### HARDEN STOPPED ###############"; Log $msg
    Log "#############################################"; exit 1
}

Push-Location $repo
$py = Join-Path $repo ".venv\Scripts\python.exe"

# ================================================ 1. purge logs from history
Log "===== 1. removing logs from the repo and its history ====="

$gitignore = Join-Path $repo ".gitignore"
$content = Get-Content $gitignore -Raw
$additions = @()
foreach ($pat in @("*.log", ".pytest_cache/", "*.egg-info/", "build/", "dist/")) {
    if ($content -notmatch [regex]::Escape($pat)) { $additions += $pat }
}
if ($additions.Count -gt 0) {
    Add-Content -Path $gitignore -Value ""
    Add-Content -Path $gitignore -Value "# Run logs contain local paths and machine details. Never publish these."
    $additions | ForEach-Object { Add-Content -Path $gitignore -Value $_ }
    Log "Added to .gitignore: $($additions -join ', ')"
}

# Untrack them (files stay on disk -- you still want them locally).
$tracked = @(git ls-files | Where-Object { $_ -like "*.log" -or $_ -like ".pytest_cache/*" })
if ($tracked.Count -gt 0) {
    Log "Untracking $($tracked.Count) file(s):"
    $tracked | ForEach-Object { Log "  $_" }
    git rm --cached --quiet $tracked 2>&1 | ForEach-Object { Log "  $_" }
} else {
    Log "No log files tracked."
}

$commitCount = [int](git rev-list --count HEAD 2>$null)
Log "Commits on main: $commitCount"

git add -A 2>&1 | Out-Null
if ($commitCount -eq 1) {
    # Single commit: amend it so the logs never existed in history.
    Log "Amending the single commit so the logs leave history entirely."
    git commit -q --amend -m "Polygraph: reconcile declared DataHub lineage against runtime-observed lineage" 2>&1 |
        ForEach-Object { Log "  $_" }
    Log "Force-pushing the rewritten commit."
    git push --force-with-lease origin main 2>&1 | ForEach-Object { Log "  $_" }
} else {
    Log "More than one commit; making a normal removal commit instead of rewriting history."
    git commit -q -m "chore: stop tracking run logs" 2>&1 | ForEach-Object { Log "  $_" }
    git push origin main 2>&1 | ForEach-Object { Log "  $_" }
    Log "NOTE: the logs remain reachable in history. To purge them, delete the"
    Log "      GitHub repo and re-run publish.ps1 on a fresh clone."
}
if ($LASTEXITCODE -ne 0) { Die "Push failed. See output above." }

$still = @(git ls-files | Where-Object { $_ -like "*.log" })
if ($still.Count -gt 0) { Die "Log files are still tracked: $($still -join ', ')" }
Log "Repo is clean of log files."

# ================================================== 2. fresh-clone test
Log ""
Log "===== 2. fresh-clone test (the Phase 9 gate) ====="
$tmp = Join-Path $env:TEMP ("polygraph-freshclone-" + [System.IO.Path]::GetRandomFileName().Substring(0,6))
$remote = git remote get-url origin
Log "Cloning $remote into $tmp"
git clone --quiet $remote $tmp 2>&1 | ForEach-Object { Log "  $_" }
if (-not (Test-Path $tmp)) { Die "Clone failed." }

Push-Location $tmp
Log "----- building a clean venv from requirements.txt only -----"
python -m venv .venv 2>&1 | Out-Null
$freshPy = Join-Path $tmp ".venv\Scripts\python.exe"
& $freshPy -m pip install --quiet --upgrade pip 2>&1 | Out-Null
& $freshPy -m pip install --quiet -r requirements.txt 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) { Pop-Location; Die "pip install from requirements.txt failed in a clean environment." }

$env:PYTHONPATH = Join-Path $tmp "src"
Log "----- running the pipeline from the clone -----"
$out = & $freshPy demo\pipeline.py --mode healthy 2>&1 | Out-String
Write-Host $out; Add-Content -Path $log -Value $out
if ($LASTEXITCODE -ne 0) { Pop-Location; Die "The pipeline does not run from a fresh clone." }

# The README publishes this number. If a judge gets a different one, the README lies.
$m = Get-Content "runs\healthy\metrics.json" -Raw | ConvertFrom-Json
Log ("fresh-clone F1 = {0}" -f $m.f1)
if ([math]::Abs($m.f1 - 0.8282290279627164) -gt 1e-12) {
    Pop-Location
    Die @"
Fresh clone produced F1 = $($m.f1), but the README publishes 0.8282290279627164.

A judge following the README would get a different number than it claims. Either
the pins in requirements.txt did not hold, or the pipeline changed. Fix before
submitting -- this is the honesty rule, not a nitpick.
"@
}
Log "F1 matches the README exactly."

Log "----- running the test suite from the clone -----"
$out = & $freshPy -m pytest tests -q 2>&1 | Out-String
Write-Host $out; Add-Content -Path $log -Value $out
if ($LASTEXITCODE -ne 0) { Pop-Location; Die "Tests fail from a fresh clone." }

Pop-Location
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Log "Fresh-clone test PASSED. Temp clone removed."

# ==================================================== 3. Gate 7: scores
Log ""
Log "===== 3. Gate 7: lineage integrity scores ====="
$env:PYTHONPATH = Join-Path $repo "src"
$out = & $py -m polygraph.cli score --report "examples\reconciliation_report.json" `
    --out-md "examples\integrity_scores.md" --gms "http://localhost:8080" 2>&1 | Out-String
Write-Host $out; Add-Content -Path $log -Value $out
if ($LASTEXITCODE -ne 0) { Die "Gate 7 failed. Is DataHub still running?" }

git add -A 2>&1 | Out-Null
$staged = @(git diff --cached --name-only)
if ($staged.Count -gt 0) {
    git commit -q -m "feat: lineage integrity score as DataHub structured properties" 2>&1 | Out-Null
    git push origin main 2>&1 | ForEach-Object { Log "  $_" }
}

Pop-Location

Log ""
Log "==================== HARDENED ===================="
Log "  logs purged, fresh-clone verified, scores written"
Log "  https://github.com/kishanraj41/datahub-polygraph"
Log "================================================="
