[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SetupPath,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSha
)

$ErrorActionPreference = "Stop"
if (-not $env:CI) { throw "The Setup installation exercise may run only on a disposable CI Windows user." }
if ($ExpectedSha -notmatch '^[0-9a-f]{40}$') { throw "The expected source SHA is invalid." }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SetupPath = (Resolve-Path -LiteralPath $SetupPath).Path
$signing = Join-Path $PSScriptRoot "windows_local_signing.ps1"
$localAppData = [Environment]::GetFolderPath("LocalApplicationData")
$current = Join-Path $localAppData "Programs\GamGUI\current"
$dataRoot = Join-Path $localAppData "GamGUI"
$statePath = Join-Path $dataRoot "updates\state.json"
$helper = Join-Path $dataRoot "updater\GamGUIUpdater.exe"
$uninstaller = Join-Path $dataRoot "installer\unins000.exe"

function Invoke-Process([string]$FilePath, [string[]]$Arguments, [int]$ExpectedExitCode = 0) {
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne $ExpectedExitCode) {
        throw "$([System.IO.Path]::GetFileName($FilePath)) returned $($process.ExitCode), expected $ExpectedExitCode."
    }
}

function Invoke-SetupFailure([string[]]$Arguments) {
    $process = Start-Process -FilePath $SetupPath -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -eq 0) { throw "An unsafe Setup invocation unexpectedly succeeded." }
    if (Test-Path -LiteralPath $current) { throw "A rejected Setup invocation created an installation." }
}

function New-TrustedIdentity() {
    $identity = & $signing -Action Enroll -TrustLocalCertificate | ConvertFrom-Json
    if ($LASTEXITCODE -or $identity.trust_required -or -not $identity.created) {
        throw "The disposable runner identity was not created and trusted."
    }
    return [string]$identity.certificate_sha256
}

function Remove-IdentityIfPresent([string]$CertificateSha256) {
    if ($CertificateSha256 -notmatch '^[0-9a-f]{64}$') { return }
    $match = @(Get-ChildItem -LiteralPath Cert:\CurrentUser\My | Where-Object {
        $_.Subject -eq "CN=GamGUI Local" -and
        ([System.BitConverter]::ToString($_.GetCertHash([System.Security.Cryptography.HashAlgorithmName]::SHA256)) -replace "-", "").ToLowerInvariant() -eq $CertificateSha256
    })
    if ($match) { & $signing -Action Remove -CertificateSha256 $CertificateSha256 | Out-Null }
}

function Assert-Installed([string]$Profile, [string]$CertificateSha256) {
    if (-not (Test-Path -LiteralPath (Join-Path $current "GamGUI.exe") -PathType Leaf)) { throw "The application executable is missing." }
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) { throw "The updater state is missing." }
    $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    if ([string]$state.installed_sha -ne $ExpectedSha -or [string]$state.installed_profile -ne $Profile) {
        throw "The installed exact-SHA profile identity is wrong."
    }
    if ([string]$state.local_signer_thumbprint -ne $CertificateSha256) { throw "The installed signer pin is wrong." }
    & $signing -Action Verify -Path $current -CertificateSha256 $CertificateSha256 | Out-Null
    if ($LASTEXITCODE) { throw "The installed application signature receipt failed." }
    foreach ($file in @($helper, $uninstaller)) {
        & $signing -Action VerifyFile -Path $file -CertificateSha256 $CertificateSha256 | Out-Null
        if ($LASTEXITCODE) { throw "A locally signed setup file failed verification." }
    }
    $registration = Get-ItemProperty -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{120C7CDF-EBCA-4D86-B724-FDBD4BE66E03}_is1" -ErrorAction Stop
    if ([string]$registration.DisplayName -ne "GamGUI") { throw "GamGUI was not registered in Apps & Features." }
    $smoke = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-setup-ci-" + [guid]::NewGuid())
    $env:GAMGUI_APP_DATA_DIR = $smoke
    try {
        Invoke-Process (Join-Path $current "GamGUI.exe") @("--self-test", "--json")
    } finally {
        Remove-Item Env:GAMGUI_APP_DATA_DIR -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $smoke -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Exercise-Profile([string]$Profile) {
    $signer = New-TrustedIdentity
    try {
        Invoke-Process $SetupPath @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/PROFILE=$Profile", "/PINNEDSIGNERSHA256=$signer"
        )
        Assert-Installed $Profile $signer
        $repeat = Start-Process -FilePath $SetupPath -ArgumentList @(
            "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/PROFILE=$Profile", "/PINNEDSIGNERSHA256=$signer"
        ) -Wait -PassThru -WindowStyle Hidden
        if ($repeat.ExitCode -eq 0) { throw "Setup overwrote an existing installation." }
        Invoke-Process $uninstaller @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
        if (Test-Path -LiteralPath $current) { throw "Uninstall left the application installed." }
        if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) { throw "Uninstall deleted local application data without consent." }
        $remaining = @(Get-ChildItem -LiteralPath Cert:\CurrentUser\My | Where-Object { $_.Subject -eq "CN=GamGUI Local" })
        if ($remaining) { throw "Uninstall left the local signing identity behind." }
    } finally {
        Remove-IdentityIfPresent $signer
    }
}

if (Test-Path -LiteralPath $current) { throw "The disposable runner was not clean before Setup verification." }
Invoke-SetupFailure @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/PROFILE=core")
Invoke-SetupFailure @(
    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/PROFILE=core",
    "/PINNEDSIGNERSHA256=0000000000000000000000000000000000000000000000000000000000000000"
)

$tamperSigner = New-TrustedIdentity
try {
    $tamperedBootstrap = Join-Path $repoRoot "dist\bootstrap-core"
    Add-Content -LiteralPath (Join-Path $tamperedBootstrap "licenses\GamGUI-LICENSE.txt") -Value "tamper-test"
    $tampered = Start-Process -FilePath powershell.exe -ArgumentList @(
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
        (Join-Path $tamperedBootstrap "install.ps1"), "-NoShortcuts", "-TrustMode", "Pretrusted",
        "-PretrustedSignerSha256", $tamperSigner
    ) -Wait -PassThru -WindowStyle Hidden
    if ($tampered.ExitCode -eq 0 -or (Test-Path -LiteralPath $current)) { throw "A tampered bootstrap was accepted." }
} finally {
    Remove-IdentityIfPresent $tamperSigner
}

Exercise-Profile "core"
Exercise-Profile "classroom-oneroster"
Write-Host "Windows Setup disposable-runner exercise passed for both profiles."
