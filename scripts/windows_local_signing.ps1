[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Enroll", "Inspect", "Sign", "Verify", "SignFile", "VerifyFile", "Remove", "RemoveTrust")]
    [string]$Action,
    [string]$Path = "",
    [string]$CertificateSha256 = "",
    [switch]$TrustLocalCertificate,
    [switch]$CiEphemeralCertificate
)

$ErrorActionPreference = "Stop"
$subject = "CN=GamGUI Local"
$codeSigningOid = "1.3.6.1.5.5.7.3.3"

function Get-CertificateSha256([System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate) {
    $bytes = $Certificate.GetCertHash([System.Security.Cryptography.HashAlgorithmName]::SHA256)
    return ([System.BitConverter]::ToString($bytes) -replace "-", "").ToLowerInvariant()
}

function Get-LocalCertificates([string]$StoreName) {
    return @(Get-ChildItem -LiteralPath "Cert:\CurrentUser\$StoreName" | Where-Object { $_.Subject -eq $subject })
}

function Find-LocalCertificate([bool]$RequirePrivateKey) {
    $matches = @(Get-LocalCertificates "My" | Where-Object {
        (Get-CertificateSha256 $_) -eq $CertificateSha256.ToLowerInvariant()
    })
    if ($matches.Count -ne 1) { throw "The pinned GamGUI Local certificate was not found exactly once." }
    $certificate = $matches[0]
    if ($RequirePrivateKey -and -not $certificate.HasPrivateKey) { throw "The pinned GamGUI Local private key is unavailable." }
    $eku = @($certificate.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId.Value })
    if ($eku -notcontains $codeSigningOid) { throw "The pinned GamGUI Local certificate is not a code-signing identity." }
    if ($certificate.NotAfter -le (Get-Date)) { throw "The pinned GamGUI Local certificate has expired." }
    return $certificate
}

function Test-Trusted([System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate) {
    $sha = Get-CertificateSha256 $Certificate
    foreach ($store in @("Root", "TrustedPublisher")) {
        $match = @(Get-LocalCertificates $store | Where-Object { (Get-CertificateSha256 $_) -eq $sha })
        if ($match.Count -ne 1) { return $false }
    }
    return $true
}

function Add-Trust([System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate) {
    $temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("gamgui-local-" + [guid]::NewGuid() + ".cer")
    try {
        [System.IO.File]::WriteAllBytes($temporary, $Certificate.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
        Import-Certificate -FilePath $temporary -CertStoreLocation "Cert:\CurrentUser\Root" | Out-Null
        Import-Certificate -FilePath $temporary -CertStoreLocation "Cert:\CurrentUser\TrustedPublisher" | Out-Null
    } finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Get-SignableFiles([System.IO.DirectoryInfo]$Root) {
    return @(Get-ChildItem -LiteralPath $Root.FullName -Recurse -File | Where-Object {
        $_.Extension.ToLowerInvariant() -in @(".exe", ".dll", ".pyd", ".ps1")
    } | Sort-Object FullName)
}

function Write-DetachedManifest([System.IO.DirectoryInfo]$Root, [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate) {
    $records = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $Root.FullName -Recurse -File | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($Root.FullName.Length).TrimStart("\").Replace("\", "/")
        if ($relative -in @("bundle-manifest.json", "bundle-manifest.p7s")) { continue }
        $records += [ordered]@{
            path = $relative
            size = [long]$file.Length
            sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    $payload = [ordered]@{
        format = "gamgui-windows-bundle-v1"
        signer_sha256 = Get-CertificateSha256 $Certificate
        files = $records
    }
    $manifestPath = Join-Path $Root.FullName "bundle-manifest.json"
    $signaturePath = Join-Path $Root.FullName "bundle-manifest.p7s"
    $json = ($payload | ConvertTo-Json -Depth 6 -Compress) + "`n"
    [System.IO.File]::WriteAllText($manifestPath, $json, [System.Text.UTF8Encoding]::new($false))
    Add-Type -AssemblyName System.Security
    $content = [System.IO.File]::ReadAllBytes($manifestPath)
    $contentInfo = New-Object System.Security.Cryptography.Pkcs.ContentInfo -ArgumentList (,$content)
    $signedCms = New-Object System.Security.Cryptography.Pkcs.SignedCms -ArgumentList $contentInfo, $true
    $signer = New-Object System.Security.Cryptography.Pkcs.CmsSigner -ArgumentList $Certificate
    $signer.IncludeOption = [System.Security.Cryptography.X509Certificates.X509IncludeOption]::EndCertOnly
    $signedCms.ComputeSignature($signer)
    [System.IO.File]::WriteAllBytes($signaturePath, $signedCms.Encode())
}

function Assert-DetachedManifest([System.IO.DirectoryInfo]$Root, [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate) {
    $manifestPath = Join-Path $Root.FullName "bundle-manifest.json"
    $signaturePath = Join-Path $Root.FullName "bundle-manifest.p7s"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or -not (Test-Path -LiteralPath $signaturePath -PathType Leaf)) {
        throw "The detached Windows bundle manifest is missing."
    }
    $payload = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($payload.format -ne "gamgui-windows-bundle-v1" -or $payload.signer_sha256 -ne (Get-CertificateSha256 $Certificate)) {
        throw "The detached Windows bundle manifest identity changed."
    }
    $actual = @{}
    foreach ($file in @(Get-ChildItem -LiteralPath $Root.FullName -Recurse -File)) {
        $relative = $file.FullName.Substring($Root.FullName.Length).TrimStart("\").Replace("\", "/")
        if ($relative -in @("bundle-manifest.json", "bundle-manifest.p7s")) { continue }
        $actual[$relative] = $file
    }
    if (@($payload.files).Count -ne $actual.Count) { throw "The Windows bundle file membership changed." }
    foreach ($record in @($payload.files)) {
        if (-not $actual.ContainsKey([string]$record.path)) { throw "The Windows bundle contains an unrecorded file change." }
        $file = $actual[[string]$record.path]
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne [string]$record.sha256 -or [long]$file.Length -ne [long]$record.size) {
            throw "The Windows bundle file hash changed: $($record.path)"
        }
    }
    Add-Type -AssemblyName System.Security
    $content = [System.IO.File]::ReadAllBytes($manifestPath)
    $contentInfo = New-Object System.Security.Cryptography.Pkcs.ContentInfo -ArgumentList (,$content)
    $signedCms = New-Object System.Security.Cryptography.Pkcs.SignedCms -ArgumentList $contentInfo, $true
    $signedCms.Decode([System.IO.File]::ReadAllBytes($signaturePath))
    $signedCms.CheckSignature($true)
    if ($signedCms.SignerInfos.Count -ne 1 -or (Get-CertificateSha256 $signedCms.SignerInfos[0].Certificate) -ne (Get-CertificateSha256 $Certificate)) {
        throw "The detached Windows bundle signature changed identity."
    }
}

if ($Action -eq "Enroll") {
    $existing = @(Get-LocalCertificates "My")
    if ($existing.Count -gt 1) { throw "More than one GamGUI Local certificate exists; refusing silent rotation." }
    $created = $existing.Count -eq 0
    if ($created) {
        $certificate = New-SelfSignedCertificate `
            -Subject $subject `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -KeyAlgorithm RSA `
            -KeyLength 3072 `
            -HashAlgorithm SHA256 `
            -KeyExportPolicy NonExportable `
            -KeyUsage DigitalSignature `
            -Type Custom `
            -TextExtension @("2.5.29.37={text}$codeSigningOid") `
            -NotAfter (Get-Date).AddYears(10)
    } else {
        $certificate = $existing[0]
    }
    $CertificateSha256 = Get-CertificateSha256 $certificate
    if ($TrustLocalCertificate) { Add-Trust $certificate }
    [ordered]@{
        certificate_sha256 = $CertificateSha256
        store_thumbprint = $certificate.Thumbprint.ToLowerInvariant()
        trust_required = -not (Test-Trusted $certificate)
        created = $created
        ci_ephemeral = [bool]$CiEphemeralCertificate
    } | ConvertTo-Json -Compress
    exit 0
}

if (-not ($CertificateSha256 -match '^[0-9a-fA-F]{64}$')) { throw "A pinned certificate SHA-256 is required." }
if ($Action -eq "Remove") {
    foreach ($store in @("My", "Root", "TrustedPublisher")) {
        foreach ($match in @(Get-LocalCertificates $store | Where-Object { (Get-CertificateSha256 $_) -eq $CertificateSha256.ToLowerInvariant() })) {
            Remove-Item -LiteralPath $match.PSPath -Force
        }
    }
    exit 0
}
if ($Action -eq "RemoveTrust") {
    foreach ($store in @("Root", "TrustedPublisher")) {
        foreach ($match in @(Get-LocalCertificates $store | Where-Object { (Get-CertificateSha256 $_) -eq $CertificateSha256.ToLowerInvariant() })) {
            Remove-Item -LiteralPath $match.PSPath -Force
        }
    }
    exit 0
}
$certificate = Find-LocalCertificate ($Action -in @("Inspect", "Sign", "SignFile"))

if ($Action -eq "Inspect") {
    if (-not (Test-Trusted $certificate)) { throw "The pinned GamGUI Local certificate is not trusted for this user." }
    [ordered]@{ certificate_sha256 = Get-CertificateSha256 $certificate; store_thumbprint = $certificate.Thumbprint.ToLowerInvariant(); trusted = $true } | ConvertTo-Json -Compress
    exit 0
}

if (-not $Path) { throw "A Windows application bundle path is required." }
$target = Get-Item -LiteralPath $Path
if ($Action -eq "SignFile") {
    if ($target.PSIsContainer) { throw "The standalone updater helper path is not a file." }
    if (-not (Test-Trusted $certificate)) { throw "The pinned GamGUI Local certificate is not trusted for this user." }
    $result = Set-AuthenticodeSignature -LiteralPath $target.FullName -Certificate $certificate -HashAlgorithm SHA256
    if ($result.Status -ne "Valid") { throw "Authenticode signing failed for the updater helper: $($result.Status)" }
    exit 0
}
if ($Action -eq "VerifyFile") {
    if ($target.PSIsContainer -or -not (Test-Trusted $certificate)) { throw "The standalone updater helper trust check failed." }
    $signature = Get-AuthenticodeSignature -LiteralPath $target.FullName
    if ($signature.Status -ne "Valid" -or $null -eq $signature.SignerCertificate -or (Get-CertificateSha256 $signature.SignerCertificate) -ne $CertificateSha256.ToLowerInvariant()) {
        throw "The standalone updater helper is unsigned or changed."
    }
    exit 0
}
$root = $target
if (-not $root.PSIsContainer -or -not (Test-Path -LiteralPath (Join-Path $root.FullName "GamGUI.exe") -PathType Leaf)) {
    throw "The Windows application bundle is incomplete."
}

if ($Action -eq "Sign") {
    if (-not (Test-Trusted $certificate)) { throw "The pinned GamGUI Local certificate is not trusted for this user." }
    foreach ($file in Get-SignableFiles $root) {
        $result = Set-AuthenticodeSignature -LiteralPath $file.FullName -Certificate $certificate -HashAlgorithm SHA256
        if ($result.Status -ne "Valid") { throw "Authenticode signing failed for $($file.Name): $($result.Status)" }
    }
    Write-DetachedManifest $root $certificate
    exit 0
}

if (-not (Test-Trusted $certificate)) { throw "The pinned GamGUI Local certificate is not trusted for this user." }
foreach ($file in Get-SignableFiles $root) {
    $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
    if ($signature.Status -ne "Valid" -or $null -eq $signature.SignerCertificate -or (Get-CertificateSha256 $signature.SignerCertificate) -ne $CertificateSha256.ToLowerInvariant()) {
        throw "A Windows executable file is unsigned or changed: $($file.Name)"
    }
}
Assert-DetachedManifest $root $certificate
