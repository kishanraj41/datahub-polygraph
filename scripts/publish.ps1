# Polygraph -- commit and push to GitHub.
#
# Two paths. Pick either.
#
#   A. No install needed. Create an EMPTY public repo in the browser first
#      (https://github.com/new -- name it datahub-polygraph, do NOT add a
#      README, .gitignore or licence), then:
#
#          .\scripts\publish.ps1 -RemoteUrl https://github.com/<you>/datahub-polygraph.git
#
#      Git Credential Manager opens a browser to authenticate on first push.
#
#   B. Use the GitHub CLI, which also sets topics and the About box:
#
#          winget install --id GitHub.cli
#          # close and reopen PowerShell so PATH picks it up
#          gh auth login
#          .\scripts\publish.ps1
#
# Either way this refuses to publish if the tests fail, the licence is not
# verbatim Apache-2.0, or examples/ is thin.

param([string]$RemoteUrl = "")

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "publish.log"
if (Test-Path $log) { Remove-Item $log }

$env:PYTHONIOENCODING = "utf-8"
$RepoName = "datahub-polygraph"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Die($msg) {
    Log ""; Log "############### PUBLISH STOPPED ###############"; Log $msg
    Log "##############################################"; exit 1
}

Push-Location $repo

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git is not on PATH." }
$hasGh = [bool](Get-Command gh -ErrorAction SilentlyContinue)

if (-not $hasGh -and -not $RemoteUrl) {
    Die @"
Neither the GitHub CLI nor -RemoteUrl is available. Pick one:

  A. NO INSTALL (fastest)
     1. Open https://github.com/new
     2. Name: $RepoName
        Visibility: PUBLIC
        Do NOT tick "Add a README", ".gitignore", or "Choose a license" --
        this repo already has all three and an initialised repo will conflict.
     3. Create, copy the HTTPS URL, then run:

        .\scripts\publish.ps1 -RemoteUrl https://github.com/<you>/$RepoName.git

  B. GITHUB CLI (also sets topics and the About box automatically)
        winget install --id GitHub.cli
        # close and REOPEN PowerShell -- PATH will not update in this window
        gh auth login
        .\scripts\publish.ps1
"@
}

# ============================================== pre-publish sanity checks
if (-not (Test-Path (Join-Path $repo "LICENSE"))) { Die "No LICENSE at repo root." }
$license = Get-Content (Join-Path $repo "LICENSE") -Raw
if ($license -notmatch "Apache License" -or $license -notmatch "Version 2\.0, January 2004") {
    Die "LICENSE is not verbatim Apache-2.0. GitHub will not detect it, and the hackathon requires it visible."
}
Log "LICENSE: verbatim Apache-2.0."

$examples = Get-ChildItem (Join-Path $repo "examples") -File -ErrorAction SilentlyContinue
if (-not $examples -or $examples.Count -lt 3) { Die "examples/ has fewer than 3 files." }
Log "examples/: $($examples.Count) files."

# Artifacts whose sha-256 is published must not carry CRLF, or the digest in
# DataHub will not match the file a judge downloads.
foreach ($f in $examples) {
    if ($f.Extension -in ".md", ".json") {
        $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
        for ($i = 0; $i -lt $bytes.Length - 1; $i++) {
            if ($bytes[$i] -eq 13 -and $bytes[$i + 1] -eq 10) {
                Die "$($f.Name) contains CRLF. Its published sha-256 will not match. Re-run the gate that produced it."
            }
        }
    }
}
Log "examples/: all LF, digests will verify."

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (Test-Path $py) {
    $env:PYTHONPATH = Join-Path $repo "src"
    Log "----- tests -----"
    $out = & $py -m pytest tests -q 2>&1 | Out-String
    Write-Host $out; Add-Content -Path $log -Value $out
    if ($LASTEXITCODE -ne 0) { Die "Tests failed. Not publishing." }
} else {
    Log "WARNING: no venv, skipping tests."
}

# ==================================================== git init and commit
if (-not (Test-Path (Join-Path $repo ".git"))) {
    Log "----- git init -----"
    git init -b main 2>&1 | ForEach-Object { Log "  $_" }
    git config core.autocrlf false 2>&1 | Out-Null   # .gitattributes governs
}

git add -A 2>&1 | Out-Null
$staged = @(git diff --cached --name-only)
if ($staged.Count -gt 0) {
    Log "----- committing $($staged.Count) files -----"
    git commit -q -m "Polygraph: reconcile declared DataHub lineage against runtime-observed lineage" 2>&1 |
        ForEach-Object { Log "  $_" }
} else {
    Log "Nothing new to commit."
}

# ========================================================== push
if ($RemoteUrl) {
    Log "----- path A: plain git to $RemoteUrl -----"
    $existing = git remote get-url origin 2>$null
    if ($existing) {
        git remote set-url origin $RemoteUrl 2>&1 | ForEach-Object { Log "  $_" }
    } else {
        git remote add origin $RemoteUrl 2>&1 | ForEach-Object { Log "  $_" }
    }
    Log "Pushing. A browser window may open for GitHub authentication."
    git push -u origin main 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Die @"
Push failed. The two usual causes:

  * The GitHub repo was created WITH a README or licence, so it has a commit
    yours does not descend from. Fix: delete it and recreate it empty, or run
        git push -u origin main --force
  * Authentication was cancelled. Re-run and complete the browser prompt.
"@
    }
    $url = $RemoteUrl -replace '\.git$', ''
} else {
    gh auth status 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "gh is installed but not authenticated. Run: gh auth login" }
    Log "gh authenticated."

    gh repo view $RepoName 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "----- creating public repo $RepoName -----"
        gh repo create $RepoName --public --source=. --remote=origin --push `
            --description "A lie detector for data catalogs: reconcile declared DataHub lineage against runtime-observed lineage." 2>&1 |
            ForEach-Object { Log "  $_" }
        if ($LASTEXITCODE -ne 0) { Die "gh repo create failed. Output above." }
    } else {
        Log "Repo exists; pushing."
        git push -u origin main 2>&1 | ForEach-Object { Log "  $_" }
    }

    Log "----- topics -----"
    gh repo edit --add-topic datahub --add-topic data-lineage --add-topic data-catalog `
        --add-topic mlops --add-topic data-quality --add-topic observability 2>&1 |
        ForEach-Object { Log "  $_" }
    $url = gh repo view $RepoName --json url -q .url 2>$null
}

Pop-Location

Log ""
Log "==================== PUSHED ===================="
Log "  $url"
Log ""
Log "Check these by eye -- the hackathon grades on them:"
Log "  1. 'Apache-2.0' appears in the About box on the right"
Log "  2. README renders and the Mermaid diagram draws"
Log "  3. examples/ is visible with the real reports"
if ($RemoteUrl) {
    Log ""
    Log "Path A does not set the About description or topics. Add them by hand:"
    Log "  Settings gear next to About -> description + topics"
}
Log "==============================================="
