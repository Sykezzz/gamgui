[CmdletBinding()]
param([switch]$RemoveData)

$ErrorActionPreference = "Stop"
$localAppData = [Environment]::GetFolderPath("LocalApplicationData")
$installRoot = Join-Path $localAppData "Programs\GamGUI"
$dataRoot = Join-Path $localAppData "GamGUI"
$statePath = Join-Path $dataRoot "updates\state.json"
$signingScript = Join-Path $dataRoot "updater\windows_local_signing.ps1"
if (-not (Test-Path -LiteralPath $signingScript)) {
    $signingScript = Join-Path $installRoot "current\_internal\resources\updater\windows_local_signing.ps1"
}
if ((Test-Path -LiteralPath $statePath) -and (Test-Path -LiteralPath $signingScript)) {
    $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    if ([string]$state.local_signer_thumbprint -match '^[0-9a-fA-F]{64}$') {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Remove -CertificateSha256 ([string]$state.local_signer_thumbprint)
    }
}
$startMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\GamGUI.lnk"
$desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "GamGUI.lnk"
Remove-Item -LiteralPath $startMenu -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $desktop -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $installRoot) { Remove-Item -LiteralPath $installRoot -Recurse -Force }
foreach ($path in @((Join-Path $dataRoot "updater"), (Join-Path $dataRoot "updates"))) {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
if ($RemoveData -and (Test-Path -LiteralPath $dataRoot)) { Remove-Item -LiteralPath $dataRoot -Recurse -Force }
Write-Host ("GamGUI was removed. Application data was " + $(if ($RemoveData) { "deleted" } else { "preserved" }) + ".")
