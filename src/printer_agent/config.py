from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os

import yaml


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("invalid agent configuration")
        self.errors = errors

    def __str__(self) -> str:
        return "; ".join(self.errors)


@dataclass(slots=True)
class BackoffConfig:
    min_s: int = 1
    max_s: int = 60


@dataclass(slots=True)
class OutboxConfig:
    database_path: Path = Path("data/outbox.sqlite3")
    max_events: int = 5000


@dataclass(slots=True)
class UpdateConfig:
    feed_url: str = ""
    auto_update: bool = False
    check_on_startup: bool = True


@dataclass(slots=True)
class PrinterConfig:
    key: str
    brand: str
    host: str
    port: int | None = None
    credentials: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentConfig:
    hub_url: str
    agent_token: str
    location_key: str
    telemetry_interval_s: int = 5
    heartbeat_interval_s: int = 15
    command_reconnect_backoff_s: BackoffConfig = field(default_factory=BackoffConfig)
    outbox: OutboxConfig = field(default_factory=OutboxConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    printers: list[PrinterConfig] = field(default_factory=list)


_REQUIRED_KEYS = ("hub_url", "agent_token", "location_key")


def parse_config(
    path: str | Path = "agent.yaml", env: os._Environ[str] | None = None
) -> tuple[AgentConfig, list[str]]:
    """Load and validate without raising: returns the config and its errors.

    Callers that can act on a partially valid config — the service installer,
    the desktop editor — need the parsed values *and* the reasons it is not
    runnable yet. Only a malformed YAML document still raises.
    """
    env = env or os.environ
    config_path = Path(path)
    data = _read_yaml(config_path) if config_path.exists() else {}
    merged = _apply_env_overrides(data, env)
    config = config_from_dict(merged)
    config.outbox.database_path = _resolve_outbox_path(config.outbox.database_path, config_path)
    return config, validate_config(config)


def _resolve_outbox_path(database_path: Path, config_path: Path) -> Path:
    """Anchor a relative outbox path to the config file, not the process CWD.

    The service starts with its working directory somewhere in System32, so a
    relative `data/outbox.sqlite3` meant "create a folder wherever Windows
    happened to launch us" — which fails with access denied and takes the whole
    service down before it can report a reason.
    """
    if database_path.is_absolute():
        return database_path
    base = config_path.parent if config_path.parent != Path("") else Path.cwd()
    return (base / database_path).resolve()


def load_config(path: str | Path = "agent.yaml", env: os._Environ[str] | None = None) -> AgentConfig:
    config, errors = parse_config(path, env)
    if errors:
        raise ConfigError(errors)
    return config


def save_config(config: AgentConfig, path: str | Path) -> None:
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        yaml.safe_dump(config_to_dict(config), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def config_to_dict(config: AgentConfig) -> dict[str, Any]:
    return {
        "hub_url": config.hub_url,
        "agent_token": config.agent_token,
        "location_key": config.location_key,
        "telemetry_interval_s": config.telemetry_interval_s,
        "heartbeat_interval_s": config.heartbeat_interval_s,
        "command_reconnect_backoff_s": {
            "min": config.command_reconnect_backoff_s.min_s,
            "max": config.command_reconnect_backoff_s.max_s,
        },
        "outbox": {
            "database_path": str(config.outbox.database_path),
            "max_events": config.outbox.max_events,
        },
        "updates": {
            "feed_url": config.updates.feed_url,
            "auto_update": config.updates.auto_update,
            "check_on_startup": config.updates.check_on_startup,
        },
        "printers": [
            {
                "key": printer.key,
                "brand": printer.brand,
                "host": printer.host,
                **({"port": printer.port} if printer.port is not None else {}),
                **({"credentials": printer.credentials} if printer.credentials else {}),
            }
            for printer in config.printers
        ],
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(["top-level config must be a mapping"])
    return raw


def _apply_env_overrides(data: dict[str, Any], env: os._Environ[str]) -> dict[str, Any]:
    merged = dict(data)
    for key in _REQUIRED_KEYS:
        if env.get(key.upper()):
            merged[key] = env[key.upper()]
    if env.get("TELEMETRY_INTERVAL_S"):
        merged["telemetry_interval_s"] = env["TELEMETRY_INTERVAL_S"]
    if env.get("HEARTBEAT_INTERVAL_S"):
        merged["heartbeat_interval_s"] = env["HEARTBEAT_INTERVAL_S"]
    if env.get("OUTBOX_DATABASE_PATH"):
        merged.setdefault("outbox", {})["database_path"] = env["OUTBOX_DATABASE_PATH"]
    if env.get("UPDATE_FEED_URL"):
        merged.setdefault("updates", {})["feed_url"] = env["UPDATE_FEED_URL"]
    if env.get("AUTO_UPDATE"):
        merged.setdefault("updates", {})["auto_update"] = env["AUTO_UPDATE"]
    if env.get("UPDATE_CHECK_ON_STARTUP"):
        merged.setdefault("updates", {})["check_on_startup"] = env["UPDATE_CHECK_ON_STARTUP"]
    return merged


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def config_from_dict(data: dict[str, Any]) -> AgentConfig:
    outbox_data = data.get("outbox") or {}
    updates_data = data.get("updates") or {}
    backoff_data = data.get("command_reconnect_backoff_s") or {}
    printers: list[PrinterConfig] = []
    for item in data.get("printers", []):
        if not isinstance(item, dict):
            continue
        printers.append(
            PrinterConfig(
                key=str(item.get("key", "")),
                brand=str(item.get("brand", "moonraker")).lower(),
                host=str(item.get("host", "")),
                port=int(item["port"]) if item.get("port") is not None else None,
                credentials=item.get("credentials") or {},
            )
        )
    return AgentConfig(
        hub_url=str(data.get("hub_url", "")).strip(),
        agent_token=str(data.get("agent_token", "")).strip(),
        location_key=str(data.get("location_key", "")).strip(),
        telemetry_interval_s=int(data.get("telemetry_interval_s", 5)),
        heartbeat_interval_s=int(data.get("heartbeat_interval_s", 15)),
        command_reconnect_backoff_s=BackoffConfig(
            min_s=int(backoff_data.get("min", 1)),
            max_s=int(backoff_data.get("max", 60)),
        ),
        outbox=OutboxConfig(
            database_path=Path(outbox_data.get("database_path", "data/outbox.sqlite3")),
            max_events=int(outbox_data.get("max_events", 5000)),
        ),
        updates=UpdateConfig(
            feed_url=str(updates_data.get("feed_url", "")).strip(),
            auto_update=_parse_bool(updates_data.get("auto_update"), False),
            check_on_startup=_parse_bool(updates_data.get("check_on_startup"), True),
        ),
        printers=printers,
    )


def validate_config(config: AgentConfig) -> list[str]:
    errors: list[str] = []
    if not config.hub_url:
        errors.append("hub_url is required")
    if not config.agent_token:
        errors.append("agent_token is required")
    if not config.location_key:
        errors.append("location_key is required")
    if config.telemetry_interval_s <= 0:
        errors.append("telemetry_interval_s must be positive")
    if config.heartbeat_interval_s <= 0:
        errors.append("heartbeat_interval_s must be positive")
    if config.command_reconnect_backoff_s.min_s <= 0:
        errors.append("command_reconnect_backoff_s.min must be positive")
    if config.command_reconnect_backoff_s.max_s < config.command_reconnect_backoff_s.min_s:
        errors.append("command_reconnect_backoff_s.max must be greater than or equal to min")
    if config.outbox.max_events <= 0:
        errors.append("outbox.max_events must be positive")
    if config.updates.feed_url and not str(config.updates.feed_url).strip():
        errors.append("updates.feed_url must not be blank")
    if not config.printers:
        errors.append("printers must not be empty")
    for printer in config.printers:
        if not printer.key:
            errors.append("each printer needs a key")
        if printer.brand not in {"moonraker", "bambu", "creality"}:
            errors.append(f"printer {printer.key}: unsupported brand {printer.brand}")
        if not printer.host:
            errors.append(f"printer {printer.key}: host is required")
        if printer.port is not None and printer.port <= 0:
            errors.append(f"printer {printer.key}: port must be positive")
        if printer.brand == "bambu":
            access_code = str((printer.credentials or {}).get("access_code", "")).strip()
            serial = str((printer.credentials or {}).get("serial", "")).strip()
            if not access_code:
                errors.append(f"printer {printer.key}: bambu access_code is required")
            if not serial:
                errors.append(f"printer {printer.key}: bambu serial is required")
    return errors
