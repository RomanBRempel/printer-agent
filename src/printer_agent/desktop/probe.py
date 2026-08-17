"""Background printer polling for the desktop app.

Adapters are async and some of them (Bambu) hold a long-lived subscription, so
the probe owns one asyncio loop on a worker thread and keeps adapter instances
alive between cycles. The UI thread only ever sees emitted signals.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..adapters.base import PrinterAdapter
from ..aio import run as run_async
from ..config import PrinterConfig
from ..contracts import PrinterSnapshot
from ..core.outbox import EventOutbox
from ..core.registry import build_adapter
from .system import ServiceInfo, query_service

PROBE_TIMEOUT_S = 12.0


async def _close_quietly(adapter: PrinterAdapter) -> None:
    """Release an adapter's connection without letting the failure escape.

    Whoever builds an adapter owns closing it: aiohttp holds an open session
    inside one, and a collected session logs `Unclosed client session` and
    strands its socket.
    """
    try:
        await asyncio.wait_for(adapter.disconnect(), timeout=3)
    except Exception:
        pass


@dataclass(slots=True)
class ProbeResult:
    printer_key: str
    snapshot: PrinterSnapshot | None
    error: str = ""


class PrinterProbe(QThread):
    """Polls every configured printer on an interval until stopped."""

    result_ready = Signal(object)  # ProbeResult
    cycle_finished = Signal()

    def __init__(self, printers: list[PrinterConfig], interval_s: int = 10, parent=None):
        super().__init__(parent)
        self._printers = list(printers)
        self._interval_s = max(3, int(interval_s))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False

    # -- control (called from the UI thread) -------------------------------- #

    def request_refresh(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wake.set)

    def request_stop(self) -> None:
        self._stopping = True
        self.request_refresh()

    def set_interval(self, interval_s: int) -> None:
        self._interval_s = max(3, int(interval_s))
        self.request_refresh()

    # -- worker thread ------------------------------------------------------ #

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            run_async(self._main())
        except Exception:
            # A dead probe must never take the window down with it.
            pass

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        adapters: dict[str, PrinterAdapter] = {}

        try:
            while not self._stopping:
                for printer in self._printers:
                    if self._stopping:
                        break
                    self.result_ready.emit(await self._probe(printer, adapters))
                self.cycle_finished.emit()
                if self._stopping:
                    break
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)
                except (TimeoutError, asyncio.TimeoutError):
                    pass
                self._wake.clear()
        finally:
            for adapter in adapters.values():
                try:
                    await asyncio.wait_for(adapter.disconnect(), timeout=5)
                except Exception:
                    pass
            self._loop = None
            self._wake = None

    async def _probe(self, printer: PrinterConfig, adapters: dict[str, PrinterAdapter]) -> ProbeResult:
        adapter = adapters.get(printer.key)
        try:
            if adapter is None:
                adapter = build_adapter(printer)
                # Bambu's broker evicts a duplicate client id, which would drop
                # the service's own session; take a distinct one for the UI.
                if hasattr(adapter, "client_identifier"):
                    serial = str((printer.credentials or {}).get("serial", "")).strip()
                    adapter.client_identifier = f"{serial}-agent-ui" if serial else None
                # The camera port has no such escape: it serves one client at a
                # time, so probing it here would take the stream away from the
                # service session that is sending frames to the hub.
                if hasattr(adapter, "camera_probes_enabled"):
                    adapter.camera_probes_enabled = False
                await asyncio.wait_for(adapter.connect(), timeout=PROBE_TIMEOUT_S)
                adapters[printer.key] = adapter
            snapshot = await asyncio.wait_for(adapter.get_state(), timeout=PROBE_TIMEOUT_S)
        except (TimeoutError, asyncio.TimeoutError):
            # A `connect()` that timed out never reached the line that files the
            # adapter, so this frame holds the only reference to it — and it is
            # already carrying an open HTTP session. Dropping it here leaks that
            # session, once per probe, for as long as the printer stays
            # unreachable. An adapter that *is* filed keeps its session on
            # purpose: only the state query timed out, and the next cycle reuses
            # the connection.
            if adapter is not None and adapters.get(printer.key) is not adapter:
                await _close_quietly(adapter)
            return ProbeResult(printer_key=printer.key, snapshot=None, error="Таймаут опроса принтера")
        except Exception as exc:
            adapters.pop(printer.key, None)
            if adapter is not None:
                await _close_quietly(adapter)
            return ProbeResult(printer_key=printer.key, snapshot=None, error=str(exc) or exc.__class__.__name__)
        return ProbeResult(printer_key=printer.key, snapshot=snapshot)


class TaskRunner(QThread):
    """Runs one blocking callable off the UI thread and reports back once.

    Keep a reference on the caller: a garbage-collected QThread aborts mid-run.
    """

    completed = Signal(object, str)  # result, error

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self._function = function

    def run(self) -> None:  # noqa: D102 - QThread entry point
        try:
            self.completed.emit(self._function(), "")
        except Exception as exc:
            self.completed.emit(None, str(exc) or exc.__class__.__name__)


@dataclass(slots=True)
class HealthSnapshot:
    service: ServiceInfo = field(default_factory=ServiceInfo)
    pending_events: int | None = None
    command_results: int | None = None
    outbox_error: str = ""


class HealthWatcher(QThread):
    """Polls the Windows service and the outbox counters off the UI thread."""

    updated = Signal(object)  # HealthSnapshot

    def __init__(self, outbox_path: Path | None, interval_s: int = 5, parent=None):
        super().__init__(parent)
        self._outbox_path = Path(outbox_path) if outbox_path else None
        self._interval_s = max(2, int(interval_s))
        self._wake = threading.Event()
        self._stopping = False

    def set_outbox_path(self, outbox_path: Path | None) -> None:
        self._outbox_path = Path(outbox_path) if outbox_path else None
        self.request_refresh()

    def request_refresh(self) -> None:
        self._wake.set()

    def request_stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        while not self._stopping:
            try:
                self.updated.emit(self._collect())
            except Exception:
                pass
            self._wake.wait(self._interval_s)
            self._wake.clear()

    def _collect(self) -> HealthSnapshot:
        snapshot = HealthSnapshot(service=query_service())
        if self._outbox_path is None:
            return snapshot
        if not self._outbox_path.exists():
            snapshot.outbox_error = "Файл outbox ещё не создан."
            return snapshot
        outbox: EventOutbox | None = None
        try:
            outbox = EventOutbox(self._outbox_path)
            summary = outbox.summary()
            snapshot.pending_events = summary["pending_events"]
            snapshot.command_results = summary["command_results"]
        except Exception as exc:
            snapshot.outbox_error = str(exc)
        finally:
            if outbox is not None:
                try:
                    outbox.close()
                except Exception:
                    pass
        return snapshot
