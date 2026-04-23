param(
    [int[]]$Seeds = @(42, 77, 101, 131),
    [int]$Ticks = 40,
    [int]$Retail = 24,
    [string]$Scenario = "staircase_formal_run",
    [string]$OutputRoot = "artifacts/paper_runs_split/exp2",
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
        "--seed", "$seed"
    )
    $noneArgs = @($common + @("--output-dir", "$OutputRoot/s${seed}_none"))
    $dos1Args = @($common + @(
            "--governance-dos-attack",
            "--dos-whale-luna", "1200",
            "--dos-sell-ust", "300000",
            "--output-dir", "$OutputRoot/s${seed}_dos1"
        ))
    $dos3Args = @($common + @(
            "--governance-dos-attack",
            "--dos-whale-luna", "4000",
            "--dos-sell-ust", "300000",
            "--output-dir", "$OutputRoot/s${seed}_dos3"
        ))
    if (-not $WithPaperCharts) {
        $noneArgs += "--no-paper-charts"
        $dos1Args += "--no-paper-charts"
        $dos3Args += "--no-paper-charts"
    }
    $runs += @{ Name = "s${seed}_none"; Args = $noneArgs }
    $runs += @{ Name = "s${seed}_dos1"; Args = $dos1Args }
    $runs += @{ Name = "s${seed}_dos3"; Args = $dos3Args }
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
Write-Host "[INFO] exp2 part=$Part/$totalParts running indexes $startIdx..$endIdx"
for ($i = $startIdx; $i -le $endIdx; $i++) {
    Invoke-Phase5 -ArgsList $runs[$i].Args
}

Write-Host "[DONE] exp2 split part $Part completed."
