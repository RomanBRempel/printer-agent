"""Windows installer UI for printer-agent.

Reuses the desktop app's Fluent theme and widgets, so the installer and the app
it installs read as one product instead of two.

The PowerShell transcript never reaches the window: it goes to a log file, and
the UI shows named steps. Console output in an installer tells an operator
nothing they can act on — it only makes a successful install look alarming.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if not getattr(sys, "frozen", False):  # running from a checkout, not a bundle
    _SRC = Path(__file__).resolve().parents[2] / "src"
    if _SRC.exists() and str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from printer_agent.desktop.theme import (
    Palette,
    ThemeMode,
    apply_window_backdrop,
    build_palette,
    build_stylesheet,
    qt_platform_hint,
)
from printer_agent.desktop.widgets import Card, InfoBar, ToggleSwitch, form_row

APP_TITLE = "Printer Agent"
DEFAULT_INSTALL_ROOT = r"C:\Program Files\printer-agent"
DEFAULT_FEED_URL = (
    "https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json"
)

#: Keys match the `[STEP] <key>` markers printed by install.ps1.
STEP_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("bootstrap", "Окружение", "Виртуальное окружение Python"),
    ("package", "Пакет", "Агент, поддержка службы, интерфейс"),
    ("config", "Конфигурация", "agent.yaml в ProgramData"),
    ("service", "Служба Windows", "Регистрация и запуск"),
    ("shortcuts", "Ярлыки", "Меню «Пуск» и рабочий стол"),
    ("finalize", "Проверка", "Финальный контроль"),
)

PENDING, ACTIVE, DONE, FAILED = "pending", "active", "done", "failed"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    args = " ".join(f'"{arg}"' for arg in sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1)


def resource_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) if frozen_root else Path(__file__).resolve().parent


def log_path() -> Path:
    base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "printer-agent" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"installer-{datetime.now():%Y%m%d-%H%M%S}.log"


# --------------------------------------------------------------------------- #
# step timeline
# --------------------------------------------------------------------------- #

class StepRow(QWidget):
    """One step of the install: painted state marker, caption, connector line."""

    def __init__(self, title: str, hint: str, *, last: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self._state = PENDING
        self._last = last
        self._angle = 0
        self._palette: Palette | None = None

        self._spin = QTimer(self)
        self._spin.setInterval(28)
        self._spin.timeout.connect(self._advance)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(38, 0, 0, 0)
        layout.setSpacing(0)
        text = QVBoxLayout()
        text.setContentsMargins(0, 6, 0, 12)
        text.setSpacing(2)
        self._title = QLabel(title, self)
        self._hint = QLabel(hint, self)
        self._hint.setObjectName("Caption")
        text.addWidget(self._title)
        text.addWidget(self._hint)
        layout.addLayout(text)
        layout.addStretch(1)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(320, 54)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self._sync_text()
        self.update()

    def set_state(self, state: str) -> None:
        self._state = state
        if state == ACTIVE:
            self._spin.start()
        else:
            self._spin.stop()
        self._sync_text()
        self.update()

    def _advance(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def _sync_text(self) -> None:
        if self._palette is None:
            return
        p = self._palette
        color = {
            PENDING: p.text_tertiary,
            ACTIVE: p.text,
            DONE: p.text_secondary,
            FAILED: p.danger,
        }[self._state]
        weight = 600 if self._state == ACTIVE else 400
        self._title.setStyleSheet(f"background: transparent; color: {color}; font-weight: {weight};")

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._palette is None:
            return
        p = self._palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        center_x, center_y, radius = 15, 17, 9

        if not self._last:  # connector down to the next step
            painter.setPen(QPen(QColor(p.divider), 2))
            painter.drawLine(center_x, center_y + radius + 4, center_x, self.height())

        if self._state == PENDING:
            painter.setPen(QPen(QColor(p.stroke_strong), 1.6))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)
            painter.end()
            return

        if self._state == ACTIVE:
            painter.setPen(QPen(QColor(p.stroke), 2))
            painter.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)
            pen = QPen(QColor(p.accent), 2)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            # Qt angles are in 1/16 degrees, counter-clockwise.
            painter.drawArc(
                center_x - radius, center_y - radius, radius * 2, radius * 2,
                -self._angle * 16, -100 * 16,
            )
            painter.end()
            return

        fill = QColor(p.accent if self._state == DONE else p.danger)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)

        mark = QPen(QColor(p.accent_text if self._state == DONE else "#FFFFFF"), 2)
        mark.setCapStyle(Qt.RoundCap)
        mark.setJoinStyle(Qt.RoundJoin)
        painter.setPen(mark)
        path = QPainterPath()
        if self._state == DONE:
            path.moveTo(center_x - 4.2, center_y + 0.2)
            path.lineTo(center_x - 1.2, center_y + 3.4)
            path.lineTo(center_x + 4.4, center_y - 3.4)
        else:
            path.moveTo(center_x - 3.4, center_y - 3.4)
            path.lineTo(center_x + 3.4, center_y + 3.4)
            path.moveTo(center_x + 3.4, center_y - 3.4)
            path.lineTo(center_x - 3.4, center_y + 3.4)
        painter.drawPath(path)
        painter.end()


class StepTimeline(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.rows: dict[str, StepRow] = {}
        for index, (key, title, hint) in enumerate(STEP_DEFINITIONS):
            row = StepRow(title, hint, last=index == len(STEP_DEFINITIONS) - 1, parent=self)
            self.rows[key] = row
            layout.addWidget(row)

    def apply_palette(self, palette: Palette) -> None:
        for row in self.rows.values():
            row.apply_palette(palette)

    def reset(self) -> None:
        for row in self.rows.values():
            row.set_state(PENDING)

    def mark(self, key: str, state: str) -> None:
        row = self.rows.get(key)
        if row is not None:
            row.set_state(state)


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #

class InstallWorker(QThread):
    step_reached = Signal(str)
    completed = Signal(int)

    def __init__(self, command: list[str], log_file: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self._command = command
        self._log_file = log_file

    def _write(self, line: str) -> None:
        with self._log_file.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")

    def run(self) -> None:  # noqa: D102 - QThread entry point
        self._write("Running: " + " ".join(self._command))
        try:
            process = subprocess.Popen(
                self._command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            self._write(f"ERROR: failed to start installer: {exc}")
            self.completed.emit(2)
            return

        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            self._write(line)
            if line.startswith("[STEP] "):
                self.step_reached.emit(line[7:].strip().lower())

        code = process.wait()
        self._write(f"Installer exit code: {code}")
        self.completed.emit(code)


# --------------------------------------------------------------------------- #
# window
# --------------------------------------------------------------------------- #

class InstallerWindow(QWidget):
    def __init__(self, icon: QIcon | None, palette: Palette | None = None):
        super().__init__()
        # The stylesheet paints the window background through this object name,
        # and a plain QWidget only honours that with WA_StyledBackground set —
        # without both, the window keeps the system colour and ignores dark mode.
        self.setObjectName("RootSurface")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle(f"{APP_TITLE} — установка")
        if icon is not None:
            self.setWindowIcon(icon)
        self.setMinimumSize(560, 600)
        self.resize(600, 640)

        self._log_file = log_path()
        self._worker: InstallWorker | None = None
        self._current_key: str | None = None
        self._palette = palette or build_palette(ThemeMode.system)

        self._build(icon)
        self._apply_theme()

    # -- construction --------------------------------------------------- #

    def _build(self, icon: QIcon | None) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 24)
        root.setSpacing(18)

        header = QHBoxLayout()
        header.setSpacing(14)
        if icon is not None:
            badge = QLabel(self)
            badge.setPixmap(icon.pixmap(44, 44))
            badge.setFixedSize(44, 44)
            header.addWidget(badge, 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self._title = QLabel(APP_TITLE, self)
        self._title.setObjectName("PageTitle")
        self._subtitle = QLabel("Установка агента печати на этот компьютер", self)
        self._subtitle.setObjectName("PageSubtitle")
        titles.addWidget(self._title)
        titles.addWidget(self._subtitle)
        header.addLayout(titles, 1)
        root.addLayout(header)

        self.info_bar = InfoBar(self)
        root.addWidget(self.info_bar)

        # phase 1 — parameters
        self.settings_card = Card(self, padding=20, spacing=14)
        self.install_root = QLineEdit(DEFAULT_INSTALL_ROOT, self)
        self.package_spec = QLineEdit("", self)
        self.package_spec.setPlaceholderText("необязательно")
        self.feed_url = QLineEdit(DEFAULT_FEED_URL, self)
        self.feed_url.setCursorPosition(0)
        self.settings_card.add(form_row("Папка установки", self.install_root))
        self.settings_card.add(
            form_row("Пакет", self.package_spec, hint="URL или путь к .whl. Пусто — взять из канала обновлений")
        )
        self.settings_card.add(form_row("Канал обновлений", self.feed_url))

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(12)
        toggle_labels = QVBoxLayout()
        toggle_labels.setSpacing(1)
        auto_title = QLabel("Автообновление", self)
        auto_hint = QLabel("Служба проверяет канал при запуске", self)
        auto_hint.setObjectName("Caption")
        toggle_labels.addWidget(auto_title)
        toggle_labels.addWidget(auto_hint)
        toggle_row.addLayout(toggle_labels, 1)
        self.auto_update = ToggleSwitch(self)
        self.auto_update.setChecked(True)
        toggle_row.addWidget(self.auto_update, 0, Qt.AlignVCenter)
        self.settings_card.add_layout(toggle_row)
        root.addWidget(self.settings_card)

        # phase 2 — progress
        self.progress_card = Card(self, padding=20, spacing=14)
        self.timeline = StepTimeline(self)
        self.progress_card.add(self.timeline)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, len(STEP_DEFINITIONS))
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)  # QSS `height` alone loses to the layout
        self.progress_card.add(self.progress)
        self.progress_card.setVisible(False)
        root.addWidget(self.progress_card)

        root.addStretch(1)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.status = QLabel("Готово к установке", self)
        self.status.setObjectName("Secondary")
        footer.addWidget(self.status, 1)

        self.log_button = QPushButton("Журнал", self)
        self.log_button.setObjectName("Subtle")
        self.log_button.setCursor(Qt.PointingHandCursor)
        self.log_button.setVisible(False)
        self.log_button.clicked.connect(self._open_log)
        footer.addWidget(self.log_button)

        self.close_button = QPushButton("Закрыть", self)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setVisible(False)
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)

        self.primary = QPushButton("Установить", self)
        self.primary.setObjectName("Accent")
        self.primary.setCursor(Qt.PointingHandCursor)
        self.primary.setMinimumWidth(140)
        self.primary.clicked.connect(self._start)
        footer.addWidget(self.primary)
        root.addLayout(footer)

    def _apply_theme(self) -> None:
        palette = self._palette
        self.setStyleSheet(build_stylesheet(palette))
        self.timeline.apply_palette(palette)
        self.auto_update.apply_palette(palette)
        self.settings_card.apply_shadow(palette)
        self.progress_card.apply_shadow(palette)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        apply_window_backdrop(int(self.winId()), self._palette.dark)

    # -- flow ----------------------------------------------------------- #

    def _start(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        script = resource_dir() / "install.ps1"
        if not script.exists():
            self.info_bar.show_message(f"Не найден install.ps1 рядом с установщиком: {script}", "error")
            return

        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-InstallRoot",
            self.install_root.text().strip() or DEFAULT_INSTALL_ROOT,
            "-UpdateFeedUrl",
            self.feed_url.text().strip(),
            "-AutoUpdate",
            "True" if self.auto_update.isChecked() else "False",
        ]
        package_spec = self.package_spec.text().strip()
        if package_spec:
            command.extend(["-PackageSpec", package_spec])

        self.info_bar.hide_message()
        self.settings_card.setVisible(False)
        self.progress_card.setVisible(True)
        self.timeline.reset()
        self.progress.setValue(0)
        self.primary.setEnabled(False)
        self.primary.setText("Установка…")
        self.status.setText("Идёт установка")
        self._current_key = None

        self._worker = InstallWorker(command, self._log_file, self)
        self._worker.step_reached.connect(self._on_step)
        self._worker.completed.connect(self._on_finished)
        self._worker.start()

    def _on_step(self, key: str) -> None:
        keys = [item[0] for item in STEP_DEFINITIONS]
        if key not in keys:
            return
        index = keys.index(key)
        for earlier in keys[:index]:
            self.timeline.mark(earlier, DONE)
        self.timeline.mark(key, ACTIVE)
        self._current_key = key
        self.progress.setValue(index)

    def _on_finished(self, code: int) -> None:
        self.primary.setEnabled(True)
        # The info bar carries the outcome; a second copy in the footer would
        # only fight the buttons for room.
        self.status.setText("")
        self.close_button.setVisible(True)
        if code == 0:
            for key, _, _ in STEP_DEFINITIONS:
                self.timeline.mark(key, DONE)
            self.progress.setValue(len(STEP_DEFINITIONS))
            self.info_bar.show_message("Установка завершена. Ярлыки созданы, служба работает.", "success")
            self.primary.setText("Открыть приложение")
            self.primary.clicked.disconnect()
            self.primary.clicked.connect(self._launch_app)
            return

        if self._current_key:
            self.timeline.mark(self._current_key, FAILED)
        self.info_bar.show_message(
            f"Установка прервалась на этапе «{self._step_title(self._current_key)}». "
            "Подробности — в журнале.",
            "error",
        )
        self.primary.setText("Повторить")
        self.log_button.setVisible(True)

    @staticmethod
    def _step_title(key: str | None) -> str:
        for step_key, title, _ in STEP_DEFINITIONS:
            if step_key == key:
                return title
        return "подготовка"

    def _open_log(self) -> None:
        try:
            os.startfile(str(self._log_file))  # noqa: S606 - opening our own log
        except Exception as exc:
            self.info_bar.show_message(f"Не удалось открыть журнал: {exc}", "warning")

    def _launch_app(self) -> None:
        install_root = Path(self.install_root.text().strip() or DEFAULT_INSTALL_ROOT)
        # pythonw.exe rather than the pip-generated launcher: it is GUI-subsystem
        # so no console appears, and updates never rewrite it.
        pythonw = install_root / ".venv" / "Scripts" / "pythonw.exe"
        config = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "printer-agent" / "agent.yaml"
        if not pythonw.exists():
            self.info_bar.show_message(f"Не найден интерпретатор: {pythonw}", "warning")
            return
        try:
            subprocess.Popen(
                [str(pythonw), "-m", "printer_agent", "gui", "--config", str(config)],
                cwd=str(install_root),
            )
            self.close()
        except Exception as exc:
            self.info_bar.show_message(f"Не удалось запустить приложение: {exc}", "error")


def main() -> int:
    if os.name != "nt":
        return 2
    if not is_admin():
        relaunch_as_admin()
        return 0

    hint = qt_platform_hint()
    if hint:
        os.environ.setdefault("QT_QPA_PLATFORM", hint)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RDControl.PrinterAgent.Installer")
    except Exception:
        pass

    app = QApplication(sys.argv[:1])
    app.setApplicationName(f"{APP_TITLE} Installer")

    icon_file = resource_dir() / "printer-agent.ico"
    icon = QIcon(str(icon_file)) if icon_file.exists() else None
    if icon is not None:
        app.setWindowIcon(icon)

    window = InstallerWindow(icon)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
