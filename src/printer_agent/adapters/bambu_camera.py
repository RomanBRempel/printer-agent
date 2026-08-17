"""Chamber-camera frames from a Bambu printer's own TCP stream.

P- and A-series firmware serves the chamber image on TCP 6000 behind TLS: the
client sends one fixed 80-byte packet carrying the user `bblp` and the LAN
access code, and the printer then pushes JPEG frames, each preceded by a 16-byte
header whose first four bytes are the payload length. There is no HTTP snapshot
URL and no MJPEG endpoint to fall back on. X1 firmware is the other way round —
RTSP on 322, nothing on 6000 — which is why availability here is *probed* and
never assumed from the brand: `capabilities.camera` has to say what this adapter
can actually do on this machine.

Two properties of that port shape the module:

- **it serves one client at a time.** Bambu Studio or Handy watching the same
  printer locks the agent out completely, so a failed probe is never final: it
  is retried on a cooldown, and the stream closes itself once nobody has asked
  for a frame for `IDLE_CLOSE_S`, handing the camera back to whoever is next.
- **frames arrive on the printer's schedule, not on request.** A task of its own
  reads the stream and hands the newest frame to whoever is waiting, so a slow
  upload to the hub never stalls the socket, and a caller never holds it.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import struct
import time
from contextlib import suppress

logger = logging.getLogger(__name__)

CHAMBER_IMAGE_PORT = 6000
CAMERA_USERNAME = "bblp"

#: The auth packet is fixed-width — four little-endian words, then the user name
#: and the access code in 32-byte NUL-padded fields. The firmware reads it by
#: offset and answers a malformed one with silence rather than an error, so the
#: padding is not cosmetic.
_AUTH_PREAMBLE = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
CREDENTIAL_FIELD_BYTES = 32

#: Bytes ahead of every frame. Only the first four — the payload length — carry
#: anything this agent needs.
FRAME_HEADER_BYTES = 16
#: A header claiming more than this is a desynchronised stream, not a big photo.
MAX_FRAME_BYTES = 8 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8\xff"

CONNECT_TIMEOUT_S = 5.0
#: How long a caller waits for the next frame. The printer pushes roughly one a
#: second, so anything beyond this is a stream that has stopped, not a slow one.
FRAME_WAIT_TIMEOUT_S = 10.0
#: How long the stream stays open with nobody asking for frames...
IDLE_CLOSE_S = 15.0
#: ...and how long a failed probe is believed before the port is tried again.
PROBE_RETRY_S = 300.0


class BambuCameraError(RuntimeError):
    """The chamber camera could not deliver a frame."""


def insecure_tls_context() -> ssl.SSLContext:
    """TLS without verification: the printer presents a self-signed certificate."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def auth_packet(username: str, access_code: str) -> bytes:
    return _AUTH_PREAMBLE + _credential_field(username) + _credential_field(access_code)


def _credential_field(value: str) -> bytes:
    raw = value.encode("ascii", "ignore")[:CREDENTIAL_FIELD_BYTES]
    return raw.ljust(CREDENTIAL_FIELD_BYTES, b"\x00")


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one length-prefixed JPEG off the chamber stream."""
    header = await reader.readexactly(FRAME_HEADER_BYTES)
    (size,) = struct.unpack("<I", header[:4])
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise BambuCameraError(f"chamber stream announced a {size} byte frame")
    payload = await reader.readexactly(size)
    if not payload.startswith(JPEG_MAGIC):
        # Reading on from a stream that has lost frame alignment returns garbage
        # for as long as it stays open; the caller reconnects instead.
        raise BambuCameraError("chamber stream sent a frame that is not a JPEG")
    return payload


class BambuChamberCamera:
    """The camera on one printer: at most one open stream, opened on demand."""

    def __init__(
        self,
        host: str,
        access_code: str,
        printer_key: str = "",
        port: int = CHAMBER_IMAGE_PORT,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self._host = host
        self._port = port or CHAMBER_IMAGE_PORT
        self._access_code = access_code
        self._printer_key = printer_key or host
        self._ssl_context = ssl_context
        self._task: asyncio.Task[None] | None = None
        self._waiters: list[asyncio.Future[bytes]] = []
        self._available = False
        self._probed = False
        self._last_probe = 0.0
        self._last_request = 0.0
        #: Why the last attempt produced nothing, for the log and the probe.
        self.last_error = ""

    @property
    def available(self) -> bool:
        """True once this printer has actually handed over a frame.

        Deliberately sticky: a stream that drops mid-session is a failure the
        operator should see as a failing camera, not as a button disappearing
        from under them — and flapping the flag would rewrite the hub's roster
        on every hiccup.
        """
        return self._available

    def due_for_probe(self) -> bool:
        if not self._access_code or self._available:
            return False
        if not self._probed:
            return True
        return (time.monotonic() - self._last_probe) >= PROBE_RETRY_S

    async def probe(self) -> bool:
        """Ask for one frame, to learn whether this model serves this port at all."""
        self._probed = True
        self._last_probe = time.monotonic()
        try:
            await self.frame()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "bambu chamber camera is not answering",
                extra={
                    "action": "camera_probe",
                    "printer_key": self._printer_key,
                    "error": str(exc) or exc.__class__.__name__,
                },
            )
            return False
        logger.info(
            "bambu chamber camera answered",
            extra={"action": "camera_probe", "printer_key": self._printer_key},
        )
        return True

    async def frame(self) -> bytes:
        if not self._access_code:
            raise BambuCameraError("bambu camera needs the printer's access code")
        self._last_request = time.monotonic()
        waiter: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        self._ensure_stream()
        try:
            return await asyncio.wait_for(waiter, FRAME_WAIT_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            raise BambuCameraError(
                f"no frame from {self._host}:{self._port} within {FRAME_WAIT_TIMEOUT_S:g}s"
                f"{f' ({self.last_error})' if self.last_error else ''}"
            ) from None
        finally:
            with suppress(ValueError):
                self._waiters.remove(waiter)

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    def _ensure_stream(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._stream_loop(), name=f"printer-agent-bambu-camera-{self._printer_key}"
        )

    async def _stream_loop(self) -> None:
        # Opening is kept apart from reading because the two fail for opposite
        # reasons and used to be reported as one: a port that never answered
        # was announced as a port that answered and then went quiet, which
        # points the operator at the access code instead of at the network.
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._host, self._port, ssl=self._ssl_context or insecure_tls_context()
                ),
                timeout=CONNECT_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            self._fail_waiters(BambuCameraError("camera stream cancelled"))
            raise
        except Exception as exc:
            self.last_error = _connect_failure(exc, self._host, self._port)
            self._fail_waiters(BambuCameraError(self.last_error))
            return
        try:
            writer.write(auth_packet(CAMERA_USERNAME, self._access_code))
            await writer.drain()
            while True:
                frame = await asyncio.wait_for(read_frame(reader), timeout=FRAME_WAIT_TIMEOUT_S)
                self._available = True
                self.last_error = ""
                self._deliver(frame)
                if time.monotonic() - self._last_request > IDLE_CLOSE_S:
                    # Nobody is watching, and the port serves one client at a
                    # time: holding it would keep Studio and Handy out for good.
                    break
        except asyncio.CancelledError:
            self._fail_waiters(BambuCameraError("camera stream cancelled"))
            raise
        except Exception as exc:
            self.last_error = _stream_failure(exc)
            self._fail_waiters(BambuCameraError(self.last_error))
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    def _deliver(self, frame: bytes) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(frame)
        self._waiters.clear()

    def _fail_waiters(self, error: Exception) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(error)
        self._waiters.clear()


def _connect_failure(exc: Exception, host: str, port: int) -> str:
    """Name a failure to reach the port, which is a printer setting far more often
    than it is a fault.

    A P-series printer only serves this port with LAN-mode liveview switched on;
    without it the port is not there at all, and the printer answers the same way
    it answers for a machine that is switched off.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return (
            f"{host}:{port} did not answer within {CONNECT_TIMEOUT_S:g}s — the printer is "
            "unreachable, or its camera port is closed (LAN-mode liveview off)"
        )
    if isinstance(exc, ConnectionRefusedError):
        return f"{host}:{port} refused the connection — this model serves no chamber stream here"
    return str(exc) or exc.__class__.__name__


def _stream_failure(exc: Exception) -> str:
    """Name a failure *after* the port answered, where the causes are different.

    The two that actually happen on a shop floor look identical on the wire: a
    wrong access code and a second client already watching both end as the
    printer closing the socket without a byte of answer.
    """
    if isinstance(exc, asyncio.IncompleteReadError):
        return "the printer closed the camera stream — wrong access code, or another client is watching"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "the camera port accepted the connection but sent no frame"
    return str(exc) or exc.__class__.__name__
