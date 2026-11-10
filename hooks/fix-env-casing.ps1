# The microsoft.foundry provider writes custom Bicep outputs into the azd env with
# mangled casing (APPLICATIONINSIGHTS_CONNECTION_STRING -> applicationinsightS_...).
# Services such as the App Insights connection reference the upper-case names, so
# copy every mixed-case key to its upper-case form.
$ErrorActionPreference = 'Stop'

$values = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
foreach ($line in (azd env get-values)) {
    if ($line -match '^([A-Za-z0-9_]+)="(.*)"$') {
        $values[$Matches[1]] = $Matches[2] -replace '\\"', '"'
    }
}

foreach ($name in @($values.Keys)) {
    $upper = $name.ToUpperInvariant()
    if ($name -cne $upper -and $values[$name] -ne '') {
        if (-not $values.ContainsKey($upper) -or $values[$upper] -ne $values[$name]) {
            azd env set $upper $values[$name] | Out-Null
            Write-Host "  env: $upper"
        }
    }
}
