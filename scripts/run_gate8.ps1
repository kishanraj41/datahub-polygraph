# Polygraph -- Gate 8: prove the MCP server works against live DataHub.
#
#     cd C:\dev\polygraph ; .\scripts\run_gate8.ps1
#
# The unit tests only cover the three tools that read local files. The three
# that read DataHub were written against SDK type signatures and never executed
# against a running server. This closes that gap by launching the MCP server as
# a real stdio subprocess -- exactly how Claude Desktop launches it -- and
# calling all six tools with real URNs.
#
# Needs DataHub running and the demo seeded (run_gate2.ps1 + run_gate3.ps1).

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "gate8.log"
if (Test-Path $log) { Remove-Item $log }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Log "No venv. Run scripts\run_gate1.ps1 first."; exit 1 }

try {
    $r = Invoke-WebRequest -Uri "http://localhost:8080/config" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -ne 200) { throw }
} catch {
    Log "DataHub is not answering on :8080. Start it and re-run."
    exit 1
}
Log "DataHub is up."

Push-Location $repo
Log "----- Gate 8: MCP tools over real stdio -----"
$out = & $py "scripts\gate8_mcp_smoke.py" 2>&1 | Out-String
Write-Host $out; Add-Content -Path $log -Value $out
$code = $LASTEXITCODE
Pop-Location

if ($code -ne 0) {
    Log ""
    Log "################ GATE 8 RED ################"
    Log "One or more MCP tools failed against the live DataHub."
    Log "The likely suspect is get_incident_report: its attribute path into"
    Log "DocumentInfoClass was inferred from a constructor signature, not"
    Log "observed on a real response. Output above shows which tool failed."
    Log "###########################################"
    exit 1
}

Log ""
Log "==================== GATE 8 GREEN ===================="
Log "  All six MCP tools work over stdio against live DataHub."
Log "  Safe to register with Claude Desktop:"
Log "    docs\claude_desktop_config.example.json"
Log "====================================================="
