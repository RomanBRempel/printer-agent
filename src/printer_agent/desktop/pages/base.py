"""Common page chrome: title block, inline message bar, scrolling content."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from ..state import AppState
from ..theme import Palette
from ..widgets import InfoBar


class Page(QWidget):
    #: Overridden by subclasses; drives the nav rail and the page header.
    title = ""
    subtitle = ""
    glyph = ""

    def __init__(self, state: AppState, parent: QWidget | None = None):
        super().__init__(parent)
        self.state = state

        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 30, 36, 24)
        outer.setSpacing(16)

        heading = QLabel(self.title, self)
        heading.setObjectName("PageTitle")
        outer.addWidget(heading)

        if self.subtitle:
            caption = QLabel(self.subtitle, self)
            caption.setObjectName("PageSubtitle")
            caption.setWordWrap(True)
            outer.addWidget(caption)

        self.info_bar = InfoBar(self)
        outer.addWidget(self.info_bar)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget(scroll)
        self.content = QVBoxLayout(holder)
        self.content.setContentsMargins(0, 4, 8, 8)
        self.content.setSpacing(16)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

    # -- hooks -------------------------------------------------------------- #

    def apply_palette(self, palette: Palette) -> None:
        """Repaint anything QSS cannot reach. Subclasses extend, not replace."""

    def on_shown(self) -> None:
        """Called each time the page becomes visible."""

    def notify(self, message: str, severity: str = "info") -> None:
        self.info_bar.show_message(message, severity)
