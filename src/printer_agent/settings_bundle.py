"""Move an agent's settings from one installation to another.

A bundle is deliberately *not* a copy of ``agent.yaml``. It drops what belongs to
the machine it was written on — the outbox database path — and, unless the
operator asks otherwise, the secrets: ``agent_token`` and printer access codes.
A redacted bundle is therefore safe to hand around, and it still carries the part
that is tedious to retype: the hub wiring, the intervals and the printer
inventory.

Importing merges over the local config instead of overwriting the file, so
applying a redacted bundle to an already-configured agent keeps the credentials
that agent already had. What could not be filled from either side is reported
rather than silently left blank.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .config import AgentConfig, PrinterConfig, config_from_dict, config_to_dict
from .contracts import REDACTED

BUNDLE_KIND = "printer-agent-settings"
BUNDLE_VERSION = 1

#: Everything the bundle carries.
MODE_FULL = "full"
#: Only the printer inventory; the local hub wiring and intervals stay.
MODE_PRINTERS = "printers"
MODES = (MODE_FULL, MODE_PRINTERS)

#: Credential fields treated as secrets and stripped from a redacted bundle.
#: A Bambu ``serial`` is not here on purpose — it identifies the printer rather
#: than granting access to it, and dropping it would make the entry useless.
SECRET_CREDENTIAL_KEYS = frozenset(
    {"access_code", "api_key", "apikey", "password", "secret", "token"}
)

#: Secrets without which an adapter of that brand cannot connect at all.
_REQUIRED_SECRETS: dict[str, tuple[str, ...]] = {"bambu": ("access_code",)}


class BundleError(ValueError):
    """The file is not a settings bundle this version of the agent can apply."""


@dataclass(slots=True)
class BundleInfo:
    """What a bundle says about itself, for a preview before applying it."""

    version: int = 0
    exported_at: str = ""
    agent_version: str = ""
    source_location_key: str = ""
    hub_url: str = ""
    note: str = ""
    contains_secrets: bool = False
    printer_keys: list[str] = field(default_factory=list)
    redacted: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TransferReport:
    """Outcome of an import, as config field names rather than prose.

    Each surface phrases these itself: the CLI prints them raw, the desktop app
    maps the known ones onto Russian labels.
    """

    applied: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    #: Fields the sender is not allowed to set. Only the remote path fills this:
    #: an import from a file is done by someone standing at the machine, who may
    #: change anything. Refusals are reported rather than dropped — a silent drop
    #: reads as a change that worked.
    rejected: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #

def build_bundle(
    config: AgentConfig, *, include_secrets: bool = False, note: str = ""
) -> dict[str, Any]:
    data = config_to_dict(config)
    redacted: list[str] = []

    settings: dict[str, Any] = {
        "hub_url": data["hub_url"],
        "location_key": data["location_key"],
        "telemetry_interval_s": data["telemetry_interval_s"],
        "heartbeat_interval_s": data["heartbeat_interval_s"],
        "command_reconnect_backoff_s": dict(data["command_reconnect_backoff_s"]),
        # No database_path: that is where *this* machine keeps its queue, and
        # carrying it over would point the new install at a folder it may not
        # own — the same failure the service hit when it ran from System32.
        "outbox": {"max_events": data["outbox"]["max_events"]},
        "updates": dict(data["updates"]),
        "printers": [],
    }

    if include_secrets:
        settings["agent_token"] = data["agent_token"]
    elif data["agent_token"]:
        redacted.append("agent_token")

    for printer in data["printers"]:
        entry = {key: value for key, value in printer.items() if key != "credentials"}
        credentials = dict(printer.get("credentials") or {})
        if not include_secrets:
            for name in sorted(credentials):
                if name.lower() in SECRET_CREDENTIAL_KEYS:
                    credentials.pop(name)
                    redacted.append(f"printers.{printer['key']}.credentials.{name}")
        if credentials:
            entry["credentials"] = credentials
        settings["printers"].append(entry)

    bundle: dict[str, Any] = {
        "kind": BUNDLE_KIND,
        "version": BUNDLE_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent_version": __version__,
        "source_location_key": config.location_key,
        "contains_secrets": _holds_secrets(settings),
        "settings": settings,
    }
    if note:
        bundle["note"] = note
    if redacted:
        bundle["redacted"] = redacted
    return bundle


def _holds_secrets(settings: dict[str, Any]) -> bool:
    """Answer from the payload, not from the flag that was asked for.

    An export "with secrets" of a config that has none must not label the file
    as sensitive — the flag drives a warning, so it has to mean something.
    """
    if str(settings.get("agent_token", "") or "").strip():
        return True
    for printer in settings.get("printers", []):
        for name, value in (printer.get("credentials") or {}).items():
            if name.lower() in SECRET_CREDENTIAL_KEYS and value:
                return True
    return False


def bundle_yaml(bundle: dict[str, Any]) -> str:
    return yaml.safe_dump(bundle, sort_keys=False, allow_unicode=True)


def write_bundle(bundle: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    if target.parent != Path(""):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(bundle_yaml(bundle), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #

def read_bundle(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BundleError(f"could not read {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise BundleError(f"{source} is not valid YAML: {exc}") from exc
    return validate_bundle(raw)


def validate_bundle(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BundleError("a settings bundle must be a mapping")
    kind = str(raw.get("kind", "")).strip()
    if kind != BUNDLE_KIND:
        raise BundleError(
            f"not a printer-agent settings bundle (kind={kind or 'missing'})"
        )
    try:
        version = int(raw.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    if version < 1:
        raise BundleError("settings bundle has no usable version")
    if version > BUNDLE_VERSION:
        # Refusing beats guessing: a newer bundle may carry fields this build
        # would drop on the floor while reporting a successful import.
        raise BundleError(
            f"settings bundle version {version} is newer than this agent understands "
            f"({BUNDLE_VERSION}); update the agent first"
        )
    if not isinstance(raw.get("settings"), dict):
        raise BundleError("settings bundle has no settings section")
    return raw


def describe_bundle(bundle: dict[str, Any]) -> BundleInfo:
    validated = validate_bundle(bundle)
    settings = validated["settings"]
    printers = [
        str(item.get("key", ""))
        for item in settings.get("printers") or []
        if isinstance(item, dict)
    ]
    redacted = [str(item) for item in validated.get("redacted") or []]
    return BundleInfo(
        version=int(validated.get("version", 0)),
        exported_at=str(validated.get("exported_at", "")),
        agent_version=str(validated.get("agent_version", "")),
        source_location_key=str(validated.get("source_location_key", "")),
        hub_url=str(settings.get("hub_url", "")),
        note=str(validated.get("note", "")),
        contains_secrets=bool(validated.get("contains_secrets", False)),
        printer_keys=printers,
        redacted=redacted,
    )


def apply_bundle(
    bundle: dict[str, Any], current: AgentConfig, *, mode: str = MODE_FULL
) -> tuple[AgentConfig, TransferReport]:
    """Merge ``bundle`` over ``current`` and report what happened to each field."""
    if mode not in MODES:
        raise BundleError(f"unsupported import mode: {mode}")
    settings = validate_bundle(bundle)["settings"]

    report = TransferReport()
    base = config_to_dict(current)
    printers = _merge_printers(settings.get("printers") or [], current.printers, report)

    if mode == MODE_PRINTERS:
        base["printers"] = printers
        report.applied.append(f"printers[{len(printers)}]")
        report.kept_local.extend(["hub_url", "location_key", "agent_token", "updates"])
        return config_from_dict(base), report

    for key in ("hub_url", "location_key", "telemetry_interval_s", "heartbeat_interval_s"):
        value = settings.get(key)
        if value not in (None, ""):
            base[key] = value
            report.applied.append(key)

    backoff = settings.get("command_reconnect_backoff_s")
    if isinstance(backoff, dict) and backoff:
        base["command_reconnect_backoff_s"] = {
            "min": backoff.get("min", base["command_reconnect_backoff_s"]["min"]),
            "max": backoff.get("max", base["command_reconnect_backoff_s"]["max"]),
        }
        report.applied.append("command_reconnect_backoff_s")

    outbox = settings.get("outbox")
    if isinstance(outbox, dict) and outbox.get("max_events"):
        base["outbox"]["max_events"] = outbox["max_events"]
        report.applied.append("outbox.max_events")
    report.kept_local.append("outbox.database_path")

    updates = settings.get("updates")
    if isinstance(updates, dict) and updates:
        base["updates"] = {**base["updates"], **updates}
        report.applied.append("updates")

    token = str(settings.get("agent_token", "") or "").strip()
    if token:
        base["agent_token"] = token
        report.applied.append("agent_token")
    elif current.agent_token:
        report.kept_local.append("agent_token")
    else:
        report.missing.append("agent_token")

    base["printers"] = printers
    report.applied.append(f"printers[{len(printers)}]")
    return config_from_dict(base), report


def _merge_printers(
    incoming: list[Any], local: list[PrinterConfig], report: TransferReport
) -> list[dict[str, Any]]:
    """Incoming entries win, except for secrets this machine already knows.

    Three ways an incoming credential can be absent, and they mean different
    things. Left out entirely, or empty, or the string :data:`REDACTED` — which
    is what the agent sent the hub in the first place, so a hub that renders the
    settings and posts them back arrives here — all mean *keep what this machine
    has*. An explicit ``null`` means *clear it*, and is the only way to take a
    credential away remotely. Collapsing the two is how a form save wipes a
    working access code, or how a revoked one comes back.
    """
    by_key = {printer.key: printer for printer in local}
    merged: list[dict[str, Any]] = []

    for item in incoming:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", ""))
        brand = str(item.get("brand", "")).lower()
        entry = {name: value for name, value in item.items() if name != "credentials"}
        credentials = {
            name: value for name, value in (item.get("credentials") or {}).items()
        }

        cleared = {name for name, value in credentials.items() if value is None}
        for name in sorted(cleared):
            credentials.pop(name)
            report.applied.append(f"printers.{key}.credentials.{name} cleared")
        for name in [name for name, value in credentials.items() if value == REDACTED]:
            credentials.pop(name)

        existing = by_key.get(key)
        for name, value in (existing.credentials if existing else {}).items():
            if name in cleared:
                continue
            if name.lower() in SECRET_CREDENTIAL_KEYS and value and not credentials.get(name):
                credentials[name] = value
                report.kept_local.append(f"printers.{key}.credentials.{name}")

        for name in _REQUIRED_SECRETS.get(brand, ()):
            if not credentials.get(name):
                report.missing.append(f"printers.{key}.credentials.{name}")

        if credentials:
            entry["credentials"] = credentials
        merged.append(entry)

    return merged


# --------------------------------------------------------------------------- #
# remote configuration
# --------------------------------------------------------------------------- #

#: What a `settings_update` from the hub may change. Everything else in the
#: config is either derived or on :data:`REMOTE_BLOCKED_KEYS`.
REMOTE_WRITABLE_KEYS = (
    "telemetry_interval_s",
    "heartbeat_interval_s",
    "command_reconnect_backoff_s",
    "outbox",
    "print_files",
    "updates",
    "printers",
)

#: What the hub may see but never set. These name the session and the open
#: database — the two things a running agent cannot swap without tearing itself
#: down — and a mistake in them leaves no channel through which to repair the
#: agent. They stay the operator's job, on the machine.
REMOTE_BLOCKED_KEYS = ("hub_url", "agent_token", "location_key")

#: Same, one level down.
REMOTE_BLOCKED_SUBKEYS = {"outbox": ("database_path",)}


def redacted_settings(config: AgentConfig) -> dict[str, Any]:
    """The writable settings, with every secret replaced by :data:`REDACTED`.

    Two things must stay distinguishable to whoever renders an editor from this:
    a secret that is set (the marker) and one that is not (absent). Collapsing
    them is how a form saves a blank over a working access code.
    """
    data = config_to_dict(config)
    settings: dict[str, Any] = {
        key: data[key] for key in REMOTE_WRITABLE_KEYS if key != "printers" and key in data
    }
    settings["outbox"] = {"max_events": data["outbox"]["max_events"]}
    settings["printers"] = [
        {
            **{name: value for name, value in printer.items() if name != "credentials"},
            **(
                {
                    "credentials": {
                        name: (REDACTED if _is_secret(name) and value else value)
                        for name, value in printer["credentials"].items()
                    }
                }
                if printer.get("credentials")
                else {}
            ),
        }
        for printer in data["printers"]
    ]
    return settings


def readonly_settings(config: AgentConfig) -> dict[str, Any]:
    """What the hub is shown but cannot write.

    `agent_token` is deliberately absent: it is the credential this very session
    authenticated with, so the hub already has it, and echoing a secret back
    serves nothing.
    """
    return {
        "hub_url": config.hub_url,
        "outbox": {"database_path": str(config.outbox.database_path)},
    }


def apply_remote_settings(
    settings: Any, current: AgentConfig
) -> tuple[AgentConfig, TransferReport]:
    """Merge a hub `settings_update` over ``current``.

    A partial change set, not a mirror: a key the sender omits keeps the value
    the agent has, so the file on the machine stays the source of truth for
    everything the hub did not mention. ``printers`` is the exception — present
    means "this is the whole list", because there is no other way to express a
    removal.
    """
    if not isinstance(settings, dict):
        raise BundleError("settings must be a mapping")

    report = TransferReport()
    base = config_to_dict(current)

    for key in REMOTE_BLOCKED_KEYS:
        if key in settings:
            report.rejected.append(key)
    for key, names in REMOTE_BLOCKED_SUBKEYS.items():
        section = settings.get(key)
        if isinstance(section, dict):
            report.rejected.extend(name for name in names if name in section)

    for key in ("telemetry_interval_s", "heartbeat_interval_s"):
        if settings.get(key) not in (None, ""):
            base[key] = settings[key]
            report.applied.append(key)

    backoff = settings.get("command_reconnect_backoff_s")
    if isinstance(backoff, dict) and backoff:
        base["command_reconnect_backoff_s"] = {
            **base["command_reconnect_backoff_s"],
            **{name: backoff[name] for name in ("min", "max") if name in backoff},
        }
        report.applied.append("command_reconnect_backoff_s")

    outbox = settings.get("outbox")
    if isinstance(outbox, dict) and outbox.get("max_events"):
        base["outbox"]["max_events"] = outbox["max_events"]
        report.applied.append("outbox.max_events")

    for section in ("print_files", "updates"):
        incoming = settings.get(section)
        if isinstance(incoming, dict) and incoming:
            base[section] = {**base[section], **incoming}
            report.applied.append(section)

    if "printers" in settings:
        incoming_printers = settings["printers"]
        if not isinstance(incoming_printers, list):
            raise BundleError("printers must be a list")
        base["printers"] = _merge_printers(incoming_printers, current.printers, report)
        report.applied.append(f"printers[{len(base['printers'])}]")
    else:
        report.kept_local.append("printers")

    return config_from_dict(base), report


def _is_secret(name: str) -> bool:
    return name.lower() in SECRET_CREDENTIAL_KEYS
