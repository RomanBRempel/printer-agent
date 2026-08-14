from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def main() -> int:
    script_path = _base_dir() / "install.ps1"
    if not script_path.exists():
        print(f"Installer script not found: {script_path}", file=sys.stderr)
        return 2

    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        *sys.argv[1:],
    ]
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
