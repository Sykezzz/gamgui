[CmdletBinding()]
param(
    [string]$ExpectedSha = "",
    [string]$ToolchainBundleDir = "",
    [string]$InnoCompilerPath = "",
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw "The Windows Setup wizard must be built on Windows." }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Run uv sync with the dev, desktop, and build extras first." }
$gamVersionOutput = & $python -c "from gamgui.core.gam.commands import EXPECTED_GAM_VERSION; print(EXPECTED_GAM_VERSION)"
$gamVersionExitCode = $LASTEXITCODE
$gamVersion = ($gamVersionOutput | Out-String).Trim()
if ($gamVersionExitCode -ne 0 -or $gamVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "The repository GAM source pin is invalid."
}
$sourceSha = (& git rev-parse HEAD).Trim().ToLowerInvariant()
if (-not $ExpectedSha) { $ExpectedSha = $sourceSha }
if ($ExpectedSha -notmatch '^[0-9a-fA-F]{40}$' -or $sourceSha -ne $ExpectedSha.ToLowerInvariant()) {
    throw "The checkout does not match the requested exact SHA."
}
if (-not $ToolchainBundleDir) { throw "The checksum-verified MinGit and uv archive directory is required." }
$ToolchainBundleDir = (Resolve-Path -LiteralPath $ToolchainBundleDir).Path

$ownedPaths = @(
    "main.py", "gamgui", "gamgui.spec", "gamgui-updater.spec", "pyproject.toml", "uv.lock",
    "scripts/build_windows_release.ps1", "scripts/build_windows_setup.ps1", "scripts/windows_setup.iss",
    "scripts/fetch_gam_windows.ps1", "scripts/gam_checksums.txt", "scripts/windows_local_signing.ps1",
    "scripts/windows_signer_preflight.ps1",
    "scripts/install_windows_bootstrap.ps1", "scripts/uninstall_windows_bootstrap.ps1", "LICENSE"
)
$dirty = & git status --porcelain --untracked-files=all -- @ownedPaths
if ($dirty -and -not $AllowDirty) {
    throw "Packaged setup source changes are present. Commit them before building an exact-SHA installer.`n$($dirty -join [Environment]::NewLine)"
}

$installerManifestPath = Join-Path $repoRoot "gamgui\resources\installer\windows-installer-toolchain.json"
$installerManifest = Get-Content -Raw -LiteralPath $installerManifestPath | ConvertFrom-Json
if (
    [string]$installerManifest.revision -ne "windows-setup-v1" -or
    [string]$installerManifest.compiler.name -ne "inno-setup" -or
    [string]$installerManifest.compiler.version -ne "7.0.2" -or
    [string]$installerManifest.compiler.sha256 -notmatch '^[0-9a-f]{64}$' -or
    [string]$installerManifest.compiler.executable_sha256 -notmatch '^[0-9a-f]{64}$'
) { throw "The committed Windows installer toolchain manifest is invalid." }
$installerToolchainDigest = (Get-FileHash -LiteralPath $installerManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()

if (-not $InnoCompilerPath) {
    $compilerRoot = Join-Path $repoRoot "build\installer-toolchain\$($installerManifest.revision)"
    $installerPath = Join-Path $compilerRoot ([string]$installerManifest.compiler.archive)
    $portableRoot = Join-Path $compilerRoot "inno"
    $InnoCompilerPath = Join-Path $portableRoot ([string]$installerManifest.compiler.executable)
    New-Item -ItemType Directory -Path $compilerRoot -Force | Out-Null
    $installerHash = if (Test-Path -LiteralPath $installerPath -PathType Leaf) {
        (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { "" }
    if ($installerHash -ne [string]$installerManifest.compiler.sha256) {
        $downloadPath = "$installerPath.download"
        Remove-Item -LiteralPath $downloadPath -Force -ErrorAction SilentlyContinue
        Invoke-WebRequest -Uri ([string]$installerManifest.compiler.url) -OutFile $downloadPath -UseBasicParsing
        $downloadHash = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($downloadHash -ne [string]$installerManifest.compiler.sha256) {
            Remove-Item -LiteralPath $downloadPath -Force -ErrorAction SilentlyContinue
            throw "The Inno Setup compiler installer failed its committed SHA-256 pin."
        }
        Move-Item -LiteralPath $downloadPath -Destination $installerPath -Force
        $installerHash = $downloadHash
    }
    if ($installerHash -ne [string]$installerManifest.compiler.sha256) { throw "The Inno Setup compiler installer failed its committed SHA-256 pin." }
    $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        -not $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notlike "*$($installerManifest.compiler.publisher)*") {
        throw "The Inno Setup compiler installer publisher signature is not valid."
    }
    if (-not (Test-Path -LiteralPath $InnoCompilerPath -PathType Leaf)) {
        New-Item -ItemType Directory -Path $portableRoot -Force | Out-Null
        $compilerInstall = Start-Process -FilePath $installerPath -ArgumentList @(
            "/PORTABLE=1", "/SILENT", "/CURRENTUSER", "/SP-", "/NORESTART", "/DIR=`"$portableRoot`""
        ) -Wait -PassThru
        if ($compilerInstall.ExitCode -ne 0) { throw "The pinned Inno Setup compiler could not be unpacked." }
    }
}
$InnoCompilerPath = (Resolve-Path -LiteralPath $InnoCompilerPath).Path
$compilerHash = (Get-FileHash -LiteralPath $InnoCompilerPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($compilerHash -ne [string]$installerManifest.compiler.executable_sha256) {
    throw "The Inno Setup compiler executable does not match the committed 7.0.2 pin."
}

$releaseArgs = @{
    ExpectedSha = $sourceSha
    ToolchainBundleDir = $ToolchainBundleDir
    Bootstrap = $true
}
if ($AllowDirty) { $releaseArgs.AllowDirty = $true }
foreach ($profile in @("core", "classroom-oneroster")) {
    & (Join-Path $PSScriptRoot "build_windows_release.ps1") -Profile $profile @releaseArgs
    if ($LASTEXITCODE) { throw "The $profile bootstrap profile build failed." }
    & git update-index --refresh -- gamgui/resources/gam7/VERSION
}

$coreBootstrap = Join-Path $repoRoot "dist\bootstrap-core"
$classroomBootstrap = Join-Path $repoRoot "dist\bootstrap-classroom-oneroster"
foreach ($path in @($coreBootstrap, $classroomBootstrap)) {
    if (-not (Test-Path -LiteralPath (Join-Path $path "bootstrap-manifest.json") -PathType Leaf)) {
        throw "A required profile bootstrap is missing: $path"
    }
}

$outputRoot = Join-Path $repoRoot "dist\windows-setup"
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$setupPath = Join-Path $outputRoot "GamGUI-Setup-0.0.1-windows-x86_64.exe"
Remove-Item -LiteralPath $setupPath -Force -ErrorAction SilentlyContinue
$iss = Join-Path $PSScriptRoot "windows_setup.iss"
$defines = @(
    "/DSourceRoot=$repoRoot",
    "/DOutputRoot=$outputRoot",
    "/DSourceSha=$sourceSha",
    "/DGamVersion=$gamVersion",
    "/DCoreBootstrap=$coreBootstrap",
    "/DClassroomBootstrap=$classroomBootstrap"
)
& $InnoCompilerPath @defines $iss
if ($LASTEXITCODE -or -not (Test-Path -LiteralPath $setupPath -PathType Leaf)) { throw "Inno Setup did not produce the unified Setup executable." }

$setupSize = (Get-Item -LiteralPath $setupPath).Length
if ($setupSize -ge 2GB) { throw "The Windows Setup executable exceeds GitHub's 2 GiB individual asset limit." }
$setupSignature = Get-AuthenticodeSignature -LiteralPath $setupPath
if ($setupSignature.Status -ne [System.Management.Automation.SignatureStatus]::NotSigned) {
    throw "The public Setup executable must have the documented NotSigned trust status."
}
$setupHash = (Get-FileHash -LiteralPath $setupPath -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumPath = "$setupPath.sha256"
Set-Content -LiteralPath $checksumPath -Value "$setupHash  $([System.IO.Path]::GetFileName($setupPath))" -Encoding ascii

$updaterToolchainPath = Join-Path $repoRoot "gamgui\resources\updater\windows-toolchain.json"
$profileReceipts = foreach ($profile in @("core", "classroom-oneroster")) {
    $bootstrap = Join-Path $repoRoot "dist\bootstrap-$profile\bootstrap-manifest.json"
    $receipt = Get-Content -Raw -LiteralPath $bootstrap | ConvertFrom-Json
    [ordered]@{
        profile = $profile
        source_sha = [string]$receipt.source_sha
        bootstrap_manifest_sha256 = (Get-FileHash -LiteralPath $bootstrap -Algorithm SHA256).Hash.ToLowerInvariant()
        toolchain_manifest_digest = [string]$receipt.toolchain_manifest_digest
    }
}
$releaseManifest = [ordered]@{
    schema_version = 1
    format = "gamgui-windows-setup-release-v1"
    version = "0.0.1-windows.1"
    source_sha = $sourceSha
    platform = "windows"
    architecture = "x86_64"
    profiles = @($profileReceipts)
    gam_version = $gamVersion
    setup = [ordered]@{
        file = [System.IO.Path]::GetFileName($setupPath)
        bytes = $setupSize
        sha256 = $setupHash
        signing_status = "NotSigned"
    }
    installer_toolchain = [ordered]@{
        revision = [string]$installerManifest.revision
        compiler = "Inno Setup 7.0.2"
        manifest_sha256 = $installerToolchainDigest
    }
    updater_toolchain_manifest_sha256 = (Get-FileHash -LiteralPath $updaterToolchainPath -Algorithm SHA256).Hash.ToLowerInvariant()
}
$releaseManifestPath = Join-Path $outputRoot "windows-bootstrap-manifest.json"
[System.IO.File]::WriteAllText(
    $releaseManifestPath,
    (($releaseManifest | ConvertTo-Json -Depth 10) + "`n"),
    [System.Text.UTF8Encoding]::new($false)
)
$releaseManifestHash = (Get-FileHash -LiteralPath $releaseManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$releaseManifestPath.sha256" -Value "$releaseManifestHash  windows-bootstrap-manifest.json" -Encoding ascii

Write-Host "==> Unified Windows Setup built: $setupPath"
Write-Host "    Source SHA: $sourceSha"
Write-Host "    SHA-256: $setupHash"
Write-Host "    Signing: NotSigned"
