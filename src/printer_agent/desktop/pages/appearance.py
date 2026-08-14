"""Theme mode, accent colour and live-status cadence."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QWidget,
)

from ..prefs import Preferences
from ..theme import ACCENT_LABELS, ACCENT_PRESETS, Palette, ThemeMode, build_palette, system_accent
from ..widgets import Caption, Card, SectionTitle, ToggleSwitch, form_row, inline_row
from .base import Page

MODE_LABELS: list[tuple[ThemeMode, str, str]] = [
    (ThemeMode.system, "Как в системе", "Следовать настройке Windows «Светлый/Тёмный»."),
    (ThemeMode.light, "Светлая", "Всегда светлая тема."),
    (ThemeMode.dark, "Тёмная", "Всегда тёмная тема."),
]


class AccentSwatch(QAbstractButton):
    """Colour circle in the accent picker."""

    def __init__(self, key: str, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.key = key
        self._color = QColor(color)
        self._ring = QColor("#0078D4")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40, 40)
        self.setToolTip(ACCENT_LABELS.get(key, key))

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def set_ring(self, color: str) -> None:
        self._ring = QColor(color)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(40, 40)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(6, 6, 28, 28)
        if self.isChecked():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(self._ring, 2))
            painter.drawEllipse(2, 2, 36, 36)


class AppearancePage(Page):
    title = "Оформление"
    subtitle = "Цветовая схема приложения и частота опроса принтеров."
    glyph = "\uE790"  # Color (Segoe Fluent Icons)

    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(state, parent)
        self._on_live_changed = None

        self._build_theme_card()
        self._build_accent_card()
        self._build_behaviour_card()
        self.content.addStretch(1)
        self._load()

    def bind(self, on_live_changed) -> None:
        self._on_live_changed = on_live_changed

    def set_live_status(self, enabled: bool) -> None:
        """Reflect a change made elsewhere without echoing it back."""
        self._live_toggle.blockSignals(True)
        self._live_toggle.setChecked(enabled)
        self._live_toggle.blockSignals(False)

    # -- construction ------------------------------------------------------- #

    def _build_theme_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Тема"))
        self._mode_group = QButtonGroup(self)
        self._mode_buttons: dict[ThemeMode, QRadioButton] = {}
        for mode, label, hint in MODE_LABELS:
            button = QRadioButton(label)
            self._mode_group.addButton(button)
            self._mode_buttons[mode] = button
            card.add(button)
            card.add(Caption(hint))
            button.toggled.connect(self._make_mode_handler(mode))
        self.content.addWidget(card)

    def _build_accent_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Акцентный цвет"))
        card.add(Caption("Влияет на кнопки, переключатели и выделение в списках."))

        row = QHBoxLayout()
        row.setSpacing(8)
        self._swatch_group = QButtonGroup(self)
        self._swatch_group.setExclusive(True)
        self._swatches: dict[str, AccentSwatch] = {}
        for key, color in ACCENT_PRESETS.items():
            swatch = AccentSwatch(key, color or system_accent())
            self._swatch_group.addButton(swatch)
            self._swatches[key] = swatch
            row.addWidget(swatch)
            swatch.clicked.connect(self._make_accent_handler(key))
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        card.add(holder)

        self._accent_caption = Caption("")
        card.add(self._accent_caption)
        self.content.addWidget(card)

    def _build_behaviour_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Живой статус принтеров"))
        self._live_toggle = ToggleSwitch()
        live_label = QLabel("Опрашивать принтеры из приложения")
        live_label.setObjectName("Secondary")
        card.add(inline_row(self._live_toggle, live_label))
        card.add(
            Caption(
                "Опрос идёт отдельно от службы и только пока окно открыто. "
                "Для Bambu Lab приложение использует собственный идентификатор MQTT, "
                "чтобы не разрывать сессию службы."
            )
        )

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(3, 300)
        self._interval_spin.setSuffix(" с")
        card.add(form_row("Интервал опроса", self._interval_spin))
        self._live_toggle.toggled.connect(self._live_changed)
        self._interval_spin.valueChanged.connect(self._interval_changed)
        self.content.addWidget(card)

    # -- handlers ----------------------------------------------------------- #

    def _make_mode_handler(self, mode: ThemeMode):
        def handler(checked: bool) -> None:
            if not checked or self._loading:
                return
            self.state.preferences.theme_mode = mode.value
            self.state.save_preferences()
            self.state.refresh_palette()

        return handler

    def _make_accent_handler(self, key: str):
        def handler() -> None:
            if self._loading:
                return
            self.state.preferences.accent = key
            self.state.save_preferences()
            self.state.refresh_palette()

        return handler

    def _live_changed(self, enabled: bool) -> None:
        if self._loading:
            return
        self.state.preferences.live_status = enabled
        self.state.save_preferences()
        if self._on_live_changed is not None:
            self._on_live_changed(enabled)

    def _interval_changed(self, value: int) -> None:
        if self._loading:
            return
        self.state.preferences.poll_interval_s = value
        self.state.save_preferences()
        if self._on_live_changed is not None:
            self._on_live_changed(self.state.preferences.live_status)

    # -- state -------------------------------------------------------------- #

    _loading = False

    def _sync_selection(self) -> None:
        """Mirror the stored preferences; the theme can change from elsewhere."""
        self._loading = True
        preferences: Preferences = self.state.preferences
        self._mode_buttons[preferences.mode].setChecked(True)
        self._swatches[preferences.accent].setChecked(True)
        self._loading = False

    def _load(self) -> None:
        self._loading = True
        preferences: Preferences = self.state.preferences
        self._live_toggle.setChecked(preferences.live_status)
        self._interval_spin.setValue(preferences.poll_interval_s)
        self._loading = False
        self.apply_palette(self.state.palette)

    def apply_palette(self, palette: Palette) -> None:
        self._live_toggle.apply_palette(palette)
        self._sync_selection()
        for key, swatch in self._swatches.items():
            resolved = ACCENT_PRESETS[key] or system_accent()
            preview = build_palette(
                ThemeMode.dark if palette.dark else ThemeMode.light, key
            )
            swatch.set_color(preview.accent if key != "system" else resolved)
            swatch.set_ring(palette.text)
        self._accent_caption.setText(
            f"Текущий акцент: {ACCENT_LABELS.get(self.state.preferences.accent, '')} · {palette.accent}"
        )
