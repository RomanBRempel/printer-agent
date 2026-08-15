"""Carry an agent's settings to another installation.

The page is a thin surface over :mod:`printer_agent.settings_bundle`; the rules
about what travels and what stays on the machine live there, not here.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from ...settings_bundle import (
    MODE_FULL,
    MODE_PRINTERS,
    BundleError,
    BundleInfo,
    TransferReport,
    apply_bundle,
    build_bundle,
    describe_bundle,
    read_bundle,
    write_bundle,
)
from ..theme import Palette
from ..widgets import Caption, Card, SectionTitle, ToggleSwitch, form_row, inline_row
from .base import Page

DEFAULT_BUNDLE_NAME = "printer-agent-settings.yaml"
BUNDLE_FILTER = "Настройки агента (*.yaml *.yml);;Все файлы (*)"

#: Config field names as the transfer report emits them.
FIELD_LABELS = {
    "hub_url": "адрес хаба",
    "location_key": "ключ локации",
    "agent_token": "токен агента",
    "telemetry_interval_s": "интервал телеметрии",
    "heartbeat_interval_s": "интервал heartbeat",
    "command_reconnect_backoff_s": "backoff переподключения",
    "outbox.max_events": "максимум событий в очереди",
    "outbox.database_path": "путь к файлу очереди",
    "updates": "настройки обновлений",
}


def field_label(entry: str) -> str:
    """Turn a report entry into something an operator reads without decoding."""
    if entry.startswith("printers[") and entry.endswith("]"):
        return f"принтеры ({entry[len('printers['):-1]})"
    parts = entry.split(".")
    if len(parts) == 4 and parts[0] == "printers" and parts[2] == "credentials":
        return f"{parts[3]} принтера «{parts[1]}»"
    return FIELD_LABELS.get(entry, entry)


class TransferPage(Page):
    title = "Перенос настроек"
    subtitle = "Выгрузка настроек этого агента в файл и загрузка их на другой машине."
    glyph = ""  # Share (Segoe Fluent Icons)

    def __init__(self, state, parent: QWidget | None = None):
        super().__init__(state, parent)

        self._build_export_card()
        self._build_import_card()
        self._build_report_card()
        self.content.addStretch(1)

    # -- construction ------------------------------------------------------- #

    def _build_export_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Выгрузить настройки"))
        card.add(
            Caption(
                "В файл попадают адрес хаба, ключ локации, интервалы, настройки обновлений "
                "и список принтеров. Путь к файлу очереди не переносится — он свой на каждой "
                "машине. Выгружаются сохранённые настройки, поэтому сначала сохраните правки "
                "на других страницах."
            )
        )

        self._secrets_toggle = ToggleSwitch()
        secrets_label = QLabel("Включить секреты: токен агента и access code принтеров")
        secrets_label.setObjectName("Secondary")
        secrets_label.setWordWrap(True)
        card.add(inline_row(self._secrets_toggle, secrets_label))
        card.add(
            Caption(
                "Без секретов файл можно передавать обычными средствами: на новой машине "
                "останется ввести токен и access code. С секретами файл становится таким же "
                "чувствительным, как agent.yaml."
            )
        )

        export_button = QPushButton("Сохранить в файл…")
        export_button.setObjectName("Accent")
        export_button.clicked.connect(self._export)
        card.add(inline_row(export_button))
        self.content.addWidget(card)

    def _build_import_card(self) -> None:
        card = Card()
        card.add(SectionTitle("Загрузить настройки"))
        card.add(
            Caption(
                "Файл накладывается на текущую конфигурацию. Секреты, которых нет в файле, "
                "берутся из текущих настроек этой машины — повторная загрузка не стирает "
                "уже введённый токен и access code."
            )
        )

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Все настройки", MODE_FULL)
        self._mode_combo.addItem("Только принтеры", MODE_PRINTERS)
        card.add(
            form_row(
                "Что загружать",
                self._mode_combo,
                hint="«Только принтеры» оставляет местный хаб, интервалы и обновления без изменений.",
            )
        )

        import_button = QPushButton("Выбрать файл…")
        import_button.clicked.connect(self._import)
        card.add(inline_row(import_button))
        self.content.addWidget(card)

    def _build_report_card(self) -> None:
        self._report_card = Card()
        self._report_card.add(SectionTitle("Результат загрузки"))
        self._report_applied = Caption("")
        self._report_kept = Caption("")
        self._report_missing = Caption("")
        for label in (self._report_applied, self._report_kept, self._report_missing):
            self._report_card.add(label)
        self._report_card.setVisible(False)
        self.content.addWidget(self._report_card)

    # -- export ------------------------------------------------------------- #

    def _export(self) -> None:
        suggested = str(Path.home() / DEFAULT_BUNDLE_NAME)
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить настройки агента", suggested, BUNDLE_FILTER
        )
        if not path:
            return

        bundle = build_bundle(
            self.state.config, include_secrets=self._secrets_toggle.isChecked()
        )
        info = describe_bundle(bundle)
        try:
            written = write_bundle(bundle, path)
        except OSError as exc:
            self.notify(f"Не удалось записать файл: {exc}", "error")
            return

        printers = len(info.printer_keys)
        if info.contains_secrets:
            self.notify(
                f"Сохранено в {written}. Принтеров: {printers}. Файл содержит секреты — "
                "передавайте его так же, как agent.yaml.",
                "warning",
            )
        else:
            self.notify(
                f"Сохранено в {written}. Принтеров: {printers}. Секреты не включены — "
                "на новой машине их нужно ввести вручную.",
                "success",
            )

    # -- import ------------------------------------------------------------- #

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Загрузить настройки агента", str(Path.home()), BUNDLE_FILTER
        )
        if not path:
            return

        try:
            bundle = read_bundle(path)
            info = describe_bundle(bundle)
        except BundleError as exc:
            self.notify(f"Файл не подходит: {exc}", "error")
            return

        mode = self._mode_combo.currentData()
        if not self._confirm(info, mode):
            return

        try:
            merged, report = apply_bundle(bundle, self.state.config, mode=mode)
        except BundleError as exc:
            self.notify(f"Не удалось применить файл: {exc}", "error")
            return

        self.state.set_config(merged)
        saved, detail = self.state.persist()
        self._show_report(report)
        if not saved:
            self.notify(detail, "error")
            return

        errors = self.state.validation_errors
        if errors:
            self.notify(
                f"Настройки загружены. {detail}. Служба пока не примет конфигурацию: "
                + "; ".join(errors),
                "warning",
            )
        else:
            self.notify(f"Настройки загружены. {detail}", "success")

    def _confirm(self, info: BundleInfo, mode: str) -> bool:
        lines = [
            f"Локация-источник: {info.source_location_key or '—'}",
            f"Выгружено: {info.exported_at or '—'} (агент {info.agent_version or '—'})",
            f"Принтеров в файле: {len(info.printer_keys)}",
        ]
        if info.note:
            lines.append(f"Примечание: {info.note}")
        if info.contains_secrets:
            lines.append("Файл содержит секреты и перезапишет текущие.")
        elif info.redacted:
            lines.append("Секреты в файл не включались — текущие сохранятся.")
        if mode == MODE_FULL:
            lines.append("")
            lines.append("Текущие настройки хаба и список принтеров будут заменены.")
        else:
            lines.append("")
            lines.append("Будет заменён только список принтеров.")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Загрузка настроек")
        box.setText("Применить настройки из файла?")
        box.setInformativeText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    def _show_report(self, report: TransferReport) -> None:
        def render(prefix: str, entries: list[str]) -> str:
            if not entries:
                return ""
            return f"{prefix}: " + ", ".join(field_label(entry) for entry in entries)

        self._report_applied.setText(render("Перенесено", report.applied))
        self._report_kept.setText(render("Оставлено местное", report.kept_local))
        self._report_missing.setText(render("Нужно заполнить вручную", report.missing))
        for label in (self._report_applied, self._report_kept, self._report_missing):
            label.setVisible(bool(label.text()))
        self._report_card.setVisible(True)

    def apply_palette(self, palette: Palette) -> None:
        self._secrets_toggle.apply_palette(palette)
