from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import (
    ErrorSnapshot,
    EventKind,
    JobSnapshot,
    PrinterSnapshot,
    PrinterStatus,
    TemperatureSnapshot,
)


@dataclass(slots=True)
class StateChange:
    printer_key: str
    previous: PrinterSnapshot | None
    current: PrinterSnapshot
    kinds: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.kinds)


class PrinterStateStore:
    def __init__(self) -> None:
        self._snapshots: dict[str, PrinterSnapshot] = {}

    def get(self, printer_key: str) -> PrinterSnapshot | None:
        return self._snapshots.get(printer_key)

    def forget(self, printer_key: str) -> None:
        """Drop a printer's history when it leaves the config.

        Without this, a printer removed and later added back would be diffed
        against state from before it left, and the first observation after its
        return would raise no `snapshot` event.
        """
        self._snapshots.pop(printer_key, None)

    def update(self, snapshot: PrinterSnapshot) -> StateChange | None:
        previous = self._snapshots.get(snapshot.printer_key)
        kinds = self._diff(previous, snapshot)
        self._snapshots[snapshot.printer_key] = snapshot
        if not kinds:
            return None
        return StateChange(snapshot.printer_key, previous, snapshot, tuple(kinds))

    def _diff(self, previous: PrinterSnapshot | None, current: PrinterSnapshot) -> list[str]:
        if previous is None:
            return [EventKind.snapshot.value]
        kinds: list[str] = []
        if previous.status != current.status:
            kinds.append(EventKind.status_changed.value)
        if self._job_signature(previous.job) != self._job_signature(current.job):
            kinds.append(EventKind.job_changed.value)
        if self._error_signature(previous.error) != self._error_signature(current.error):
            kinds.append(EventKind.error_changed.value)
        return kinds

    @staticmethod
    def _job_signature(job: JobSnapshot) -> tuple[Any, ...]:
        """Identity and phase of the job, deliberately without moving values.

        Progress, layer and timing counters change on every poll; including them
        would put a durable event in the outbox each cycle of a print. They ride
        along in lossy telemetry instead, same as temperature drift.
        """
        return (job.name, job.status)

    @staticmethod
    def _error_signature(error: ErrorSnapshot) -> tuple[Any, ...]:
        return (error.code, error.message)
