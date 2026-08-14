[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$CertificateSha256,
    [Parameter(Mandatory = $true)]
    [string]$SigningScript,
    [ValidateRange(1, 60)]
    [int]$TimeoutSeconds = 15
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $SigningScript -PathType Leaf)) { exit 1 }

$receiptBase = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-signer-preflight-" + [guid]::NewGuid())
$stdoutPath = "$receiptBase.out"
$stderrPath = "$receiptBase.err"
$process = $null

function Get-NativeWindowsPowerShellPath() {
    # Inno Setup is a 32-bit launcher even when the installation itself is in
    # 64-bit mode.  A relative powershell.exe can therefore inspect the pinned
    # certificate through the 32-bit key-provider view and incorrectly report
    # that its private key is unavailable.  Always run the read-only inspection
    # through native Windows PowerShell, matching the transactional backend.
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        $sysnative = Join-Path $env:SystemRoot "Sysnative\WindowsPowerShell\v1.0\powershell.exe"
        if (Test-Path -LiteralPath $sysnative -PathType Leaf) { return $sysnative }
    }
    return (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe")
}

function Stop-PreflightProcess([System.Diagnostics.Process]$Target) {
    if ($null -eq $Target -or $Target.HasExited) { return }
    $killer = Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" `
        -ArgumentList @("/PID", $Target.Id, "/T", "/F") -WindowStyle Hidden -PassThru
    if (-not $killer.WaitForExit(5000)) {
        Stop-Process -Id $killer.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $Target.Id -Force -ErrorAction SilentlyContinue
}

try {
    $argumentLine = @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy Bypass",
        "-File `"$SigningScript`"",
        "-Action Inspect",
        "-CertificateSha256 $CertificateSha256"
    ) -join " "
    $process = Start-Process -FilePath (Get-NativeWindowsPowerShellPath) -ArgumentList $argumentLine `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath `
        -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-PreflightProcess $process
        $process = $null
        exit 2
    }
    [void]$process.WaitForExit()
    $process.Refresh()
    if ($null -eq $process.ExitCode) { exit 1 }
    exit [int]$process.ExitCode
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-PreflightProcess $process
    }
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
}
