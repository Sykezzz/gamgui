[CmdletBinding()]
param(
    [ValidateSet("core", "classroom-oneroster")]
    [string]$Profile = "classroom-oneroster",
    [switch]$AllowDirty,
    [string]$ExpectedSha = "",
    [string]$CertificateSha256 = "",
    [switch]$CiEphemeralCertificate,
    [string]$ToolchainBundleDir = "",
    [switch]$Bootstrap
)

$ErrorActionPreference = "Stop"
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw "Windows releases must be built on Windows." }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run uv sync with the dev, desktop, and build extras first." }

$packagedPaths = @(
    "main.py", "gamgui", "gamgui.spec", "gamgui-updater.spec", "pyproject.toml", "uv.lock",
    "scripts/build_windows_release.ps1", "scripts/fetch_gam_windows.ps1", "scripts/gam_checksums.txt",
    "scripts/windows_local_signing.ps1", "scripts/install_windows_bootstrap.ps1", "scripts/uninstall_windows_bootstrap.ps1"
)
$dirty = & git status --porcelain --untracked-files=all -- @packagedPaths
if ($dirty -and -not $AllowDirty) {
    throw "Packaged source changes are present. Commit them before building an exact-SHA Windows release.`n$($dirty -join [Environment]::NewLine)"
}
$sourceSha = (& git rev-parse HEAD).Trim().ToLowerInvariant()
if ($ExpectedSha -and $sourceSha -ne $ExpectedSha.ToLowerInvariant()) { throw "The checkout does not match the requested exact SHA." }

$toolchainManifest = Join-Path $repoRoot "gamgui\resources\updater\windows-toolchain.json"
$toolchainDigest = (Get-FileHash -LiteralPath $toolchainManifest -Algorithm SHA256).Hash.ToLowerInvariant()
if ($Bootstrap) {
    if (-not $ToolchainBundleDir -or -not (Test-Path -LiteralPath $ToolchainBundleDir -PathType Container)) {
        throw "Bootstrap builds require the checksum-verified MinGit and uv archive directory."
    }
    $toolchain = Get-Content -Raw -LiteralPath $toolchainManifest | ConvertFrom-Json
    foreach ($asset in @($toolchain.assets)) {
        $archive = Join-Path $ToolchainBundleDir ([string]$asset.archive)
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw "Bundled toolchain archive is missing: $($asset.archive)" }
        $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne [string]$asset.sha256) { throw "Bundled toolchain archive failed its committed SHA-256 pin: $($asset.archive)" }
    }
}

& (Join-Path $PSScriptRoot "fetch_gam_windows.ps1")
if ($LASTEXITCODE) { throw "Pinned GAM vendoring failed." }
& $python -c "import PyInstaller, webview, keyring"
if ($LASTEXITCODE) { throw "Locked build dependencies are unavailable." }

$metadataRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-profile-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $metadataRoot | Out-Null
try {
    $env:GAMGUI_BUILD_PROFILE = $Profile
    $env:GAMGUI_SOURCE_SHA = $sourceSha
    $env:GAMGUI_BUILD_ARCH = "x86_64"
    $env:GAMGUI_BUILD_PLATFORM = "windows"
    $env:GAMGUI_BUNDLE_FORMAT = "onedir"
    $env:GAMGUI_MINIMUM_MACOS = "10.0"
    $env:GAMGUI_PACKAGING_REVISION = "2-windows-local"
    $env:GAMGUI_BUILD_METADATA_DIR = $metadataRoot
    $env:GAMGUI_SIGNER_THUMBPRINT = $CertificateSha256.ToLowerInvariant()
    $env:GAMGUI_TOOLCHAIN_MANIFEST_DIGEST = $toolchainDigest
    if ($ToolchainBundleDir) { $env:GAMGUI_TOOLCHAIN_BUNDLE_DIR = (Resolve-Path -LiteralPath $ToolchainBundleDir).Path }
    & $python -m PyInstaller --noconfirm --clean gamgui.spec
    if ($LASTEXITCODE) { throw "PyInstaller failed." }
    & $python -m PyInstaller --noconfirm --clean --distpath "dist\updater-helper" --workpath "build\updater-helper" gamgui-updater.spec
    if ($LASTEXITCODE) { throw "Standalone updater-helper build failed." }
} finally {
    Remove-Item -LiteralPath $metadataRoot -Recurse -Force
    Remove-Item Env:GAMGUI_TOOLCHAIN_BUNDLE_DIR -ErrorAction SilentlyContinue
}

$bundle = Join-Path $repoRoot "dist\GamGUI"
$executable = Join-Path $bundle "GamGUI.exe"
$helper = Join-Path $repoRoot "dist\updater-helper\GamGUIUpdater.exe"
if (-not (Test-Path -LiteralPath $executable)) { throw "The Windows bundle did not contain GamGUI.exe." }
if (-not (Test-Path -LiteralPath $helper)) { throw "The Windows build did not contain GamGUIUpdater.exe." }
$embeddedProfilePath = Join-Path $bundle "_internal\resources\components\profile.json"
$embeddedProfile = Get-Content -Raw -LiteralPath $embeddedProfilePath | ConvertFrom-Json
if (
    [string]$embeddedProfile.artifact.source_sha -ne $sourceSha -or
    [string]$embeddedProfile.artifact.profile -ne $Profile -or
    [string]$embeddedProfile.artifact.platform -ne "windows" -or
    [string]$embeddedProfile.artifact.bundle_format -ne "onedir" -or
    [string]$embeddedProfile.artifact.architecture -ne "x86_64" -or
    [string]$embeddedProfile.artifact.toolchain_manifest_digest -ne $toolchainDigest -or
    [string]$embeddedProfile.artifact.signer_thumbprint -ne $CertificateSha256.ToLowerInvariant()
) { throw "The embedded Windows artifact identity did not match the exact build inputs." }
$embeddedGamVersion = (Get-Content -Raw -LiteralPath (Join-Path $bundle "_internal\resources\gam7\VERSION")).Trim()
if ($embeddedGamVersion -notmatch '7\.47\.02') { throw "The embedded Windows GAM version did not match the tested pin." }
& $python -c "import struct,sys; p=open(sys.argv[1],'rb'); p.seek(0x3c); p.seek(struct.unpack('<I',p.read(4))[0]+4); assert struct.unpack('<H',p.read(2))[0] == 0x8664" $executable
if ($LASTEXITCODE) { throw "The Windows executable architecture did not match x86_64." }
$helperProbe = Start-Process -FilePath $helper -Wait -PassThru -WindowStyle Hidden
if ($helperProbe.ExitCode -ne 2) { throw "The standalone updater helper accepted an ordinary application launch." }

$signingScript = Join-Path $PSScriptRoot "windows_local_signing.ps1"
if ($CertificateSha256) {
    $ciSigning = @()
    if ($CiEphemeralCertificate) { $ciSigning += "-CiEphemeralCertificate" }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Sign -Path $bundle -CertificateSha256 $CertificateSha256 @ciSigning
    if ($LASTEXITCODE) { throw "Application signing failed." }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action SignFile -Path $helper -CertificateSha256 $CertificateSha256 @ciSigning
    if ($LASTEXITCODE) { throw "Updater-helper signing failed." }
    & $python -c "import sys; from pathlib import Path; from gamgui.core.components import write_artifact_sidecar; write_artifact_sidecar(Path(sys.argv[1]), signing_channel='local', signing_authority='GamGUI Local')" $bundle
    if ($LASTEXITCODE) { throw "Artifact identity generation failed." }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action Verify -Path $bundle -CertificateSha256 $CertificateSha256 @ciSigning
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $signingScript -Action VerifyFile -Path $helper -CertificateSha256 $CertificateSha256 @ciSigning
    if ($LASTEXITCODE) { throw "Signed Windows bundle verification failed." }
}

$selfTest = Join-Path $repoRoot "dist\GamGUI-windows-self-test.json"
$smokeData = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-win-self-test-" + [guid]::NewGuid())
$env:GAMGUI_APP_DATA_DIR = $smokeData
$env:GAMGUI_SELF_TEST_OUTPUT = $selfTest
try {
    $process = Start-Process -FilePath $executable -ArgumentList "--self-test", "--json" -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $selfTest)) { throw "Packaged self-test failed." }
    $receipt = Get-Content -Raw -LiteralPath $selfTest | ConvertFrom-Json
    if (-not $receipt.ok) { throw "Packaged self-test reported failure." }
} finally {
    Remove-Item Env:GAMGUI_SELF_TEST_OUTPUT -ErrorAction SilentlyContinue
    Remove-Item Env:GAMGUI_APP_DATA_DIR -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $smokeData) { Remove-Item -LiteralPath $smokeData -Recurse -Force }
}

$assetStem = "GamGUI-0.0.1-windows-x86_64-$Profile"
if ($Bootstrap) { $assetStem += "-bootstrap-$($sourceSha.Substring(0,12))" }
$archive = Join-Path $repoRoot "dist\$assetStem.zip"
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }

if ($Bootstrap) {
    $stage = Join-Path $repoRoot "dist\bootstrap-$Profile"
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    New-Item -ItemType Directory -Path (Join-Path $stage "application") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $stage "updater") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $stage "toolchain") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $stage "licenses") -Force | Out-Null
    Copy-Item -LiteralPath $bundle -Destination (Join-Path $stage "application\current") -Recurse
    Copy-Item -LiteralPath $helper -Destination (Join-Path $stage "updater\GamGUIUpdater.exe")
    Copy-Item -LiteralPath $toolchainManifest -Destination (Join-Path $stage "toolchain\windows-toolchain.json")
    Get-ChildItem -LiteralPath $ToolchainBundleDir -Filter "*.zip" -File | Copy-Item -Destination (Join-Path $stage "toolchain")
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install_windows_bootstrap.ps1") -Destination (Join-Path $stage "install.ps1")
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "uninstall_windows_bootstrap.ps1") -Destination (Join-Path $stage "uninstall.ps1")
    Copy-Item -LiteralPath $signingScript -Destination (Join-Path $stage "windows_local_signing.ps1")
    Copy-Item -LiteralPath $selfTest -Destination (Join-Path $stage "self-test.json")
    Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination (Join-Path $stage "licenses\GamGUI-LICENSE.txt")
    Copy-Item -LiteralPath (Join-Path $repoRoot "gamgui\resources\gam7\LICENSE") -Destination (Join-Path $stage "licenses\GAM-LICENSE.txt")
    $records = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $stage -Recurse -File | Sort-Object FullName)) {
        $records += [ordered]@{
            path = $file.FullName.Substring($stage.Length).TrimStart("\").Replace("\", "/")
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    [ordered]@{
        format = "gamgui-windows-bootstrap-v1"
        source_sha = $sourceSha
        profile = $Profile
        platform = "windows"
        toolchain_revision = (Get-Content -Raw -LiteralPath $toolchainManifest | ConvertFrom-Json).revision
        toolchain_manifest_digest = $toolchainDigest
        files = $records
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $stage "bootstrap-manifest.json") -Encoding utf8
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $archive -CompressionLevel Optimal
} else {
    $items = @($bundle, $selfTest, $helper)
    if (Test-Path -LiteralPath "$bundle.artifact.json") { $items += "$bundle.artifact.json" }
    Compress-Archive -Path $items -DestinationPath $archive -CompressionLevel Optimal
}

$archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$archive.sha256" -Value "$archiveHash  $assetStem.zip" -Encoding ascii
Write-Host "==> Windows artifact built: $archive"
Write-Host "    Source SHA: $sourceSha"
Write-Host "    SHA-256: $archiveHash"
Write-Host ("    Signing: " + $(if ($CertificateSha256) { "GamGUI Local $CertificateSha256" } else { "bootstrap trust boundary; installer enrolls the user's local identity before first run" }))
