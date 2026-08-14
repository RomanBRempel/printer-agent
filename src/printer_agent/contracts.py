from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = 1


class PrinterStatus(StrEnum):
    offline = "offline"
    idle = "idle"
    printing = "printing"
    paused = "paused"
    finished = "finished"
    error = "error"
    maintenance = "maintenance"


class JobStatus(StrEnum):
    queued = "queued"
    uploading = "uploading"
    printing = "printing"
    paused = "paused"
    finished = "finished"
    failed = "failed"
    cancelled = "cancelled"


class CommandStatus(StrEnum):
    done = "done"
    failed = "failed"
    unsupported = "unsupported"
    timeout = "timeout"


@dataclass(slots=True)
class JobSnapshot:
    name: str | None = None
    progress_pct: float | None = None
    layer: int | None = None
    layers_total: int | None = None
    time_elapsed_s: int | None = None
    time_remaining_s: int | None = None
    status: JobStatus | None = None


@dataclass(slots=True)
class TemperatureSnapshot:
    nozzle: float | None = None
    nozzle_target: float | None = None
    bed: float | None = None
    bed_target: float | None = None
    chamber: float | None = None


@dataclass(slots=True)
class ErrorSnapshot:
    code: str | None = None
    message: str | None = None


@dataclass(slots=True)
class PrinterCapabilities:
    camera: bool = False
    ams: bool = False
    cfs: bool = False
    pause: bool = False
    resume: bool = False
    cancel: bool = False
    upload: bool = False


@dataclass(slots=True)
class PrinterSnapshot:
    printer_key: str
    status: PrinterStatus
    status_raw: str
    job: JobSnapshot = field(default_factory=JobSnapshot)
    temps: TemperatureSnapshot = field(default_factory=TemperatureSnapshot)
    error: ErrorSnapshot = field(default_factory=ErrorSnapshot)
    capabilities: PrinterCapabilities = field(default_factory=PrinterCapabilities)
    ts: str = field(default_factory=lambda: utc_now_iso())

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer_key": self.printer_key,
            "status": self.status.value,
            "status_raw": self.status_raw,
            "job": _clean_nested(asdict(self.job)),
            "temps": _clean_nested(asdict(self.temps)),
            "error": _clean_nested(asdict(self.error)),
            "capabilities": _clean_nested(asdict(self.capabilities)),
            "ts": self.ts,
        }


@dataclass(slots=True)
class Envelope:
    type: str
    payload: dict[str, Any]
    msg_id: str = field(default_factory=lambda: uuid4().hex)
    v: int = PROTOCOL_VERSION
    ts: str = field(default_factory=lambda: utc_now_iso())

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "type": self.type,
            "msg_id": self.msg_id,
            "ts": self.ts,
            "payload": self.payload,
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_envelope(message_type: str, payload: dict[str, Any], *, msg_id: str | None = None, ts: str | None = None) -> dict[str, Any]:
    envelope = Envelope(type=message_type, payload=payload)
    if msg_id is not None:
        envelope.msg_id = msg_id
    if ts is not None:
        envelope.ts = ts
    return envelope.to_dict()


def _clean_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean_nested(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean_nested(item) for item in value if item is not None]
    if isinstance(value, StrEnum):
        return value.value
    return value
