"""Main window: navigation rail, page stack, and the background workers."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from .pages import (
    AppearancePage,
    DashboardPage,
    HubPage,
    LogsPage,
    Page,
    PrintersPage,
    UpdatesPage,
)
from .probe import HealthSnapshot, HealthWatcher, PrinterProbe, ProbeResult
from .state import AppState
from .theme import Palette, apply_window_backdrop, build_stylesheet, system_accent, system_prefers_dark
from .widgets import NavItem

SYSTEM_THEME_POLL_MS = 4000


class MainWindow(QMainWindow):
    def __init__(self, state: AppState, icon: QIcon | None = None):
        super().__init__()
        self.state = state
        self._probe: PrinterProbe | None = None
        self._probe_signature: tuple = ()
        self._system_theme = (system_prefers_dark(), system_accent())

        self.setWindowTitle("Printer Agent")
        self.resize(state.preferences.window_width, state.preferences.window_height)
        self.setMinimumSize(960, 640)
        if icon is not None:
            self.setWindowIcon(icon)

        self._build_layout()
        self._build_pages()
        self._start_health_watcher()
        self._sync_probe()

        self.state.palette_changed.connect(self._apply_palette)
        self._apply_palette(self.state.palette)

        # Windows can flip light/dark or change the accent while we are open.
        self._theme_timer = QTimer(self)
        self._theme_timer.setInterval(SYSTEM_THEME_POLL_MS)
        self._theme_timer.timeout.connect(self._poll_system_theme)
        self._theme_timer.start()

    # -- layout ------------------------------------------------------------- #

    def _build_layout(self) -> None:
        root = QWidget(self)
        root.setObjectName("RootSurface")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._nav = QWidget(root)
        self._nav.setObjectName("NavRail")
        self._nav.setFixedWidth(232)
        nav_layout = QVBoxLayout(self._nav)
        nav_layout.setContentsMargins(12, 20, 12, 16)
        nav_layout.setSpacing(4)

        brand = QLabel("Printer Agent")
        brand.setObjectName("NavBrand")
        version = QLabel(f"версия {__version__}")
        version.setObjectName("NavVersion")
        nav_layout.addWidget(brand)
        nav_layout.addWidget(version)
        nav_layout.addSpacing(18)
        self._nav_layout = nav_layout

        self._stack = QStackedWidget(root)
        layout.addWidget(self._nav)
        layout.addWidget(self._stack, 1)
        self.setCentralWidget(root)

    def _build_pages(self) -> None:
        self.dashboard = DashboardPage(self.state)
        self.hub = HubPage(self.state)
        self.printers = PrintersPage(self.state)
        self.updates = UpdatesPage(self.state)
        self.logs = LogsPage(self.state)
        self.appearance = AppearancePage(self.state)

        # Hub before printers: the link upward is configured first, and the two
        # are separate pages so neither hides inside a generic "settings" screen.
        self._pages: list[Page] = [
            self.dashboard,
            self.hub,
            self.printers,
            self.updates,
            self.logs,
            self.appearance,
        ]
        self._nav_items: list[NavItem] = []
        for index, page in enumerate(self._pages):
            self._stack.addWidget(page)
            item = NavItem(page.title, page.glyph, self._nav)
            item.clicked.connect(lambda _=False, position=index: self._select(position))
            self._nav_layout.addWidget(item)
            self._nav_items.append(item)
        self._nav_layout.addStretch(1)

        self.printers.bind(on_refresh=self._refresh_probe, on_live_toggle=self._live_toggled)
        self.appearance.bind(on_live_changed=self._live_toggled)
        self.state.config_changed.connect(self._sync_probe)
        self._select(0)

    def _select(self, index: int) -> None:
        for position, item in enumerate(self._nav_items):
            item.setChecked(position == index)
        self._stack.setCurrentIndex(index)
        self._pages[index].on_shown()

    # -- theme -------------------------------------------------------------- #

    def _apply_palette(self, palette: Palette) -> None:
        self.setStyleSheet(build_stylesheet(palette))
        apply_window_backdrop(int(self.winId()), palette.dark)
        for item in self._nav_items:
            item.apply_palette(palette)
        for page in self._pages:
            page.apply_palette(palette)

    def _poll_system_theme(self) -> None:
        current = (system_prefers_dark(), system_accent())
        if current == self._system_theme:
            return
        self._system_theme = current
        follows_system = (
            self.state.preferences.theme_mode == "system" or self.state.preferences.accent == "system"
        )
        if follows_system:
            self.state.refresh_palette()

    # -- workers ------------------------------------------------------------ #

    def _start_health_watcher(self) -> None:
        self._health = HealthWatcher(self._outbox_path(), parent=self)
        self._health.updated.connect(self._on_health)
        self._health.start()

    def _outbox_path(self) -> Path | None:
        path = self.state.config.outbox.database_path
        return Path(path) if path else None

    def _on_health(self, health: HealthSnapshot) -> None:
        self.state.set_service(health.service)
        self.dashboard.apply_health(health)

    def _printer_signature(self) -> tuple:
        return tuple(
            (printer.key, printer.brand, printer.host, printer.port)
            for printer in self.state.config.printers
        )

    def _sync_probe(self) -> None:
        self._health.set_outbox_path(self._outbox_path())

        signature = self._printer_signature()
        wanted = bool(self.state.preferences.live_status and signature)
        if not wanted:
            self._stop_probe()
            self._probe_signature = ()
            return
        if self._probe is not None and signature == self._probe_signature:
            self._probe.set_interval(self.state.preferences.poll_interval_s)
            return

        self._stop_probe()
        self._probe_signature = signature
        self._probe = PrinterProbe(
            list(self.state.config.printers), self.state.preferences.poll_interval_s, parent=self
        )
        self._probe.result_ready.connect(self._on_probe_result)
        self._probe.start()

    def _stop_probe(self) -> None:
        if self._probe is None:
            return
        probe, self._probe = self._probe, None
        probe.request_stop()
        # Bambu's disconnect can take a moment; do not block the UI on it.
        if not probe.wait(4000):
            probe.terminate()

    def _on_probe_result(self, result: ProbeResult) -> None:
        self.printers.apply_probe_result(result)

    def _refresh_probe(self) -> None:
        if self._probe is None:
            self._sync_probe()
        else:
            self._probe.request_refresh()

    def _live_toggled(self, enabled: bool) -> None:
        self.printers.set_live_status(enabled)
        self.appearance.set_live_status(enabled)
        self._sync_probe()

    # -- lifecycle ---------------------------------------------------------- #

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.state.preferences.window_width = self.width()
        self.state.preferences.window_height = self.height()
        self.state.save_preferences()
        self._theme_timer.stop()
        self._stop_probe()
        self._health.request_stop()
        self._health.wait(3000)
        super().closeEvent(event)
