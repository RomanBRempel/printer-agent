"""Shared application state and the tolerant config loader the UI needs.

The service refuses to start on an invalid config; the editor must do the
opposite and open anyway — an unopenable settings window is exactly how the old
GUI failed. Nothing here raises on a bad file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml
from PySide6.QtCore import QObject, Signal

from ..config import (
    AgentConfig,
    BackoffConfig,
    OutboxConfig,
    UpdateConfig,
    config_from_dict,
    config_to_dict,
    validate_config,
)
from .prefs import Preferences, load_preferences, save_preferences
from .system import ServiceInfo, copy_elevated, default_config_path, program_data_dir
from .theme import Palette, build_palette


def empty_config() -> AgentConfig:
    return AgentConfig(
        hub_url="",
        agent_token="",
        location_key="",
        telemetry_interval_s=5,
        heartbeat_interval_s=15,
        command_reconnect_backoff_s=BackoffConfig(),
        outbox=OutboxConfig(database_path=program_data_dir() / "outbox.sqlite3", max_events=5000),
        updates=UpdateConfig(),
        printers=[],
    )


def read_config(path: Path) -> tuple[AgentConfig, str]:
    """Load ``path`` as far as it parses. Returns the config and a load error."""
    if not path.exists():
        return empty_config(), ""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return empty_config(), f"Не удалось прочитать {path}: {exc}"
    if raw is None:
        return empty_config(), ""
    if not isinstance(raw, dict):
        return empty_config(), f"{path}: верхний уровень конфигурации должен быть словарём."
    try:
        return config_from_dict(raw), ""
    except Exception as exc:
        return empty_config(), f"{path}: {exc}"


def config_yaml(config: AgentConfig) -> str:
    return yaml.safe_dump(config_to_dict(config), sort_keys=False, allow_unicode=True)


def write_config(config: AgentConfig, path: Path) -> tuple[bool, bool, str]:
    """Persist the config.

    Returns ``(saved, needs_elevation, error)``. ProgramData is admin-owned on a
    normal install, so a denied write is expected rather than exceptional.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config_yaml(config), encoding="utf-8")
        return True, False, ""
    except PermissionError as exc:
        return False, True, str(exc)
    except OSError as exc:
        return False, False, str(exc)


def write_config_elevated(config: AgentConfig, path: Path) -> tuple[bool, str]:
    """Save through a UAC prompt when the direct write was denied."""
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8", newline="\n"
    )
    try:
        handle.write(config_yaml(config))
    finally:
        handle.close()
    staged = Path(handle.name)
    ok, message = copy_elevated(staged, path)
    if not ok:
        return False, message
    return True, ""


class AppState(QObject):
    """One place the pages read from and one set of signals they react to."""

    config_changed = Signal()
    palette_changed = Signal(object)  # Palette
    service_changed = Signal(object)  # ServiceInfo

    def __init__(self, config_path: Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.config_path = Path(config_path) if config_path else default_config_path()
        self.preferences: Preferences = load_preferences()
        self.config, self.load_error = read_config(self.config_path)
        self.service = ServiceInfo()
        self._palette = build_palette(self.preferences.mode, self.preferences.accent)

    # -- config ------------------------------------------------------------- #

    @property
    def validation_errors(self) -> list[str]:
        return validate_config(self.config)

    def reload_config(self) -> None:
        self.config, self.load_error = read_config(self.config_path)
        self.config_changed.emit()

    def set_config(self, config: AgentConfig) -> None:
        self.config = config
        self.load_error = ""
        self.config_changed.emit()

    def persist(self) -> tuple[bool, str]:
        """Write the in-memory config to disk, escalating to UAC if needed."""
        saved, needs_elevation, error = write_config(self.config, self.config_path)
        if saved:
            return True, f"Сохранено в {self.config_path}"
        if not needs_elevation:
            return False, error or "Не удалось сохранить конфигурацию."
        elevated, message = write_config_elevated(self.config, self.config_path)
        if elevated:
            return True, "Сохранение выполняется с правами администратора."
        return False, message or error

    # -- theme -------------------------------------------------------------- #

    @property
    def palette(self) -> Palette:
        return self._palette

    def refresh_palette(self) -> None:
        """Rebuild from current preferences — also picks up a system theme flip."""
        self._palette = build_palette(self.preferences.mode, self.preferences.accent)
        self.palette_changed.emit(self._palette)

    def save_preferences(self) -> None:
        save_preferences(self.preferences)

    # -- service ------------------------------------------------------------ #

    def set_service(self, info: ServiceInfo) -> None:
        changed = info.state != self.service.state
        self.service = info
        if changed:
            self.service_changed.emit(info)
