param(
    [int[]]$Seeds = @(42, 77, 101, 131),
    [int]$Ticks = 40,
    [int]$Retail = 24,
    [string]$Scenario = "staircase_formal_run",
    [string]$OutputRoot = "artifacts/paper_runs/exp2",
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

foreach ($seed in $Seeds) {
    $common = @(
        "--ticks", "$Ticks",
        "--retail", "$Retail",
        "--scenario", "$Scenario",
        "--seed", "$seed"
    )

    $noneArgs = @($common + @(
            "--output-dir", "$OutputRoot/s${seed}_none"
        ))
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

    Invoke-Phase5 -ArgsList $noneArgs
    Invoke-Phase5 -ArgsList $dos1Args
    Invoke-Phase5 -ArgsList $dos3Args
}

Write-Host "[DONE] exp2 matrix completed."
