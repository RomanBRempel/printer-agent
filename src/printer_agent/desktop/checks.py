"""UI plumbing for the connectivity checks in :mod:`printer_agent.uplink.diagnostics`."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..aio import run as run_async
from ..uplink.diagnostics import CheckResult, CheckStep
from .theme import Palette
from .widgets import Caption

#: Marker glyphs, chosen to stay legible without the icon font.
MARKERS = {True: "✓", False: "✕", None: "•"}


class CheckRunner(QThread):
    """Runs one async check off the UI thread.

    Hold a reference on the caller: a garbage-collected QThread aborts mid-run.
    """

    completed = Signal(object)  # CheckResult

    def __init__(self, factory: Callable[[], object], parent=None):
        super().__init__(parent)
        self._factory = factory

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            result = run_async(self._factory())
        except Exception as exc:
            result = CheckResult(
                ok=False,
                summary=str(exc) or exc.__class__.__name__,
                steps=[CheckStep(key="error", label="Проверка", ok=False, detail=str(exc))],
            )
        self.completed.emit(result)


class AsyncRunner(QThread):
    """Runs any coroutine factory off the UI thread and reports (result, error).

    Hold a reference on the caller, as with :class:`CheckRunner`.
    """

    completed = Signal(object, str)

    def __init__(self, factory: Callable[[], object], parent=None):
        super().__init__(parent)
        self._factory = factory

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            self.completed.emit(run_async(self._factory()), "")
        except Exception as exc:
            self.completed.emit(None, str(exc) or exc.__class__.__name__)


class CheckStepsView(QWidget):
    """Renders a check's stages so a failure names the stage it happened at."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._palette: Palette | None = None
        self._result: CheckResult | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self.setVisible(False)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        if self._result is not None:
            self.show_result(self._result)

    def clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Unparent before deleteLater: deletion is deferred to the event
                # loop, and until then the old rows would keep painting under the
                # new ones.
                widget.setParent(None)
                widget.deleteLater()

    def show_pending(self, message: str) -> None:
        self._result = None
        self.clear()
        self._layout.addWidget(Caption(message))
        self.setVisible(True)

    def show_result(self, result: CheckResult) -> None:
        self._result = result
        self.clear()
        palette = self._palette
        for step in result.steps:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)

            marker = QLabel(MARKERS[step.ok])
            marker.setFixedWidth(14)
            marker.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
            if palette is not None:
                color = {
                    True: palette.success,
                    False: palette.danger,
                    None: palette.text_tertiary,
                }[step.ok]
                marker.setStyleSheet(f"background: transparent; color: {color}; font-weight: 600;")
            layout.addWidget(marker)

            text = QVBoxLayout()
            text.setSpacing(1)
            title = QLabel(step.label)
            if step.duration_ms is not None:
                title.setText(f"{step.label}  ·  {step.duration_ms} мс")
            if palette is not None and step.ok is None:
                title.setStyleSheet(f"background: transparent; color: {palette.text_tertiary};")
            text.addWidget(title)
            if step.detail:
                text.addWidget(Caption(step.detail))
            layout.addLayout(text)
            layout.addStretch(1)

            self._layout.addWidget(row)
        self.setVisible(True)
