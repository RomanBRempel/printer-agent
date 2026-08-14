"""Well-known on-disk locations shared by the service, CLI and desktop app."""

from __future__ import annotations

import os
from pathlib import Path


def program_data_dir() -> Path:
    program_data = os.environ.get("PROGRAMDATA")
    base = Path(program_data) if program_data else Path.home()
    return base / "printer-agent"


def default_config_path() -> Path:
    return program_data_dir() / "agent.yaml"


def log_dir() -> Path:
    return program_data_dir() / "logs"


def agent_log_path() -> Path:
    return log_dir() / "agent.log"
