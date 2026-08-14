from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

PROTOCOL_VERSION = 1


class MessageType(StrEnum):
    hello = "hello"
    telemetry = "telemetry"
    event = "event"
    command_result = "command_result"
    heartbeat = "heartbeat"
    hello_ack = "hello_ack"
    hello_reject = "hello_reject"
    command = "command"
    file_offer = "file_offer"
    camera_request = "camera_request"
    camera_stop = "camera_stop"
    ack = "ack"
    error = "error"


AGENT_TO_HUB_TYPES = frozenset(
    {
        MessageType.hello.value,
        MessageType.telemetry.value,
        MessageType.event.value,
        MessageType.command_result.value,
        MessageType.heartbeat.value,
    }
)

HUB_TO_AGENT_TYPES = frozenset(
    {
        MessageType.hello_ack.value,
        MessageType.hello_reject.value,
        MessageType.command.value,
        MessageType.file_offer.value,
        MessageType.camera_request.value,
        MessageType.camera_stop.value,
        MessageType.ack.value,
        MessageType.error.value,
    }
)

# Hub messages that carry a command_id and are answered with a command_result.
COMMAND_BEARING_TYPES = frozenset(
    {
        MessageType.command.value,
        MessageType.file_offer.value,
        MessageType.camera_request.value,
        MessageType.camera_stop.value,
    }
)


class EventKind(StrEnum):
    snapshot = "snapshot"
    status_changed = "status_changed"
    job_changed = "job_changed"
    error_changed = "error_changed"


class CommandAction(StrEnum):
    start_print = "start_print"
    pause = "pause"
    resume = "resume"
    cancel = "cancel"
    upload_file = "upload_file"


class HelloRejectCode(StrEnum):
    protocol_unsupported = "protocol_unsupported"
    auth_rejected = "auth_rejected"
    location_unknown = "location_unknown"
    temporarily_unavailable = "temporarily_unavailable"


# Only a hub that says it is busy is worth reconnecting to. Every other code, an
# unknown code and a missing code are terminal: retrying cannot change the answer,
# and treating them as network failures is what produces an endless reconnect loop.
RETRYABLE_HELLO_REJECT_CODES = frozenset({HelloRejectCode.temporarily_unavailable.value})


def is_retryable_hello_reject(code: str) -> bool:
    return code in RETRYABLE_HELLO_REJECT_CODES


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


_JOB_STATUS_BY_PRINTER_STATUS = {
    PrinterStatus.printing: JobStatus.printing,
    PrinterStatus.paused: JobStatus.paused,
    PrinterStatus.finished: JobStatus.finished,
    PrinterStatus.error: JobStatus.failed,
}


def job_status_for(printer_status: PrinterStatus) -> JobStatus | None:
    """Job status implied by a printer status, or None when no job is on the bed.

    `PrinterStatus` and `JobStatus` are separate vocabularies on the wire: idle,
    offline and maintenance describe a machine, not a job, so they map to no job
    status at all rather than leaking a printer value into `job.status`.
    """
    return _JOB_STATUS_BY_PRINTER_STATUS.get(printer_status)


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
