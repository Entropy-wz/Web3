param(
    [int[]]$Seeds = @(42, 77, 101, 131),
    [int]$Ticks = 40,
    [int]$Retail = 24,
    [string]$Scenario = "staircase_formal_run",
    [string]$OutputRoot = "artifacts/paper_runs_split/exp4",
    [string]$PromptProfilePath = "configs/prompt_profiles/whale_eclipse_extreme.json",
    [int]$RunsPerPart = 4,
    [int]$Part = 1,
    [switch]$ListParts,
    [switch]$WithPaperCharts,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$scriptPath = "scripts/visualization/phase5_governance_visualizer.py"

function Invoke-Phase5 {
    param([string[]]$ArgsList)
    $cmdPreview = "python $scriptPath " + ($ArgsList -join " ")
    Write-Host "[RUN] $cmdPreview"
    if ($DryRun) { return }
    & python $scriptPath @ArgsList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

$runs = @()
foreach ($seed in $Seeds) {
    $common = @(
        "--ticks", "$Ticks",
        "--retail", "$Retail",
        "--scenario", "$Scenario",
        "--seed", "$seed",
        "--traffic-profile", "eval",
        "--social-eclipse-attack",
        "--eclipse-attacker-id", "whale_1",
        "--eclipse-trigger-tick", "1",
        "--eclipse-window-ticks", "5",
        "--eclipse-sell-ust", "150000",
        "--prompt-profile-path", $PromptProfilePath
    )
    $noneArgs = @($common + @("--output-dir", "$OutputRoot/s${seed}_none"))
    $onlyAArgs = @($common + @("--enable-mitigation-a", "--output-dir", "$OutputRoot/s${seed}_onlyA"))
    $onlyBArgs = @($common + @(
            "--enable-mitigation-b",
            "--mitigation-b-warm-start",
            "--mitigation-b-panic-threshold", "0.0",
            "--output-dir", "$OutputRoot/s${seed}_onlyB"
        ))
    $abArgs = @($common + @(
            "--enable-mitigation-a",
            "--enable-mitigation-b",
            "--mitigation-b-warm-start",
            "--mitigation-b-panic-threshold", "0.0",
            "--output-dir", "$OutputRoot/s${seed}_ab"
        ))
    if (-not $WithPaperCharts) {
        $noneArgs += "--no-paper-charts"
        $onlyAArgs += "--no-paper-charts"
        $onlyBArgs += "--no-paper-charts"
        $abArgs += "--no-paper-charts"
    }
    $runs += @{ Name = "s${seed}_none"; Args = $noneArgs }
    $runs += @{ Name = "s${seed}_onlyA"; Args = $onlyAArgs }
    $runs += @{ Name = "s${seed}_onlyB"; Args = $onlyBArgs }
    $runs += @{ Name = "s${seed}_ab"; Args = $abArgs }
}

$totalRuns = $runs.Count
if ($RunsPerPart -le 0) { throw "RunsPerPart must be > 0" }
$totalParts = [Math]::Ceiling($totalRuns / $RunsPerPart)

if ($ListParts) {
    Write-Host "[INFO] total_runs=$totalRuns total_parts=$totalParts runs_per_part=$RunsPerPart"
    for ($p = 1; $p -le $totalParts; $p++) {
        $start = ($p - 1) * $RunsPerPart
        $end = [Math]::Min($start + $RunsPerPart - 1, $totalRuns - 1)
        $names = @()
        for ($i = $start; $i -le $end; $i++) { $names += $runs[$i].Name }
        Write-Host ("Part {0}: {1}" -f $p, ($names -join ", "))
    }
    return
}

if ($Part -lt 1 -or $Part -gt $totalParts) {
    throw "Part must be in [1,$totalParts]"
}

$startIdx = ($Part - 1) * $RunsPerPart
$endIdx = [Math]::Min($startIdx + $RunsPerPart - 1, $totalRuns - 1)
Write-Host "[INFO] exp4 part=$Part/$totalParts running indexes $startIdx..$endIdx"
for ($i = $startIdx; $i -le $endIdx; $i++) {
    Invoke-Phase5 -ArgsList $runs[$i].Args
}

Write-Host "[DONE] exp4 split part $Part completed."
