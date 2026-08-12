from __future__ import annotations

import os
from pathlib import Path

from gamgui.core.windows_acl import _dacl_is_current_user_only, _dacl_is_protected


def assert_current_user_only_acl(path: Path, *, directory: bool = False) -> None:
    assert os.name == "nt"
    assert _dacl_is_protected(path) is True
    assert _dacl_is_current_user_only(path, directory=directory) is True
