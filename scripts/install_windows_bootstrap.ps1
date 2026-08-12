[CmdletBinding()]
param([switch]$NoShortcuts)

$ErrorActionPreference = "Stop"
$bootstrapRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifestPath = Join-Path $bootstrapRoot "bootstrap-manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "The bootstrap manifest is missing." }
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
foreach ($record in @($manifest.files)) {
    $candidate = Join-Path $bootstrapRoot ([string]$record.path).Replace("/", "\")
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "A bootstrap file is missing: $($record.path)" }
    $hash = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne [string]$record.sha256) { throw "A bootstrap file failed its SHA-256 receipt: $($record.path)" }
}

$localAppData = [Environment]::GetFolderPath("LocalApplicationData")
$installRoot = Join-Path $localAppData "Programs\GamGUI"
$current = Join-Path $installRoot "current"
$dataRoot = Join-Path $localAppData "GamGUI"
$updaterRoot = Join-Path $dataRoot "updater"
$helper = Join-Path $updaterRoot "GamGUIUpdater.exe"
$signingScript = Join-Path $bootstrapRoot "windows_local_signing.ps1"
if (Test-Path -LiteralPath $current) {
    throw "GamGUI is already installed. Use its updater or uninstall it first."
}

$createdCertificate = $false
$addedTrust = $false
$incoming = $null
$installedCurrent = $false
$installedHelper = $false
$installedSigningScript = $false
$signerSha = ""
$stateTemporary = $null
try {
    $enrollment = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Enroll | ConvertFrom-Json
    $createdCertificate = [bool]$enrollment.created
    $signerSha = [string]$enrollment.certificate_sha256
    if ($enrollment.trust_required) {
        Write-Host "GamGUI created a private, non-exportable signing key for this Windows user."
        Write-Host "To verify future local updates, its PUBLIC certificate must be added to this user's Trusted Root and Trusted Publisher stores."
        $answer = Read-Host "Type TRUST GAMGUI LOCAL to allow that trust change, or anything else to stop"
        if ($answer -cne "TRUST GAMGUI LOCAL") {
            throw "Certificate trust was not approved. Nothing was installed."
        }
        $enrollment = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Enroll -TrustLocalCertificate | ConvertFrom-Json
        $addedTrust = $true
    }
    if ($enrollment.trust_required) { throw "The GamGUI Local certificate could not be trusted for this user." }
    $signerSha = [string]$enrollment.certificate_sha256

    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $updaterRoot -Force | Out-Null
    $incoming = Join-Path $installRoot (".bootstrap-" + [guid]::NewGuid())
    Copy-Item -LiteralPath (Join-Path $bootstrapRoot "application") -Destination $incoming -Recurse
    $incomingCurrent = Join-Path $incoming "current"
    $profilePath = Join-Path $incomingCurrent "_internal\resources\components\profile.json"
    $profile = Get-Content -Raw -LiteralPath $profilePath | ConvertFrom-Json
    $profile.artifact.signer_thumbprint = $signerSha
    [System.IO.File]::WriteAllText(
        $profilePath,
        (($profile | ConvertTo-Json -Depth 12) + "`n"),
        [System.Text.UTF8Encoding]::new($false)
    )

    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Sign -Path $incomingCurrent -CertificateSha256 $signerSha
    if ($LASTEXITCODE) { throw "The installed application could not be locally signed." }
    $incomingHelper = Join-Path $bootstrapRoot "updater\GamGUIUpdater.exe"
    Copy-Item -LiteralPath $incomingHelper -Destination $helper -Force
    $installedHelper = $true
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action SignFile -Path $helper -CertificateSha256 $signerSha
    if ($LASTEXITCODE) { throw "The updater helper could not be locally signed." }
    $installedSigningPath = Join-Path $updaterRoot "windows_local_signing.ps1"
    Copy-Item -LiteralPath $signingScript -Destination $installedSigningPath -Force
    $installedSigningScript = $true
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action SignFile -Path $installedSigningPath -CertificateSha256 $signerSha
    if ($LASTEXITCODE) { throw "The updater signing support could not be locally signed." }

    Move-Item -LiteralPath $incomingCurrent -Destination $current
    $installedCurrent = $true
    Remove-Item -LiteralPath $incoming -Force
    $incoming = $null
    $executable = Join-Path $current "GamGUI.exe"
    & $executable --write-artifact-sidecar
    if ($LASTEXITCODE) { throw "The locally signed artifact receipt could not be created." }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Verify -Path $current -CertificateSha256 $signerSha
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action VerifyFile -Path $helper -CertificateSha256 $signerSha
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action VerifyFile -Path $installedSigningPath -CertificateSha256 $signerSha
    if ($LASTEXITCODE) { throw "The locally signed installation did not verify." }

$smokeData = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-bootstrap-self-test-" + [guid]::NewGuid())
$env:GAMGUI_APP_DATA_DIR = $smokeData
try {
    $process = Start-Process -FilePath $executable -ArgumentList "--self-test", "--json" -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "The installed application self-test failed." }
} finally {
    Remove-Item Env:GAMGUI_APP_DATA_DIR -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $smokeData) { Remove-Item -LiteralPath $smokeData -Recurse -Force }
}

    $artifact = Get-Content -Raw -LiteralPath "$current.artifact.json" | ConvertFrom-Json
    $state = [ordered]@{
    installed_sha = [string]$artifact.artifact.source_sha
    installed_profile = [string]$artifact.artifact.profile
    desired_profile = [string]$artifact.artifact.profile
    installed_components = @($profile.components | ForEach-Object { $_.id })
    desired_components = @($profile.components | ForEach-Object { $_.id })
    enabled_components = @($profile.components | ForEach-Object { $_.id })
    installed_artifact = $artifact.artifact
    installed_signing_channel = "local"
    installed_signing_authority = "GamGUI Local"
    installed_platform = "windows"
    candidate_platform = ""
    local_signer_thumbprint = $signerSha
    toolchain_revision = [string]$manifest.toolchain_revision
    windows_installation_root = $installRoot
    pending_bundle = ""
    }
    $statePath = Join-Path $dataRoot "updates\state.json"
    New-Item -ItemType Directory -Path (Split-Path -Parent $statePath) -Force | Out-Null
    $stateTemporary = "$statePath.bootstrap"
    [System.IO.File]::WriteAllText(
        $stateTemporary,
        (($state | ConvertTo-Json -Depth 12) + "`n"),
        [System.Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $stateTemporary -Destination $statePath -Force

    if (-not $NoShortcuts) {
        $shell = New-Object -ComObject WScript.Shell
        $startMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\GamGUI.lnk"
        $shortcut = $shell.CreateShortcut($startMenu)
        $shortcut.TargetPath = $executable
        $shortcut.WorkingDirectory = $current
        $shortcut.Save()
    }
} catch {
    if ($incoming -and (Test-Path -LiteralPath $incoming)) { Remove-Item -LiteralPath $incoming -Recurse -Force -ErrorAction SilentlyContinue }
    if ($installedCurrent -and (Test-Path -LiteralPath $current)) { Remove-Item -LiteralPath $current -Recurse -Force -ErrorAction SilentlyContinue }
    if ($installedHelper -and (Test-Path -LiteralPath $helper)) { Remove-Item -LiteralPath $helper -Force -ErrorAction SilentlyContinue }
    if ($installedSigningScript -and (Test-Path -LiteralPath $installedSigningPath)) { Remove-Item -LiteralPath $installedSigningPath -Force -ErrorAction SilentlyContinue }
    if ($stateTemporary -and (Test-Path -LiteralPath $stateTemporary)) { Remove-Item -LiteralPath $stateTemporary -Force -ErrorAction SilentlyContinue }
    if ($createdCertificate -and $signerSha) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Remove -CertificateSha256 $signerSha
    } elseif ($addedTrust -and $signerSha) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action RemoveTrust -CertificateSha256 $signerSha
    }
    throw
}
Write-Host "GamGUI installed for this Windows user at $current"
Write-Host "Source SHA: $($artifact.artifact.source_sha)"
Write-Host "Profile: $($artifact.artifact.profile)"
Write-Host "Signer SHA-256: $signerSha"
