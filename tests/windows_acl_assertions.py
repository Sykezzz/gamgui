from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from gamgui.core.windows_acl import _dacl_is_protected


def assert_current_user_only_acl(path: Path, *, directory: bool = False) -> None:
    script = r"""
$acl = Get-Acl -LiteralPath $env:GAMGUI_ACL_TEST_PATH
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = @($acl.Access | ForEach-Object {
    [ordered]@{
        sid = $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
        inherited = [bool]$_.IsInherited
        type = [string]$_.AccessControlType
        rights = [int]$_.FileSystemRights
        inheritance = [int]$_.InheritanceFlags
    }
})
[ordered]@{ protected = [bool]$acl.AreAccessRulesProtected; current = $current; rules = $rules } | ConvertTo-Json -Compress -Depth 5
"""
    environment = os.environ.copy()
    environment["GAMGUI_ACL_TEST_PATH"] = str(path)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    receipt = json.loads(completed.stdout)
    rules = receipt["rules"]
    if isinstance(rules, dict):
        rules = [rules]
    # Query the persisted descriptor control through the same native Windows API
    # that defines SE_DACL_PROTECTED.  PowerShell's managed FileSecurity wrapper
    # reports AreAccessRulesProtected=False on GitHub's D: runner even when the
    # underlying descriptor has SE_DACL_PROTECTED set; the native flag and the
    # enumerated ACEs are the security properties this test needs to prove.
    assert _dacl_is_protected(path) is True
    assert len(rules) == 1
    assert rules[0]["sid"] == receipt["current"]
    assert rules[0]["inherited"] is False
    assert rules[0]["type"] == "Allow"
    assert rules[0]["rights"] & 0x1F01FF == 0x1F01FF
    if directory:
        assert rules[0]["inheritance"] & 3 == 3
