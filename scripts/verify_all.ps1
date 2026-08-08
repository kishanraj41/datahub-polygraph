# Polygraph -- verify the entire project, end to end, one verdict.
#
#     cd C:\dev\polygraph ; .\scripts\verify_all.ps1
#
# Runs every gate in dependency order and prints a single table. Any red gate
# fails the whole run. Takes roughly 8-12 minutes, most of it building a clean
# venv for the fresh-clone check.
#
# What this is for: individual gates have each passed at some point, but never
# all of them against the same commit and the same catalog state. That gap is
# how a project ends up "verified" while some combination has never actually
# been exercised together.
#
# Order matters:
#   0  preconditions        docker, DataHub, venv, git state
#   1  local tests          fast failure before anything expensive
#   2  catalog reset        known state, so later gates are not testing leftovers
#   3  gates 2-7            seed, capture, reconcile, write back, incident, score
#   4  gate 8              MCP tools over real stdio
#   5  gate 9              proposals, approve, revert
#   6  fresh clone         the pushed repo, README only  <-- the one that counts
#   7  submission checks   the things the hackathon actually grades

$ErrorActionPreference = "Continue"
$repo = (Get-Item $PSScriptRoot).Parent.FullName
$log  = Join-Path $repo "verify_all.log"
if (Test-Path $log) { Remove-Item $log }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONHASHSEED = "0"
$env:PYTHONPATH = Join-Path $repo "src"

$EXPECTED_F1 = 0.8282290279627164
$EXPECTED_SHA = "acbedff47da6255e6b69877f722e52c2421f711e560d8517919e04bfe12ee5d3"
$EXPECTED_SCORE = 0.3333

$results = [System.Collections.ArrayList]::new()
$failures = [System.Collections.ArrayList]::new()
$humanTodos = [System.Collections.ArrayList]::new()

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line; Add-Content -Path $log -Value $line
}
function Record($phase, $check, $ok, $detail, $human = $false) {
    <#
      $human marks a check that can only be satisfied by Kishan (post a paper,
      record a video). Those are real blockers for submission but they are not
      code defects, and lumping them together hides genuine regressions in a
      list of known TODOs.
    #>
    [void]$results.Add([pscustomobject]@{
        Phase = $phase; Check = $check; OK = $ok; Detail = $detail; Human = $human
    })
    $mark = if ($ok) { "PASS" } elseif ($human) { "TODO" } else { "FAIL" }
    Log ("  [{0}] {1}: {2}" -f $mark, $check, $detail)
    if (-not $ok) {
        if ($human) { [void]$humanTodos.Add("$check -- $detail") }
        else { [void]$failures.Add("$phase / $check -- $detail") }
    }
}
function RunScript($phase, $name, $script) {
    Log ""
    Log "===== $phase : $name ====="
    $out = & (Join-Path $PSScriptRoot $script) 2>&1 | Out-String
    Add-Content -Path $log -Value $out
    $ok = $LASTEXITCODE -eq 0
    if (-not $ok) {
        Write-Host $out
        Record $phase $name $false "exited $LASTEXITCODE (full output in verify_all.log)"
    } else {
        Record $phase $name $true "green"
    }
    return $ok
}

$py = Join-Path $repo ".venv\Scripts\python.exe"
Push-Location $repo

# ==================================================== 0. preconditions
Log "===== 0. preconditions ====="
Record "0" "venv exists" (Test-Path $py) $py

docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
Record "0" "docker daemon" ($LASTEXITCODE -eq 0) "docker info"

$gmsOk = $false
try {
    $r = Invoke-WebRequest -Uri "http://localhost:8080/config" -UseBasicParsing -TimeoutSec 5
    $gmsOk = $r.StatusCode -eq 200
} catch { }
Record "0" "DataHub GMS on :8080" $gmsOk "required by gates 2-9"

$uiOk = $false
try {
    $r = Invoke-WebRequest -Uri "http://localhost:9002" -UseBasicParsing -TimeoutSec 5
    $uiOk = $r.StatusCode -eq 200
} catch { }
Record "0" "DataHub UI on :9002" $uiOk "the demo is recorded here"

# Everything downstream clones from GitHub, so unpushed work is untested work.
$dirty = @(git status --porcelain)
Record "0" "working tree clean" ($dirty.Count -eq 0) "$($dirty.Count) uncommitted change(s)"

git fetch origin main --quiet 2>$null
$ahead = (git rev-list --count origin/main..HEAD 2>$null)
Record "0" "local == origin/main" ($ahead -eq "0") "$ahead commit(s) unpushed"

$trackedLogs = @(git ls-files | Where-Object { $_ -like "*.log" })
Record "0" "no logs tracked in git" ($trackedLogs.Count -eq 0) "$($trackedLogs.Count) log file(s) tracked"

if ($failures.Count -gt 0) {
    Log ""
    Log "Preconditions failed. Fix these before the rest is meaningful:"
    $failures | ForEach-Object { Log "  - $_" }
    Pop-Location; exit 1
}

# ==================================================== 1. local tests
Log ""
Log "===== 1. local test suite ====="
$out = & $py -m pytest tests -q -rs 2>&1 | Out-String
Add-Content -Path $log -Value $out
$passed = 0; $skipped = 0; $failed = 0
if ($out -match '(\d+) passed')  { $passed  = [int]$Matches[1] }
if ($out -match '(\d+) skipped') { $skipped = [int]$Matches[1] }
if ($out -match '(\d+) failed')  { $failed  = [int]$Matches[1] }
Record "1" "tests pass" ($failed -eq 0) "$passed passed, $skipped skipped, $failed failed"
Record "1" "skips within budget" ($skipped -le 2) "$skipped skipped (a skip proves nothing)"

# ============================================ 2. reset the catalog
# Later gates must not be testing leftovers from an earlier run.
Log ""
Log "===== 2. reset catalog to a known state ====="
& $py demo\seed_catalog.py 2>&1 | Out-String | Add-Content -Path $log
Record "2" "catalog reseeded" ($LASTEXITCODE -eq 0) "seed_catalog.py"

# ==================================================== 3-5. the gates
$g2 = RunScript "3" "gates 2-7 (seed, capture, reconcile, writeback)" "run_gate2.ps1"
$g3 = RunScript "3" "gate 6 (incident path)" "run_gate3.ps1"
$g8 = RunScript "4" "gate 8 (MCP over stdio)" "run_gate8.ps1"
$g9 = RunScript "5" "gate 9 (proposals + revert)" "run_gate9.ps1"
$g10 = RunScript "5" "gate 10 (declared lineage via DataHub's MCP Server)" "run_gate10.ps1"

# ============================================ 6. the fresh-clone gate
$gv = RunScript "6" "fresh clone reproduces the README" "verify.ps1"

# ==================================================== 7. submission checks
Log ""
Log "===== 7. submission requirements ====="

$license = Get-Content (Join-Path $repo "LICENSE") -Raw -ErrorAction SilentlyContinue
$licOk = $license -and $license -match "Apache License" -and $license -match "Version 2\.0, January 2004"
Record "7" "LICENSE is verbatim Apache-2.0" $licOk "GitHub detects this for the About box"

$examples = @(Get-ChildItem (Join-Path $repo "examples") -File -ErrorAction SilentlyContinue)
Record "7" "examples/ has sample outputs" ($examples.Count -ge 5) "$($examples.Count) files"

# The README publishes a digest. A judge will hash the file.
$incident = Join-Path $repo "examples\incident_report.md"
$shaOk = $false; $actualSha = "missing"
if (Test-Path $incident) {
    $bytes = [System.IO.File]::ReadAllBytes($incident)
    $actualSha = [System.BitConverter]::ToString(
        [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace("-","").ToLower()
    $shaOk = $actualSha -eq $EXPECTED_SHA
}
Record "7" "shipped digest matches the README" $shaOk $actualSha

$crlf = $false
foreach ($f in $examples) {
    if ($f.Extension -in ".md", ".json") {
        $b = [System.IO.File]::ReadAllBytes($f.FullName)
        for ($i = 0; $i -lt $b.Length - 1; $i++) {
            if ($b[$i] -eq 13 -and $b[$i+1] -eq 10) { $crlf = $true; break }
        }
    }
}
Record "7" "no CRLF in examples/" (-not $crlf) "CRLF would break every published digest"

$readme = Get-Content (Join-Path $repo "README.md") -Raw
Record "7" "README publishes the real F1" ($readme -match [regex]::Escape("0.8282")) "0.8282"
Record "7" "README has a Limitations section" ($readme -match "## Limitations") "required by the brief"
$placeholder = $readme -match "\[KISHAN: Paper 2 SSRN URL\]"
Record "7" "Paper 2 SSRN URL filled in" (-not $placeholder) `
    $(if ($placeholder) { "still [KISHAN: Paper 2 SSRN URL] -- post to SSRN and replace" } else { "resolved" }) `
    $true

# The numbers the demo turns on, read back from the live catalog.
$rep = Get-Content (Join-Path $repo "examples\reconciliation_report.json") -Raw | ConvertFrom-Json
$verdictOk = $rep.summary.VERIFIED -eq 1 -and $rep.summary.PHANTOM -eq 1 -and $rep.summary.UNDECLARED -eq 1
Record "7" "verdicts are 1/1/1" $verdictOk `
    "V=$($rep.summary.VERIFIED) P=$($rep.summary.PHANTOM) U=$($rep.summary.UNDECLARED)"

# Emit the score as JSON and compare numerically. No console parsing.
$scoreJson = & $py -c @"
import json, sys
sys.path.insert(0, 'src')
from polygraph.score import score_all_consumers
report = json.load(open('examples/reconciliation_report.json'))
print(json.dumps([s.to_dict() for s in score_all_consumers(report)]))
"@ 2>&1 | Out-String

$scoreOk = $false; $scoreDetail = "could not compute"
try {
    $scores = $scoreJson | ConvertFrom-Json
    $job = $scores | Where-Object { $_.entity_urn -like "*train_fraud_model*" } | Select-Object -First 1
    if ($null -ne $job) {
        $scoreOk = [math]::Abs([double]$job.score - $EXPECTED_SCORE) -lt 1e-6
        $scoreDetail = "score=$($job.score) precision=$($job.precision) recall=$($job.recall)"
    } else {
        $scoreDetail = "no score for train_fraud_model in the report"
    }
} catch {
    $scoreDetail = "score computation failed: $($_.Exception.Message). Raw: $($scoreJson.Trim())"
}
Record "7" "integrity score is $EXPECTED_SCORE" $scoreOk $scoreDetail

$docs = @("docs\VIDEO_SCRIPT.md","docs\DEVPOST.md","docs\claude_desktop_config.example.json",
          "docs\upstream\PR_DRAFT.md","docs\upstream\autolineage-pathlib-fix.patch")
foreach ($d in $docs) {
    Record "7" "$d present" (Test-Path (Join-Path $repo $d)) ""
}

Pop-Location

# ==================================================== verdict
Log ""
Log "=================================================================="
$results | ForEach-Object {
    $mark = if ($_.OK) { "PASS" } elseif ($_.Human) { "TODO" } else { "FAIL" }
    Log ("  {0}  {1,-46} {2}" -f $mark, $_.Check, $_.Detail)
}
Log "=================================================================="
$total = $results.Count
$bad = $failures.Count
$todo = $humanTodos.Count
Log ""
if ($todo -gt 0) {
    Log "$todo item(s) waiting on you (not code defects):"
    $humanTodos | ForEach-Object { Log "  - $_" }
    Log ""
}
if ($bad -eq 0) {
    if ($todo -eq 0) {
        Log "ALL $total CHECKS GREEN. Submission-ready."
    } else {
        Log "NO CODE DEFECTS. $($total - $todo) of $total green; $todo waiting on you."
    }
    Log ""
    Log "Verified against one commit and one catalog state:"
    Log "  - $passed tests, $skipped skipped"
    Log "  - F1 $EXPECTED_F1"
    Log "  - verdicts 1 VERIFIED / 1 PHANTOM / 1 UNDECLARED"
    Log "  - integrity score $EXPECTED_SCORE"
    Log "  - incident digest $EXPECTED_SHA"
    Log "  - MCP tools live over stdio"
    Log "  - proposals applied and reverted cleanly"
    Log "  - fresh clone reproduces the README"
    Log ""
    Log "Still requires a human: UI screenshots, the video, the Devpost submission."
    exit $(if ($todo -gt 0) { 2 } else { 0 })
} else {
    Log "$bad CODE DEFECT(S) OUT OF $total CHECKS:"
    $failures | ForEach-Object { Log "  - $_" }
    Log ""
    Log "Full output: $log"
    exit 1
}
