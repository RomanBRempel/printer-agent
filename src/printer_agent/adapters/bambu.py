from __future__ import annotations

import asyncio
import ftplib
import json
import ssl
from contextlib import suppress
from pathlib import Path
from typing import Any

from aiomqtt import Client

from ..config import PrinterConfig
from ..contracts import AmsSlot, ErrorSnapshot, JobSnapshot, JobStatus, PrinterCapabilities, PrinterSnapshot, PrinterStatus, TemperatureSnapshot, ams_state, job_status_for, utc_now_iso
from .base import PrinterAdapter, UnsupportedCommandError
from .bambu_camera import BambuChamberCamera


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

#: Implicit FTPS port. Bambu wraps the control channel in TLS from the first
#: byte instead of upgrading a plain session with `AUTH TLS` on 21, which is why
#: `ftplib.FTP_TLS` cannot be used as it comes.
BAMBU_FTPS_PORT = 990

#: The only account the printer has. The password is the access code — the same
#: one MQTT and the camera port authenticate with.
BAMBU_FTPS_USER = "bblp"

#: Where uploads land. The FTP root is the printer's storage; a subdirectory
#: would have to exist on every model, and it does not.
BAMBU_UPLOAD_DIR = "/"

#: Default address the print command uses to find an uploaded file. X-series
#: mounts storage at `/sdcard`, P- and A-series address the root — and the MQTT
#: report does not say which machine this is (see `capabilities()` on the same
#: fact for the camera). Overridden per printer by `credentials.print_url_prefix`
#: in `agent.yaml`; the value actually used is returned in the command result so
#: a print that never starts can be diagnosed without guessing.
BAMBU_PRINT_URL_PREFIX = "file:///sdcard/"

#: How long the FTPS control channel waits for the printer to answer. The data
#: channel inherits it, so it also bounds a single block of the transfer — which
#: is why the default is generous rather than snappy: on shop-floor Wi-Fi a
#: printer routinely stops reading for tens of seconds in the middle of a
#: multi-megabyte file, and a timeout there is reported as "the read operation
#: timed out" — a message that reads like a broken printer, not like a slow link.
#: Overridable per printer (`credentials.ftps_timeout_s` in `agent.yaml`) for a
#: link that needs even more; a value that is not a positive number is ignored
#: rather than obeyed, because a zero here would mean "never wait".
FTPS_TIMEOUT_S = 120

#: Deadline for the TLS `close_notify` handshake that ends a transfer, kept
#: separate from — and far below — `FTPS_TIMEOUT_S`: the file is already on the
#: printer by then, so a firmware that never answers should cost seconds, not
#: the whole upload. See :meth:`_ImplicitFTPS.storbinary`.
FTPS_TLS_SHUTDOWN_TIMEOUT_S = 5

#: Plate inside a sliced `.3mf` project. Bambu Studio numbers plates from one
#: and writes this path for the first of them; multi-plate projects are not a
#: case the hub produces — it sends one part per program.
BAMBU_PROJECT_PLATE = "Metadata/plate_1.gcode"


class _ImplicitFTPS(ftplib.FTP_TLS):
    """`ftplib` client for implicit FTPS.

    Three departures from the standard class:

    * the control socket is wrapped in TLS as soon as it is created, because
      the server expects a handshake and not a plaintext greeting;
    * the data connection reuses the control connection's TLS session. Servers
      built on OpenSSL 1.1+ commonly require this to prove the data channel
      belongs to the authenticated session, and without it a transfer opens and
      then dies — which reads as a network fault rather than a protocol one;
    * the TLS shutdown at the end of a transfer is given its own short deadline
      instead of the connection's. See :meth:`storbinary`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sock: Any = None

    @property
    def sock(self) -> Any:
        return self._sock

    @sock.setter
    def sock(self, value: Any) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd: str, rest: Any = None) -> tuple[Any, int | None]:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = getattr(self.sock, "session", None)
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=session)
        return conn, size

    def storbinary(  # type: ignore[override]
        self,
        cmd: str,
        fp: Any,
        blocksize: int = 8192,
        callback: Any = None,
        rest: Any = None,
    ) -> str:
        """`FTP.storbinary`, but the TLS shutdown cannot hang the upload.

        The standard implementation calls `unwrap()` on the data socket once the
        file is sent, which writes a TLS `close_notify` and then *waits for the
        peer to answer with its own*. Bambu firmware does not answer: it closes
        the connection at the TCP level and moves on. So the wait runs to the
        socket timeout and raises `TimeoutError("The read operation timed out")`
        — for a file the printer has already received in full. The operator sees
        a transfer that failed and a printer that holds the file.

        Closing the data socket unannounced is what the transfer means anyway;
        the receipt is the `226` the server sends on the control channel, which
        is still read below. The shutdown is still *attempted*, on a deadline of
        its own, because firmware that does answer should get a clean close.
        """
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while buf := fp.read(blocksize):
                conn.sendall(buf)
                if callback:
                    callback(buf)
            # Asked for by capability rather than by type: a data connection is
            # either a TLS socket or a plain one, and only the first has an
            # `unwrap`. It also lets the branch be tested without a TLS server.
            unwrap = getattr(conn, "unwrap", None)
            if unwrap is not None:
                with suppress(OSError, ValueError):
                    conn.settimeout(FTPS_TLS_SHUTDOWN_TIMEOUT_S)
                    unwrap()
        return self.voidresp()


#: Trays per AMS unit, used to give the slots one flat numbering across units —
#: the printer numbers trays 0..3 inside each unit and identifies the unit
#: separately, but the hub compares against a single list of loaded filaments.
BAMBU_TRAYS_PER_UNIT = 4


def bambu_ams_slots(print_state: dict[str, Any]) -> list[AmsSlot]:
    """Read the feeding system out of a `print` report.

    Shapes come from the printer's own pushall report: `ams.ams[]` is the list
    of units, each with a `tray[]` of loaded spools. `tray_color` is RGBA hex
    without a marker, and `remain` is -1 when the printer cannot tell — both are
    dropped rather than reported as a value the hub would trust.
    """
    ams = print_state.get("ams")
    units = ams.get("ams") if isinstance(ams, dict) else None
    if not isinstance(units, list):
        return []

    slots: list[AmsSlot] = []
    for ordinal, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        unit_index = BambuAdapter._safe_int(unit.get("id"))
        unit_index = ordinal if unit_index is None else unit_index
        trays = unit.get("tray")
        if not isinstance(trays, list):
            continue
        for tray_ordinal, tray in enumerate(trays):
            if not isinstance(tray, dict):
                continue
            tray_index = BambuAdapter._safe_int(tray.get("id"))
            tray_index = tray_ordinal if tray_index is None else tray_index
            material = str(tray.get("tray_type") or "").strip() or None
            remaining = BambuAdapter._safe_float(tray.get("remain"))
            slots.append(
                AmsSlot(
                    index=unit_index * BAMBU_TRAYS_PER_UNIT + tray_index,
                    material=material,
                    color=_tray_color(tray.get("tray_color")),
                    remaining_pct=remaining if remaining is not None and remaining >= 0 else None,
                )
            )
    return slots


def _tray_color(value: Any) -> str | None:
    text = str(value or "").strip().lstrip("#")
    if len(text) < 6:
        return None
    try:
        int(text[:6], 16)
    except ValueError:
        return None
    return f"#{text[:6].upper()}"

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
        #: Chamber camera on its own TCP port. P- and A-series serve it and X1
        #: does not, and nothing in the MQTT report names the model reliably, so
        #: the capability follows a probe rather than the brand.
        self._camera = BambuChamberCamera(
            host=printer.host,
            access_code=self._access_code,
            printer_key=printer.key,
            ssl_context=self._tls_context(),
        )
        self._camera_probe_task: asyncio.Task[None] | None = None
        #: Whether this consumer may open the camera port at all. The port takes
        #: one client at a time, so a second consumer of the same printer — the
        #: desktop app, a connectivity check — would take the stream away from
        #: the session that actually sends frames to the hub. Only the service
        #: leaves this on.
        self.camera_probes_enabled = True

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
        # Beside the connect, never inside it: the caller bounds `connect()` by
        # the poll budget, and a camera port that accepts and then says nothing
        # would spend that budget and leave the whole printer reported offline.
        self._schedule_camera_probe()

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._camera_probe_task is not None:
            self._camera_probe_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._camera_probe_task
            self._camera_probe_task = None
        await self._camera.close()
        if self._connection_task is not None:
            self._connection_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._connection_task
        self._connection_task = None
        self._connected = False

    async def get_state(self) -> PrinterSnapshot:
        # The port serves one client at a time, so a printer watched from Bambu
        # Studio when the agent started answers nothing at all. Retrying on the
        # poll is what lets the camera appear once that client goes away.
        self._schedule_camera_probe()
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
        # `upload` follows what this adapter can actually do, never intent: the
        # hub turns the flag into a button, and a button that can only answer
        # "unsupported" reads to the operator as a broken printer. FTPS
        # authenticates with the access code, so without one the answer is no —
        # and the hub then says so before anyone presses anything, instead of
        # after a failed transfer. `camera` follows the probe for the same
        # reason — X1 firmware has no chamber stream on this port, and a Bambu
        # is not enough to know which it is.
        return PrinterCapabilities(
            pause=True,
            resume=True,
            cancel=True,
            upload=bool(self._access_code),
            camera=self._camera.available,
            ams=bool(bambu_ams_slots(self._latest_print or {})),
        )

    async def start_print(
        self,
        file_ref: str,
        remote_name: str | None = None,
        ams_mapping: list[int] | None = None,
    ) -> dict[str, Any]:
        """Start a print of a file already uploaded to the printer's storage.

        Two shapes, chosen by what the file is. A sliced Bambu project (`.3mf`)
        is printed with `project_file`, and the plate inside it is named
        separately in `param` — the file is an archive, and the printer needs to
        be told which plate of it to run. A bare `.gcode` is printed with
        `gcode_file`, whose `param` is the path itself.

        The URL prefix is configuration, not a guess. X-series addresses SD as
        `/sdcard`, P- and A-series address the FTP root directly, and nothing in
        the MQTT report names the model reliably (the same fact that made the
        camera capability a probe rather than a brand rule). Guessing it wrong
        fails silently — MQTT does not acknowledge, so the outcome is "the print
        never started" with nothing to read — so the prefix is a printer setting
        with a documented default, and the value actually used comes back in the
        result for exactly that diagnosis.
        """
        name = (remote_name or file_ref or "").strip()
        if not name:
            raise RuntimeError("start_print needs a file name")

        prefix = self._print_url_prefix()
        url = f"{prefix}{name}"
        payload: dict[str, Any] = {
            "sequence_id": "0",
            "url": url,
            "subtask_name": Path(name).stem,
            # Levelling stays on: it costs a minute and saves a plate. The
            # calibrations below do not — they add minutes to every job, and the
            # operator runs them when the machine needs them, not per print.
            "bed_leveling": True,
            "flow_cali": False,
            "vibration_cali": False,
            "layer_inspect": False,
            "timelapse": False,
            "use_ams": False,
        }
        if name.lower().endswith(".3mf"):
            payload["command"] = "project_file"
            payload["param"] = BAMBU_PROJECT_PLATE
            # Zeroes, not the hub's identifiers: these name a task in Bambu's
            # own cloud, and a foreign number here has been known to make the
            # printer look for a project it cannot fetch.
            payload.update({"profile_id": "0", "project_id": "0", "subtask_id": "0", "task_id": "0"})
        else:
            payload["command"] = "gcode_file"
            payload["param"] = url[len("file://"):] if url.startswith("file://") else url

        if ams_mapping:
            # The hub matched the program against the slots this printer
            # reported, so its answer is more informed than the printer's own
            # pick. Without a mapping the AMS stays out of it entirely: letting
            # the printer choose a slot it was not told about is how a job comes
            # out in the wrong material.
            payload["use_ams"] = True
            payload["ams_mapping"] = list(ams_mapping)

        await self._publish_json({"print": payload})
        return {
            "ok": True,
            "filename": name,
            "url": url,
            "command": payload["command"],
            "use_ams": payload["use_ams"],
        }

    def _ftps_timeout(self) -> float:
        """How long one FTPS operation may stall before the upload is given up.

        Same shape as :meth:`_print_url_prefix`: a per-printer override with a
        documented default, because the answer depends on the link rather than
        on the model. Anything that is not a positive number falls back to the
        default — a zero or a typo would otherwise mean "wait forever" or "give
        up at once", and both fail in a way the operator cannot read.
        """
        raw = str(self.printer.credentials.get("ftps_timeout_s", "")).strip()
        try:
            configured = float(raw)
        except ValueError:
            return float(FTPS_TIMEOUT_S)
        return configured if configured > 0 else float(FTPS_TIMEOUT_S)

    def _print_url_prefix(self) -> str:
        """Where the printer expects to find the uploaded file.

        Configurable per printer (`credentials.print_url_prefix` in `agent.yaml`)
        because the answer differs by series and cannot be detected. A trailing
        slash is added rather than demanded: an operator editing YAML should not
        have a print fail over a missing character.
        """
        configured = str(self.printer.credentials.get("print_url_prefix", "")).strip()
        prefix = configured or BAMBU_PRINT_URL_PREFIX
        return prefix if prefix.endswith("/") else f"{prefix}/"

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
        """Put a print file on the printer's storage over FTPS.

        Bambu serves **implicit** FTPS on 990 — TLS from the first byte, not
        `AUTH TLS` on 21 — with a self-signed certificate and the access code as
        the password. `ftplib` speaks explicit FTPS only, hence the subclass
        below; verification is off for the same reason it is off for MQTT and
        the camera port: the certificate is the printer's own and there is no
        authority to check it against.

        Runs in a worker thread: `ftplib` is blocking, and a multi-megabyte
        upload on the event loop would stall telemetry for the whole location
        for as long as it takes.
        """
        source = Path(local_path)
        if not source.is_file():
            raise RuntimeError(f"{source} is not a file")
        if not self._access_code:
            raise UnsupportedCommandError(
                f"printer {self.printer.key} has no access code, which FTPS authenticates with"
            )
        name = (remote_name or source.name).strip() or source.name

        await asyncio.to_thread(self._ftps_upload, source, name)
        return {"ok": True, "remote_name": name, "size_bytes": source.stat().st_size}

    def _ftps_upload(self, source: Path, name: str) -> None:
        """Blocking half of :meth:`upload_file` — runs in a worker thread.

        Every failure is re-raised naming the step it happened in. A bare socket
        error here reaches the operator as the hub's whole explanation of why a
        job did not print, and "the read operation timed out" does not say
        whether the printer refused the connection, refused the login, or took
        the file — three faults with three different answers.
        """
        stage = "connect"
        client = _ImplicitFTPS(context=self._tls_context())
        try:
            client.connect(
                host=self.printer.host, port=BAMBU_FTPS_PORT, timeout=self._ftps_timeout()
            )
            stage = "login"
            client.login(user=BAMBU_FTPS_USER, passwd=self._access_code)
            # Encrypts the data channel too. Without it the printer accepts the
            # login and then refuses the transfer — an authentication that looks
            # like it worked, which is the confusing half of this protocol.
            stage = "secure the data channel"
            client.prot_p()
            if BAMBU_UPLOAD_DIR not in ("", "/"):
                stage = f"open {BAMBU_UPLOAD_DIR}"
                client.cwd(BAMBU_UPLOAD_DIR)
            stage = f"send {name}"
            with source.open("rb") as handle:
                client.storbinary(f"STOR {name}", handle)
        except TimeoutError as exc:
            raise TimeoutError(
                f"FTPS upload to {self.printer.host}:{BAMBU_FTPS_PORT} timed out"
                f" trying to {stage} ({exc})"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"FTPS upload to {self.printer.host}:{BAMBU_FTPS_PORT} failed"
                f" trying to {stage}: {exc}"
            ) from exc
        finally:
            # The printer keeps a small number of control connections and hands
            # out no more until they time out, so a leaked one costs the next
            # upload, not just this one.
            with suppress(Exception):
                client.quit()
            with suppress(Exception):
                client.close()

    async def get_camera_frame(self) -> bytes:
        if not self._access_code:
            raise UnsupportedCommandError(
                f"printer {self.printer.key} has no access code, which the camera port authenticates with"
            )
        return await self._camera.frame()

    def _schedule_camera_probe(self) -> None:
        """Find out whether this printer serves the chamber stream, off the poll.

        The task is kept referenced: a probe collected mid-connect would leave
        the capability down with nothing in the log to say why.
        """
        if not self.camera_probes_enabled:
            return
        if self._camera_probe_task is not None and not self._camera_probe_task.done():
            return
        if not self._camera.due_for_probe():
            return
        self._camera_probe_task = asyncio.create_task(
            self._probe_camera(), name=f"printer-agent-bambu-camera-probe-{self.printer.key}"
        )

    async def _probe_camera(self) -> None:
        if await self._camera.probe():
            # The cached snapshot was built while the flag was still down, and
            # the hub reads capabilities out of it.
            self._latest_snapshot = None

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
            state=ams_state(bambu_ams_slots(print_state)),
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
