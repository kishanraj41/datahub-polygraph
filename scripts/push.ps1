# Polygraph -- commit everything and push.
#
#     cd C:\dev\polygraph ; .\scripts\push.ps1 "commit message"
#
# Exists because verify.ps1 clones from GitHub: anything not pushed is not
# tested. A local fix that never reaches the remote is a fix that does not exist.

param([string]$Message = "chore: sync")

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
Push-Location $repo

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = Join-Path $repo "src"
$py = Join-Path $repo ".venv\Scripts\python.exe"

if (Test-Path $py) {
    Write-Host "----- tests -----"
    & $py -m pytest tests -q
    if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Host "Tests failed. Not pushing."; exit 1 }
}

git add -A
$staged = @(git diff --cached --name-only)
if ($staged.Count -eq 0) {
    Write-Host "Nothing to commit."
} else {
    Write-Host "Committing $($staged.Count) file(s):"
    $staged | ForEach-Object { Write-Host "  $_" }
    git commit -q -m $Message
}
git push origin main
Pop-Location
