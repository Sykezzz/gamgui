"""Owner-only filesystem protection that uses real Windows DACLs."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERITED_ACE = 0x10
_FILE_ALL_ACCESS = 0x001F01FF


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("ace_count", ctypes.c_uint32),
        ("acl_bytes_in_use", ctypes.c_uint32),
        ("acl_bytes_free", ctypes.c_uint32),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("ace_type", ctypes.c_ubyte),
        ("ace_flags", ctypes.c_ubyte),
        ("ace_size", ctypes.c_uint16),
    ]


def restrict_owner_only(path: Path, *, directory: bool | None = None) -> None:
    """Apply 0700/0600 on POSIX or a protected current-user DACL on Windows."""

    target = Path(path)
    is_directory = target.is_dir() if directory is None else bool(directory)
    if sys.platform != "win32":
        os.chmod(target, 0o700 if is_directory else 0o600)
        return
    _set_current_user_dacl(target, directory=is_directory)


def _set_current_user_dacl(path: Path, *, directory: bool) -> None:
    if not path.exists() or path.is_symlink():
        raise OSError("Owner-only ACL target must exist and cannot be a symlink.")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    token = ctypes.c_void_p()
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.OpenProcessToken.restype = ctypes.c_int
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = ctypes.c_uint32()
        advapi32.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        advapi32.GetTokenInformation.restype = ctypes.c_int
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            size.value,
            ctypes.byref(size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.c_void_p.from_buffer(buffer).value
        sid_text = ctypes.c_wchar_p()
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            inheritance = "OICI" if directory else ""
            sddl = f"D:P(A;{inheritance};FA;;;{sid_text.value})"
        finally:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle(token)

    descriptor = ctypes.c_void_p()
    descriptor_size = ctypes.c_uint32()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl_present = ctypes.c_int()
        dacl_defaulted = ctypes.c_int()
        dacl = ctypes.c_void_p()
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = ctypes.c_int
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value:
            raise ctypes.WinError(ctypes.get_last_error())
        advapi32.SetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        advapi32.SetNamedSecurityInfoW.restype = ctypes.c_uint32
        security_information = (
            _DACL_SECURITY_INFORMATION
            | _PROTECTED_DACL_SECURITY_INFORMATION
        )
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            1,
            security_information,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise ctypes.WinError(result)
        if not _dacl_is_protected(path) or not _dacl_is_current_user_only(
            path,
            directory=directory,
        ):
            advapi32.SetSecurityDescriptorControl.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint16,
                ctypes.c_uint16,
            ]
            advapi32.SetSecurityDescriptorControl.restype = ctypes.c_int
            if not advapi32.SetSecurityDescriptorControl(
                descriptor,
                _SE_DACL_PROTECTED,
                _SE_DACL_PROTECTED,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            advapi32.SetFileSecurityW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            advapi32.SetFileSecurityW.restype = ctypes.c_int
            if not advapi32.SetFileSecurityW(
                str(path),
                security_information,
                descriptor,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        if not _dacl_is_protected(path) or not _dacl_is_current_user_only(
            path,
            directory=directory,
        ):
            raise OSError("Windows refused to apply a protected owner-only DACL.")
    finally:
        kernel32.LocalFree(descriptor)


def _dacl_is_protected(path: Path) -> bool:
    """Read the applied descriptor and confirm inheritance is disabled."""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    advapi32.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        control = ctypes.c_uint16()
        revision = ctypes.c_uint32()
        advapi32.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        advapi32.GetSecurityDescriptorControl.restype = ctypes.c_int
        if not advapi32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(control.value & _SE_DACL_PROTECTED)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(descriptor)


def _dacl_is_current_user_only(path: Path, *, directory: bool) -> bool:
    """Confirm the persisted DACL has one explicit current-user Full Control ACE."""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    advapi32.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)

    token = ctypes.c_void_p()
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.OpenProcessToken.restype = ctypes.c_int
    try:
        if not dacl.value:
            return False
        size_information = _AclSizeInformation()
        advapi32.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        advapi32.GetAclInformation.restype = ctypes.c_int
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(size_information),
            ctypes.sizeof(size_information),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if size_information.ace_count != 1:
            return False

        ace = ctypes.c_void_p()
        advapi32.GetAce.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetAce.restype = ctypes.c_int
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not ace.value:
            return False
        header = _AceHeader.from_address(ace.value)
        required_flags = (
            _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE if directory else 0
        )
        if (
            header.ace_type != _ACCESS_ALLOWED_ACE_TYPE
            or header.ace_flags & _INHERITED_ACE
            or header.ace_flags & (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
            != required_flags
            or ctypes.c_uint32.from_address(ace.value + 4).value != _FILE_ALL_ACCESS
        ):
            return False

        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            0x0008,
            ctypes.byref(token),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_size = ctypes.c_uint32()
        advapi32.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        advapi32.GetTokenInformation.restype = ctypes.c_int
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(token_size))
        if not token_size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        token_buffer = ctypes.create_string_buffer(token_size.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            token_buffer,
            token_size.value,
            ctypes.byref(token_size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        current_sid = ctypes.c_void_p.from_buffer(token_buffer).value
        ace_sid = ctypes.c_void_p(ace.value + 8)
        advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        advapi32.EqualSid.restype = ctypes.c_int
        return bool(advapi32.EqualSid(current_sid, ace_sid))
    finally:
        if token.value:
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(token)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(descriptor)
