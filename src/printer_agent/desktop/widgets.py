"""Reusable Fluent-styled widgets.

Most of the look comes from the stylesheet in :mod:`theme`; the widgets here are
the pieces QSS cannot express — painted indicators, the toggle switch, and the
small compositions the pages assemble.
"""

from __future__ import annotations

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import Palette

#: Windows ships Fluent icons as a font; MDL2 is the Windows 10 fallback and
#: shares most of its codepoints. This has to be applied through the widget's
#: own stylesheet — the app-wide `* { font-family }` rule outranks setFont().
ICON_FONT_CSS = 'font-family: "Segoe Fluent Icons", "Segoe MDL2 Assets"; font-size: 15px;'


class Card(QFrame):
    """A surface with the Fluent card treatment and a vertical content layout."""

    def __init__(self, parent: QWidget | None = None, *, flat: bool = False, padding: int = 20, spacing: int = 12):
        super().__init__(parent)
        self.setObjectName("CardFlat" if flat else "Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(spacing)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)

    def apply_shadow(self, palette: Palette) -> None:
        effect = QGraphicsDropShadowEffect(self)
        effect.setBlurRadius(22 if palette.dark else 18)
        effect.setOffset(0, 2)
        effect.setColor(QColor(0, 0, 0, 90 if palette.dark else 26))
        self.setGraphicsEffect(effect)


class SectionTitle(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("SectionTitle")


class Caption(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("Caption")
        self.setWordWrap(True)


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)


class StatusPill(QWidget):
    """Coloured dot plus label — the status vocabulary of the whole app."""

    def __init__(self, text: str = "", color: str = "#8A8A8A", parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._dot = _Dot(self._color, self)
        self._label = QLabel(text, self)
        layout.addWidget(self._dot)
        layout.addWidget(self._label)
        layout.addStretch(1)

    def set_status(self, text: str, color: str) -> None:
        self._color = QColor(color)
        self._dot.set_color(self._color)
        self._label.setText(text)

    def set_bold(self, bold: bool) -> None:
        font = self._label.font()
        font.setWeight(QFont.DemiBold if bold else QFont.Normal)
        self._label.setFont(font)


class _Dot(QWidget):
    def __init__(self, color: QColor, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(10, 10)

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(self._color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, 10, 10)


class NavItem(QPushButton):
    """Navigation entry with the Fluent selection bar painted on the left edge.

    The glyph lives in its own label so it can use the Windows icon font while
    the caption stays on the UI font.
    """

    def __init__(self, text: str, glyph: str = "", parent: QWidget | None = None):
        super().__init__("", parent)
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(38)
        self._indicator = QColor("#0078D4")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(12)
        self._glyph = QLabel(glyph, self)
        self._glyph.setFixedWidth(18)
        self._glyph.setAlignment(Qt.AlignCenter)
        self._caption = QLabel(text, self)
        for label in (self._glyph, self._caption):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setStyleSheet("background: transparent;")
        layout.addWidget(self._glyph)
        layout.addWidget(self._caption, 1)

        self._palette: Palette | None = None
        self.toggled.connect(lambda _: self._sync_labels())

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self._indicator = QColor(palette.accent)
        self._sync_labels()
        self.update()

    def _sync_labels(self) -> None:
        if self._palette is None:
            return
        checked = self.isChecked()
        color = self._palette.text if checked else self._palette.text_secondary
        self._glyph.setStyleSheet(f"background: transparent; color: {color}; {ICON_FONT_CSS}")
        self._caption.setStyleSheet(
            f"background: transparent; color: {color}; font-weight: {600 if checked else 400};"
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if not self.isChecked():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        height = 16
        top = (self.height() - height) // 2
        path = QPainterPath()
        path.addRoundedRect(3, top, 3, height, 1.5, 1.5)
        painter.fillPath(path, self._indicator)


class ToggleSwitch(QAbstractButton):
    """Fluent toggle. Painted by hand because QCheckBox cannot be shaped this way."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(44, 22)
        self._offset = 0.0
        self._on = QColor("#0078D4")
        self._on_knob = QColor("#FFFFFF")
        self._off_track = QColor("#8A8A8A")
        self._off_knob = QColor("#5C5C5C")
        self._animation = QPropertyAnimation(self, b"offset", self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def apply_palette(self, palette: Palette) -> None:
        self._on = QColor(palette.accent)
        self._on_knob = QColor(palette.accent_text)
        self._off_track = QColor(palette.stroke_strong)
        self._off_knob = QColor(palette.text_secondary)
        self.update()

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def get_offset(self) -> float:
        return self._offset

    def set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, get_offset, set_offset)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(44, 22)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        radius = self.height() / 2

        if self.isChecked():
            painter.setBrush(self._on)
            painter.setPen(Qt.NoPen)
        else:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(self._off_track, 1.4))
        painter.drawRoundedRect(
            self.rect().adjusted(1, 1, -1, -1), radius - 1, radius - 1
        )

        knob_radius = 6 if not self.isChecked() else 7
        travel = self.width() - 2 * radius
        center_x = radius + self._offset * travel
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._on_knob if self.isChecked() else self._off_knob)
        painter.drawEllipse(
            int(center_x - knob_radius),
            int(self.height() / 2 - knob_radius),
            knob_radius * 2,
            knob_radius * 2,
        )


class MetricTile(Card):
    """Large-number tile used across the dashboard."""

    def __init__(self, label: str, value: str = "—", parent: QWidget | None = None):
        super().__init__(parent, padding=16, spacing=2)
        self._value = QLabel(value, self)
        self._value.setObjectName("MetricValue")
        self._caption = QLabel(label, self)
        self._caption.setObjectName("MetricLabel")
        self.add(self._value)
        self.add(self._caption)

    def set_value(self, value: str) -> None:
        self._value.setText(value)


class InfoBar(QFrame):
    """Inline status message; hidden until something has to be said."""

    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("InfoBar")
        self.setVisible(False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(10)
        self._label = QLabel("", self)
        self._label.setWordWrap(True)
        layout.addWidget(self._label, 1)
        close = QPushButton("✕", self)
        close.setObjectName("Subtle")
        close.setFixedWidth(30)
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.hide_message)
        layout.addWidget(close)

    def show_message(self, text: str, severity: str = "info") -> None:
        severity_names = {
            "success": "InfoBarSuccess",
            "error": "InfoBarError",
            "warning": "InfoBarWarning",
            "info": "InfoBarInfo",
        }
        self.setProperty("class", severity)
        self._label.setText(text)
        # Object name drives the QSS rule; re-polish so the change takes effect.
        self.setObjectName(severity_names.get(severity, "InfoBarInfo"))
        self.style().unpolish(self)
        self.style().polish(self)
        self.setVisible(True)

    def hide_message(self) -> None:
        self.setVisible(False)
        self.dismissed.emit()


def form_row(label_text: str, field: QWidget, *, hint: str = "") -> QWidget:
    """Label above field, optional hint below — the app's one form rhythm."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    label = QLabel(label_text)
    label.setObjectName("Secondary")
    layout.addWidget(label)
    layout.addWidget(field)
    if hint:
        layout.addWidget(Caption(hint))
    return container


def inline_row(*widgets: QWidget, spacing: int = 10, stretch_last: bool = False) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if (stretch_last and index == len(widgets) - 1) else 0)
    if not stretch_last:
        layout.addStretch(1)
    return container
