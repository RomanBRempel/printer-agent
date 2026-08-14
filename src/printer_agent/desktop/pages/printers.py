"""Printer inventory with live status pulled from the adapters."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...config import PrinterConfig
from ...uplink.diagnostics import check_printer
from ..checks import CheckRunner, CheckStepsView
from ..probe import ProbeResult
from ..theme import Palette, status_color
from ..widgets import Caption, Card, SectionTitle, StatusPill, ToggleSwitch
from .base import Page
from .discovery_dialog import DiscoveryDialog
from .printer_dialog import DEFAULT_PORTS, PrinterDialog

STATUS_LABELS = {
    "idle": "Ожидание",
    "printing": "Печатает",
    "paused": "Пауза",
    "finished": "Завершено",
    "error": "Ошибка",
    "offline": "Не в сети",
    "maintenance": "Обслуживание",
}

BRAND_LABELS = {"moonraker": "Moonraker", "bambu": "Bambu Lab"}


def format_duration(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    return f"{minutes} мин"


def format_temperature(current: float | None, target: float | None) -> str:
    if current is None:
        return "—"
    if target:
        return f"{current:.0f} → {target:.0f} °C"
    return f"{current:.0f} °C"


class PrinterCard(Card):
    """One printer: identity, live status, current job, temperatures."""

    def __init__(self, printer: PrinterConfig, parent: QWidget | None = None):
        super().__init__(parent, padding=18, spacing=10)
        self.printer_key = printer.key

        header = QHBoxLayout()
        header.setSpacing(12)
        identity = QVBoxLayout()
        identity.setSpacing(2)
        self._name = QLabel(printer.key or "без ключа")
        self._name.setObjectName("SectionTitle")
        port = printer.port or DEFAULT_PORTS.get(printer.brand, "")
        self._address = Caption(
            f"{BRAND_LABELS.get(printer.brand, printer.brand)} · {printer.host or '—'}:{port}"
        )
        identity.addWidget(self._name)
        identity.addWidget(self._address)
        holder = QWidget()
        holder.setLayout(identity)
        header.addWidget(holder, 1)

        self._status = StatusPill("Опрос…", "#8A8A8A")
        self._status.set_bold(True)
        header.addWidget(self._status, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.add_layout(header)

        self._job_label = Caption("")
        self._job_label.setVisible(False)
        self.add(self._job_label)

        self._progress = QProgressBar()
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        self.add(self._progress)

        self._temps = Caption("")
        self.add(self._temps)

    def update_config(self, printer: PrinterConfig) -> None:
        self._name.setText(printer.key or "без ключа")
        port = printer.port or DEFAULT_PORTS.get(printer.brand, "")
        self._address.setText(
            f"{BRAND_LABELS.get(printer.brand, printer.brand)} · {printer.host or '—'}:{port}"
        )

    def set_idle_state(self, palette: Palette, text: str) -> None:
        self._status.set_status(text, palette.text_tertiary)
        self._job_label.setVisible(False)
        self._progress.setVisible(False)
        self._temps.setText("")

    def apply_result(self, result: ProbeResult, palette: Palette) -> None:
        if result.snapshot is None:
            self._status.set_status("Недоступен", palette.danger)
            self._job_label.setVisible(True)
            self._job_label.setText(result.error or "Принтер не ответил.")
            self._progress.setVisible(False)
            self._temps.setText("")
            return

        snapshot = result.snapshot
        status_value = str(snapshot.status)
        self._status.set_status(
            STATUS_LABELS.get(status_value, status_value), status_color(palette, status_value)
        )

        job = snapshot.job
        active = job.name or job.progress_pct is not None
        self._job_label.setVisible(bool(active))
        self._progress.setVisible(job.progress_pct is not None)
        if job.progress_pct is not None:
            self._progress.setValue(int(round(job.progress_pct)))
        if active:
            parts = [job.name or "задание"]
            if job.progress_pct is not None:
                parts.append(f"{job.progress_pct:.0f}%")
            if job.layer is not None and job.layers_total:
                parts.append(f"слой {job.layer}/{job.layers_total}")
            if job.time_remaining_s is not None:
                parts.append(f"осталось {format_duration(job.time_remaining_s)}")
            self._job_label.setText("  ·  ".join(parts))

        temps = snapshot.temps
        pieces = [
            f"Сопло {format_temperature(temps.nozzle, temps.nozzle_target)}",
            f"Стол {format_temperature(temps.bed, temps.bed_target)}",
        ]
        if temps.chamber is not None:
            pieces.append(f"Камера {temps.chamber:.0f} °C")
        if snapshot.error and snapshot.error.message:
            pieces.append(snapshot.error.message)
        self._temps.setText("   ".join(pieces))


class PrintersPage(Page):
    title = "Принтеры"
    subtitle = "Список принтеров локации и их текущее состояние."
    glyph = "\uE749"  # Print (Segoe Fluent Icons)

    #: Emitted upward by the main window when the probe delivers a result.
    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(state, parent)
        self._cards: dict[str, PrinterCard] = {}
        self._results: dict[str, ProbeResult] = {}
        self._on_live_toggle = None
        self._on_refresh = None
        self._check_runner: CheckRunner | None = None

        self._build_toolbar()
        self._list_holder = QWidget()
        self._list_layout = QVBoxLayout(self._list_holder)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(12)
        self.content.addWidget(self._list_holder)

        self._empty = Card(flat=True)
        self._empty.add(SectionTitle("Принтеров пока нет"))
        self._empty.add(
            Caption(
                "Добавьте первый принтер — служба не запустится, пока список пуст. "
                "Для Moonraker нужен хост и порт, для Bambu Lab — ещё access code и серийный номер."
            )
        )
        self.content.addWidget(self._empty)
        self.content.addStretch(1)

        self.state.config_changed.connect(self.rebuild)
        self.rebuild()

    def bind(self, on_refresh, on_live_toggle) -> None:
        self._on_refresh = on_refresh
        self._on_live_toggle = on_live_toggle

    def set_live_status(self, enabled: bool) -> None:
        """Reflect a change made elsewhere without echoing it back."""
        self._live_toggle.blockSignals(True)
        self._live_toggle.setChecked(enabled)
        self._live_toggle.blockSignals(False)
        if not enabled:
            self._results.clear()
            for card in self._cards.values():
                card.set_idle_state(self.state.palette, "Живой статус выключен")

    # -- toolbar ------------------------------------------------------------ #

    def _build_toolbar(self) -> None:
        # Two rows on purpose: six controls plus the toggle do not fit the
        # minimum window width, and a clipped toolbar hides the actions that
        # matter most.
        discover_button = QPushButton("Найти в сети")
        discover_button.setObjectName("Accent")
        add_button = QPushButton("Добавить вручную")
        self._edit_button = QPushButton("Изменить")
        self._remove_button = QPushButton("Удалить")
        self._remove_button.setObjectName("Danger")
        self._check_button = QPushButton("Проверить связь")
        refresh_button = QPushButton("Обновить статус")

        primary = QHBoxLayout()
        primary.setContentsMargins(0, 0, 0, 0)
        primary.setSpacing(10)
        for button in (discover_button, add_button, self._edit_button, self._remove_button):
            primary.addWidget(button)
        primary.addStretch(1)

        secondary = QHBoxLayout()
        secondary.setContentsMargins(0, 0, 0, 0)
        secondary.setSpacing(10)
        secondary.addWidget(self._check_button)
        secondary.addWidget(refresh_button)
        secondary.addStretch(1)

        live_label = QLabel("Живой статус")
        live_label.setObjectName("Secondary")
        self._live_toggle = ToggleSwitch()
        self._live_toggle.setChecked(self.state.preferences.live_status)
        secondary.addWidget(live_label)
        secondary.addWidget(self._live_toggle)

        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(10)
        rows.addLayout(primary)
        rows.addLayout(secondary)
        self.content.addWidget(holder)

        discover_button.clicked.connect(self._discover_printers)
        add_button.clicked.connect(self._add_printer)
        self._edit_button.clicked.connect(self._edit_selected)
        self._remove_button.clicked.connect(self._remove_selected)
        self._check_button.clicked.connect(self._check_selected)
        refresh_button.clicked.connect(lambda: self._on_refresh and self._on_refresh())
        self._live_toggle.toggled.connect(self._toggle_live)

        self._selected_key: str | None = None

        self._check_card = Card()
        self._check_title = SectionTitle("Проверка связи агент → принтер")
        self._check_card.add(self._check_title)
        self._check_view = CheckStepsView()
        self._check_card.add(self._check_view)
        self._check_card.setVisible(False)
        self.content.addWidget(self._check_card)

    # -- list --------------------------------------------------------------- #

    def rebuild(self) -> None:
        printers = self.state.config.printers
        keys = {printer.key for printer in printers}

        for key in list(self._cards):
            if key not in keys:
                card = self._cards.pop(key)
                self._list_layout.removeWidget(card)
                card.deleteLater()

        for index, printer in enumerate(printers):
            card = self._cards.get(printer.key)
            if card is None:
                card = PrinterCard(printer)
                card.mousePressEvent = self._make_selector(printer.key, card)
                self._cards[printer.key] = card
            else:
                card.update_config(printer)
            self._list_layout.insertWidget(index, card)
            result = self._results.get(printer.key)
            if result is not None:
                card.apply_result(result, self.state.palette)
            elif not self.state.preferences.live_status:
                card.set_idle_state(self.state.palette, "Живой статус выключен")

        self._empty.setVisible(not printers)
        if self._selected_key not in keys:
            self._selected_key = printers[0].key if printers else None
        self._sync_selection()

    def _make_selector(self, key: str, card: PrinterCard):
        def handler(event) -> None:
            self._selected_key = key
            self._sync_selection()
            QWidget.mousePressEvent(card, event)

        return handler

    def _sync_selection(self) -> None:
        has_selection = self._selected_key is not None
        self._edit_button.setEnabled(has_selection)
        self._remove_button.setEnabled(has_selection)
        self._check_button.setEnabled(has_selection)
        palette = self.state.palette
        for key, card in self._cards.items():
            selected = key == self._selected_key
            card.setStyleSheet(
                f"#Card {{ border: 1px solid {palette.accent}; }}" if selected else ""
            )

    # -- editing ------------------------------------------------------------ #

    def _selected_index(self) -> int | None:
        for index, printer in enumerate(self.state.config.printers):
            if printer.key == self._selected_key:
                return index
        return None

    def _discover_printers(self) -> None:
        printers = list(self.state.config.printers)
        dialog = DiscoveryDialog(
            self,
            existing_keys={item.key for item in printers},
            existing_hosts={item.host for item in printers if item.host},
            palette=self.state.palette,
        )
        if dialog.exec() != DiscoveryDialog.Accepted or not dialog.result_printers:
            return
        printers.extend(dialog.result_printers)
        found = ", ".join(item.key for item in dialog.result_printers)
        self._commit(printers, f"Добавлено из сети: {found}.")

    def _check_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            self.notify("Выберите принтер в списке.", "warning")
            return
        printer = self.state.config.printers[index]
        self._check_button.setEnabled(False)
        self._check_title.setText(f"Проверка связи агент → {printer.key}")
        self._check_card.setVisible(True)
        self._check_view.apply_palette(self.state.palette)
        self._check_view.show_pending(f"Проверяю {printer.host}…")

        self._check_runner = CheckRunner(lambda: check_printer(printer), self)
        self._check_runner.completed.connect(self._check_done)
        self._check_runner.start()

    def _check_done(self, result) -> None:
        self._check_button.setEnabled(True)
        self._check_view.show_result(result)
        self.notify(result.summary, "success" if result.ok else "error")

    def _add_printer(self) -> None:
        dialog = PrinterDialog(self)
        if dialog.exec() != PrinterDialog.Accepted or dialog.result_printer is None:
            return
        printers = list(self.state.config.printers)
        if any(item.key == dialog.result_printer.key for item in printers):
            self.notify(f"Принтер с ключом «{dialog.result_printer.key}» уже есть.", "error")
            return
        printers.append(dialog.result_printer)
        self._commit(printers, f"Принтер «{dialog.result_printer.key}» добавлен.")

    def _edit_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        printers = list(self.state.config.printers)
        dialog = PrinterDialog(self, printers[index])
        if dialog.exec() != PrinterDialog.Accepted or dialog.result_printer is None:
            return
        printers[index] = dialog.result_printer
        self._selected_key = dialog.result_printer.key
        self._commit(printers, f"Принтер «{dialog.result_printer.key}» обновлён.")

    def _remove_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        printers = list(self.state.config.printers)
        removed = printers.pop(index)
        self._results.pop(removed.key, None)
        self._selected_key = None
        self._commit(printers, f"Принтер «{removed.key}» удалён.")

    def _commit(self, printers: list[PrinterConfig], message: str) -> None:
        config = self.state.config
        config.printers = printers
        self.state.set_config(config)
        saved, detail = self.state.persist()
        self.notify(f"{message} {detail}" if saved else detail, "success" if saved else "error")

    def _toggle_live(self, enabled: bool) -> None:
        self.state.preferences.live_status = enabled
        self.state.save_preferences()
        if self._on_live_toggle is not None:
            self._on_live_toggle(enabled)
        if not enabled:
            self._results.clear()
            for card in self._cards.values():
                card.set_idle_state(self.state.palette, "Живой статус выключен")

    # -- probe feed --------------------------------------------------------- #

    def apply_probe_result(self, result: ProbeResult) -> None:
        self._results[result.printer_key] = result
        card = self._cards.get(result.printer_key)
        if card is not None:
            card.apply_result(result, self.state.palette)

    def apply_palette(self, palette: Palette) -> None:
        self._live_toggle.apply_palette(palette)
        self._check_view.apply_palette(palette)
        for key, card in self._cards.items():
            result = self._results.get(key)
            if result is not None:
                card.apply_result(result, palette)
        self._sync_selection()
