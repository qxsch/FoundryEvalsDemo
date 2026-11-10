<#
.SYNOPSIS
  Create or update the Entra ID app registration used by the agent console's sign-in (Container Apps auth).

.DESCRIPTION
  Idempotent. It finds the app registration by display name or creates it:
    - single tenant (any user of the tenant can sign in);
    - ID tokens enabled, with a service principal.
  It stores ENTRA_CLIENT_ID / ENTRA_APP_OBJECT_ID in the azd environment. It also creates a client secret and
  stores it as ENTRA_CLIENT_SECRET, either when none is set yet or when -RotateSecret is given.
  Once the console exists (CONSOLE_URL is set after `azd provision`), it registers the redirect URI
  <CONSOLE_URL>/.auth/login/aad/callback.

  Typical order on a fresh environment:
    ./demo/setup-entra.ps1     # app registration + secret
    azd provision              # creates the console with Entra sign-in
    ./demo/setup-entra.ps1     # adds the redirect URI for the new console URL
    azd deploy console

  Rotate the secret (valid 90 days):
    ./demo/setup-entra.ps1 -RotateSecret; azd provision

  Requires the Az.Accounts and Az.Resources modules (Connect-AzAccount) and permission to create app registrations.
#>
param(
    [string]$DisplayName = 'Contoso T&E Agent Console (foundry-evals-demo)',
    [switch]$RotateSecret,
    [int]$SecretDays = 90
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (Test-Path "$root\.tools\azd\azd.exe") { $env:PATH = "$root\.tools\azd;$env:PATH" }
Import-Module Az.Accounts, Az.Resources

function Get-AzdValue([string]$name) {
    $value = azd env get-value $name --cwd $root 2>$null
    if ($LASTEXITCODE) { return '' }
    return "$value".Trim()
}

$subscription = Get-AzdValue 'AZURE_SUBSCRIPTION_ID'
if ($subscription) { Set-AzContext -Subscription $subscription | Out-Null }

$app = Get-AzADApplication -DisplayName $DisplayName | Select-Object -First 1
if (-not $app) {
    Write-Host "Creating app registration '$DisplayName'" -ForegroundColor Cyan
    $app = New-AzADApplication -DisplayName $DisplayName -SignInAudience AzureADMyOrg `
        -Web @{ ImplicitGrantSetting = @{ EnableIdTokenIssuance = $true } }
} else {
    Write-Host "Using app registration '$DisplayName' ($($app.AppId))" -ForegroundColor Cyan
}
if (-not (Get-AzADServicePrincipal -ApplicationId $app.AppId -ErrorAction SilentlyContinue)) {
    New-AzADServicePrincipal -ApplicationId $app.AppId | Out-Null
}
azd env set ENTRA_CLIENT_ID $app.AppId --cwd $root | Out-Null
azd env set ENTRA_APP_OBJECT_ID $app.Id --cwd $root | Out-Null

if ($RotateSecret -or -not (Get-AzdValue 'ENTRA_CLIENT_SECRET')) {
    $cred = New-AzADAppCredential -ObjectId $app.Id -StartDate (Get-Date) -EndDate (Get-Date).AddDays($SecretDays)
    azd env set ENTRA_CLIENT_SECRET $cred.SecretText --cwd $root | Out-Null
    Write-Host "New client secret stored in the azd environment (expires $($cred.EndDateTime.ToString('yyyy-MM-dd')))." -ForegroundColor Yellow
    Write-Host 'Run `azd provision` to hand it to the console.' -ForegroundColor Yellow
}

$consoleUrl = Get-AzdValue 'CONSOLE_URL'
if ($consoleUrl) {
    $callback = "$($consoleUrl.TrimEnd('/'))/.auth/login/aad/callback"
    $uris = @($app.Web.RedirectUri)
    if ($uris -notcontains $callback) {
        Update-AzADApplication -ObjectId $app.Id -Web @{
            RedirectUri = @($uris + $callback | Where-Object { $_ })
            HomePageUrl = $consoleUrl
            ImplicitGrantSetting = @{ EnableIdTokenIssuance = $true }
        }
        Write-Host "Registered redirect URI $callback" -ForegroundColor Green
    } else {
        Write-Host "Redirect URI already registered: $callback" -ForegroundColor Green
    }
} else {
    Write-Host 'CONSOLE_URL not set yet: run `azd provision`, then this script again to register the redirect URI.' -ForegroundColor Yellow
}
