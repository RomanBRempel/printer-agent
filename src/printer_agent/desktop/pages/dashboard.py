"""Overview page: service state, outbox counters, hub wiring, config health."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QWidget

from ... import __version__
from ..probe import HealthSnapshot, TaskRunner
from ..system import agent_log_path, control_service, is_admin, reveal
from ..theme import Palette, status_color
from ..widgets import Caption, Card, Divider, MetricTile, SectionTitle, StatusPill
from .base import Page


class DashboardPage(Page):
    title = "Обзор"
    subtitle = "Состояние службы, очередь событий и связь с хабом."
    glyph = "\uE80F"  # Home (Segoe Fluent Icons)

    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(state, parent)
        self._runner: TaskRunner | None = None
        self._health = HealthSnapshot()

        self._build_service_card()
        self._build_metrics()
        self._build_hub_card()
        self._build_health_card()
        self.content.addStretch(1)

        self.state.config_changed.connect(self.refresh)
        self.refresh()

    # -- construction ------------------------------------------------------- #

    def _build_service_card(self) -> None:
        card = Card()
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(SectionTitle("Служба Windows"), 1)
        self._service_pill = StatusPill("Проверка…", "#8A8A8A")
        self._service_pill.set_bold(True)
        header.addWidget(self._service_pill, 0, Qt.AlignRight)
        card.add_layout(header)

        self._service_detail = Caption("")
        card.add(self._service_detail)
        card.add(Divider())

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self._start_button = QPushButton("Запустить")
        self._start_button.setObjectName("Accent")
        self._stop_button = QPushButton("Остановить")
        self._restart_button = QPushButton("Перезапустить")
        logs_button = QPushButton("Открыть логи")
        for button in (self._start_button, self._stop_button, self._restart_button, logs_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        holder = QWidget()
        holder.setLayout(buttons)
        card.add(holder)

        self._start_button.clicked.connect(lambda: self._service_action("start"))
        self._stop_button.clicked.connect(lambda: self._service_action("stop"))
        self._restart_button.clicked.connect(lambda: self._service_action("restart"))
        logs_button.clicked.connect(lambda: reveal(agent_log_path()))

        self._elevation_hint = Caption(
            "" if is_admin() else "Управление службой запросит права администратора."
        )
        card.add(self._elevation_hint)
        self.content.addWidget(card)

    def _build_metrics(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(16)
        self._printers_tile = MetricTile("Принтеров настроено")
        self._pending_tile = MetricTile("Событий в очереди")
        self._results_tile = MetricTile("Результатов команд")
        self._version_tile = MetricTile("Версия агента", __version__)
        for tile in (self._printers_tile, self._pending_tile, self._results_tile, self._version_tile):
            row.addWidget(tile, 1)
        holder = QWidget()
        holder.setLayout(row)
        self.content.addWidget(holder)

    def _build_hub_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Подключение к хабу"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        self._hub_values: dict[str, QLabel] = {}
        rows = [
            ("hub_url", "Адрес хаба"),
            ("location_key", "Ключ локации"),
            ("intervals", "Телеметрия / heartbeat"),
            ("outbox", "Файл outbox"),
            ("config", "Файл конфигурации"),
        ]
        for index, (key, label) in enumerate(rows):
            caption = QLabel(label)
            caption.setObjectName("Secondary")
            value = QLabel("—")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            grid.addWidget(caption, index, 0, Qt.AlignTop)
            grid.addWidget(value, index, 1)
            self._hub_values[key] = value
        holder = QWidget()
        holder.setLayout(grid)
        card.add(holder)
        self.content.addWidget(card)

    def _build_health_card(self) -> None:
        self._health_card = Card()
        self._health_card.add(SectionTitle("Проверка конфигурации"))
        self._health_status = StatusPill("—", "#8A8A8A")
        self._health_card.add(self._health_status)
        self._health_details = Caption("")
        self._health_card.add(self._health_details)
        self.content.addWidget(self._health_card)

    # -- updates ------------------------------------------------------------ #

    def refresh(self) -> None:
        config = self.state.config
        self._printers_tile.set_value(str(len(config.printers)))
        self._hub_values["hub_url"].setText(config.hub_url or "не задан")
        self._hub_values["location_key"].setText(config.location_key or "не задан")
        self._hub_values["intervals"].setText(
            f"{config.telemetry_interval_s} с / {config.heartbeat_interval_s} с"
        )
        self._hub_values["outbox"].setText(str(config.outbox.database_path))
        self._hub_values["config"].setText(str(self.state.config_path))

        palette = self.state.palette
        if self.state.load_error:
            self._health_status.set_status("Файл не прочитан", palette.danger)
            self._health_details.setText(self.state.load_error)
            return
        errors = self.state.validation_errors
        if errors:
            self._health_status.set_status(f"Найдено проблем: {len(errors)}", palette.warning)
            self._health_details.setText("• " + "\n• ".join(errors))
        else:
            self._health_status.set_status("Конфигурация корректна", palette.success)
            self._health_details.setText("Служба может стартовать с этими настройками.")

    def apply_health(self, health: HealthSnapshot) -> None:
        self._health = health
        palette = self.state.palette
        service = health.service
        self._service_pill.set_status(service.label, status_color(palette, service.state))
        if service.state == "not_installed":
            self._service_detail.setText(
                "Служба не зарегистрирована. Запустите установщик или "
                "«printer-agent install-service» от имени администратора."
            )
        elif service.detail:
            self._service_detail.setText(service.detail)
        else:
            self._service_detail.setText("Имя службы: printer-agent")

        installed = service.installed
        self._start_button.setEnabled(installed and not service.running)
        self._stop_button.setEnabled(installed and service.running)
        self._restart_button.setEnabled(installed)

        self._pending_tile.set_value("—" if health.pending_events is None else str(health.pending_events))
        self._results_tile.set_value("—" if health.command_results is None else str(health.command_results))

    def apply_palette(self, palette: Palette) -> None:
        self.refresh()
        self.apply_health(self._health)

    # -- actions ------------------------------------------------------------ #

    def _service_action(self, action: str) -> None:
        if action in {"start", "restart"}:
            # The service validates the config on startup and exits. Letting it
            # try anyway answers a question the operator did not ask, with an
            # SCM error that names nothing actionable.
            blockers = self.state.validation_errors
            if blockers:
                self.notify(
                    "Служба не запустится с текущей конфигурацией: "
                    + "; ".join(blockers)
                    + ". Заполните страницы «Хаб» и «Принтеры», затем повторите.",
                    "warning",
                )
                return

        for button in (self._start_button, self._stop_button, self._restart_button):
            button.setEnabled(False)
        self.notify({"start": "Запуск службы…", "stop": "Остановка службы…", "restart": "Перезапуск службы…"}[action])

        self._runner = TaskRunner(lambda: control_service(action), self)
        self._runner.completed.connect(lambda result, error: self._service_action_done(result, error))
        self._runner.start()

    def _service_action_done(self, result, error: str) -> None:
        if error:
            self.notify(error, "error")
        elif result is not None:
            ok, message = result
            if ok:
                self.notify("Команда отправлена службе.", "success")
            else:
                self.notify(f"{message} Подробности — на странице «Логи».", "error")
        self.apply_health(self._health)
