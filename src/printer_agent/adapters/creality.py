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
import uuid
from contextlib import suppress
from pathlib import Path
from pathlib import PurePosixPath
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import aiohttp

from ..config import PrinterConfig, normalize_device_id
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


#: The ways a Creality printer is known to take a file and start it. The
#: firmware documents none, so none is assumed: `upload_file` tries them in this
#: order, starting with the one this printer took last time
#: (`printers[].upload_route`), and the agent records whichever worked — nobody
#: types an address.
#:
#: `upload` — multipart `POST /upload/<name>`, then `opGcodeFile:
#:   "printprt:<dir>/<name>"` over the WebSocket: the firmware web UI's own
#:   route (`static/js/app.*.js`) and OrcaSlicer's CrealityPrint host. Measured
#:   12.10.2026 on two K1, a K1C, the Ender-5 Max and a K2 Plus: all take it.
#: `api` — multipart `POST /api/files/upload`, then `{"print": <name>}`. The
#:   same five machines answer it `501 Not Implemented`; kept second for a
#:   firmware that still has it, since it once delivered to the Ender-5 Max.
#: `moonraker` — `POST :7125/server/files/upload`, then
#:   `POST :7125/printer/print/start`, for firmware that serves Moonraker (a
#:   rooted K-series). Port 7125 is closed on all five measured machines.
UPLOAD_ROUTE_API = "api"
UPLOAD_ROUTE_UPLOAD = "upload"
UPLOAD_ROUTE_MOONRAKER = "moonraker"
UPLOAD_ROUTES: tuple[str, ...] = (UPLOAD_ROUTE_UPLOAD, UPLOAD_ROUTE_API, UPLOAD_ROUTE_MOONRAKER)

MOONRAKER_PORT = 7125

#: Where the firmware keeps G-code, when the printer has not said. Measured on
#: 12.10.2026: K1, K1C and the Ender-5 Max keep it under `/usr/data`, the K2 Plus
#: under `/mnt/UDISK`. OrcaSlicer's rule ("K1 family `/usr/data`, the rest
#: `/mnt/UDISK`") is wrong for the Ender — which is why this is the last resort,
#: after the printer's own file list and the file it last reported.
_USR_GCODE_DIR = "/usr/data/printer_data/gcodes"
_UDISK_GCODE_DIR = "/mnt/UDISK/printer_data/gcodes"
_UDISK_MODELS = frozenset({"F008", "F012", "F021", "F022"})  # K2 Plus, K2 Pro, K2, SPARKX i7
_GCODES_SUFFIX = "/printer_data/gcodes"

#: Asks for the file list; the answer names each file's directory.
REQUEST_FILES = {"method": "get", "params": {"reqGcodeFile": 1}}

#: How long to wait for the file list after an upload. Short: without it the
#: start falls back to the reported file and the model rule.
FILE_LIST_WAIT_S = 5.0


def creality_gcode_dir(state: Mapping[str, Any], known_dir: str = "") -> str:
    """The directory `opGcodeFile` addresses a stored file in, for this printer.

    The printer's own word first: the directory its file list gave for this
    file, then the file it last reported. The model rule only stands in when
    it has said neither.
    """
    if known_dir:
        return known_dir
    reported = str(state.get("printFileName") or "")
    if _GCODES_SUFFIX + "/" in reported:
        return reported.split(_GCODES_SUFFIX + "/", 1)[0] + _GCODES_SUFFIX
    model = str(state.get("model") or "").strip().upper()
    hostname = str(state.get("hostname") or "").strip().upper()
    if model in _UDISK_MODELS or hostname.startswith("K2"):
        return _UDISK_GCODE_DIR
    return _USR_GCODE_DIR


def file_dirs_from_list(payload: Mapping[str, Any]) -> dict[str, str]:
    """`{name: directory}` from either file-list answer the firmware sends.

    `retGcodeFileInfo` is `"dir:name:size:...;"` records (what the web UI
    reads); `retGcodeFileInfo2` is a list of objects whose `path` is the full
    file path. Firmware differs in which it sends — the Ender-5 Max only the
    first, the K-series both.
    """
    dirs: dict[str, str] = {}
    first = payload.get("retGcodeFileInfo")
    if isinstance(first, Mapping):
        for record in str(first.get("fileInfo") or "").split(";"):
            parts = record.split(":")
            if len(parts) >= 2 and parts[0] and parts[1]:
                dirs[parts[1]] = parts[0]
    second = payload.get("retGcodeFileInfo2")
    if isinstance(second, list):
        for item in second:
            if isinstance(item, Mapping):
                path = str(item.get("path") or "")
                if "/" in path:
                    directory, _, name = path.rpartition("/")
                    dirs[name] = directory
    return dirs


#: Read size for streaming a print file into the request body.
_UPLOAD_CHUNK = 256 * 1024


def multipart_upload(
    fields: Mapping[str, str], name: str, source: Path
) -> tuple[Any, int, str]:
    """A `multipart/form-data` body streamed from disk, with its exact length.

    Built here rather than by `aiohttp.FormData`, whose part headers put
    `Content-Type` before `Content-Disposition`: the libhv server in K-series
    firmware drops the connection on that order — measured 12.10.2026 on two
    K1, a K1C and a K2 Plus, about half the uploads cut with WinError 64 /
    `ServerDisconnectedError`, while the same file in curl's order (disposition
    first, length known) went through every time. The length is sent up front
    so the body is never chunked, which the same server is not known to accept.
    """
    boundary = f"----rdcontrol{uuid.uuid4().hex}"
    head = b"".join(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
        for field, value in fields.items()
    )
    head += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    length = len(head) + source.stat().st_size + len(tail)

    async def stream():
        yield head
        with source.open("rb") as handle:
            while chunk := handle.read(_UPLOAD_CHUNK):
                yield chunk
        yield tail

    return stream(), length, f"multipart/form-data; boundary={boundary}"


class _RouteAbsent(Exception):
    """This route is not there on this printer; the next one may be."""


#: Answers meaning the endpoint is not there at all, as opposed to a refusal of
#: this file or a fault a retry may clear.
_UPLOAD_ABSENT_STATUSES = frozenset({404, 405, 501})


def _creality_uploaded_name(name: str) -> str:
    """Keep only the printer-safe filename, with no path separators."""
    cleaned = str(name or "").strip().replace("\\", "/")
    stem = PurePosixPath(cleaned).name or cleaned
    return stem or "print.gcode"


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
    hostname = normalize_device_id(state.get("hostname"))
    return {
        "brand": "creality",
        "host": host,
        "port": port,
        "name": str(state.get("hostname") or "") or host,
        "model": str(state.get("model") or ""),
        "serial": "",
        "device_ids": [hostname] if hostname else [],
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
        #: The firmware hostname *this* socket reported. Kept apart from the
        #: merged state, which outlives a reconnect: after one, the old value
        #: would vouch for whatever device now holds the address.
        self._hostname = ""
        self._connected = False
        self._last_error: str = ""
        #: Snapshot URL, configured or found by probing. Empty means no camera,
        #: and `capabilities.camera` says so. A separate HTTP session serves it:
        #: the mjpg-streamer port is not the vendor WebSocket this adapter holds.
        self._camera_url = printer.camera_snapshot_url.strip()
        self._camera_probed = bool(self._camera_url)
        #: Every route refused a file in this process: `upload` goes down so the
        #: hub stops offering a button whose only answer is "unsupported". Not
        #: written anywhere — a firmware update may open a route, and a restart
        #: is when the agent looks again.
        self._no_upload_route = False
        #: Name Moonraker stored a file under, by the name it was offered:
        #: Moonraker may normalise a name, and the start must use the stored one.
        self._stored_by_name: dict[str, str] = {}
        #: Directory of each file, as the printer's own file list gave it.
        self._file_dirs: dict[str, str] = {}
        self._file_list_event = asyncio.Event()
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

    async def device_ids(self) -> frozenset[str]:
        """The hostname the firmware reports, e.g. `K1C-B24E`.

        This socket offers no serial and no MAC, but the K-series firmware
        builds its hostname from the last two bytes of the MAC, so it is as
        stable as the board. Empty until the first full state frame.
        """
        return frozenset({self._hostname}) if self._hostname else frozenset()

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
            upload=not self._no_upload_route,
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
        """Start a stored file by the route that delivered it.

        The start is the second half of a route, not a separate choice: a file
        that went in by `/upload/<name>` is not known to the `print` command.
        With no route learned yet (a file put there by hand, before any upload
        through the agent) the `api` command is the one that has worked.
        Adapters are expected to accept the common signature even when the
        machine has no AMS mapping to apply.
        """
        requested = (remote_name or file_ref or "").strip()
        if not requested:
            raise RuntimeError("start_print needs a file name")
        filename = _creality_uploaded_name(requested)
        route = self.printer.upload_route or UPLOAD_ROUTE_API
        if route == UPLOAD_ROUTE_MOONRAKER:
            stored = self._stored_by_name.get(filename, filename)
            session = self._ensure_http_session()
            url = f"http://{self.printer.host}:{MOONRAKER_PORT}/printer/print/start"
            async with session.post(
                url, params={"filename": stored}, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status >= 400:
                    body = (await response.text())[:300]
                    raise RuntimeError(f"{url} answered {response.status} for {stored}: {body}")
            return {"ok": True, "filename": stored, "requested_name": requested, "route": route}
        if route == UPLOAD_ROUTE_UPLOAD:
            # The firmware's own web UI form (`app.*.js`: `printprt:` + dir + name),
            # confirmed on K1 and K1C 1.0.0. The firmware may close the
            # socket right after accepting it; the connection loop reconnects,
            # and the print has already started.
            params: dict[str, Any] = {
                "opGcodeFile": (
                    f"printprt:{creality_gcode_dir(self._state, self._file_dirs.get(filename, ''))}"
                    f"/{filename}"
                ),
            }
        else:
            params = {"print": filename}
        await self._send(params)
        return {
            "ok": True,
            "filename": filename,
            "requested_name": requested,
            "route": route,
            "command": params,
        }

    async def upload_file(self, local_path: str | Path, remote_name: str) -> dict[str, Any]:
        """Deliver a file by the first route this printer accepts, and remember it.

        The route that worked last time goes first, so a known printer costs one
        request; the others follow in `UPLOAD_ROUTES` order. A route whose
        address is not there (404/405/501, or its port closed) passes to the
        next. Any other refusal stops here as a failure: the printer took the
        request and said no to this file, and a different route would not
        change that. Only when every route is absent is the answer
        `unsupported` — naming each answer, since that is the whole diagnosis.
        """
        source = Path(local_path)
        if not source.is_file():
            raise RuntimeError(f"{source} is not a file")
        name = _creality_uploaded_name((remote_name or source.name).strip() or source.name)
        learned = self.printer.upload_route
        order = ([learned] if learned in UPLOAD_ROUTES else []) + [
            route for route in UPLOAD_ROUTES if route != learned
        ]
        refusals: list[str] = []
        for route in order:
            try:
                result = await self._upload_by(route, source, name)
            except _RouteAbsent as absent:
                refusals.append(f"{route}: {absent}")
                continue
            self._no_upload_route = False
            self._remember_route(route)
            return {"ok": True, "root": "gcodes", "route": route, **result}
        self._no_upload_route = True
        raise UnsupportedCommandError(
            f"{self.printer.key} accepts files by none of the known routes — "
            + "; ".join(refusals)
        )

    async def _upload_by(self, route: str, source: Path, name: str) -> dict[str, Any]:
        """One route's upload; raises `_RouteAbsent` when the route is not there."""
        host = self.printer.host
        if route == UPLOAD_ROUTE_API:
            url = f"http://{host}/api/files/upload"
            fields = {"print": "false"}
        elif route == UPLOAD_ROUTE_UPLOAD:
            url = f"http://{host}/upload/{quote(name, safe='')}"
            fields = {}
        else:
            url = f"http://{host}:{MOONRAKER_PORT}/server/files/upload"
            fields = {"root": "gcodes"}
        session = self._ensure_http_session()
        body_stream, length, content_type = multipart_upload(fields, name, source)
        try:
            async with session.post(
                url,
                data=body_stream,
                headers={"Content-Type": content_type, "Content-Length": str(length)},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=300),
            ) as response:
                if response.status in _UPLOAD_ABSENT_STATUSES:
                    raise _RouteAbsent(f"{url} answered {response.status}")
                if response.status >= 400:
                    body = (await response.text())[:300]
                    raise RuntimeError(f"{url} answered {response.status}: {body}")
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = {}
        except aiohttp.ClientConnectorError as exc:
            # The port is closed: this firmware does not serve the route at all.
            raise _RouteAbsent(f"{url} refused the connection ({exc.os_error})") from exc
        body = body if isinstance(body, dict) else {}
        stored = name
        if route == UPLOAD_ROUTE_UPLOAD:
            await self._learn_file_dir(name)
        if route == UPLOAD_ROUTE_MOONRAKER:
            item = (body.get("result") or body).get("item") or {}
            stored = str(item.get("path") or name)
            self._stored_by_name[name] = stored
        return {"remote_name": stored, "path": stored}

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
                        self._hostname = ""
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

    async def _learn_file_dir(self, name: str) -> None:
        """Ask the printer where it put `name`; best effort, never fails the upload."""
        if name in self._file_dirs:
            return
        self._file_list_event.clear()
        try:
            await self._send(REQUEST_FILES)
            deadline = asyncio.get_running_loop().time() + FILE_LIST_WAIT_S
            while name not in self._file_dirs:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return
                self._file_list_event.clear()
                await asyncio.wait_for(self._file_list_event.wait(), timeout=remaining)
        except Exception:
            return

    def _handle_frame(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        # The file list is an answer, not state: kept out of the cache it would
        # ride along in every merge, and it can run to hundreds of files.
        if "retGcodeFileInfo" in payload or "retGcodeFileInfo2" in payload:
            self._file_dirs.update(file_dirs_from_list(payload))
            self._file_list_event.set()
            payload = {
                key: value
                for key, value in payload.items()
                if key not in ("retGcodeFileInfo", "retGcodeFileInfo2")
            }
        updates = {key: value for key, value in payload.items() if key not in IGNORED_FIELDS}
        if not updates:
            return
        self._state.update(updates)
        if updates.get("hostname"):
            self._hostname = normalize_device_id(updates["hostname"])
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
