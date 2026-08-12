from __future__ import annotations

import os
import shutil
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures"
if os.name == "nt":
    _shell = shutil.which("sh")
    if not _shell:
        raise RuntimeError("Git for Windows sh.exe is required for the offline GAM fixture.")
    MOCK_GAM = Path(_shell)
    MOCK_GAM_COMMAND_PREFIX = (str(MOCK_GAM), str(FIXTURES / "mock_gam.sh"))
else:
    MOCK_GAM = FIXTURES / "mock_gam.sh"
    MOCK_GAM_COMMAND_PREFIX = (str(MOCK_GAM),)
