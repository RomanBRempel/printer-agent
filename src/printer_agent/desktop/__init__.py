"""Native desktop app for printer-agent (PySide6, Fluent-styled)."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Sequence

__all__ = ["main", "run_app"]

ICON_NAME = "printer-agent.ico"


def resource_path(name: str) -> Path:
    """Resolve a bundled resource, both from source and from a PyInstaller bundle."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidate = Path(frozen_root) / name
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent / "resources" / name


def _crash_log_path() -> Path:
    from .system import log_dir

    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "desktop.log"


def _record_crash(text: str) -> Path | None:
    try:
        path = _crash_log_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} ===\n{text}\n")
        return path
    except Exception:
        return None


def _install_excepthook() -> None:
    """Under pythonw there is no stderr; an unhandled error would vanish."""
    from PySide6.QtWidgets import QMessageBox

    def handler(exc_type, exc_value, exc_traceback) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        path = _record_crash(text)
        try:
            box = QMessageBox(QMessageBox.Critical, "Printer Agent", str(exc_value) or exc_type.__name__)
            box.setInformativeText(f"Подробности записаны в {path}" if path else "")
            box.setDetailedText(text)
            box.exec()
        except Exception:
            pass

    sys.excepthook = handler


def build_parser() -> argparse.ArgumentParser:
    from .system import default_config_path

    parser = argparse.ArgumentParser(prog="printer-agent-gui", description="Printer Agent desktop app")
    parser.add_argument("--config", default=str(default_config_path()), help="Путь к agent.yaml")
    return parser


def run_app(config_path: str | Path | None = None) -> int:
    """Create the QApplication and show the main window."""
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow
    from .state import AppState
    from .system import default_config_path
    from .theme import qt_platform_hint

    platform_hint = qt_platform_hint()
    if platform_hint:
        os.environ.setdefault("QT_QPA_PLATFORM", platform_hint)

    if os.name == "nt":
        try:  # give the window its own taskbar identity and icon
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RDControl.PrinterAgent.Desktop")
        except Exception:
            pass

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Printer Agent")
    app.setOrganizationName("RD Control")
    _install_excepthook()

    icon_path = resource_path(ICON_NAME)
    icon = QIcon(str(icon_path)) if icon_path.exists() else None
    if icon is not None:
        app.setWindowIcon(icon)

    state = AppState(Path(config_path) if config_path else default_config_path())
    window = MainWindow(state, icon)
    window.show()
    return app.exec()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
        return run_app(args.config)
    except SystemExit:
        raise
    except ImportError as exc:
        _fail_on_missing_dependency(exc)
        return 3
    except Exception:
        text = traceback.format_exc()
        path = _record_crash(text)
        _fail_with_message(f"Printer Agent не смог запуститься.\n\nПодробности: {path}")
        return 1


QT_MODULES = {"PySide6", "shiboken6"}


def _fail_on_missing_dependency(exc: ImportError) -> None:
    """Name the actual missing package — a wrong diagnosis wastes an operator's time."""
    module = (getattr(exc, "name", "") or "").split(".")[0]
    if module in QT_MODULES or not module:
        headline = "Не установлен графический пакет PySide6."
    else:
        headline = f"Не установлена зависимость «{module}»."
    message = (
        f"{headline}\n\n"
        "Установите зависимости приложения командой:\n"
        '    pip install "printer-agent[gui]"\n\n'
        f"Исходная ошибка: {exc}"
    )
    _record_crash(message)
    _fail_with_message(message)


def _fail_with_message(message: str) -> None:
    """Last-resort user-visible error that works without a console and without Qt."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Printer Agent", 0x10)
            return
        except Exception:
            pass
    stream = sys.stderr or sys.stdout
    if stream is not None:
        print(message, file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
