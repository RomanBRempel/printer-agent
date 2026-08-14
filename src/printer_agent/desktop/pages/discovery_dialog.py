"""Find printers on the local network and turn the picks into config entries."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...config import PrinterConfig
from ...core.discovery import DiscoveredPrinter, discover, local_ipv4_networks, parse_networks
from ..checks import AsyncRunner
from ..theme import Palette
from ..widgets import Caption, Card, SectionTitle, form_row, inline_row

BRAND_LABELS = {"moonraker": "Moonraker", "bambu": "Bambu Lab"}


class DiscoveryRow(QWidget):
    """One found printer, plus the access code Bambu will not tell us."""

    def __init__(self, printer: DiscoveredPrinter, parent: QWidget | None = None):
        super().__init__(parent)
        self.printer = printer

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        title = f"{printer.name or printer.host}"
        self.check = QCheckBox(title)
        self.check.setChecked(not printer.needs_credentials)
        layout.addWidget(self.check)

        details = [BRAND_LABELS.get(printer.brand, printer.brand), f"{printer.host}:{printer.port}"]
        if printer.model:
            details.append(printer.model)
        if printer.serial:
            details.append(f"S/N {printer.serial}")
        details.append(f"найден через {printer.source}")
        layout.addWidget(Caption("  ·  ".join(details)))

        self.access_code = QLineEdit()
        self.access_code.setEchoMode(QLineEdit.Password)
        self.access_code.setPlaceholderText("Access code с экрана принтера")
        self._credentials = QWidget()
        credentials_layout = QVBoxLayout(self._credentials)
        credentials_layout.setContentsMargins(0, 0, 0, 0)
        credentials_layout.addWidget(
            form_row(
                "Access code",
                self.access_code,
                hint="Настройки → Сеть на экране принтера. Без него Bambu не пустит агента.",
            )
        )
        self._credentials.setVisible(printer.needs_credentials)
        layout.addWidget(self._credentials)

    @property
    def selected(self) -> bool:
        return self.check.isChecked()

    def blocking_issue(self) -> str:
        if self.printer.needs_credentials and not self.access_code.text().strip():
            return f"{self.printer.name or self.printer.host}: нужен access code."
        return ""

    def to_config(self, key: str) -> PrinterConfig:
        credentials: dict[str, object] = {}
        if self.printer.serial:
            credentials["serial"] = self.printer.serial
        code = self.access_code.text().strip()
        if code:
            credentials["access_code"] = code
        return PrinterConfig(
            key=key,
            brand=self.printer.brand,
            host=self.printer.host,
            port=self.printer.port or None,
            credentials=credentials,
        )


class DiscoveryDialog(QDialog):
    """Returns the chosen printers through :attr:`result_printers`."""

    def __init__(self, parent: QWidget | None = None, existing_keys: set[str] | None = None,
                 existing_hosts: set[str] | None = None, palette: Palette | None = None):
        super().__init__(parent)
        self.setWindowTitle("Поиск принтеров в сети")
        self.setMinimumSize(620, 560)
        self.result_printers: list[PrinterConfig] = []
        self._existing_keys = set(existing_keys or set())
        self._existing_hosts = set(existing_hosts or set())
        self._rows: list[DiscoveryRow] = []
        self._runner: AsyncRunner | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        card = Card(padding=18)
        card.add(SectionTitle("Где искать"))
        detected = local_ipv4_networks()
        self.networks_edit = QLineEdit(", ".join(str(network) for network in detected))
        self.networks_edit.setPlaceholderText("192.168.1.0/24")
        card.add(
            form_row(
                "Подсети",
                self.networks_edit,
                hint="Moonraker ищется опросом адресов, Bambu Lab — прослушиванием его "
                     "объявлений в сети. Уже добавленные принтеры в списке не показываются.",
            )
        )
        self.search_button = QPushButton("Искать")
        self.search_button.setObjectName("Accent")
        self.search_button.clicked.connect(self.start_search)
        card.add(inline_row(self.search_button))
        layout.addWidget(card)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status = QLabel("Нажмите «Искать», чтобы опросить сеть.")
        self.status.setObjectName("Secondary")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        self._results_layout = QVBoxLayout(holder)
        self._results_layout.setContentsMargins(0, 0, 8, 0)
        self._results_layout.setSpacing(16)
        self._results_layout.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        self.add_button = QPushButton("Добавить выбранные")
        self.add_button.setObjectName("Accent")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self._accept_selection)
        cancel = QPushButton("Закрыть")
        cancel.clicked.connect(self.reject)
        layout.addWidget(inline_row(self.add_button, cancel))

    # -- search -------------------------------------------------------------- #

    def start_search(self) -> None:
        networks = parse_networks(self.networks_edit.text().split(","))
        if not networks:
            self.status.setText("Не удалось разобрать подсети. Пример: 192.168.1.0/24")
            return

        self._clear_results()
        self.search_button.setEnabled(False)
        self.add_button.setEnabled(False)
        self.progress.setVisible(True)
        self.status.setText("Идёт поиск. Bambu Lab объявляет себя не сразу — это занимает несколько секунд.")

        known_hosts = set(self._existing_hosts)
        self._runner = AsyncRunner(
            lambda: discover(networks=networks, known_hosts=known_hosts), self
        )
        self._runner.completed.connect(self._search_done)
        self._runner.start()

    def _search_done(self, printers, error: str) -> None:
        self.search_button.setEnabled(True)
        self.progress.setVisible(False)

        if error:
            self.status.setText(f"Поиск не удался: {error}")
            return
        found = list(printers or [])
        if not found:
            self.status.setText(
                "Ничего не найдено. Проверьте, что принтеры в той же сети, "
                "и что брандмауэр не блокирует UDP-порт 2021 для Bambu Lab."
            )
            return

        self.status.setText(f"Найдено принтеров: {len(found)}")
        for printer in found:
            row = DiscoveryRow(printer)
            self._rows.append(row)
            self._results_layout.insertWidget(self._results_layout.count() - 1, row)
        self.add_button.setEnabled(True)

    def _clear_results(self) -> None:
        for row in self._rows:
            self._results_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

    # -- selection ----------------------------------------------------------- #

    def _accept_selection(self) -> None:
        chosen = [row for row in self._rows if row.selected]
        if not chosen:
            self.status.setText("Отметьте хотя бы один принтер.")
            return

        issues = [row.blocking_issue() for row in chosen]
        issues = [issue for issue in issues if issue]
        if issues:
            self.status.setText(" ".join(issues))
            return

        used = set(self._existing_keys)
        printers: list[PrinterConfig] = []
        for row in chosen:
            key = _unique_key(row.printer.suggested_key, used)
            used.add(key)
            printers.append(row.to_config(key))
        self.result_printers = printers
        self.accept()


def _unique_key(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    index = 2
    while f"{base}-{index}" in used:
        index += 1
    return f"{base}-{index}"
