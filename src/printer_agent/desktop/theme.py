"""Fluent-flavoured theming for the desktop app.

Deliberately free of Qt imports: palettes are plain data and the stylesheet is a
string, so the whole colour system is unit-testable without a display server.
Only :func:`apply_window_backdrop` touches Windows APIs, and it degrades to a
no-op everywhere else.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum

DEFAULT_ACCENT = "#0078D4"

FONT_STACK = '"Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif'
MONO_FONT_STACK = '"Cascadia Mono", "Consolas", "Courier New", monospace'


class ThemeMode(StrEnum):
    light = "light"
    dark = "dark"
    system = "system"


#: Named accent presets offered in Appearance. ``system`` resolves at runtime to
#: the colour the user picked in Windows Settings.
ACCENT_PRESETS: dict[str, str] = {
    "system": "",
    "blue": "#0078D4",
    "teal": "#038387",
    "purple": "#8764B8",
    "green": "#107C10",
    "orange": "#CA5010",
    "red": "#C42B1C",
    "pink": "#E3008C",
}

ACCENT_LABELS: dict[str, str] = {
    "system": "Системный",
    "blue": "Синий",
    "teal": "Бирюзовый",
    "purple": "Фиолетовый",
    "green": "Зелёный",
    "orange": "Оранжевый",
    "red": "Красный",
    "pink": "Розовый",
}


# --------------------------------------------------------------------------- #
# colour helpers
# --------------------------------------------------------------------------- #

def parse_hex(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        raise ValueError(f"not a hex colour: {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    red, green, blue = (max(0, min(255, int(round(channel)))) for channel in rgb)
    return f"#{red:02X}{green:02X}{blue:02X}"


def mix(first: str, second: str, weight: float) -> str:
    """Blend ``first`` toward ``second``; ``weight`` 0 keeps ``first``."""
    ratio = max(0.0, min(1.0, weight))
    left = parse_hex(first)
    right = parse_hex(second)
    return to_hex(tuple(a + (b - a) * ratio for a, b in zip(left, right)))


def lighten(color: str, amount: float) -> str:
    return mix(color, "#FFFFFF", amount)


def darken(color: str, amount: float) -> str:
    return mix(color, "#000000", amount)


def relative_luminance(color: str) -> float:
    channels = []
    for raw in parse_hex(color):
        value = raw / 255.0
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def readable_text_on(color: str) -> str:
    """Pick black or white text for ``color`` by contrast, the way Fluent does."""
    return "#000000" if relative_luminance(color) > 0.42 else "#FFFFFF"


def with_alpha(color: str, alpha: float) -> str:
    red, green, blue = parse_hex(color)
    return f"rgba({red}, {green}, {blue}, {max(0.0, min(1.0, alpha)):.3f})"


# --------------------------------------------------------------------------- #
# system integration
# --------------------------------------------------------------------------- #

def system_prefers_dark() -> bool:
    """Read the Windows apps-theme preference; default to light off-Windows."""
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:
        return False


def system_accent() -> str:
    """Best-effort read of the Windows accent colour."""
    if os.name != "nt":
        return DEFAULT_ACCENT
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent",
        ) as key:
            palette, _ = winreg.QueryValueEx(key, "AccentPalette")
        if isinstance(palette, bytes) and len(palette) >= 16:
            # 8 BGRA entries, darkest first; offset 12 is the base accent.
            blue, green, red = palette[12], palette[13], palette[14]
            return to_hex((red, green, blue))
    except Exception:
        pass
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
        # DWORD is 0xAABBGGRR.
        raw = int(value) & 0xFFFFFF
        return to_hex((raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF))
    except Exception:
        return DEFAULT_ACCENT


def apply_window_backdrop(window_id: int, dark: bool) -> bool:
    """Switch the native title bar to dark mode. Returns True when it took."""
    if os.name != "nt" or not window_id:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        dwmapi = ctypes.windll.dwmapi
        flag = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # 20 on Win10 2004+, 19 on earlier builds
            result = dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(window_id),
                wintypes.DWORD(attribute),
                ctypes.byref(flag),
                ctypes.sizeof(flag),
            )
            if result == 0:
                return True
    except Exception:
        return False
    return False


# --------------------------------------------------------------------------- #
# palette
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Palette:
    dark: bool
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    accent_soft: str
    window: str
    layer: str
    layer_alt: str
    card: str
    card_hover: str
    control: str
    control_hover: str
    control_pressed: str
    stroke: str
    stroke_strong: str
    divider: str
    text: str
    text_secondary: str
    text_tertiary: str
    text_disabled: str
    success: str
    warning: str
    danger: str
    info: str
    selection: str


def build_palette(mode: ThemeMode, accent_key: str = "system") -> Palette:
    """Resolve a mode + accent choice into concrete colours."""
    dark = system_prefers_dark() if mode is ThemeMode.system else mode is ThemeMode.dark
    base_accent = ACCENT_PRESETS.get(accent_key) or system_accent()

    if dark:
        # Windows lightens the accent on dark surfaces so it stays legible.
        accent = lighten(base_accent, 0.42)
        accent_hover = lighten(base_accent, 0.52)
        accent_pressed = lighten(base_accent, 0.30)
    else:
        accent = darken(base_accent, 0.02)
        accent_hover = lighten(base_accent, 0.12)
        accent_pressed = darken(base_accent, 0.16)

    if dark:
        return Palette(
            dark=True,
            accent=accent,
            accent_hover=accent_hover,
            accent_pressed=accent_pressed,
            accent_text=readable_text_on(accent),
            accent_soft=mix(accent, "#202020", 0.78),
            window="#1F1F1F",
            layer="#272727",
            layer_alt="#202020",
            card="#2B2B2B",
            card_hover="#323232",
            control="#2D2D2D",
            control_hover="#353535",
            control_pressed="#272727",
            stroke="#383838",
            stroke_strong="#4A4A4A",
            divider="#303030",
            text="#FFFFFF",
            text_secondary="#C7C7C7",
            text_tertiary="#9A9A9A",
            text_disabled="#6E6E6E",
            success="#6CCB5F",
            warning="#FCE100",
            danger="#FF99A4",
            info="#60CDFF",
            selection=mix(accent, "#2B2B2B", 0.72),
        )

    return Palette(
        dark=False,
        accent=accent,
        accent_hover=accent_hover,
        accent_pressed=accent_pressed,
        accent_text=readable_text_on(accent),
        accent_soft=mix(accent, "#FFFFFF", 0.88),
        window="#F3F3F3",
        layer="#FFFFFF",
        layer_alt="#FAFAFA",
        card="#FFFFFF",
        card_hover="#F6F6F6",
        control="#FFFFFF",
        control_hover="#F6F6F6",
        control_pressed="#F0F0F0",
        stroke="#E2E2E2",
        stroke_strong="#CFCFCF",
        divider="#EAEAEA",
        text="#1A1A1A",
        text_secondary="#5C5C5C",
        text_tertiary="#8A8A8A",
        text_disabled="#A5A5A5",
        success="#0F7B0F",
        warning="#9D5D00",
        danger="#C42B1C",
        info="#005FB8",
        selection=mix(accent, "#FFFFFF", 0.82),
    )


STATUS_COLOR_KEYS: dict[str, str] = {
    "idle": "info",
    "printing": "success",
    "paused": "warning",
    "finished": "success",
    "error": "danger",
    "offline": "text_tertiary",
    "maintenance": "warning",
    "running": "success",
    "stopped": "text_tertiary",
    "unknown": "text_tertiary",
}


def status_color(palette: Palette, status: str) -> str:
    return getattr(palette, STATUS_COLOR_KEYS.get(status.lower(), "text_tertiary"))


# --------------------------------------------------------------------------- #
# stylesheet
# --------------------------------------------------------------------------- #

def build_stylesheet(palette: Palette) -> str:
    """Render the palette into the app-wide QSS."""
    p = palette
    focus = p.accent
    scroll_handle = p.stroke_strong
    return f"""
* {{
    font-family: {FONT_STACK};
    font-size: 14px;
    outline: none;
}}

QWidget {{
    color: {p.text};
    background: transparent;
}}

QMainWindow, QDialog, #RootSurface {{
    background: {p.window};
}}

#NavRail {{
    background: {p.layer_alt};
    border-right: 1px solid {p.divider};
}}

#NavBrand {{
    font-size: 15px;
    font-weight: 600;
    color: {p.text};
}}

#NavVersion {{
    font-size: 12px;
    color: {p.text_tertiary};
}}

QPushButton#NavItem {{
    text-align: left;
    padding: 9px 12px 9px 14px;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: {p.text_secondary};
    font-size: 14px;
}}

QPushButton#NavItem:hover {{
    background: {p.card_hover};
    color: {p.text};
}}

QPushButton#NavItem:checked {{
    background: {p.card};
    color: {p.text};
    font-weight: 600;
}}

#NavIndicator {{
    background: {p.accent};
    border-radius: 2px;
}}

#PageTitle {{
    font-size: 26px;
    font-weight: 600;
    color: {p.text};
}}

#PageSubtitle {{
    font-size: 13px;
    color: {p.text_secondary};
}}

#SectionTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {p.text};
}}

#Caption {{
    font-size: 12px;
    color: {p.text_tertiary};
}}

#Secondary {{
    color: {p.text_secondary};
}}

#MetricValue {{
    font-size: 28px;
    font-weight: 600;
    color: {p.text};
}}

#MetricLabel {{
    font-size: 12px;
    color: {p.text_tertiary};
}}

#Card {{
    background: {p.card};
    border: 1px solid {p.stroke};
    border-radius: 8px;
}}

#CardFlat {{
    background: {p.layer_alt};
    border: 1px solid {p.stroke};
    border-radius: 8px;
}}

#Divider {{
    background: {p.divider};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}

QLabel:disabled {{
    color: {p.text_disabled};
}}

QPushButton {{
    background: {p.control};
    border: 1px solid {p.stroke_strong};
    border-radius: 6px;
    padding: 7px 16px;
    color: {p.text};
    min-height: 18px;
}}

QPushButton:hover {{
    background: {p.control_hover};
}}

QPushButton:pressed {{
    background: {p.control_pressed};
    color: {p.text_secondary};
}}

QPushButton:disabled {{
    background: {p.control};
    border-color: {p.stroke};
    color: {p.text_disabled};
}}

QPushButton#Accent {{
    background: {p.accent};
    border: 1px solid {p.accent};
    color: {p.accent_text};
    font-weight: 600;
}}

QPushButton#Accent:hover {{
    background: {p.accent_hover};
    border-color: {p.accent_hover};
}}

QPushButton#Accent:pressed {{
    background: {p.accent_pressed};
    border-color: {p.accent_pressed};
}}

QPushButton#Accent:disabled {{
    background: {p.stroke};
    border-color: {p.stroke};
    color: {p.text_disabled};
}}

QPushButton#Subtle {{
    background: transparent;
    border: 1px solid transparent;
    color: {p.accent};
    padding: 6px 10px;
}}

QPushButton#Subtle:hover {{
    background: {p.card_hover};
}}

QPushButton#Danger {{
    color: {p.danger};
}}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {p.control};
    border: 1px solid {p.stroke_strong};
    border-bottom: 2px solid {p.stroke_strong};
    border-radius: 6px;
    padding: 6px 10px;
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
    min-height: 18px;
}}

QLineEdit:hover, QPlainTextEdit:hover, QSpinBox:hover, QComboBox:hover {{
    background: {p.control_hover};
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border-bottom: 2px solid {focus};
    background: {p.layer};
}}

QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {p.text_disabled};
    background: {p.layer_alt};
}}

QLineEdit[echoMode="2"] {{
    letter-spacing: 1px;
}}

QComboBox::drop-down {{
    border: none;
    width: 26px;
}}

/* No ::down-arrow rule on purpose: the style's own arrow reads better here
   than a border-triangle, which Qt renders as a blob at this size. */

QComboBox QAbstractItemView {{
    background: {p.card};
    border: 1px solid {p.stroke};
    border-radius: 6px;
    padding: 4px;
    selection-background-color: {p.selection};
    selection-color: {p.text};
    outline: none;
}}

QSpinBox::up-button, QSpinBox::down-button {{
    width: 20px;
    border: none;
    background: transparent;
}}

QCheckBox {{
    spacing: 10px;
    color: {p.text};
}}

QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid {p.stroke_strong};
    background: {p.control};
}}

QCheckBox::indicator:hover {{
    border-color: {p.text_tertiary};
}}

QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
}}

QCheckBox::indicator:disabled {{
    border-color: {p.stroke};
    background: {p.layer_alt};
}}

QRadioButton {{
    spacing: 10px;
}}

QRadioButton::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 9px;
    border: 1px solid {p.stroke_strong};
    background: {p.control};
}}

QRadioButton::indicator:checked {{
    /* A thick border renders as a squircle at this size, so the inner dot is a
       radial gradient instead and the ring stays a hairline. */
    border-radius: 9px;
    border: 1px solid {p.accent};
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 {p.accent}, stop:0.45 {p.accent}, stop:0.5 {p.control}, stop:1 {p.control});
}}

QRadioButton::indicator:hover {{
    border-color: {p.text_tertiary};
}}

QTreeView, QTableView, QListView {{
    background: {p.card};
    border: 1px solid {p.stroke};
    border-radius: 8px;
    alternate-background-color: {p.layer_alt};
    selection-background-color: {p.selection};
    selection-color: {p.text};
    outline: none;
    padding: 4px;
}}

QTreeView::item, QTableView::item, QListView::item {{
    padding: 7px 8px;
    border-radius: 5px;
}}

QTreeView::item:hover, QTableView::item:hover, QListView::item:hover {{
    background: {p.card_hover};
}}

QTreeView::item:selected, QTableView::item:selected, QListView::item:selected {{
    background: {p.selection};
    color: {p.text};
}}

QHeaderView::section {{
    background: transparent;
    border: none;
    border-bottom: 1px solid {p.divider};
    padding: 8px;
    color: {p.text_tertiary};
    font-size: 12px;
    font-weight: 600;
}}

QProgressBar {{
    background: {p.stroke};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}}

QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 3px;
}}

QScrollArea {{
    border: none;
    background: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 2px;
}}

QScrollBar::handle:vertical {{
    background: {scroll_handle};
    border-radius: 4px;
    min-height: 28px;
    margin: 0 3px;
}}

QScrollBar::handle:vertical:hover {{
    background: {p.text_tertiary};
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 2px;
}}

QScrollBar::handle:horizontal {{
    background: {scroll_handle};
    border-radius: 4px;
    min-width: 28px;
    margin: 3px 0;
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

QToolTip {{
    background: {p.card};
    color: {p.text};
    border: 1px solid {p.stroke};
    border-radius: 6px;
    padding: 6px 8px;
}}

#LogView {{
    font-family: {MONO_FONT_STACK};
    font-size: 12px;
    background: {p.layer_alt};
    border: 1px solid {p.stroke};
    border-radius: 8px;
    padding: 10px;
    color: {p.text_secondary};
}}

#InfoBar {{
    border-radius: 6px;
    padding: 10px 14px;
}}

#InfoBarSuccess {{
    background: {with_alpha(p.success, 0.14)};
    border: 1px solid {with_alpha(p.success, 0.45)};
}}

#InfoBarError {{
    background: {with_alpha(p.danger, 0.14)};
    border: 1px solid {with_alpha(p.danger, 0.45)};
}}

#InfoBarWarning {{
    background: {with_alpha(p.warning, 0.16)};
    border: 1px solid {with_alpha(p.warning, 0.45)};
}}

#InfoBarInfo {{
    background: {with_alpha(p.info, 0.14)};
    border: 1px solid {with_alpha(p.info, 0.40)};
}}
""".strip()


def qt_platform_hint() -> str:
    """Headless CI has no display; keep imports importable there."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return "offscreen"
    return ""
