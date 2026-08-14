from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import ErrorSnapshot, JobSnapshot, PrinterSnapshot, PrinterStatus, TemperatureSnapshot


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

    def update(self, snapshot: PrinterSnapshot) -> StateChange | None:
        previous = self._snapshots.get(snapshot.printer_key)
        kinds = self._diff(previous, snapshot)
        self._snapshots[snapshot.printer_key] = snapshot
        if not kinds:
            return None
        return StateChange(snapshot.printer_key, previous, snapshot, tuple(kinds))

    def _diff(self, previous: PrinterSnapshot | None, current: PrinterSnapshot) -> list[str]:
        if previous is None:
            return ["snapshot"]
        kinds: list[str] = []
        if previous.status != current.status:
            kinds.append("status_changed")
        if self._job_signature(previous.job) != self._job_signature(current.job):
            kinds.append("job_changed")
        if self._error_signature(previous.error) != self._error_signature(current.error):
            kinds.append("error_changed")
        return kinds

    @staticmethod
    def _job_signature(job: JobSnapshot) -> tuple[Any, ...]:
        return (
            job.name,
            job.progress_pct,
            job.layer,
            job.layers_total,
            job.time_elapsed_s,
            job.time_remaining_s,
            job.status,
        )

    @staticmethod
    def _error_signature(error: ErrorSnapshot) -> tuple[Any, ...]:
        return (error.code, error.message)
