<#
.SYNOPSIS
  Reset the Foundry evals demo to its starting point.

.DESCRIPTION
  1. Deletes the production agent, all bake-off candidate agents and (unless -KeepRuns) all eval runs.
  2. Re-creates contoso-te-assistant as version 1 (gpt-5.4-mini + file_search) from azure.yaml.
  3. With -Baseline: runs the gated baseline evaluation of v1 (about 4 minutes) so the console and the
     portal start with a green run.

  Infrastructure, model deployments, the golden dataset, the evaluators and the console are untouched.

.EXAMPLE
  ./demo/reset.ps1 -Baseline
#>
param(
    [switch]$KeepRuns,
    [switch]$Baseline
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    if (Test-Path "$root\.tools\azd\azd.exe") { $env:PATH = "$root\.tools\azd;$env:PATH" }
    $python = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { 'python' }
    $env:PYTHONIOENCODING = 'utf-8'

    Write-Host '==> Removing demo state (agents, candidates, eval runs)' -ForegroundColor Cyan
    $resetArgs = @('demo/reset.py') + $(if ($KeepRuns) { @('--keep-runs') } else { @() })
    & $python @resetArgs
    if ($LASTEXITCODE) { throw 'demo/reset.py failed' }

    Write-Host '==> Re-creating contoso-te-assistant v1 from azure.yaml' -ForegroundColor Cyan
    azd deploy contoso-te-assistant --no-prompt
    if ($LASTEXITCODE) { throw 'azd deploy contoso-te-assistant failed' }

    if ($Baseline) {
        Write-Host '==> Baseline evaluation of v1 (gate: pass rate >= 0.9)' -ForegroundColor Cyan
        azd ai eval run start --eval te-policy-regression --name baseline-v1 --fail-on pass-rate=0.9
    }
    $url = (azd env get-value CONSOLE_URL 2>$null)
    Write-Host "`nReady. Console: $url" -ForegroundColor Green
}
finally {
    Pop-Location
}
