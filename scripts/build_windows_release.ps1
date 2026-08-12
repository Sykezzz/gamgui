[CmdletBinding()]
param(
    [ValidateSet("core", "classroom-oneroster")]
    [string]$Profile = "classroom-oneroster",
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw "Windows releases must be built on Windows." }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run uv sync with the dev, desktop, and build extras first." }

$packagedPaths = @("main.py", "gamgui", "gamgui.spec", "pyproject.toml", "uv.lock", "scripts/build_windows_release.ps1", "scripts/fetch_gam_windows.ps1", "scripts/gam_checksums.txt")
$dirty = & git status --porcelain --untracked-files=all -- @packagedPaths
if ($dirty -and -not $AllowDirty) {
    throw "Packaged source changes are present. Commit them before building an exact-SHA Windows release.`n$($dirty -join [Environment]::NewLine)"
}

& (Join-Path $PSScriptRoot "fetch_gam_windows.ps1")
if ($LASTEXITCODE) { throw "Pinned GAM vendoring failed." }
& $python -c "import PyInstaller, webview, keyring"
if ($LASTEXITCODE) { throw "Locked build dependencies are unavailable." }

$sourceSha = (& git rev-parse HEAD).Trim().ToLowerInvariant()
$metadataRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-profile-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $metadataRoot | Out-Null
try {
    $env:GAMGUI_BUILD_PROFILE = $Profile
    $env:GAMGUI_SOURCE_SHA = $sourceSha
    $env:GAMGUI_BUILD_ARCH = "x86_64"
    $env:GAMGUI_MINIMUM_MACOS = "10.0"
    $env:GAMGUI_PACKAGING_REVISION = "1-windows"
    $env:GAMGUI_BUILD_METADATA_DIR = $metadataRoot
    & $python -m PyInstaller --noconfirm --clean gamgui.spec
    if ($LASTEXITCODE) { throw "PyInstaller failed." }
} finally {
    Remove-Item -LiteralPath $metadataRoot -Recurse -Force
}

$bundle = Join-Path $repoRoot "dist\GamGUI"
$executable = Join-Path $bundle "GamGUI.exe"
if (-not (Test-Path -LiteralPath $executable)) { throw "The Windows bundle did not contain GamGUI.exe." }

& $python -c "import sys; from pathlib import Path; from gamgui.core.components import write_artifact_sidecar; print(write_artifact_sidecar(Path(sys.argv[1])))" $bundle
if ($LASTEXITCODE) { throw "Artifact identity generation failed." }
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
$archive = Join-Path $repoRoot "dist\$assetStem.zip"
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
Compress-Archive -Path $bundle, "$bundle.artifact.json", $selfTest -DestinationPath $archive -CompressionLevel Optimal
$archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$archive.sha256" -Value "$archiveHash  $assetStem.zip" -Encoding ascii
Write-Host "==> Windows release built: $archive"
Write-Host "    SHA-256: $archiveHash"
Write-Host "    Signing: unsigned local artifact (Authenticode not configured)"
