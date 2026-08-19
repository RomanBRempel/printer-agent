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
from pathlib import Path
from pathlib import PurePosixPath
from collections.abc import Mapping
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

#: mjpg-streamer's port on a rooted K-series: the printer's own web UI loads its
#: camera from `http://<host>:8080/?action=stream`, so the still is the same
#: server's `?action=snapshot`. The vendor firmware does not open this port until
#: the streamer service is running, which is why `camera` is a probe and not a
#: brand rule — a raised flag on a printer whose streamer is off would show the
#: operator a button that only ever answers `unsupported`.
CREALITY_CAMERA_PORT = 8080

#: Snapshot URLs tried when the printer entry names none. The first is
#: mjpg-streamer's own; the second is the nginx-fronted layout some rooted images
#: use. Anything else has to be configured explicitly via `camera_snapshot_url`.
CAMERA_SNAPSHOT_CANDIDATES: tuple[str, ...] = (
    "http://{host}:8080/?action=snapshot",
    "http://{host}/webcam/?action=snapshot",
)

#: A snapshot request has to fail fast: it runs inside the hub's frame interval.
CAMERA_TIMEOUT_S = 10.0

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


def _looks_like_image(payload: bytes) -> bool:
    """Judge a probe answer by its bytes: a captive portal also returns 200."""
    return payload.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (
        payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    )


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
        #: Snapshot URL, configured or found by probing. Empty means no camera,
        #: and `capabilities.camera` says so. A separate HTTP session serves it:
        #: the mjpg-streamer port is not the vendor WebSocket this adapter holds.
        self._camera_url = printer.camera_snapshot_url.strip()
        self._camera_probed = bool(self._camera_url)
        self._http_session: aiohttp.ClientSession | None = None

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
        # The camera rides its own HTTP port, independent of the vendor socket;
        # probe it once here so the `hello` capabilities are already right.
        await self._probe_camera()

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._connection_task is not None:
            self._connection_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._connection_task
        self._connection_task = None
        self._websocket = None
        self._connected = False
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

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
            # Follows the probe, not the brand: the mjpg-streamer port is closed
            # on stock firmware, so a raised flag would be a dead button.
            camera=bool(self._camera_url),
        )

    async def start_print(
        self,
        file_ref: str,
        remote_name: str | None = None,
        ams_mapping: Mapping[int, int] | None = None,
        local_path: str | Path | None = None,
    ) -> dict[str, Any]:
        # Accepted for one signature across adapters; this printer has no
        # addressable feeding system to map filaments onto.
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

    async def get_camera_frame(self) -> bytes:
        if not self._camera_url:
            raise UnsupportedCommandError(
                f"no camera snapshot URL for {self.printer.key}; set camera_snapshot_url"
            )
        session = self._ensure_http_session()
        async with session.get(
            self._camera_url, timeout=aiohttp.ClientTimeout(total=CAMERA_TIMEOUT_S)
        ) as response:
            response.raise_for_status()
            frame = await response.read()
        if not frame:
            raise RuntimeError(f"camera at {self._camera_url} returned an empty frame")
        return frame

    async def _probe_camera(self) -> None:
        """Find a snapshot endpoint once, or leave the capability switched off."""
        if self._camera_probed:
            return
        self._camera_probed = True
        session = self._ensure_http_session()
        for template in CAMERA_SNAPSHOT_CANDIDATES:
            url = template.format(host=self.printer.host)
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=CAMERA_TIMEOUT_S)
                ) as response:
                    if response.status != 200:
                        continue
                    content_type = response.headers.get("Content-Type", "")
                    frame = await response.read()
            except Exception:
                continue
            if frame and (content_type.startswith("image/") or _looks_like_image(frame)):
                self._camera_url = url
                return

    def _ensure_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

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
            filament_used_mm=self._filament_used_mm(state, status),
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
    def _filament_used_mm(state: dict[str, Any], status: PrinterStatus) -> float | None:
        """Extruded filament of the current print, in millimetres.

        `usedMaterialLength` is a running counter that the firmware resets with
        each print — watched growing on two K-series machines mid-job, in step
        with progress. The unit is not a guess either: the printer's own web UI
        renders the field as `usedMaterialLength + "mm"`
        (`http://<printer>/static/js/app.*.js`, the only documentation this
        protocol has). Metres would have understated the shop by a thousand,
        which reads as plausible in a report rather than as a fault.

        Reported only while a print is running. The field keeps the last job's
        total when the machine is idle, and the hub remembers the largest value
        it has seen for a job — so a stale total arriving with the first
        snapshot of the next print would be recorded as that print's spend.
        """
        if status not in (PrinterStatus.printing, PrinterStatus.paused):
            return None
        return CrealityAdapter._safe_float(state.get("usedMaterialLength"))

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
