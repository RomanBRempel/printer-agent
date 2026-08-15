"""Update feed: check the manifest, apply a release, toggle auto-update."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QSpinBox, QWidget

from ... import __version__
from ...updates import apply_update, check_for_update
from ..probe import TaskRunner
from ..theme import Palette
from ..widgets import Caption, Card, SectionTitle, ToggleSwitch, form_row, inline_row
from .base import Page


class UpdatesPage(Page):
    title = "Обновления"
    subtitle = "Канал обновлений агента и установка новой версии."
    glyph = "\uE896"  # Download (Segoe Fluent Icons)

    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(state, parent)
        self._runner: TaskRunner | None = None
        self._pending_manifest = None

        self._build_version_card()
        self._build_feed_card()
        self.content.addStretch(1)

        self.state.config_changed.connect(self.load_from_state)
        self.load_from_state()

    def _build_version_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Текущая версия"))
        self._version_label = QLabel(__version__)
        self._version_label.setObjectName("MetricValue")
        card.add(self._version_label)
        self._latest_label = Caption("Проверка ещё не выполнялась.")
        card.add(self._latest_label)

        self._check_button = QPushButton("Проверить обновления")
        self._check_button.setObjectName("Accent")
        self._apply_button = QPushButton("Установить обновление")
        self._apply_button.setEnabled(False)
        self._check_button.clicked.connect(self._check)
        self._apply_button.clicked.connect(self._apply)
        card.add(inline_row(self._check_button, self._apply_button))
        self.content.addWidget(card)

    def _build_feed_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Канал обновлений"))
        self.feed_edit = QLineEdit()
        self.feed_edit.setPlaceholderText("https://.../printer-agent-update.json")
        card.add(form_row("URL манифеста", self.feed_edit))

        self.auto_toggle = ToggleSwitch()
        self.startup_toggle = ToggleSwitch()
        auto_label = QLabel("Обновлять службу автоматически")
        auto_label.setObjectName("Secondary")
        startup_label = QLabel("Проверять обновления при старте")
        startup_label.setObjectName("Secondary")
        card.add(inline_row(self.auto_toggle, auto_label))
        card.add(inline_row(self.startup_toggle, startup_label))

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(0, 720)
        self.interval_spin.setSuffix(" ч")
        card.add(
            form_row(
                "Проверять раз в",
                self.interval_spin,
                hint="0 — проверять только при старте службы.",
            )
        )
        card.add(
            Caption(
                "Служба проверяет ленту сама и ставит новую версию, когда ни один файл не "
                "передаётся на принтер и никто не смотрит камеру: идущую печать перезапуск "
                "не трогает. После установки служба перезапускается сама."
            )
        )

        save = QPushButton("Сохранить")
        save.setObjectName("Accent")
        save.clicked.connect(self._save)
        card.add(inline_row(save))
        self.content.addWidget(card)

    # -- data --------------------------------------------------------------- #

    def load_from_state(self) -> None:
        updates = self.state.config.updates
        self.feed_edit.setText(updates.feed_url)
        self.auto_toggle.setChecked(updates.auto_update)
        self.startup_toggle.setChecked(updates.check_on_startup)
        self.interval_spin.setValue(updates.check_interval_h)

    def _save(self) -> None:
        config = self.state.config
        config.updates.feed_url = self.feed_edit.text().strip()
        config.updates.auto_update = self.auto_toggle.isChecked()
        config.updates.check_on_startup = self.startup_toggle.isChecked()
        config.updates.check_interval_h = self.interval_spin.value()
        self.state.set_config(config)
        saved, detail = self.state.persist()
        self.notify(detail, "success" if saved else "error")

    # -- actions ------------------------------------------------------------ #

    def _check(self) -> None:
        feed_url = self.feed_edit.text().strip()
        if not feed_url:
            self.notify("Укажите URL манифеста обновлений.", "warning")
            return
        self._check_button.setEnabled(False)
        self.notify("Запрашиваю манифест…", "info")
        self._runner = TaskRunner(lambda: check_for_update(feed_url), self)
        self._runner.completed.connect(self._check_done)
        self._runner.start()

    def _check_done(self, status, error: str) -> None:
        self._check_button.setEnabled(True)
        if error or status is None:
            self._latest_label.setText("Проверка не удалась.")
            self.notify(error or "Не удалось получить манифест.", "error")
            return
        self._latest_label.setText(f"Последняя доступная версия: {status.latest_version}")
        self._pending_manifest = status.manifest if status.update_available else None
        self._apply_button.setEnabled(bool(self._pending_manifest))
        self.notify(
            "Доступно обновление." if status.update_available else "Установлена последняя версия.",
            "warning" if status.update_available else "success",
        )

    def _apply(self) -> None:
        manifest = self._pending_manifest
        if manifest is None:
            return
        self._apply_button.setEnabled(False)
        self._check_button.setEnabled(False)
        self.notify("Устанавливаю обновление…", "info")
        self._runner = TaskRunner(lambda: apply_update(manifest), self)
        self._runner.completed.connect(self._apply_done)
        self._runner.start()

    def _apply_done(self, status, error: str) -> None:
        self._check_button.setEnabled(True)
        if error or status is None:
            self._apply_button.setEnabled(True)
            self.notify(error or "Установка не удалась.", "error")
            return
        if status.installed:
            self._pending_manifest = None
            self.notify(
                "Обновление установлено. Перезапустите приложение и службу, чтобы перейти на новую версию.",
                "success",
            )
        else:
            self._apply_button.setEnabled(True)
            self.notify(status.message, "error")

    def apply_palette(self, palette: Palette) -> None:
        self.auto_toggle.apply_palette(palette)
        self.startup_toggle.apply_palette(palette)
