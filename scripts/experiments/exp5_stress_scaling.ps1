param(
    [int[]]$Seeds = @(42, 77, 101, 131),
    [int]$Ticks = 40,
    [string]$Scenario = "staircase_formal_run",
    [string]$OutputRoot = "artifacts/paper_runs/exp5",
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

$profiles = @(
    @{ Name = "mild"; Retail = 12; MaxTx = 50 },
    @{ Name = "medium"; Retail = 24; MaxTx = 50 },
    @{ Name = "extreme"; Retail = 48; MaxTx = 20 }
)

$defenses = @(
    @{ Name = "none"; Args = @() },
    @{ Name = "ab"; Args = @("--enable-mitigation-a", "--enable-mitigation-b", "--mitigation-b-warm-start", "--mitigation-b-panic-threshold", "0.0") }
)

foreach ($seed in $Seeds) {
    foreach ($profile in $profiles) {
        foreach ($def in $defenses) {
            $profileName = [string]$profile["Name"]
            $defName = [string]$def["Name"]
            $retailCount = [int]$profile["Retail"]
            $maxTx = [int]$profile["MaxTx"]
            $out = "$OutputRoot/s${seed}_${profileName}_${defName}"
            $args = @(
                "--ticks", "$Ticks",
                "--retail", "$retailCount",
                "--scenario", "$Scenario",
                "--seed", "$seed",
                "--max-tx-per-tick", "$maxTx",
                "--traffic-profile", "eval",
                "--social-eclipse-attack",
                "--eclipse-attacker-id", "whale_1",
                "--eclipse-trigger-tick", "1",
                "--eclipse-window-ticks", "5",
                "--eclipse-sell-ust", "150000",
                "--prompt-profile-path", $PromptProfilePath,
                "--output-dir", $out
            )
            $defArgs = @($def["Args"])
            if ($defArgs.Count -gt 0) {
                $args += $defArgs
            }
            if (-not $WithPaperCharts) {
                $args += "--no-paper-charts"
            }
            Invoke-Phase5 -ArgsList $args
        }
    }
}

Write-Host "[DONE] exp5 stress scaling matrix completed."
