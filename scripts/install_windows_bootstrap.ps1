[CmdletBinding()]
param(
    [switch]$NoShortcuts,
    [ValidateSet("Interactive", "Pretrusted")]
    [string]$TrustMode = "Interactive",
    [switch]$TrustApproved,
    [string]$PretrustedSignerSha256 = "",
    [string]$ProgressReceipt = "",
    [string]$AdditionalFileToSign = ""
)

$ErrorActionPreference = "Stop"
$bootstrapRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifestPath = Join-Path $bootstrapRoot "bootstrap-manifest.json"
$localAppData = [Environment]::GetFolderPath("LocalApplicationData")
$installRoot = Join-Path $localAppData "Programs\GamGUI"
$current = Join-Path $installRoot "current"
$dataRoot = Join-Path $localAppData "GamGUI"
$updatesRoot = Join-Path $dataRoot "updates"
$updaterRoot = Join-Path $dataRoot "updater"
$helper = Join-Path $updaterRoot "GamGUIUpdater.exe"
$installedSigningPath = Join-Path $updaterRoot "windows_local_signing.ps1"
$installedUninstallPath = Join-Path $updaterRoot "uninstall.ps1"
$statePath = Join-Path $updatesRoot "state.json"
$journalPath = Join-Path $updatesRoot "bootstrap-install.json"
$incoming = Join-Path $installRoot ".bootstrap-pending"
$signingScript = Join-Path $bootstrapRoot "windows_local_signing.ps1"
$uninstallScript = Join-Path $bootstrapRoot "uninstall.ps1"

function Write-AtomicJson([string]$Path, $Value) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = "$Path.tmp"
    [System.IO.File]::WriteAllText(
        $temporary,
        (($Value | ConvertTo-Json -Depth 12 -Compress) + "`n"),
        [System.Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Write-SetupProgress([string]$Phase, [string]$Status, [string]$MessageCode) {
    if (-not $ProgressReceipt) { return }
    if ($Phase -notmatch '^[a-z0-9_]{2,40}$' -or $Status -notin @("pending", "working", "complete", "failed") -or $MessageCode -notmatch '^[A-Z0-9-]{3,64}$') {
        throw "The setup progress receipt is invalid."
    }
    Write-AtomicJson $ProgressReceipt ([ordered]@{
        schema_version = 1
        phase = $Phase
        status = $Status
        profile = if ($script:manifest) { [string]$script:manifest.profile } else { "" }
        source_sha = if ($script:manifest) { [string]$script:manifest.source_sha } else { "" }
        message_code = $MessageCode
    })
}

function Write-InstallJournal([string]$Phase) {
    Write-AtomicJson $journalPath ([ordered]@{
        schema_version = 1
        source_sha = [string]$script:manifest.source_sha
        profile = [string]$script:manifest.profile
        phase = $Phase
        signer_sha256 = $script:signerSha
        created_certificate = [bool]$script:createdCertificate
        added_trust = [bool]$script:addedTrust
        installed_current = [bool]$script:installedCurrent
        installed_helper = [bool]$script:installedHelper
        installed_signing_support = [bool]$script:installedSigningSupport
        installed_uninstall_support = [bool]$script:installedUninstallSupport
    })
}

function Get-InstalledSourceSha() {
    $profilePath = Join-Path $current "_internal\resources\components\profile.json"
    if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) { return "" }
    try {
        return [string]((Get-Content -Raw -LiteralPath $profilePath | ConvertFrom-Json).artifact.source_sha)
    } catch {
        return ""
    }
}

function Remove-RecordedCertificate($Journal) {
    $sha = [string]$Journal.signer_sha256
    if ($sha -notmatch '^[0-9a-fA-F]{64}$') { return }
    if ([bool]$Journal.created_certificate) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Remove -CertificateSha256 $sha
        if ($LASTEXITCODE) { throw "The interrupted setup identity could not be removed." }
    } elseif ([bool]$Journal.added_trust) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action RemoveTrust -CertificateSha256 $sha
        if ($LASTEXITCODE) { throw "The interrupted setup trust record could not be removed." }
    }
}

function Recover-IncompleteBootstrap() {
    if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) { return }
    $journal = Get-Content -Raw -LiteralPath $journalPath | ConvertFrom-Json
    if (
        [int]$journal.schema_version -ne 1 -or
        [string]$journal.source_sha -notmatch '^[0-9a-f]{40}$' -or
        [string]$journal.profile -notin @("core", "classroom-oneroster")
    ) {
        throw "The previous bootstrap recovery journal is invalid."
    }
    if ([string]$journal.phase -eq "complete" -and (Test-Path -LiteralPath $statePath -PathType Leaf) -and (Get-InstalledSourceSha) -eq [string]$journal.source_sha) {
        Remove-Item -LiteralPath $journalPath -Force
        return
    }
    if (Test-Path -LiteralPath $incoming) { Remove-Item -LiteralPath $incoming -Recurse -Force }
    if ([bool]$journal.installed_current -and (Get-InstalledSourceSha) -eq [string]$journal.source_sha) {
        Remove-Item -LiteralPath $current -Recurse -Force
        Remove-Item -LiteralPath "$current.artifact.json" -Force -ErrorAction SilentlyContinue
    }
    if ([bool]$journal.installed_helper) { Remove-Item -LiteralPath $helper -Force -ErrorAction SilentlyContinue }
    if ([bool]$journal.installed_signing_support) { Remove-Item -LiteralPath $installedSigningPath -Force -ErrorAction SilentlyContinue }
    if ([bool]$journal.installed_uninstall_support) { Remove-Item -LiteralPath $installedUninstallPath -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    Remove-RecordedCertificate $journal
    Remove-Item -LiteralPath $journalPath -Force
}

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "The bootstrap manifest is missing." }
if (-not (Test-Path -LiteralPath $signingScript -PathType Leaf)) { throw "The bootstrap signing support is missing." }
if (-not (Test-Path -LiteralPath $uninstallScript -PathType Leaf)) { throw "The bootstrap uninstall support is missing." }
$script:manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if (
    $manifest.format -ne "gamgui-windows-bootstrap-v1" -or
    [string]$manifest.source_sha -notmatch '^[0-9a-f]{40}$' -or
    [string]$manifest.profile -notin @("core", "classroom-oneroster") -or
    $manifest.platform -ne "windows"
) { throw "The bootstrap manifest identity is invalid." }

Write-SetupProgress "package" "working" "SETUP-CHECKING-PACKAGE"
foreach ($record in @($manifest.files)) {
    $relative = [string]$record.path
    if (-not $relative -or [System.IO.Path]::IsPathRooted($relative) -or $relative.Replace("\", "/").Split("/") -contains "..") {
        throw "The bootstrap manifest contains an unsafe path."
    }
    $candidate = Join-Path $bootstrapRoot $relative.Replace("/", "\")
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "A bootstrap file is missing: $relative" }
    $hash = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne [string]$record.sha256) { throw "A bootstrap file failed its SHA-256 receipt: $relative" }
}
Write-SetupProgress "package" "complete" "SETUP-PACKAGE-CHECKED"

New-Item -ItemType Directory -Path $updatesRoot -Force | Out-Null
Recover-IncompleteBootstrap
if (Test-Path -LiteralPath $current) { throw "GamGUI is already installed. Use its updater or uninstall it first." }

$script:createdCertificate = $false
$script:addedTrust = $false
$script:installedCurrent = $false
$script:installedHelper = $false
$script:installedSigningSupport = $false
$script:installedUninstallSupport = $false
$script:signerSha = ""
$stateTemporary = "$statePath.bootstrap"
$additionalSigned = $false

try {
    Write-InstallJournal "consent_accepted"
    Write-SetupProgress "identity" "working" "SETUP-CREATING-IDENTITY"
    if ($TrustMode -eq "Pretrusted") {
        if ($PretrustedSignerSha256 -notmatch '^[0-9a-fA-F]{64}$') { throw "Pretrusted setup requires a pinned signer SHA-256." }
        $inspection = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Inspect -CertificateSha256 $PretrustedSignerSha256 | ConvertFrom-Json
        if ($LASTEXITCODE) { throw "The pinned pretrusted signer could not be inspected." }
        $script:signerSha = [string]$inspection.certificate_sha256
        if (-not [bool]$inspection.trusted) { throw "The pinned pretrusted signer is not trusted for this user." }
    } else {
        if (-not $TrustApproved) { throw "Certificate trust was not approved. Nothing was installed." }
        $enrollment = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Enroll | ConvertFrom-Json
        if ($LASTEXITCODE) { throw "The GamGUI Local identity could not be created." }
        $script:createdCertificate = [bool]$enrollment.created
        $script:signerSha = [string]$enrollment.certificate_sha256
        Write-InstallJournal "identity_created"
        if ($enrollment.trust_required) {
            $enrollment = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Enroll -TrustLocalCertificate | ConvertFrom-Json
            if ($LASTEXITCODE) { throw "The GamGUI Local identity could not be trusted." }
            $script:addedTrust = $true
        }
        if ($enrollment.trust_required) { throw "The GamGUI Local certificate could not be trusted for this user." }
        $script:signerSha = [string]$enrollment.certificate_sha256
    }
    Write-InstallJournal "identity_ready"
    Write-SetupProgress "identity" "complete" "SETUP-IDENTITY-READY"

    New-Item -ItemType Directory -Path $installRoot, $updaterRoot -Force | Out-Null
    if (Test-Path -LiteralPath $incoming) { Remove-Item -LiteralPath $incoming -Recurse -Force }
    Copy-Item -LiteralPath (Join-Path $bootstrapRoot "application") -Destination $incoming -Recurse
    $incomingCurrent = Join-Path $incoming "current"
    $profilePath = Join-Path $incomingCurrent "_internal\resources\components\profile.json"
    $profile = Get-Content -Raw -LiteralPath $profilePath | ConvertFrom-Json
    if ([string]$profile.artifact.source_sha -ne [string]$manifest.source_sha -or [string]$profile.artifact.profile -ne [string]$manifest.profile) {
        throw "The selected profile does not match the bootstrap manifest."
    }
    $profile.artifact.signer_thumbprint = $signerSha
    [System.IO.File]::WriteAllText($profilePath, (($profile | ConvertTo-Json -Depth 12) + "`n"), [System.Text.UTF8Encoding]::new($false))
    Write-InstallJournal "payload_staged"

    Write-SetupProgress "signing" "working" "SETUP-SIGNING-FILES"
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Sign -Path $incomingCurrent -CertificateSha256 $signerSha
    if ($LASTEXITCODE) { throw "The installed application could not be locally signed." }
    $script:installedHelper = $true
    Write-InstallJournal "installing_helper"
    Copy-Item -LiteralPath (Join-Path $bootstrapRoot "updater\GamGUIUpdater.exe") -Destination $helper -Force
    $script:installedSigningSupport = $true
    Write-InstallJournal "installing_signing_support"
    Copy-Item -LiteralPath $signingScript -Destination $installedSigningPath -Force
    $script:installedUninstallSupport = $true
    Write-InstallJournal "installing_uninstall_support"
    Copy-Item -LiteralPath $uninstallScript -Destination $installedUninstallPath -Force
    foreach ($file in @($helper, $installedSigningPath, $installedUninstallPath)) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action SignFile -Path $file -CertificateSha256 $signerSha
        if ($LASTEXITCODE) { throw "Installed support files could not be locally signed." }
    }
    if ($AdditionalFileToSign) {
        $resolvedAdditional = (Resolve-Path -LiteralPath $AdditionalFileToSign).Path
        $allowedInstallerRoot = [System.IO.Path]::GetFullPath((Join-Path $dataRoot "installer"))
        if (-not $resolvedAdditional.StartsWith($allowedInstallerRoot + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "The additional setup file is outside the installer directory."
        }
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action SignFile -Path $resolvedAdditional -CertificateSha256 $signerSha
        if ($LASTEXITCODE) { throw "The Windows uninstaller could not be locally signed." }
        $additionalSigned = $true
    }
    Write-InstallJournal "payload_signed"
    Write-SetupProgress "signing" "complete" "SETUP-FILES-SIGNED"

    $script:installedCurrent = $true
    Write-InstallJournal "activating"
    Move-Item -LiteralPath $incomingCurrent -Destination $current
    Remove-Item -LiteralPath $incoming -Force
    Write-InstallJournal "activated"
    $executable = Join-Path $current "GamGUI.exe"
    & $executable --write-artifact-sidecar
    if ($LASTEXITCODE) { throw "The locally signed artifact receipt could not be created." }

    Write-SetupProgress "verification" "working" "SETUP-VERIFYING-INSTALLATION"
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Verify -Path $current -CertificateSha256 $signerSha
    if ($LASTEXITCODE) { throw "The installed application signature did not verify." }
    foreach ($file in @($helper, $installedSigningPath, $installedUninstallPath)) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action VerifyFile -Path $file -CertificateSha256 $signerSha
        if ($LASTEXITCODE) { throw "An installed support-file signature did not verify." }
    }
    if ($AdditionalFileToSign -and $additionalSigned) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action VerifyFile -Path $AdditionalFileToSign -CertificateSha256 $signerSha
        if ($LASTEXITCODE) { throw "The Windows uninstaller signature did not verify." }
    }
    Write-InstallJournal "verified"
    Write-SetupProgress "verification" "complete" "SETUP-INSTALLATION-VERIFIED"

    Write-SetupProgress "self_test" "working" "SETUP-RUNNING-SELF-TEST"
    $smokeData = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-bootstrap-self-test-" + [guid]::NewGuid())
    $env:GAMGUI_APP_DATA_DIR = $smokeData
    try {
        $process = Start-Process -FilePath $executable -ArgumentList "--self-test", "--json" -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -ne 0) { throw "The installed application self-test failed." }
    } finally {
        Remove-Item Env:GAMGUI_APP_DATA_DIR -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $smokeData) { Remove-Item -LiteralPath $smokeData -Recurse -Force }
    }
    Write-SetupProgress "self_test" "complete" "SETUP-SELF-TEST-PASSED"

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
    if (-not $NoShortcuts) {
        $shell = New-Object -ComObject WScript.Shell
        $startMenu = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\GamGUI.lnk"
        $shortcut = $shell.CreateShortcut($startMenu)
        $shortcut.TargetPath = $helper
        $shortcut.Arguments = "--launch-installed"
        $shortcut.WorkingDirectory = $updaterRoot
        $shortcut.Save()
    }
    Write-AtomicJson $statePath $state
    Write-InstallJournal "complete"
    Write-SetupProgress "complete" "complete" "SETUP-INSTALLATION-COMPLETE"
    Remove-Item -LiteralPath $journalPath -Force
} catch {
    Write-SetupProgress "install" "failed" "SETUP-INSTALLATION-FAILED"
    if (Test-Path -LiteralPath $incoming) { Remove-Item -LiteralPath $incoming -Recurse -Force -ErrorAction SilentlyContinue }
    if ($installedCurrent -and (Get-InstalledSourceSha) -eq [string]$manifest.source_sha) {
        Remove-Item -LiteralPath $current -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath "$current.artifact.json" -Force -ErrorAction SilentlyContinue
    }
    if ($installedHelper) { Remove-Item -LiteralPath $helper -Force -ErrorAction SilentlyContinue }
    if ($installedSigningSupport) { Remove-Item -LiteralPath $installedSigningPath -Force -ErrorAction SilentlyContinue }
    if ($installedUninstallSupport) { Remove-Item -LiteralPath $installedUninstallPath -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $stateTemporary -Force -ErrorAction SilentlyContinue
    if ($createdCertificate -and $signerSha) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Remove -CertificateSha256 $signerSha
    } elseif ($addedTrust -and $signerSha) {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action RemoveTrust -CertificateSha256 $signerSha
    }
    Remove-Item -LiteralPath $journalPath -Force -ErrorAction SilentlyContinue
    throw
}

Write-Host "GamGUI installed for this Windows user at $current"
Write-Host "Source SHA: $($artifact.artifact.source_sha)"
Write-Host "Profile: $($artifact.artifact.profile)"
Write-Host "Signer SHA-256: $signerSha"
