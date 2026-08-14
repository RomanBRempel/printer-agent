"""Add/edit dialog for a single printer entry."""

from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...config import PrinterConfig
from ..widgets import Caption, form_row

BRANDS: tuple[tuple[str, str], ...] = (
    ("moonraker", "Moonraker (Klipper)"),
    ("bambu", "Bambu Lab"),
    ("creality", "Creality (порт 9999)"),
)

DEFAULT_PORTS = {"moonraker": 7125, "bambu": 8883, "creality": 9999}


class PrinterDialog(QDialog):
    """Returns a :class:`PrinterConfig` through :attr:`result_printer`."""

    def __init__(self, parent: QWidget | None = None, printer: PrinterConfig | None = None):
        super().__init__(parent)
        self.setWindowTitle("Принтер" if printer else "Новый принтер")
        self.setMinimumWidth(460)
        self.result_printer: PrinterConfig | None = None

        source = printer or PrinterConfig(key="", brand="moonraker", host="")
        credentials = dict(source.credentials or {})
        access_code = str(credentials.pop("access_code", ""))
        serial = str(credentials.pop("serial", ""))
        api_key = str(credentials.pop("api_key", ""))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        self.key_edit = QLineEdit(source.key)
        self.key_edit.setPlaceholderText("printer-1")
        self.brand_combo = QComboBox()
        for value, label in BRANDS:
            self.brand_combo.addItem(label, value)
        index = self.brand_combo.findData(source.brand)
        self.brand_combo.setCurrentIndex(max(0, index))
        self.host_edit = QLineEdit(source.host)
        self.host_edit.setPlaceholderText("192.168.1.50")
        self.port_edit = QLineEdit("" if source.port is None else str(source.port))
        self.access_code_edit = QLineEdit(access_code)
        self.access_code_edit.setEchoMode(QLineEdit.Password)
        self.serial_edit = QLineEdit(serial)
        self.api_key_edit = QLineEdit(api_key)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.extra_edit = QLineEdit(
            json.dumps(credentials, ensure_ascii=False, sort_keys=True) if credentials else ""
        )
        self.extra_edit.setPlaceholderText("{}")

        layout.addWidget(form_row("Ключ принтера", self.key_edit, hint="Уникальный идентификатор в контракте с хабом."))
        layout.addWidget(form_row("Бренд", self.brand_combo))
        layout.addWidget(form_row("Хост", self.host_edit))
        layout.addWidget(
            form_row("Порт", self.port_edit, hint="Пусто — значение по умолчанию для бренда (7125 / 8883 / 9999).")
        )

        self._bambu_fields = QWidget()
        bambu_layout = QVBoxLayout(self._bambu_fields)
        bambu_layout.setContentsMargins(0, 0, 0, 0)
        bambu_layout.setSpacing(14)
        bambu_layout.addWidget(form_row("Access code", self.access_code_edit))
        bambu_layout.addWidget(form_row("Серийный номер", self.serial_edit))
        layout.addWidget(self._bambu_fields)

        self._moonraker_fields = QWidget()
        moonraker_layout = QVBoxLayout(self._moonraker_fields)
        moonraker_layout.setContentsMargins(0, 0, 0, 0)
        moonraker_layout.setSpacing(14)
        moonraker_layout.addWidget(form_row("API key", self.api_key_edit, hint="Необязательно, если Moonraker без авторизации."))
        layout.addWidget(self._moonraker_fields)

        layout.addWidget(form_row("Дополнительные credentials (JSON)", self.extra_edit))

        self._error = Caption("")
        self._error.setVisible(False)
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Save).setObjectName("Accent")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.brand_combo.currentIndexChanged.connect(self._sync_brand_fields)
        self._sync_brand_fields()

    def _sync_brand_fields(self) -> None:
        brand = self.brand_combo.currentData()
        self._bambu_fields.setVisible(brand == "bambu")
        self._moonraker_fields.setVisible(brand == "moonraker")
        if not self.port_edit.text().strip():
            self.port_edit.setPlaceholderText(str(DEFAULT_PORTS.get(brand, "")))

    def _fail(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(True)

    def _accept(self) -> None:
        key = self.key_edit.text().strip()
        brand = str(self.brand_combo.currentData())
        host = self.host_edit.text().strip()
        port_text = self.port_edit.text().strip()

        if not key:
            return self._fail("Укажите ключ принтера.")
        if not host:
            return self._fail("Укажите хост принтера.")

        port: int | None = None
        if port_text:
            try:
                port = int(port_text)
            except ValueError:
                return self._fail("Порт должен быть числом.")
            if port <= 0:
                return self._fail("Порт должен быть положительным.")

        credentials: dict[str, object] = {}
        extra_text = self.extra_edit.text().strip()
        if extra_text:
            try:
                extra = json.loads(extra_text)
            except json.JSONDecodeError as exc:
                return self._fail(f"Некорректный JSON в дополнительных credentials: {exc}")
            if not isinstance(extra, dict):
                return self._fail("Дополнительные credentials должны быть JSON-объектом.")
            credentials.update(extra)

        if brand == "bambu":
            access_code = self.access_code_edit.text().strip()
            serial = self.serial_edit.text().strip()
            if not access_code:
                return self._fail("Для Bambu нужен access code.")
            if not serial:
                return self._fail("Для Bambu нужен серийный номер.")
            credentials["access_code"] = access_code
            credentials["serial"] = serial
        else:
            api_key = self.api_key_edit.text().strip()
            if api_key:
                credentials["api_key"] = api_key

        self.result_printer = PrinterConfig(
            key=key, brand=brand, host=host, port=port, credentials=credentials
        )
        self.accept()
