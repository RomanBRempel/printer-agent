"""Tail of the agent log files written under ProgramData."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QLabel, QPlainTextEdit, QPushButton, QWidget

from ..system import list_log_files, log_dir, reveal, tail_text
from ..theme import Palette
from ..widgets import Caption, Card, SectionTitle, ToggleSwitch, inline_row
from .base import Page

REFRESH_INTERVAL_MS = 3000


class LogsPage(Page):
    title = "Логи"
    subtitle = "Диагностика службы и установщика."
    glyph = "\uE9D9"  # Diagnostic (Segoe Fluent Icons)

    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(state, parent)

        card = Card()
        card.add(SectionTitle("Файлы журналов"))

        self._file_combo = QComboBox()
        self._file_combo.setMinimumWidth(280)
        self._file_combo.currentIndexChanged.connect(lambda _: self.reload_current())

        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.reload_files)
        open_folder = QPushButton("Открыть папку")
        open_folder.clicked.connect(lambda: reveal(log_dir()))

        self._follow_toggle = ToggleSwitch()
        self._follow_toggle.setChecked(True)
        follow_label = QLabel("Автообновление")
        follow_label.setObjectName("Secondary")

        card.add(inline_row(self._file_combo, refresh, open_folder, self._follow_toggle, follow_label))
        self._path_caption = Caption(str(log_dir()))
        card.add(self._path_caption)
        self.content.addWidget(card)

        self._view = QPlainTextEdit()
        self._view.setObjectName("LogView")
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._view.setMinimumHeight(360)
        self.content.addWidget(self._view, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self.reload_files()

    # -- data --------------------------------------------------------------- #

    def reload_files(self) -> None:
        previous = self._file_combo.currentData()
        files = list_log_files()
        self._file_combo.blockSignals(True)
        self._file_combo.clear()
        for path in files:
            self._file_combo.addItem(path.name, str(path))
        self._file_combo.blockSignals(False)

        if not files:
            self._view.setPlainText(
                f"В {log_dir()} пока нет файлов журнала.\n\n"
                "Служба пишет журнал при запуске; установщик — во время установки."
            )
            return
        index = self._file_combo.findData(previous)
        self._file_combo.setCurrentIndex(index if index >= 0 else 0)
        self.reload_current()

    def reload_current(self) -> None:
        data = self._file_combo.currentData()
        if not data:
            return
        path = Path(str(data))
        self._path_caption.setText(str(path))
        scrollbar = self._view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        self._view.setPlainText(tail_text(path))
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _tick(self) -> None:
        if self._follow_toggle.isChecked():
            self.reload_current()

    # -- lifecycle ---------------------------------------------------------- #

    def on_shown(self) -> None:
        self.reload_files()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        super().hideEvent(event)

    def apply_palette(self, palette: Palette) -> None:
        self._follow_toggle.apply_palette(palette)
