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
try {
    $argumentLine = @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy Bypass",
        "-File `"$SigningScript`"",
        "-Action Inspect",
        "-CertificateSha256 $CertificateSha256"
    ) -join " "
    $process = Start-Process -FilePath powershell.exe -ArgumentList $argumentLine `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath `
        -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F *> $null
        exit 2
    }
    [void]$process.WaitForExit()
    $process.Refresh()
    if ($null -eq $process.ExitCode) { exit 1 }
    exit [int]$process.ExitCode
} finally {
    if ($process -and -not $process.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F *> $null
    }
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
}
