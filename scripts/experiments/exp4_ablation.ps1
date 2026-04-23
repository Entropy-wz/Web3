param(
    [int[]]$Seeds = @(42, 77, 101, 131),
    [int]$Ticks = 40,
    [int]$Retail = 24,
    [string]$Scenario = "staircase_formal_run",
    [string]$OutputRoot = "artifacts/paper_runs/exp4",
    [string]$PromptProfilePath = "configs/prompt_profiles/whale_eclipse_extreme.json",
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
        "--seed", "$seed",
        "--traffic-profile", "eval",
        "--social-eclipse-attack",
        "--eclipse-attacker-id", "whale_1",
        "--eclipse-trigger-tick", "1",
        "--eclipse-window-ticks", "5",
        "--eclipse-sell-ust", "150000",
        "--prompt-profile-path", $PromptProfilePath
    )

    $noneArgs = @($common + @(
            "--output-dir", "$OutputRoot/s${seed}_none"
        ))

    $onlyAArgs = @($common + @(
            "--enable-mitigation-a",
            "--output-dir", "$OutputRoot/s${seed}_onlyA"
        ))

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

    Invoke-Phase5 -ArgsList $noneArgs
    Invoke-Phase5 -ArgsList $onlyAArgs
    Invoke-Phase5 -ArgsList $onlyBArgs
    Invoke-Phase5 -ArgsList $abArgs
}

Write-Host "[DONE] exp4 2x2 matrix completed."
