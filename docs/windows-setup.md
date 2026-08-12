# Windows setup, trust, and recovery

GamGUI's first Windows release is one offline `GamGUI-Setup-0.0.1-windows-x86_64.exe` for Windows
11 x64. It contains both fixed profiles, the pinned GAM 7.47.02 payload, the updater helper, the
checksum-pinned local-build toolchain, licenses, and packaged self-test receipts. Classroom +
OneRoster is selected by default; Core is also available.

## Before running Setup

The GitHub prerelease installer is intentionally **unsigned** because the project has no public
Authenticode certificate. GitHub hosting does not remove Microsoft SmartScreen warnings, and each
unsigned build develops reputation independently. Download the `.exe` and its `.sha256` receipt
from the same `v0.0.1-windows.1` prerelease, then compare them before bypassing SmartScreen:

```powershell
$expected = (Get-Content .\GamGUI-Setup-0.0.1-windows-x86_64.exe.sha256).Split()[0]
$actual = (Get-FileHash .\GamGUI-Setup-0.0.1-windows-x86_64.exe -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "GamGUI Setup checksum mismatch" }
```

If the checksum matches, use **More info > Run anyway**. Do not continue when the checksum differs,
the filename differs, or the file came from somewhere other than the project GitHub release.

## Interactive first install

Setup uses standard Windows controls and installs only for the current user. It does not need
administrator elevation and does not offer custom locations. The wizard:

1. Checks for Windows 11 x64, enough disk space, and an existing GamGUI installation.
2. Lets the user keep the recommended Classroom + OneRoster profile or choose Core.
3. Shows the fixed application and data locations and the exact source SHA.
4. Explains the local signing identity and requires an unchecked consent box before **Trust and
   install** is available.
5. Checks the embedded package, creates the local identity, signs local files, verifies them, runs
   the offline self-test, and finishes.
6. Creates a Start Menu entry, optionally creates an unchecked desktop shortcut, and offers to
   launch GamGUI.

Setup does not contact Google, GAM, Keychain, or tenant services and performs no tenant reads or
mutations. Its fixed locations are:

- Application: `%LOCALAPPDATA%\Programs\GamGUI\current`
- Data, updater state, and recovery journal: `%LOCALAPPDATA%\GamGUI`
- Detached updater helper: `%LOCALAPPDATA%\GamGUI\updater\GamGUIUpdater.exe`

The consented first install creates a ten-year RSA-3072 `GamGUI Local` code-signing identity in the
current user's Personal certificate store. Its private key is non-exportable. Setup adds only the
public certificate to that user's Trusted Root and Trusted Publisher stores. The certificate's
SHA-256 identity is pinned in updater state; GamGUI never rotates or replaces it silently.

## Existing installs and interrupted setup

Setup never overwrites `%LOCALAPPDATA%\Programs\GamGUI\current`. When it finds an installation, it
shows the profile and abbreviated source SHA and offers to open GamGUI, start uninstall, or close.
Normal application updates continue through the exact-SHA `update-ready` channel.

First install records a durable journal at
`%LOCALAPPDATA%\GamGUI\updates\bootstrap-install.json`. A failed or interrupted run leaves no
partially activated candidate. The next run validates the journal and reconciles the staged app,
helper, signing support, trust additions, and shortcuts before proceeding. Package hash drift,
profile/SHA mismatch, signer collision, missing trust, self-test failure, or insufficient disk fails
closed; an existing working installation is never replaced.

## Uninstall

GamGUI appears in Windows Apps & Features. Uninstall removes the application, updater helper,
local-build toolchain, shortcuts, installer registration, and the pinned local certificate. Local
application data is preserved by default. Select the unchecked **Also delete local application
data** option only when the data should be permanently removed.

After uninstall, a later clean interactive install may create a new local signer after the user
approves the trust step. An existing signer is never silently rotated.

## Managed and silent installation

An interactive install is the normal first-install path. Silent setup is fail-closed and is allowed
only when IT has already provisioned a matching, private-key-backed `GamGUI Local` code-signing
certificate in the current user's Personal store and its public certificate in that user's Trusted
Root and Trusted Publisher stores:

```powershell
.\GamGUI-Setup-0.0.1-windows-x86_64.exe /VERYSILENT `
  /PROFILE=classroom-oneroster `
  /PINNEDSIGNERSHA256=<64-hex-certificate-sha256>
```

Both `/PROFILE=core|classroom-oneroster` and the 64-hex signer SHA-256 are mandatory. Silent setup
never creates, trusts, rotates, or replaces a certificate. A missing key, missing trust record,
changed signer, or invalid argument rejects installation. GPO or another software-distribution
system may distribute the exact GitHub asset and command, but it must separately establish the
current-user certificate prerequisites through district-approved PKI or enrollment policy.

## Release boundary

The `v0.0.1-windows.1` prerelease workflow accepts only a tag whose commit equals both protected
`district-main` and `update-ready`. It runs Windows Python 3.10, 3.12, and 3.14 tests, builds and
self-tests both profiles, exercises the installer and uninstaller on a disposable runner user,
verifies the documented `NotSigned` Setup status and asset checksums, and creates GitHub build
provenance. A maintainer must approve the protected `windows-prerelease` environment before the
public prerelease is created.

The later stable macOS `v0.0.1` release must use that same validated source SHA. If application
changes are needed first, both platforms advance to a new version so one version never names two
different source revisions. Future stable versions should publish Windows and macOS assets
together. Downloaded release assets must be rechecked against their hosted checksum and installed
SHA; a successful GitHub workflow does not by itself prove a particular computer updated.
