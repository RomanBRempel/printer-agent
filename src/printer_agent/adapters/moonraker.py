from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from ..config import PrinterConfig
from ..contracts import ErrorSnapshot, JobSnapshot, PrinterCapabilities, PrinterSnapshot, PrinterStatus, TemperatureSnapshot, job_status_for, utc_now_iso
from .base import PrinterAdapter, UnsupportedCommandError


_MOONRAKER_STATUS_MAP = {
    "standby": PrinterStatus.idle,
    "idle": PrinterStatus.idle,
    "ready": PrinterStatus.idle,
    "printing": PrinterStatus.printing,
    "paused": PrinterStatus.paused,
    "complete": PrinterStatus.finished,
    "done": PrinterStatus.finished,
    "error": PrinterStatus.error,
    "cancelled": PrinterStatus.finished,
    "offline": PrinterStatus.offline,
    "shutdown": PrinterStatus.maintenance,
    "startup": PrinterStatus.offline,
    "disconnected": PrinterStatus.offline,
}


def normalize_moonraker_status(raw_status: str) -> PrinterStatus:
    return _MOONRAKER_STATUS_MAP.get(raw_status.lower(), PrinterStatus.maintenance)


def _looks_like_image(payload: bytes) -> bool:
    """Judge a probe answer by its bytes: a captive portal also returns 200."""
    return payload.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (
        payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    )


MOONRAKER_DISCOVERY_PORTS: tuple[int, ...] = (7125, 80)

#: Printer objects a snapshot needs. Keys only — Moonraker returns every field
#: when no field list is given.
QUERY_OBJECTS: dict[str, None] = {
    "webhooks": None,
    "print_stats": None,
    "virtual_sdcard": None,
    "extruder": None,
    "heater_bed": None,
    "temperature_sensor": None,
    "temperature_fan": None,
}

#: REST equivalents of the JSON-RPC methods this adapter uses. Every Moonraker
#: build has these; `/server/jsonrpc` is a later addition that Creality's fork
#: never picked up.
REST_ROUTES: dict[str, tuple[str, str]] = {
    "server.info": ("GET", "/server/info"),
    "printer.info": ("GET", "/printer/info"),
    "printer.objects.query": ("GET", "/printer/objects/query"),
    "printer.print.start": ("POST", "/printer/print/start"),
    "printer.print.pause": ("POST", "/printer/print/pause"),
    "printer.print.resume": ("POST", "/printer/print/resume"),
    "printer.print.cancel": ("POST", "/printer/print/cancel"),
}

#: Where the file lands on the printer. Moonraker only prints from `gcodes`.
UPLOAD_ROOT = "gcodes"

#: Snapshot URLs tried when the printer entry names none. crowsnest answers the
#: first on nearly every Mainsail/Fluidd image; the second is the older layout
#: where the stream service has its own port. Anything else has to be configured
#: explicitly — guessing further would raise `capabilities.camera` on printers
#: that have no camera at all.
CAMERA_SNAPSHOT_CANDIDATES: tuple[str, ...] = (
    "http://{host}/webcam/?action=snapshot",
    "http://{host}:8080/?action=snapshot",
)

#: A snapshot request has to fail fast: it runs inside the frame interval.
CAMERA_TIMEOUT_S = 10.0

#: Bound on a state query. aiohttp's own default is five minutes, so without
#: this a host that drops packets rather than refusing them rides all the way to
#: the caller's timeout — and that one arrives as a bare `TimeoutError` with no
#: message. Kept under the poll budget in `uplink.connection` so the failure the
#: hub sees is this adapter's own ("Cannot connect to host ...") and names the
#: address. `sock_connect` is the short one: a printer that is off answers
#: nothing at the TCP layer, and there is no point spending the whole budget
#: learning that.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15.0, sock_connect=5.0)


async def discover_moonraker(
    hosts: list[str], *, ports: tuple[int, ...] = MOONRAKER_DISCOVERY_PORTS, timeout_s: float = 1.5
) -> list[dict[str, Any]]:
    """Probe hosts for a Moonraker HTTP API.

    Klipper has no service announcement to listen for, so the only reliable
    local discovery is asking each address whether it answers `/printer/info`.
    A bare TCP connect comes first because it fails fast on the ~99% of
    addresses that are not printers.
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
        results = await asyncio.gather(
            *(_probe_moonraker_host(session, host, ports, timeout_s) for host in hosts)
        )
    return [item for item in results if item is not None]


async def _probe_moonraker_host(
    session: aiohttp.ClientSession, host: str, ports: tuple[int, ...], timeout_s: float
) -> dict[str, Any] | None:
    for port in ports:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
        except (OSError, asyncio.TimeoutError, TimeoutError):
            continue
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass

        try:
            async with session.get(f"http://{host}:{port}/printer/info") as response:
                if response.status != 200:
                    continue
                body = await response.json(content_type=None)
        except Exception:
            continue

        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict) or "state" not in result:
            continue  # something else is listening on this port
        return {
            "brand": "moonraker",
            "host": host,
            "port": port,
            "name": str(result.get("hostname") or "") or host,
            "model": str(result.get("software_version") or ""),
            "serial": "",
            "source": "http",
        }
    return None


class MoonrakerAdapter(PrinterAdapter):
    def __init__(self, printer: PrinterConfig):
        super().__init__(printer)
        self._session: aiohttp.ClientSession | None = None
        self._base_url = self._build_base_url()
        self._last_snapshot: PrinterSnapshot | None = None
        #: Set once a 404 proves this printer has no JSON-RPC endpoint.
        self._prefers_rest = False
        #: Snapshot URL, configured or found by probing. Empty means no camera,
        #: and `capabilities.camera` says so.
        self._camera_url = printer.camera_snapshot_url.strip()
        self._camera_probed = bool(self._camera_url)

    async def connect(self) -> None:
        self._ensure_session()
        # printer.info rather than server.info: discovery already proves every
        # Moonraker build answers it, including Creality's fork.
        await self._call("printer.info")
        await self._probe_camera()

    async def disconnect(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_state(self) -> PrinterSnapshot:
        try:
            response = await self._call("printer.objects.query", {"objects": QUERY_OBJECTS})
            status = self._build_snapshot_from_response(response)
        except Exception as exc:
            status = PrinterSnapshot(
                printer_key=self.printer.key,
                status=PrinterStatus.offline,
                status_raw="offline",
                job=JobSnapshot(),
                temps=TemperatureSnapshot(),
                # Carry the real reason: "request failed" tells an operator
                # nothing they can act on.
                error=ErrorSnapshot(code="offline", message=str(exc) or exc.__class__.__name__),
                capabilities=self.capabilities(),
                ts=utc_now_iso(),
            )
        self._last_snapshot = status
        return status

    def capabilities(self) -> PrinterCapabilities:
        # `camera` follows the probe, not the intention: the hub turns the flag
        # into a button, and a button that leads to a refusal is worse than an
        # absent one.
        return PrinterCapabilities(
            pause=True, resume=True, cancel=True, upload=True, camera=bool(self._camera_url)
        )

    async def start_print(
        self,
        file_ref: str,
        remote_name: str | None = None,
        ams_mapping: list[int] | None = None,
    ) -> dict[str, Any]:
        # Klipper has no feeding system of its own, so `ams_mapping` has nothing
        # to address and is accepted only to keep one signature across adapters.
        # Moonraker addresses a print by the name the file has on the printer,
        # which is what the upload put there — the cache's file_ref never
        # reaches the machine.
        filename = (remote_name or file_ref or "").strip()
        if not filename:
            raise RuntimeError("start_print needs a file name")
        await self._call("printer.print.start", {"filename": filename})
        return {"ok": True, "filename": filename}

    async def pause(self) -> dict[str, Any]:
        await self._call("printer.print.pause")
        return {"ok": True}

    async def resume(self) -> dict[str, Any]:
        await self._call("printer.print.resume")
        return {"ok": True}

    async def cancel(self) -> dict[str, Any]:
        await self._call("printer.print.cancel")
        return {"ok": True}

    async def upload_file(self, local_path: str | Path, remote_name: str) -> dict[str, Any]:
        """Push a file into Moonraker's gcode store.

        `/server/files/upload` is a plain multipart POST that every Moonraker
        build has, including Creality's fork — unlike `/server/jsonrpc`, which
        is why this one does not go through :meth:`_call`. The file is streamed
        from disk: the caller's copy is the only one that exists.
        """
        source = Path(local_path)
        if not source.is_file():
            raise RuntimeError(f"{source} is not a file")
        name = (remote_name or source.name).strip() or source.name

        session = self._ensure_session()
        with source.open("rb") as handle:
            form = aiohttp.FormData()
            form.add_field("root", UPLOAD_ROOT)
            # The print is started by its own command, with its own command_id
            # and its own result; starting it here would leave that print with
            # no command to attribute its outcome to.
            form.add_field("print", "false")
            form.add_field("file", handle, filename=name, content_type="application/octet-stream")
            async with session.post(
                f"{self._base_url}/server/files/upload",
                data=form,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300),
            ) as response:
                response.raise_for_status()
                body = await response.json(content_type=None)

        item = body.get("item") if isinstance(body, dict) else None
        stored = item.get("path") if isinstance(item, dict) else None
        return {"ok": True, "remote_name": name, "path": str(stored or name), "root": UPLOAD_ROOT}

    async def get_camera_frame(self) -> bytes:
        if not self._camera_url:
            raise UnsupportedCommandError(
                f"no camera snapshot URL for {self.printer.key}; set camera_snapshot_url"
            )
        session = self._ensure_session()
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
        session = self._ensure_session()
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

    def _build_base_url(self) -> str:
        scheme = "http"
        host = self.printer.host
        port = self.printer.port or 7125
        return f"{scheme}://{host}:{port}"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {}
            api_key = self.printer.credentials.get("api_key") if isinstance(self.printer.credentials, dict) else None
            bearer_token = self.printer.credentials.get("bearer_token") if isinstance(self.printer.credentials, dict) else None
            if api_key:
                headers["X-Api-Key"] = str(api_key)
            if bearer_token:
                headers["Authorization"] = f"Bearer {bearer_token}"
            # Upload and camera pass their own timeouts; this is the default the
            # state queries and `printer.info` inherit.
            self._session = aiohttp.ClientSession(headers=headers, timeout=REQUEST_TIMEOUT)
        return self._session

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a Moonraker method over whichever transport this printer has.

        Creality's K-series firmware ships an older Moonraker fork with no
        JSON-RPC-over-HTTP endpoint: `/server/jsonrpc` answers 404 there. The
        REST API exists in every build, so a 404 switches this adapter over for
        the rest of its life rather than re-probing on every poll.
        """
        if self._prefers_rest:
            return await self._rest(method, params)
        try:
            return await self._jsonrpc(method, params)
        except aiohttp.ClientResponseError as exc:
            if exc.status != 404:
                raise
            self._prefers_rest = True
            return await self._rest(method, params)

    async def _rest(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            verb, path = REST_ROUTES[method]
        except KeyError as exc:
            raise RuntimeError(f"Moonraker method {method} has no REST equivalent") from exc

        url = f"{self._base_url}{path}"
        if method == "printer.objects.query":
            objects = (params or {}).get("objects") or {}
            # Moonraker takes bare names in the query string; a value after `=`
            # restricts the fields, and we want them all.
            url = f"{url}?{'&'.join(objects)}"
        elif method == "printer.print.start":
            filename = str((params or {}).get("filename", ""))
            url = f"{url}?filename={quote(filename, safe='/')}"

        session = self._ensure_session()
        async with session.request(verb, url) as response:
            response.raise_for_status()
            body = await response.json(content_type=None)
        result = body.get("result") if isinstance(body, dict) else None
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            # `ok` from the print-control endpoints.
            return {"result": result}
        raise RuntimeError(f"Moonraker {path} returned unexpected payload")

    async def _jsonrpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._ensure_session()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": 1}
        if params:
            payload["params"] = params
        async with session.post(f"{self._base_url}/server/jsonrpc", json=payload) as response:
            response.raise_for_status()
            body = await response.json()
        if "error" in body:
            raise RuntimeError(str(body["error"]))
        result = body.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            # The print-control methods answer with a bare "ok", exactly as their
            # REST equivalents do.
            return {"result": result}
        raise RuntimeError(f"Moonraker RPC {method} returned unexpected payload")

    def _build_snapshot_from_response(self, response: dict[str, Any]) -> PrinterSnapshot:
        status_map = response.get("status") or {}
        webhooks = status_map.get("webhooks") or {}
        print_stats = status_map.get("print_stats") or {}
        virtual_sdcard = status_map.get("virtual_sdcard") or {}
        extruder = status_map.get("extruder") or {}
        heater_bed = status_map.get("heater_bed") or {}
        temp_sensor = self._pick_temperature_sensor(status_map)
        temp_fan = self._pick_temperature_fan(status_map)

        hooks_state = str(webhooks.get("state", "offline")).lower()
        job_state = str(print_stats.get("state", "standby")).lower()
        printer_status = self._map_status(hooks_state, job_state)
        is_active = printer_status in {PrinterStatus.printing, PrinterStatus.paused}

        progress = self._progress_value(virtual_sdcard, print_stats)
        total_duration = print_stats.get("total_duration")
        print_duration = print_stats.get("print_duration")
        time_remaining = self._estimate_remaining(print_duration, progress, total_duration)
        current_layer = self._safe_int(print_stats.get("info", {}).get("current_layer"))
        total_layer = self._safe_int(print_stats.get("info", {}).get("total_layer"))
        current_file = print_stats.get("filename") or virtual_sdcard.get("file_path")
        if isinstance(current_file, str) and current_file:
            current_file = Path(current_file).name
        else:
            current_file = None

        job = JobSnapshot(
            name=current_file,
            progress_pct=progress,
            layer=current_layer,
            layers_total=total_layer,
            time_elapsed_s=self._safe_int(print_duration),
            time_remaining_s=time_remaining,
            status=job_status_for(PrinterStatus.printing if is_active else printer_status),
            # Klipper counts extruded filament per print and resets it with the
            # next one, which is exactly the contract's semantics. Absent on
            # very old builds: leave the field unset rather than send 0, since
            # zero is what a print that just started legitimately reports.
            filament_used_mm=self._safe_float(print_stats.get("filament_used")),
        )
        temps = TemperatureSnapshot(
            nozzle=self._safe_float(extruder.get("temperature")),
            nozzle_target=self._safe_float(extruder.get("target")),
            bed=self._safe_float(heater_bed.get("temperature")),
            bed_target=self._safe_float(heater_bed.get("target")),
            chamber=self._safe_float(temp_sensor.get("temperature") if temp_sensor else None)
            if temp_sensor
            else self._safe_float(temp_fan.get("temperature") if temp_fan else None),
        )
        error_message = webhooks.get("message") or print_stats.get("message") or None
        error = ErrorSnapshot(
            code=None if printer_status != PrinterStatus.error else hooks_state or job_state,
            message=str(error_message) if error_message else None,
        )
        return PrinterSnapshot(
            printer_key=self.printer.key,
            status=printer_status,
            status_raw=f"webhooks={hooks_state};print_stats={job_state}",
            job=job,
            temps=temps,
            error=error,
            capabilities=self.capabilities(),
        )

    @staticmethod
    def _map_status(webhooks_state: str, job_state: str) -> PrinterStatus:
        if webhooks_state in {"startup", "disconnected", "offline"}:
            return PrinterStatus.offline
        if webhooks_state == "shutdown":
            return PrinterStatus.maintenance
        if webhooks_state == "error":
            return PrinterStatus.error
        return normalize_moonraker_status(job_state)

    @staticmethod
    def _progress_value(virtual_sdcard: dict[str, Any], print_stats: dict[str, Any]) -> float | None:
        progress = virtual_sdcard.get("progress")
        if progress is None:
            progress = print_stats.get("info", {}).get("current_layer")
        if progress is None:
            return None
        try:
            progress_value = float(progress)
        except (TypeError, ValueError):
            return None
        if progress_value <= 1.0:
            progress_value *= 100.0
        return max(0.0, min(100.0, progress_value))

    @staticmethod
    def _estimate_remaining(print_duration: Any, progress_pct: float | None, total_duration: Any) -> int | None:
        elapsed = MoonrakerAdapter._safe_float(print_duration)
        if elapsed is None:
            return None
        if total_duration is not None:
            total = MoonrakerAdapter._safe_float(total_duration)
            if total is not None:
                return max(0, int(round(total - elapsed)))
        if progress_pct is None or progress_pct <= 0:
            return None
        total_estimate = elapsed / (progress_pct / 100.0)
        return max(0, int(round(total_estimate - elapsed)))

    @staticmethod
    def _pick_temperature_sensor(status_map: dict[str, Any]) -> dict[str, Any] | None:
        sensors = status_map.get("temperature_sensor") or {}
        if isinstance(sensors, dict) and sensors:
            first_key = next(iter(sensors))
            value = sensors.get(first_key)
            if isinstance(value, dict):
                return value
        return None

    @staticmethod
    def _pick_temperature_fan(status_map: dict[str, Any]) -> dict[str, Any] | None:
        fans = status_map.get("temperature_fan") or {}
        if isinstance(fans, dict) and fans:
            first_key = next(iter(fans))
            value = fans.get(first_key)
            if isinstance(value, dict):
                return value
        return None

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
