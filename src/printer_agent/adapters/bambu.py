from __future__ import annotations

import asyncio
import json
import ssl
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiomqtt import Client

from ..config import PrinterConfig
from ..contracts import ErrorSnapshot, JobSnapshot, JobStatus, PrinterCapabilities, PrinterSnapshot, PrinterStatus, TemperatureSnapshot, job_status_for, utc_now_iso
from .base import PrinterAdapter, UnsupportedCommandError


_BAMBU_STATUS_MAP = {
    "idle": PrinterStatus.idle,
    "printing": PrinterStatus.printing,
    "run": PrinterStatus.printing,
    "running": PrinterStatus.printing,
    "prepare": PrinterStatus.printing,
    "pause": PrinterStatus.paused,
    "paused": PrinterStatus.paused,
    "finish": PrinterStatus.finished,
    "finished": PrinterStatus.finished,
    "failed": PrinterStatus.error,
    "error": PrinterStatus.error,
    "offline": PrinterStatus.offline,
    "maintenance": PrinterStatus.maintenance,
}


def normalize_bambu_status(raw_status: str) -> PrinterStatus:
    return _BAMBU_STATUS_MAP.get(raw_status.lower(), PrinterStatus.maintenance)

BAMBU_USER_CANCELLED = 50348044

BAMBU_SSDP_PORT = 2021
BAMBU_SSDP_GROUP = "239.255.255.250"
BAMBU_MQTT_PORT = 8883


def parse_bambu_ssdp(datagram: bytes, sender_ip: str) -> dict[str, Any] | None:
    """Parse one Bambu SSDP NOTIFY into a discovery record.

    Bambu printers announce themselves on UDP 2021 with an SSDP-shaped message
    whose vendor headers carry the serial, model and user-assigned name — which
    is everything needed to fill in a printer entry except the access code.
    """
    try:
        text = datagram.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.splitlines()
    if not lines or not lines[0].upper().startswith(("NOTIFY", "HTTP/1.1 200")):
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().upper()] = value.strip()

    location = headers.get("LOCATION", "").strip()
    host = location or sender_ip
    serial = headers.get("USN", "").strip()
    if not serial and not host:
        return None
    return {
        "brand": "bambu",
        "host": host,
        "port": BAMBU_MQTT_PORT,
        "name": headers.get("DEVNAME.BAMBU.COM", "").strip() or serial or host,
        "model": headers.get("DEVMODEL.BAMBU.COM", "").strip(),
        "serial": serial,
        "source": "ssdp",
    }


async def discover_bambu(timeout_s: float = 6.0) -> list[dict[str, Any]]:
    """Listen on the Bambu SSDP port for printer announcements.

    Printers broadcast on their own schedule, so this listens for the whole
    window rather than expecting a reply to a probe; an M-SEARCH is sent first
    because some firmware answers it immediately.
    """
    loop = asyncio.get_running_loop()
    found: dict[str, dict[str, Any]] = {}

    class _Protocol(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            record = parse_bambu_ssdp(data, addr[0])
            if record is not None:
                found[record.get("serial") or record["host"]] = record

    transport = None
    try:
        transport, _ = await loop.create_datagram_endpoint(
            _Protocol,
            local_addr=("0.0.0.0", BAMBU_SSDP_PORT),
            reuse_port=None,
            allow_broadcast=True,
        )
    except OSError:
        # Port already bound (often by Bambu Studio) — nothing to listen with.
        return []

    try:
        search = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {BAMBU_SSDP_GROUP}:{BAMBU_SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 1\r\n"
            "ST: urn:bambulab-com:device:3dprinter:1\r\n\r\n"
        ).encode()
        with suppress(OSError):
            transport.sendto(search, (BAMBU_SSDP_GROUP, BAMBU_SSDP_PORT))
        await asyncio.sleep(timeout_s)
    finally:
        transport.close()

    return list(found.values())


class BambuAdapter(PrinterAdapter):
    def __init__(self, printer: PrinterConfig):
        super().__init__(printer)
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._connection_task: asyncio.Task[None] | None = None
        self._latest_print: dict[str, Any] | None = None
        self._latest_info: dict[str, Any] | None = None
        self._latest_error: dict[str, Any] | None = None
        self._latest_snapshot: PrinterSnapshot | None = None
        self._connected = False
        self._message_count = 0
        #: Why the subscription is not delivering, kept so an empty cache can say
        #: what went wrong instead of only that it is empty.
        self._last_error: str = ""
        #: MQTT client id. Defaults to the serial; a second consumer (the desktop
        #: app) overrides it so the broker does not evict the service's session.
        self.client_identifier: str | None = None

    async def connect(self) -> None:
        if self._connection_task is not None and not self._connection_task.done():
            return
        self._stop_event.clear()
        self._connected_event.clear()
        self._connection_task = asyncio.create_task(self._run_connection_loop())
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=15)
        except TimeoutError:
            pass

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._connection_task is not None:
            self._connection_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._connection_task
        self._connection_task = None
        self._connected = False

    async def get_state(self) -> PrinterSnapshot:
        if self._latest_snapshot is not None and self._connected:
            return self._latest_snapshot
        if self._latest_print is None:
            return PrinterSnapshot(
                printer_key=self.printer.key,
                status=PrinterStatus.offline,
                status_raw="offline",
                job=JobSnapshot(),
                temps=TemperatureSnapshot(),
                error=ErrorSnapshot(code="offline", message=self._empty_cache_message()),
                capabilities=self.capabilities(),
                ts=utc_now_iso(),
            )
        snapshot = self._snapshot_from_state(self._latest_print, self._latest_info, self._latest_error)
        self._latest_snapshot = snapshot
        return snapshot

    def _empty_cache_message(self) -> str:
        if not self._serial_number:
            return "Bambu printer requires a serial number"
        if self._last_error:
            return f"Bambu MQTT is not delivering: {self._last_error}"
        if self._connected:
            return "Bambu MQTT connected, waiting for the first report"
        return "Bambu MQTT cache is empty"

    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(pause=True, resume=True, cancel=True, upload=True, ams=True, camera=True)

    async def start_print(self, file_ref: str) -> dict[str, Any]:
        raise UnsupportedCommandError("Bambu live start_print is not implemented yet")

    async def pause(self) -> dict[str, Any]:
        await self._publish_json({"print": {"sequence_id": "0", "command": "pause"}})
        return {"ok": True}

    async def resume(self) -> dict[str, Any]:
        await self._publish_json({"print": {"sequence_id": "0", "command": "resume"}})
        return {"ok": True}

    async def cancel(self) -> dict[str, Any]:
        await self._publish_json({"print": {"sequence_id": "0", "command": "stop"}})
        return {"ok": True}

    async def upload_file(self, local_path: str | Path, remote_name: str) -> dict[str, Any]:
        raise UnsupportedCommandError("Bambu FTPS upload is not implemented in the live test adapter")

    async def get_camera_frame(self) -> bytes:
        raise UnsupportedCommandError("Bambu camera frame capture is not implemented yet")

    async def _run_connection_loop(self) -> None:
        serial = self._serial_number
        if not serial:
            self._connected = False
            self._connected_event.set()
            return

        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with Client(
                    hostname=self.printer.host,
                    port=self.printer.port or 8883,
                    username="bblp",
                    password=self._access_code,
                    identifier=self.client_identifier or serial,
                    keepalive=30,
                    tls_context=self._tls_context(),
                    tls_insecure=True,
                    timeout=10,
                ) as client:
                    self._connected = True
                    self._last_error = ""
                    self._connected_event.set()
                    await client.subscribe(f"device/{serial}/report")
                    # The printer answers a pushall with a full report but never
                    # sends a PUBACK for it, so anything above qos 0 waits out the
                    # client timeout and tears down the subscription we just made.
                    await client.publish(
                        f"device/{serial}/request",
                        json.dumps({"pushing": {"sequence_id": "0", "command": "pushall", "push_target": 1}}),
                        qos=0,
                    )
                    backoff = 1.0
                    async for message in client.messages:
                        if self._stop_event.is_set():
                            break
                        try:
                            payload = json.loads(message.payload)
                        except json.JSONDecodeError:
                            continue
                        await self._handle_payload(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc) or exc.__class__.__name__
                if not self._connected_event.is_set():
                    self._connected_event.set()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 60.0)

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        if payload.get("info"):
            self._latest_info = self._merge_payload(self._latest_info, payload.get("info"))
        if payload.get("print"):
            self._latest_print = self._merge_payload(self._latest_print, payload.get("print"))
        if payload.get("hms"):
            error_message = self._extract_error_message(payload)
            if error_message:
                self._latest_error = {"message": error_message}
        if self._latest_print is not None:
            self._latest_snapshot = self._snapshot_from_state(self._latest_print, self._latest_info, self._latest_error)
        self._message_count += 1

    async def _publish_json(self, message: dict[str, Any]) -> None:
        serial = self._serial_number
        if not serial:
            raise UnsupportedCommandError("Bambu printer requires a serial number")
        # A second client on the subscription's own id makes the broker evict the
        # session that feeds the cache, so commands take a suffixed one; qos stays
        # at 0 because the printer does not acknowledge publishes on this topic.
        async with Client(
            hostname=self.printer.host,
            port=self.printer.port or 8883,
            username="bblp",
            password=self._access_code,
            identifier=f"{self.client_identifier or serial}-cmd",
            keepalive=30,
            tls_context=self._tls_context(),
            tls_insecure=True,
            timeout=10,
        ) as client:
            await client.publish(f"device/{serial}/request", json.dumps(message), qos=0)

    @property
    def _serial_number(self) -> str:
        credentials = self.printer.credentials if isinstance(self.printer.credentials, dict) else {}
        return str(credentials.get("serial", "")).strip()

    @property
    def _access_code(self) -> str:
        credentials = self.printer.credentials if isinstance(self.printer.credentials, dict) else {}
        return str(credentials.get("access_code", "")).strip()

    def _tls_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    @staticmethod
    def _merge_payload(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
        if existing is None:
            return dict(incoming)
        merged = dict(existing)
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = BambuAdapter._merge_payload(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _snapshot_from_state(
        self,
        print_state: dict[str, Any],
        info_state: dict[str, Any] | None,
        error_state: dict[str, Any] | None,
    ) -> PrinterSnapshot:
        status_raw = str(print_state.get("gcode_state", "offline"))
        status = self._map_status(status_raw, print_state)
        progress = self._safe_float(print_state.get("mc_percent"))
        if progress is not None and progress <= 1.0:
            progress *= 100.0
        time_remaining = self._safe_int(print_state.get("mc_remaining_time"))
        if time_remaining is not None:
            time_remaining *= 60
        current_file = print_state.get("subtask_name") or print_state.get("gcode_file")
        if isinstance(current_file, str) and current_file:
            current_file = current_file.removeprefix("/mnt/sdcard/")
            current_file = current_file.removeprefix("/sdcard/")
            current_file = current_file.removeprefix("/")
        else:
            current_file = None
        layers_total = self._safe_int(print_state.get("total_layer_num"))
        current_layer = self._safe_int(print_state.get("layer_num"))
        error_code = self._safe_int(print_state.get("print_error"))
        job_status = self._job_status_from_printer(status, error_code)
        job = JobSnapshot(
            name=current_file,
            progress_pct=progress,
            layer=current_layer,
            layers_total=layers_total,
            time_elapsed_s=self._safe_int(print_state.get("gcode_start_time")),
            time_remaining_s=time_remaining,
            status=job_status,
        )
        temps = TemperatureSnapshot(
            nozzle=self._safe_float(print_state.get("nozzle_temper")),
            nozzle_target=self._safe_float(print_state.get("nozzle_target_temper")),
            bed=self._safe_float(print_state.get("bed_temper")),
            bed_target=self._safe_float(print_state.get("bed_target_temper")),
            chamber=self._safe_float(print_state.get("chamber_temper")),
        )
        error_message = None
        if error_state and error_state.get("message"):
            error_message = str(error_state.get("message"))
        elif error_code:
            error_message = self._bambu_error_message(error_code)
        if status == PrinterStatus.error and error_code == 0:
            error_message = "Print stopped by user"
        error = ErrorSnapshot(code=str(error_code) if error_code is not None else None, message=error_message)
        capabilities = self.capabilities()
        return PrinterSnapshot(
            printer_key=self.printer.key,
            status=status,
            status_raw=status_raw,
            job=job,
            temps=temps,
            error=error,
            capabilities=capabilities,
        )

    @staticmethod
    def _map_status(status_raw: str, print_state: dict[str, Any]) -> PrinterStatus:
        status = normalize_bambu_status(status_raw)
        if status == PrinterStatus.error:
            print_error = BambuAdapter._safe_int(print_state.get("print_error")) or 0
            if print_error in {0, BAMBU_USER_CANCELLED}:
                return PrinterStatus.finished
        return status

    @staticmethod
    def _job_status_from_printer(printer_status: PrinterStatus, print_error: int | None) -> JobStatus | None:
        if printer_status is PrinterStatus.finished and print_error == BAMBU_USER_CANCELLED:
            return JobStatus.cancelled
        return job_status_for(printer_status)

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

    @staticmethod
    def _extract_error_message(payload: dict[str, Any]) -> str | None:
        hms = payload.get("hms")
        if isinstance(hms, dict):
            return json.dumps(hms, ensure_ascii=False)
        return None

    @staticmethod
    def _bambu_error_message(code: int) -> str:
        if code == BAMBU_USER_CANCELLED:
            return "Print cancelled by user"
        return f"Bambu error code {code}"
