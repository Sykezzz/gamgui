[CmdletBinding()]
param(
    [string]$Tag = "v7.47.06"
)

$ErrorActionPreference = "Stop"
$tagMatch = [regex]::Match($Tag, '^v(?<version>\d+\.\d+\.\d+)$')
if (-not $tagMatch.Success) {
    throw "Tag must be an exact GAM semantic release such as v7.47.06."
}
$requestedVersion = $tagMatch.Groups["version"].Value
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$destination = Join-Path $repoRoot "gamgui\resources\gam7"
$destinationParent = Split-Path -Parent $destination
$assetName = "gam-$requestedVersion-windows-x86_64.zip"
$checksumFile = Join-Path $repoRoot "scripts\gam_checksums.txt"
$checksumLines = @(
    Get-Content -LiteralPath $checksumFile |
        Where-Object { $_ -cmatch "^[0-9a-f]{64}\s+$([regex]::Escape($assetName))$" }
)
if ($checksumLines.Count -eq 0) {
    throw "No committed checksum exists for $assetName."
}
if ($checksumLines.Count -ne 1) {
    throw "Expected exactly one committed checksum for $assetName; found $($checksumLines.Count)."
}
$expected = ($checksumLines[0] -split "\s+")[0].ToLowerInvariant()

$taskRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-gam-windows-" + [guid]::NewGuid())
$archive = Join-Path $taskRoot $assetName
$cacheRoot = Join-Path ([System.IO.Path]::GetTempPath()) "gamgui-gam-download-cache"
$cachedArchive = Join-Path $cacheRoot $assetName
$expanded = Join-Path $taskRoot "expanded"
$stage = Join-Path $destinationParent (".gam7-stage-" + [guid]::NewGuid())
$backup = Join-Path $destinationParent (".gam7-backup-" + [guid]::NewGuid())
$cacheVerified = $false

try {
    New-Item -ItemType Directory -Path $taskRoot, $expanded, $stage, $cacheRoot -Force | Out-Null
    $url = "https://github.com/GAM-team/GAM/releases/download/$Tag/$assetName"
    $cachedHash = if (Test-Path -LiteralPath $cachedArchive -PathType Leaf) {
        (Get-FileHash -LiteralPath $cachedArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { "" }
    if ($cachedHash -ne $expected) {
        Remove-Item -LiteralPath $cachedArchive -Force -ErrorAction SilentlyContinue
        Write-Host "==> Downloading pinned GAM $Tag for Windows..."
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $download = "$cachedArchive.download"
            Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
            try {
                Invoke-WebRequest -Uri $url -OutFile $download -UseBasicParsing
                $downloadHash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($downloadHash -ne $expected) { throw "Downloaded GAM archive failed its committed checksum." }
                Move-Item -LiteralPath $download -Destination $cachedArchive -Force
                break
            } catch {
                Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
                if ($attempt -eq 3) { throw }
                Start-Sleep -Seconds (2 * $attempt)
            }
        }
    } else {
        Write-Host "==> Reusing checksum-verified pinned GAM $Tag download."
    }
    Copy-Item -LiteralPath $cachedArchive -Destination $archive
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "Checksum mismatch for $assetName. Expected $expected, received $actual."
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded
    $payloads = @(
        Get-ChildItem -LiteralPath $expanded -Directory |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "gam.exe") -PathType Leaf }
    )
    if ($payloads.Count -ne 1) {
        throw "The verified archive must contain exactly one GAM Windows payload with gam.exe; found $($payloads.Count)."
    }
    Copy-Item -Path (Join-Path $payloads[0].FullName "*") -Destination $stage -Recurse -Force

    # Verify the staged executable by explicit path before the destination is touched.
    # An isolated config directory prevents a local GAM profile or PATH entry from
    # influencing the probe. The first call initializes that empty directory; the
    # second must emit only the exact requested version.
    $stagedGam = Join-Path $stage "gam.exe"
    if (-not (Test-Path -LiteralPath $stagedGam -PathType Leaf)) {
        throw "The verified GAM Windows payload did not stage gam.exe."
    }
    $probeConfig = Join-Path $taskRoot "gam-config"
    New-Item -ItemType Directory -Path $probeConfig -Force | Out-Null
    $previousGamCfgDir = $env:GAMCFGDIR
    try {
        $env:GAMCFGDIR = $probeConfig
        & $stagedGam version simple *> $null
        if ($LASTEXITCODE -ne 0) { throw "The staged GAM executable version probe failed." }
        $reportedVersion = (& $stagedGam version simple 2>&1 | Out-String).Trim()
        $versionExitCode = $LASTEXITCODE
    } finally {
        if ($null -eq $previousGamCfgDir) {
            Remove-Item Env:GAMCFGDIR -ErrorAction SilentlyContinue
        } else {
            $env:GAMCFGDIR = $previousGamCfgDir
        }
    }
    if ($versionExitCode -ne 0 -or $reportedVersion -cne $requestedVersion) {
        throw "The staged GAM executable reported version '$reportedVersion'; expected '$requestedVersion'."
    }
    $cacheVerified = $true

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
    if (-not $cacheVerified -and (Test-Path -LiteralPath $cachedArchive -PathType Leaf)) {
        Remove-Item -LiteralPath $cachedArchive -Force -ErrorAction SilentlyContinue
    }
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
