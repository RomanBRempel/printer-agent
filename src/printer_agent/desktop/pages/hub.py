"""Connection to the RD Control hub: credentials, cadence, delivery queue.

Deliberately separate from the printers page. This one owns everything about the
link *upward*; nothing here knows what a printer is.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from ...config import AgentConfig, BackoffConfig, OutboxConfig, UpdateConfig
from ...uplink.diagnostics import check_hub
from ..checks import CheckRunner, CheckStepsView
from ..system import reveal
from ..theme import Palette
from ..widgets import Caption, Card, SectionTitle, form_row, inline_row
from .base import Page


def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    box.setAccelerated(True)
    return box


class HubPage(Page):
    title = "Хаб"
    subtitle = "Подключение агента к RD Control: доступ, интервалы и очередь доставки."
    glyph = "\uE753"  # Cloud (Segoe Fluent Icons)

    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(state, parent)
        self._runner: CheckRunner | None = None

        self._build_hub_card()
        self._build_check_card()
        self._build_timing_card()
        self._build_outbox_card()
        self._build_actions()
        self.content.addStretch(1)

        self.state.config_changed.connect(self.load_from_state)
        self.load_from_state()

    # -- construction ------------------------------------------------------- #

    def _build_hub_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Доступ к хабу"))
        self.hub_url_edit = QLineEdit()
        self.hub_url_edit.setPlaceholderText("wss://rd-control.example.com/ws/agent")
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText("loc-001")

        card.add(form_row("Адрес хаба", self.hub_url_edit, hint="Агент сам выводит из него wss://-адрес сессии."))

        token_row = QWidget()
        token_layout = QHBoxLayout(token_row)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.setSpacing(8)
        token_layout.addWidget(self.token_edit, 1)
        self._reveal_button = QPushButton("Показать")
        self._reveal_button.setCheckable(True)
        self._reveal_button.toggled.connect(self._toggle_token_visibility)
        token_layout.addWidget(self._reveal_button)
        card.add(form_row("Токен агента", token_row, hint="Хранится только в agent.yaml и не попадает в логи."))

        card.add(form_row("Ключ локации", self.location_edit))
        self.content.addWidget(card)

    def _build_check_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Проверка связи агент → хаб"))
        card.add(
            Caption(
                "Открывает отдельную короткую сессию с текущими значениями из полей выше "
                "и проходит рукопожатие hello. Служба при этом не затрагивается."
            )
        )
        self._check_button = QPushButton("Проверить связь")
        self._check_button.setObjectName("Accent")
        self._check_button.clicked.connect(self.run_check)
        card.add(inline_row(self._check_button))

        self._check_view = CheckStepsView()
        card.add(self._check_view)
        self.content.addWidget(card)

    def _build_timing_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Интервалы и переподключение"))
        self.telemetry_spin = _spin(1, 3600, 5)
        self.heartbeat_spin = _spin(1, 3600, 15)
        self.backoff_min_spin = _spin(1, 3600, 1)
        self.backoff_max_spin = _spin(1, 86400, 60)

        row_one = QWidget()
        layout_one = QHBoxLayout(row_one)
        layout_one.setContentsMargins(0, 0, 0, 0)
        layout_one.setSpacing(16)
        layout_one.addWidget(form_row("Телеметрия, с", self.telemetry_spin), 1)
        layout_one.addWidget(form_row("Heartbeat, с", self.heartbeat_spin), 1)
        card.add(row_one)

        row_two = QWidget()
        layout_two = QHBoxLayout(row_two)
        layout_two.setContentsMargins(0, 0, 0, 0)
        layout_two.setSpacing(16)
        layout_two.addWidget(form_row("Backoff min, с", self.backoff_min_spin), 1)
        layout_two.addWidget(form_row("Backoff max, с", self.backoff_max_spin), 1)
        card.add(row_two)
        card.add(Caption("Backoff применяется к переподключению к хабу: от минимума до максимума с удвоением."))
        self.content.addWidget(card)

    def _build_outbox_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Локальная очередь событий"))
        self.outbox_edit = QLineEdit()
        self.outbox_max_spin = _spin(1, 1_000_000, 5000)

        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(8)
        path_layout.addWidget(self.outbox_edit, 1)
        open_button = QPushButton("Открыть папку")
        open_button.clicked.connect(lambda: reveal(Path(self.outbox_edit.text().strip() or ".").parent))
        path_layout.addWidget(open_button)

        card.add(form_row("Файл SQLite", path_row, hint="События переживают перезапуск службы и подтверждаются хабом."))
        card.add(form_row("Максимум событий", self.outbox_max_spin))
        self.content.addWidget(card)

    def _build_actions(self) -> None:
        save = QPushButton("Сохранить")
        save.setObjectName("Accent")
        reload_button = QPushButton("Перечитать с диска")
        open_config = QPushButton("Показать файл")
        save.clicked.connect(self.save)
        reload_button.clicked.connect(self._reload)
        open_config.clicked.connect(lambda: reveal(self.state.config_path))
        self.content.addWidget(inline_row(save, reload_button, open_config))

    # -- data --------------------------------------------------------------- #

    def load_from_state(self) -> None:
        config = self.state.config
        self.hub_url_edit.setText(config.hub_url)
        self.token_edit.setText(config.agent_token)
        self.location_edit.setText(config.location_key)
        self.telemetry_spin.setValue(config.telemetry_interval_s)
        self.heartbeat_spin.setValue(config.heartbeat_interval_s)
        self.backoff_min_spin.setValue(config.command_reconnect_backoff_s.min_s)
        self.backoff_max_spin.setValue(config.command_reconnect_backoff_s.max_s)
        self.outbox_edit.setText(str(config.outbox.database_path))
        self.outbox_max_spin.setValue(config.outbox.max_events)
        if self.state.load_error:
            self.notify(self.state.load_error, "error")

    def collect(self) -> AgentConfig:
        config = self.state.config
        return AgentConfig(
            hub_url=self.hub_url_edit.text().strip(),
            agent_token=self.token_edit.text().strip(),
            location_key=self.location_edit.text().strip(),
            telemetry_interval_s=self.telemetry_spin.value(),
            heartbeat_interval_s=self.heartbeat_spin.value(),
            command_reconnect_backoff_s=BackoffConfig(
                min_s=self.backoff_min_spin.value(), max_s=self.backoff_max_spin.value()
            ),
            outbox=OutboxConfig(
                database_path=Path(self.outbox_edit.text().strip() or "data/outbox.sqlite3"),
                max_events=self.outbox_max_spin.value(),
            ),
            # Carried over field by field: this page does not edit the update
            # channel, and rebuilding the block from three of its four fields
            # would quietly reset the fourth to its default on every save.
            updates=UpdateConfig(
                feed_url=config.updates.feed_url,
                auto_update=config.updates.auto_update,
                check_on_startup=config.updates.check_on_startup,
                check_interval_h=config.updates.check_interval_h,
            ),
            printers=list(config.printers),
        )

    def save(self) -> None:
        self.state.set_config(self.collect())
        errors = self.state.validation_errors
        saved, detail = self.state.persist()
        if not saved:
            self.notify(detail, "error")
            return
        if errors:
            self.notify(
                f"{detail}. Служба пока не примет конфигурацию: " + "; ".join(errors), "warning"
            )
        else:
            self.notify(detail, "success")

    def _reload(self) -> None:
        self.state.reload_config()
        self.notify("Конфигурация перечитана с диска.", "info")

    def _toggle_token_visibility(self, visible: bool) -> None:
        self.token_edit.setEchoMode(QLineEdit.Normal if visible else QLineEdit.Password)
        self._reveal_button.setText("Скрыть" if visible else "Показать")

    # -- connectivity check -------------------------------------------------- #

    def run_check(self) -> None:
        # Checks the form, not the saved file: the point is to try a value
        # before committing it.
        config = self.collect()
        self._check_button.setEnabled(False)
        self._check_view.show_pending("Проверяю связь с хабом…")
        self.info_bar.hide_message()

        self._runner = CheckRunner(lambda: check_hub(config), self)
        self._runner.completed.connect(self._check_done)
        self._runner.start()

    def _check_done(self, result) -> None:
        self._check_button.setEnabled(True)
        self._check_view.show_result(result)
        self.notify(result.summary, "success" if result.ok else "error")

    def apply_palette(self, palette: Palette) -> None:
        self._check_view.apply_palette(palette)
