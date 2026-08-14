"""Backwards-compatible entry point for the desktop app.

Shortcuts created by earlier installers invoke ``python -m printer_agent.gui``.
The real UI now lives in :mod:`printer_agent.desktop`; this module keeps that
older command working.
"""

from __future__ import annotations

from typing import Sequence

from .desktop import build_parser, main
from .paths import default_config_path

__all__ = ["build_parser", "default_config_path", "main"]


def run(argv: Sequence[str] | None = None) -> int:
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
