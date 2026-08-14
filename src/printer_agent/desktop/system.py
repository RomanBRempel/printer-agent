"""Windows integration used by the desktop app: paths, service control, shell.

Everything here degrades gracefully on non-Windows so the module stays
importable in tests and on developer machines.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..paths import agent_log_path, default_config_path, log_dir, program_data_dir

__all__ = [
    "SERVICE_NAME",
    "STATE_LABELS",
    "ServiceInfo",
    "agent_log_path",
    "control_service",
    "copy_elevated",
    "default_config_path",
    "is_admin",
    "list_log_files",
    "log_dir",
    "program_data_dir",
    "query_service",
    "reveal",
    "tail_text",
]

SERVICE_NAME = "printer-agent"

#: Service states as reported by the Windows SCM.
_STATE_NAMES: dict[int, str] = {
    1: "stopped",
    2: "start_pending",
    3: "stop_pending",
    4: "running",
    5: "continue_pending",
    6: "pause_pending",
    7: "paused",
}

STATE_LABELS: dict[str, str] = {
    "running": "Работает",
    "stopped": "Остановлена",
    "start_pending": "Запускается",
    "stop_pending": "Останавливается",
    "continue_pending": "Возобновляется",
    "pause_pending": "Приостанавливается",
    "paused": "Приостановлена",
    "not_installed": "Не установлена",
    "unknown": "Неизвестно",
}


def _no_window_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


# --------------------------------------------------------------------------- #
# paths (canonical definitions live in printer_agent.paths)
# --------------------------------------------------------------------------- #

def list_log_files() -> list[Path]:
    directory = log_dir()
    if not directory.is_dir():
        return []
    files = [item for item in directory.iterdir() if item.is_file() and item.suffix in {".log", ".txt"}]
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)


def tail_text(path: Path, max_bytes: int = 256_000) -> str:
    """Read the last ``max_bytes`` of a log file without loading the whole thing."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # drop the partial first line
            data = handle.read()
    except OSError as exc:
        return f"Не удалось прочитать {path}: {exc}"
    return data.decode("utf-8", errors="replace")


def reveal(path: Path) -> None:
    """Open a file or folder in Explorer / the platform file manager."""
    target = path if path.exists() else path.parent
    try:
        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606 - user-initiated shell open
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# elevation
# --------------------------------------------------------------------------- #

def is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# service control
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class ServiceInfo:
    state: str = "unknown"
    detail: str = ""

    @property
    def installed(self) -> bool:
        return self.state not in {"not_installed", "unknown"}

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, STATE_LABELS["unknown"])


def query_service(name: str = SERVICE_NAME) -> ServiceInfo:
    if os.name != "nt":
        return ServiceInfo(state="not_installed", detail="Служба доступна только в Windows.")

    try:  # pywin32 is present wherever the service itself is installed
        import win32service
        import win32serviceutil

        status = win32serviceutil.QueryServiceStatus(name)
        return ServiceInfo(state=_STATE_NAMES.get(int(status[1]), "unknown"))
    except ImportError:
        pass
    except Exception as exc:
        if "1060" in str(exc):  # ERROR_SERVICE_DOES_NOT_EXIST
            return ServiceInfo(state="not_installed")
        return ServiceInfo(state="unknown", detail=str(exc))

    # Fallback: Get-Service returns an English .NET enum name, so it survives
    # localized Windows installs the way parsing sc.exe output would not.
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-Service -Name '{name}' -ErrorAction Stop).Status.ToString()",
        ],
        capture_output=True,
        text=True,
        **_no_window_kwargs(),
    )
    if completed.returncode != 0:
        return ServiceInfo(state="not_installed", detail=completed.stderr.strip())
    value = completed.stdout.strip().lower()
    mapping = {
        "running": "running",
        "stopped": "stopped",
        "startpending": "start_pending",
        "stoppending": "stop_pending",
        "paused": "paused",
        "pausepending": "pause_pending",
        "continuepending": "continue_pending",
    }
    return ServiceInfo(state=mapping.get(value, "unknown"))


def control_service(action: str, name: str = SERVICE_NAME) -> tuple[bool, str]:
    """Start/stop/restart the service, elevating through UAC when needed."""
    if action not in {"start", "stop", "restart"}:
        raise ValueError(f"unsupported service action: {action}")
    if os.name != "nt":
        return False, "Управление службой доступно только в Windows."

    command = {
        "start": f"Start-Service -Name '{name}'",
        "stop": f"Stop-Service -Name '{name}' -Force",
        "restart": f"Restart-Service -Name '{name}' -Force",
    }[action]

    if is_admin():
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            **_no_window_kwargs(),
        )
        if completed.returncode == 0:
            return True, ""
        return False, completed.stderr.strip() or completed.stdout.strip() or "Команда завершилась с ошибкой."

    return _elevate(command)


def copy_elevated(source: Path, destination: Path) -> tuple[bool, str]:
    """Copy a file into an admin-only location through a UAC prompt.

    Used when the config lives under ProgramData and the desktop app runs
    unelevated, which is the normal case.
    """
    if os.name != "nt":
        return False, "Повышение прав доступно только в Windows."
    command = (
        f"New-Item -ItemType Directory -Force -Path '{destination.parent}' | Out-Null; "
        f"Copy-Item -LiteralPath '{source}' -Destination '{destination}' -Force"
    )
    return _elevate(command)


def _elevate(command: str) -> tuple[bool, str]:
    try:
        import ctypes

        arguments = f'-NoProfile -NonInteractive -WindowStyle Hidden -Command "{command}"'
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell", arguments, None, 0)
        if int(result) <= 32:
            if int(result) == 1223:  # ERROR_CANCELLED
                return False, "Запрос прав администратора отклонён."
            return False, "Не удалось запросить права администратора."
        return True, ""
    except Exception as exc:
        return False, str(exc)
