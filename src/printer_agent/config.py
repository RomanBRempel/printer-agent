from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import logging
import os

import yaml

logger = logging.getLogger(__name__)


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
    #: How often a *running* agent re-asks the feed. Without this the check
    #: happened once at service start, so a box that stays up for weeks never
    #: saw a release. Zero keeps the old startup-only behaviour.
    check_interval_h: int = 24


@dataclass(slots=True)
class PrintFilesConfig:
    """Where files delivered by `file_offer` are kept, and for how long.

    The cache has to outlive a restart — `start_print` arrives as its own command
    after the upload — but it must not grow without bound on a shop-floor box.
    An empty ``directory`` means "next to the outbox database".
    """

    directory: Path | None = None
    max_age_h: int = 72
    max_total_mb: int = 2048


@dataclass(slots=True)
class PrinterConfig:
    key: str
    brand: str
    host: str
    port: int | None = None
    credentials: dict[str, Any] = field(default_factory=dict)
    #: Still-image URL for the camera, when the printer's is not where the
    #: adapter would look by itself. Not a credential: it is an address.
    camera_snapshot_url: str = ""


@dataclass(slots=True)
class AgentConfig:
    hub_url: str
    agent_token: str
    location_key: str
    telemetry_interval_s: int = 5
    heartbeat_interval_s: int = 15
    command_reconnect_backoff_s: BackoffConfig = field(default_factory=BackoffConfig)
    outbox: OutboxConfig = field(default_factory=OutboxConfig)
    print_files: PrintFilesConfig = field(default_factory=PrintFilesConfig)
    updates: UpdateConfig = field(default_factory=UpdateConfig)
    printers: list[PrinterConfig] = field(default_factory=list)
    #: File this config was read from, so a long-running agent can notice the
    #: operator editing it. Set by :func:`parse_config`; ``None`` for a config
    #: built in memory, and never written back out by :func:`config_to_dict`.
    source_path: Path | None = None

    def print_files_directory(self) -> Path:
        """The print-file cache, defaulted next to the outbox it belongs with."""
        if self.print_files.directory is not None:
            return self.print_files.directory
        return self.outbox.database_path.parent / "print-files"


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
    if config.print_files.directory is not None:
        config.print_files.directory = _resolve_outbox_path(config.print_files.directory, config_path)
    config.source_path = config_path
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


def load_config_file(path: str | Path = "agent.yaml") -> AgentConfig:
    """Read the file exactly as it sits on disk: no env overrides, no path fixups.

    Tools that read a config only to write it back — the settings transfer — must
    round-trip what the operator wrote. Going through :func:`parse_config` would
    bake a temporary ``HUB_URL`` from the environment into the saved file.
    """
    config_path = Path(path)
    return config_from_dict(_read_yaml(config_path) if config_path.exists() else {})


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
        "print_files": {
            **(
                {"directory": str(config.print_files.directory)}
                if config.print_files.directory is not None
                else {}
            ),
            "max_age_h": config.print_files.max_age_h,
            "max_total_mb": config.print_files.max_total_mb,
        },
        "updates": {
            "feed_url": config.updates.feed_url,
            "auto_update": config.updates.auto_update,
            "check_on_startup": config.updates.check_on_startup,
            "check_interval_h": config.updates.check_interval_h,
        },
        "printers": [
            {
                "key": printer.key,
                "brand": printer.brand,
                "host": printer.host,
                **({"port": printer.port} if printer.port is not None else {}),
                **(
                    {"camera_snapshot_url": printer.camera_snapshot_url}
                    if printer.camera_snapshot_url
                    else {}
                ),
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


def _text(value: Any) -> str:
    """A YAML scalar as text, where an empty key reads as empty.

    `host:` with nothing after it parses as None, and `str(None)` is the
    four-character token "None" — a value that looks set, satisfies every
    required-field check, and surfaces much later as a printer whose hostname
    cannot be resolved.
    """
    return "" if value is None else str(value).strip()


def _int(value: Any, default: int, name: str) -> int:
    """A YAML scalar as a whole number, or a readable refusal.

    An empty key means "unset" and takes the default. Anything else that is not
    a number is a mistake worth naming: `int()` raising bare `ValueError` out of
    config loading tells the operator which type failed but not which setting.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError([f"{name} must be a whole number"]) from None


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


#: Everything a `printers[]` entry may say about itself. Anything else is a
#: mistake worth naming rather than dropping.
_PRINTER_KEYS = frozenset({"key", "brand", "host", "port", "credentials", "camera_snapshot_url"})

#: Settings that belong one level down, under `credentials`. Written beside
#: `host` they parse as valid YAML, vanish into nothing, and leave the printer
#: running on defaults — which for `print_url_prefix` means a Bambu answering
#: `0500-4002` while the operator looks at the line in the file that should have
#: fixed it.
_CREDENTIAL_KEYS = frozenset(
    {"access_code", "serial", "api_key", "password", "print_url_prefix", "ftps_timeout_s"}
)


def _warn_about_stray_printer_keys(entry: dict[str, Any], key: str) -> None:
    """Say what was ignored, and where it should have gone.

    Unknown keys are not an error: refusing to start over one would make every
    future field a breaking change for older agents. But silence is worse than
    either — the setting is in the file, the operator can see it, and the agent
    behaves as though it is not.
    """
    for name in entry:
        if name in _PRINTER_KEYS:
            continue
        where = " (it belongs under credentials)" if name in _CREDENTIAL_KEYS else ""
        logger.warning(
            "ignored an unknown printer setting",
            extra={"action": "config", "printer_key": key, "error": f"{name}{where}"},
        )


def config_from_dict(data: dict[str, Any]) -> AgentConfig:
    outbox_data = data.get("outbox") or {}
    print_files_data = data.get("print_files") or {}
    updates_data = data.get("updates") or {}
    backoff_data = data.get("command_reconnect_backoff_s") or {}
    printers: list[PrinterConfig] = []
    for item in data.get("printers", []):
        if not isinstance(item, dict):
            continue
        key = _text(item.get("key"))
        _warn_about_stray_printer_keys(item, key)
        printers.append(
            PrinterConfig(
                key=key,
                brand=(_text(item.get("brand")) or "moonraker").lower(),
                host=_text(item.get("host")),
                port=_int(item.get("port"), 0, f"printer {key}: port") if item.get("port") is not None else None,
                credentials=item.get("credentials") or {},
                camera_snapshot_url=_text(item.get("camera_snapshot_url")),
            )
        )
    return AgentConfig(
        hub_url=_text(data.get("hub_url")),
        agent_token=_text(data.get("agent_token")),
        location_key=_text(data.get("location_key")),
        telemetry_interval_s=_int(data.get("telemetry_interval_s"), 5, "telemetry_interval_s"),
        heartbeat_interval_s=_int(data.get("heartbeat_interval_s"), 15, "heartbeat_interval_s"),
        command_reconnect_backoff_s=BackoffConfig(
            min_s=_int(backoff_data.get("min"), 1, "command_reconnect_backoff_s.min"),
            max_s=_int(backoff_data.get("max"), 60, "command_reconnect_backoff_s.max"),
        ),
        outbox=OutboxConfig(
            database_path=Path(_text(outbox_data.get("database_path")) or "data/outbox.sqlite3"),
            max_events=_int(outbox_data.get("max_events"), 5000, "outbox.max_events"),
        ),
        print_files=PrintFilesConfig(
            directory=Path(_text(print_files_data.get("directory"))) if _text(print_files_data.get("directory")) else None,
            max_age_h=_int(print_files_data.get("max_age_h"), 72, "print_files.max_age_h"),
            max_total_mb=_int(print_files_data.get("max_total_mb"), 2048, "print_files.max_total_mb"),
        ),
        updates=UpdateConfig(
            feed_url=_text(updates_data.get("feed_url")),
            auto_update=_parse_bool(updates_data.get("auto_update"), False),
            check_on_startup=_parse_bool(updates_data.get("check_on_startup"), True),
            check_interval_h=int(updates_data.get("check_interval_h", 24) or 0),
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
    if config.print_files.max_age_h <= 0:
        errors.append("print_files.max_age_h must be positive")
    if config.print_files.max_total_mb <= 0:
        errors.append("print_files.max_total_mb must be positive")
    if config.updates.feed_url and not str(config.updates.feed_url).strip():
        errors.append("updates.feed_url must not be blank")
    if config.updates.check_interval_h < 0:
        errors.append("updates.check_interval_h must not be negative")
    # `auto_update` without a feed is pointless but deliberately *not* an error:
    # an agent already running that combination would refuse to start after an
    # update, which turns a useless setting into an outage.
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
        if printer.camera_snapshot_url and not printer.camera_snapshot_url.lower().startswith(
            ("http://", "https://")
        ):
            errors.append(f"printer {printer.key}: camera_snapshot_url must be an http(s) URL")
        if printer.brand == "bambu":
            access_code = str((printer.credentials or {}).get("access_code", "")).strip()
            serial = str((printer.credentials or {}).get("serial", "")).strip()
            if not access_code:
                errors.append(f"printer {printer.key}: bambu access_code is required")
            if not serial:
                errors.append(f"printer {printer.key}: bambu serial is required")
    return errors
