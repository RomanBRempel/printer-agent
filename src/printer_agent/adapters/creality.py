"""Creality's own LAN protocol: a WebSocket on port 9999.

The K-series ships Klipper behind a Creality front end, but Moonraker's port
7125 is closed on most of that firmware — only this socket is exposed, and it is
what Creality Print and the printer's built-in web UI both speak. A printer that
*does* answer on 7125 is better served by the Moonraker adapter; this one exists
for the rest.

The printer pushes a flat JSON state object on its own schedule and answers
`{"method": "get", ...}` immediately, so the adapter holds one long-lived
connection and serves `get_state()` from a merged cache — the same shape as the
Bambu adapter, and necessary here too: the firmware caps concurrent clients, so
connecting per poll would lock other consumers out.

Every message shape below is taken from the firmware's own web UI
(`http://<printer>/static/js/app.*.js`), not guessed.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import PurePosixPath
from typing import Any

import aiohttp

from ..config import PrinterConfig
from ..contracts import (
    ErrorSnapshot,
    JobSnapshot,
    JobStatus,
    PrinterCapabilities,
    PrinterSnapshot,
    PrinterStatus,
    TemperatureSnapshot,
    job_status_for,
    utc_now_iso,
)
from .base import PrinterAdapter, UnsupportedCommandError

CREALITY_WS_PORT = 9999

#: The web UI's own cadence (`{time: 5e3}`); the firmware drops a silent client.
HEARTBEAT_INTERVAL_S = 5.0

#: Asking for the parameter block yields a full state frame at once, instead of
#: waiting out the printer's push interval.
REQUEST_STATE = {"method": "get", "params": {"reqPrinterPara": 1}}

#: Print-geometry blobs — tens of KB per frame, and nothing here reads them.
IGNORED_FIELDS = frozenset({"objects", "excluded_objects"})

#: `state` indexes the web UI's own status list:
#: ["Printing stopped", "Printing", "printing complete", "Printing failed",
#:  "print abort", "Printing Paused"]
_CREALITY_STATE_MAP = {
    0: PrinterStatus.idle,
    1: PrinterStatus.printing,
    2: PrinterStatus.finished,
    3: PrinterStatus.error,
    4: PrinterStatus.finished,
    5: PrinterStatus.paused,
}

#: "print abort" — the job ended because someone stopped it, which is a finished
#: printer with a cancelled job rather than a failure.
CREALITY_ABORTED_STATE = 4


def normalize_creality_status(raw_state: Any) -> PrinterStatus:
    try:
        value = int(raw_state)
    except (TypeError, ValueError):
        return PrinterStatus.maintenance
    return _CREALITY_STATE_MAP.get(value, PrinterStatus.maintenance)


async def discover_creality(
    hosts: list[str], *, port: int = CREALITY_WS_PORT, timeout_s: float = 1.5
) -> list[dict[str, Any]]:
    """Probe hosts for the Creality WebSocket.

    Like Klipper, this firmware announces nothing, so discovery is per-address.
    A bare TCP connect comes first because it fails fast on the ~99% of
    addresses that are not printers.
    """
    results = await asyncio.gather(*(_probe_creality_host(host, port, timeout_s) for host in hosts))
    return [item for item in results if item is not None]


async def _probe_creality_host(host: str, port: int, timeout_s: float) -> dict[str, Any] | None:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
    except (OSError, asyncio.TimeoutError, TimeoutError):
        return None
    writer.close()
    with suppress(OSError, asyncio.TimeoutError):
        await writer.wait_closed()

    try:
        state = await asyncio.wait_for(_fetch_state_frame(host, port), timeout=timeout_s)
    except Exception:
        # Something else is listening on 9999.
        return None
    if state is None:
        return None
    return creality_discovery_record(state, host, port)


async def _fetch_state_frame(host: str, port: int) -> dict[str, Any] | None:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://{host}:{port}", heartbeat=None) as websocket:
            await websocket.send_str(json.dumps(REQUEST_STATE))
            while True:
                message = await websocket.receive()
                if message.type is not aiohttp.WSMsgType.TEXT:
                    return None
                try:
                    payload = json.loads(message.data)
                except ValueError:
                    return None
                if isinstance(payload, dict) and "state" in payload:
                    return payload


def creality_discovery_record(state: dict[str, Any], host: str, port: int = CREALITY_WS_PORT) -> dict[str, Any]:
    """Turn a state frame into a discovery record.

    `model` is a marketing name on some machines ("K1C") and an internal code on
    others ("F004" for the Ender-5 Max); it is passed through as reported rather
    than translated, because a wrong translation is worse than a raw code.
    """
    return {
        "brand": "creality",
        "host": host,
        "port": port,
        "name": str(state.get("hostname") or "") or host,
        "model": str(state.get("model") or ""),
        "serial": "",
        "source": "ws",
    }


class CrealityAdapter(PrinterAdapter):
    def __init__(self, printer: PrinterConfig):
        super().__init__(printer)
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._connection_task: asyncio.Task[None] | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._state: dict[str, Any] = {}
        self._connected = False
        self._last_error: str = ""

    @property
    def _ws_url(self) -> str:
        return f"ws://{self.printer.host}:{self.printer.port or CREALITY_WS_PORT}"

    async def connect(self) -> None:
        if self._connection_task is not None and not self._connection_task.done():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._connection_task = asyncio.create_task(self._run_connection_loop())
        # Wait for real state, not just a socket: callers poll immediately after.
        with suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._ready_event.wait(), timeout=15)

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._connection_task is not None:
            self._connection_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._connection_task
        self._connection_task = None
        self._websocket = None
        self._connected = False

    async def get_state(self) -> PrinterSnapshot:
        # The cache is only worth reporting while the session behind it is alive:
        # a printer that was switched off mid-print must not keep reporting
        # `printing` off stale data for as long as the process runs.
        if self._state and self._connected:
            return self._snapshot_from_state(self._state)
        return PrinterSnapshot(
            printer_key=self.printer.key,
            status=PrinterStatus.offline,
            status_raw="offline",
            job=JobSnapshot(),
            temps=TemperatureSnapshot(),
            error=ErrorSnapshot(
                code="offline",
                message=self._last_error or f"No session with {self._ws_url}",
            ),
            capabilities=self.capabilities(),
            ts=utc_now_iso(),
        )

    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(
            pause=True,
            resume=True,
            cancel=True,
            upload=False,
            cfs=bool(self._safe_int(self._state.get("cfsConnect"))),
            camera=False,
        )

    async def start_print(self, file_ref: str, remote_name: str | None = None) -> dict[str, Any]:
        raise UnsupportedCommandError("Creality print start is not implemented yet")

    async def pause(self) -> dict[str, Any]:
        await self._send({"pause": 1})
        return {"ok": True}

    async def resume(self) -> dict[str, Any]:
        await self._send({"pause": 0})
        return {"ok": True}

    async def cancel(self) -> dict[str, Any]:
        await self._send({"stop": 1})
        return {"ok": True}

    async def _send(self, params: dict[str, Any]) -> None:
        websocket = self._websocket
        if websocket is None or websocket.closed:
            raise RuntimeError(f"Creality printer {self.printer.host} is not connected")
        await websocket.send_str(json.dumps({"method": "set", "params": params}))

    async def _run_connection_loop(self) -> None:
        loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                # No total timeout: this session is meant to stay open.
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(self._ws_url, heartbeat=None) as websocket:
                        self._websocket = websocket
                        self._connected = True
                        self._last_error = ""
                        backoff = 1.0
                        await websocket.send_str(json.dumps(REQUEST_STATE))
                        next_beat = loop.time() + HEARTBEAT_INTERVAL_S
                        while not self._stop_event.is_set():
                            wait_for = max(0.1, next_beat - loop.time())
                            try:
                                message = await websocket.receive(timeout=wait_for)
                            except (TimeoutError, asyncio.TimeoutError):
                                # The firmware expects an application-level beat;
                                # a WebSocket ping does not keep it satisfied.
                                await websocket.send_str(
                                    json.dumps({"ModeCode": "heart_beat", "msg": utc_now_iso()})
                                )
                                next_beat = loop.time() + HEARTBEAT_INTERVAL_S
                                continue
                            if message.type is aiohttp.WSMsgType.TEXT:
                                self._handle_frame(message.data)
                                continue
                            if message.type in {
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                self._last_error = f"Printer closed the session ({message.type.name})"
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc) or exc.__class__.__name__
            finally:
                self._websocket = None
                self._connected = False
                # Unblock connect() even when the printer never answered.
                self._ready_event.set()
            if self._stop_event.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)

    def _handle_frame(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        updates = {key: value for key, value in payload.items() if key not in IGNORED_FIELDS}
        if not updates:
            return
        self._state.update(updates)
        if "state" in self._state:
            self._ready_event.set()

    def _snapshot_from_state(self, state: dict[str, Any]) -> PrinterSnapshot:
        raw_state = state.get("state")
        status = normalize_creality_status(raw_state)
        error = self._error_from_state(state)

        job = JobSnapshot(
            name=self._file_name(state.get("printFileName")),
            progress_pct=self._safe_float(state.get("printProgress")),
            layer=self._safe_int(state.get("layer")),
            layers_total=self._safe_int(state.get("TotalLayer")),
            time_elapsed_s=self._safe_int(state.get("printJobTime")),
            time_remaining_s=self._safe_int(state.get("printLeftTime")),
            status=self._job_status(status, raw_state),
        )
        temps = TemperatureSnapshot(
            nozzle=self._safe_float(state.get("nozzleTemp")),
            nozzle_target=self._safe_float(state.get("targetNozzleTemp")),
            bed=self._safe_float(state.get("bedTemp0")),
            bed_target=self._safe_float(state.get("targetBedTemp0")),
            chamber=self._safe_float(state.get("boxTemp")),
        )
        return PrinterSnapshot(
            printer_key=self.printer.key,
            status=status,
            status_raw=f"state={raw_state};deviceState={state.get('deviceState')}",
            job=job,
            temps=temps,
            error=error,
            capabilities=self.capabilities(),
        )

    @staticmethod
    def _job_status(printer_status: PrinterStatus, raw_state: Any) -> JobStatus | None:
        if CrealityAdapter._safe_int(raw_state) == CREALITY_ABORTED_STATE:
            return JobStatus.cancelled
        return job_status_for(printer_status)

    @staticmethod
    def _error_from_state(state: dict[str, Any]) -> ErrorSnapshot:
        error = state.get("err")
        if not isinstance(error, dict):
            return ErrorSnapshot()
        code = CrealityAdapter._safe_int(error.get("errcode"))
        if not code:
            return ErrorSnapshot()
        message = str(error.get("value") or "").strip()
        key = error.get("key")
        return ErrorSnapshot(code=str(code), message=message or f"Creality error {code} (key {key})")

    @staticmethod
    def _file_name(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        # Always a POSIX path on the printer, whatever the agent runs on.
        return PurePosixPath(value).name or None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return None if value is None else int(float(value))
        except (TypeError, ValueError):
            return None
