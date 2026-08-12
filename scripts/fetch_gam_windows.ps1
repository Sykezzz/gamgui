[CmdletBinding()]
param(
    [string]$Tag = "v7.47.02"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$destination = Join-Path $repoRoot "gamgui\resources\gam7"
$destinationParent = Split-Path -Parent $destination
$assetName = "gam-$($Tag.TrimStart('v'))-windows-x86_64.zip"
$checksumFile = Join-Path $repoRoot "scripts\gam_checksums.txt"
$checksumLine = Get-Content -LiteralPath $checksumFile |
    Where-Object { $_ -match "^[0-9a-f]{64}\s+$([regex]::Escape($assetName))$" } |
    Select-Object -First 1
if (-not $checksumLine) {
    throw "No committed checksum exists for $assetName."
}
$expected = ($checksumLine -split "\s+")[0].ToLowerInvariant()

$taskRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-gam-windows-" + [guid]::NewGuid())
$archive = Join-Path $taskRoot $assetName
$expanded = Join-Path $taskRoot "expanded"
$stage = Join-Path $destinationParent (".gam7-stage-" + [guid]::NewGuid())
$backup = Join-Path $destinationParent (".gam7-backup-" + [guid]::NewGuid())

try {
    New-Item -ItemType Directory -Path $taskRoot, $expanded, $stage -Force | Out-Null
    $url = "https://github.com/GAM-team/GAM/releases/download/$Tag/$assetName"
    Write-Host "==> Downloading pinned GAM $Tag for Windows..."
    Invoke-WebRequest -Uri $url -OutFile $archive
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "Checksum mismatch for $assetName. Expected $expected, received $actual."
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded
    $payload = Get-ChildItem -LiteralPath $expanded -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "gam.exe") } |
        Select-Object -First 1
    if (-not $payload) {
        throw "The verified archive did not contain gam.exe."
    }
    Copy-Item -Path (Join-Path $payload.FullName "*") -Destination $stage -Recurse -Force
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $stage "VERSION"),
        "$Tag`n",
        $utf8NoBom
    )
    Set-Content -LiteralPath (Join-Path $stage "SHA256") -Value "$actual  $assetName" -Encoding ascii
    $catalog = Join-Path $destination "command_catalog.json"
    if (Test-Path -LiteralPath $catalog) {
        Copy-Item -LiteralPath $catalog -Destination (Join-Path $stage "command_catalog.json")
    }

    $resolvedParent = (Resolve-Path $destinationParent).Path
    if (-not ((Resolve-Path $stage).Path).StartsWith($resolvedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe GAM staging path."
    }
    if (Test-Path -LiteralPath $destination) {
        Move-Item -LiteralPath $destination -Destination $backup
    }
    Move-Item -LiteralPath $stage -Destination $destination
    if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Recurse -Force
    }
    Write-Host "==> Vendored GAM $Tag for Windows."
}
catch {
    if ((Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $destination)) {
        Move-Item -LiteralPath $backup -Destination $destination
    }
    throw
}
finally {
    foreach ($candidate in @($stage, $backup, $taskRoot)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $resolved = (Resolve-Path $candidate).Path
        $safeTemp = $resolved.StartsWith(([System.IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase)
        $safeRepo = $resolved.StartsWith($destinationParent, [StringComparison]::OrdinalIgnoreCase) -and
            ((Split-Path -Leaf $resolved) -like ".gam7-*")
        if ($safeTemp -or $safeRepo) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}
