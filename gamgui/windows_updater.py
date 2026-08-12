"""Standalone Windows activation helper entry point."""

from __future__ import annotations

import sys


def main() -> int:
    if "--apply-update-helper" not in sys.argv[1:]:
        return 2
    from .app import main as app_main

    return app_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
