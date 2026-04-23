param(
    [int[]]$Seeds = @(42, 77, 101, 131),
    [int]$Ticks = 40,
    [int]$Retail = 24,
    [string]$Scenario = "staircase_formal_run",
    [string]$TrafficProfile = "stress",
    [string]$OutputRoot = "artifacts/paper_runs/exp1",
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
    $baseOut = "$OutputRoot/s${seed}_baseline"
    $atkOut = "$OutputRoot/s${seed}_attack"

    $common = @(
        "--ticks", "$Ticks",
        "--retail", "$Retail",
        "--scenario", "$Scenario",
        "--seed", "$seed",
        "--traffic-profile", "$TrafficProfile"
    )

    $baselineArgs = @($common + @("--output-dir", $baseOut))
    $attackArgs = @($common + @(
            "--social-eclipse-attack",
            "--eclipse-attacker-id", "whale_1",
            "--eclipse-trigger-tick", "1",
            "--eclipse-window-ticks", "5",
            "--eclipse-sell-ust", "150000",
            "--prompt-profile-path", $PromptProfilePath,
            "--output-dir", $atkOut
        ))

    if (-not $WithPaperCharts) {
        $baselineArgs += "--no-paper-charts"
        $attackArgs += "--no-paper-charts"
    }

    Invoke-Phase5 -ArgsList $baselineArgs
    Invoke-Phase5 -ArgsList $attackArgs
}

Write-Host "[DONE] exp1 matrix completed."
