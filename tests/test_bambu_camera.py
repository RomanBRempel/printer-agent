"""The Bambu chamber camera: its framing, and what it does when the port is busy.

The protocol is undocumented and unforgiving — a fixed-width auth packet, then
length-prefixed JPEGs — and the two ways it fails on a shop floor look the same
on the wire: a wrong access code and a second client already watching both end
with the printer closing the socket without a byte of answer. Both are pinned
here, together with the rule that `capabilities.camera` only rises once a frame
has really arrived.
"""

from __future__ import annotations

import asyncio
import struct
import time

import pytest

from printer_agent.adapters import bambu_camera
from printer_agent.adapters.bambu import BambuAdapter
from printer_agent.adapters.bambu_camera import (
    CREDENTIAL_FIELD_BYTES,
    FRAME_HEADER_BYTES,
    BambuCameraError,
    BambuChamberCamera,
    auth_packet,
    read_frame,
)
from printer_agent.config import PrinterConfig

JPEG = b"\xff\xd8\xff\xe0" + b"chamber" + b"\xff\xd9"
AUTH_PACKET_BYTES = 16 + 2 * CREDENTIAL_FIELD_BYTES


@pytest.fixture
def plaintext_camera(monkeypatch):
    """Take TLS out of the way: the framing is what these tests are about."""
    monkeypatch.setattr(bambu_camera, "insecure_tls_context", lambda: None)


def frame_bytes(payload: bytes = JPEG) -> bytes:
    return struct.pack("<I", len(payload)) + b"\x00" * (FRAME_HEADER_BYTES - 4) + payload


def make_adapter() -> BambuAdapter:
    return BambuAdapter(
        PrinterConfig(
            key="jekson-p1s",
            brand="bambu",
            host="10.0.0.5",
            port=8883,
            credentials={"serial": "0309DA4B0803132", "access_code": "12345678"},
        )
    )


def test_auth_packet_is_fixed_width():
    """The firmware reads the packet by offset and answers a short one with silence."""
    packet = auth_packet("bblp", "12345678")

    assert len(packet) == AUTH_PACKET_BYTES
    assert packet[16:20] == b"bblp"
    assert packet[20:48] == b"\x00" * 28
    assert packet[48:56] == b"12345678"
    assert packet[56:] == b"\x00" * 24


def test_read_frame_takes_the_length_from_the_header():
    stream = frame_bytes() + b"trailing bytes of the next frame"

    assert asyncio.run(_read_one(stream)) == JPEG


def test_a_frame_that_is_not_a_jpeg_is_refused():
    """Reading on from a stream that lost alignment returns garbage forever."""
    with pytest.raises(BambuCameraError):
        asyncio.run(_read_one(frame_bytes(b"not an image")))


def test_an_absurd_frame_size_is_refused_before_allocating():
    header = struct.pack("<I", 1 << 30) + b"\x00" * (FRAME_HEADER_BYTES - 4)

    with pytest.raises(BambuCameraError):
        asyncio.run(_read_one(header))


async def _read_one(stream: bytes) -> bytes:
    # Built inside the loop: a StreamReader binds to the running one.
    reader = asyncio.StreamReader()
    reader.feed_data(stream)
    return await read_frame(reader)


def test_a_frame_arrives_and_raises_the_capability(plaintext_camera):
    received: list[bytes] = []

    async def scenario() -> bytes:
        async def handler(reader, writer):
            received.append(await reader.readexactly(AUTH_PACKET_BYTES))
            for _ in range(3):
                writer.write(frame_bytes())
                await writer.drain()
                await asyncio.sleep(0.01)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        camera = BambuChamberCamera(host="127.0.0.1", access_code="12345678", port=port)
        try:
            assert camera.available is False
            return await camera.frame()
        finally:
            await camera.close()
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == JPEG
    assert received == [auth_packet("bblp", "12345678")]


def test_a_printer_that_hangs_up_names_the_two_causes(plaintext_camera):
    """One client at a time: Studio watching the printer looks like a bad code."""

    async def scenario() -> tuple[bool, str]:
        async def handler(reader, writer):
            await reader.readexactly(AUTH_PACKET_BYTES)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        camera = BambuChamberCamera(host="127.0.0.1", access_code="12345678", port=port)
        try:
            probed = await camera.probe()
            return probed, camera.last_error
        finally:
            await camera.close()
            server.close()
            await server.wait_closed()

    probed, error = asyncio.run(scenario())

    assert probed is False
    assert "another client" in error


def test_a_closed_port_is_not_reported_as_a_silent_one(plaintext_camera):
    """Two opposite causes were once announced with one sentence.

    A port that never answers is a printer setting or a network; a port that
    answers and then says nothing is the access code or another client. Saying
    the second when the first happened sends the operator hunting credentials.
    """
    camera = BambuChamberCamera(host="127.0.0.1", access_code="12345678", port=1)

    assert asyncio.run(camera.probe()) is False
    assert "refused" in camera.last_error
    assert "sent no frame" not in camera.last_error


def test_a_failed_probe_is_retried_rather_than_believed_forever(plaintext_camera):
    """The operator closes Bambu Studio; the camera has to appear without a restart."""
    camera = BambuChamberCamera(host="127.0.0.1", access_code="12345678", port=1)

    assert camera.due_for_probe() is True
    assert asyncio.run(camera.probe()) is False
    assert camera.due_for_probe() is False

    camera._last_probe = time.monotonic() - bambu_camera.PROBE_RETRY_S - 1

    assert camera.due_for_probe() is True


def test_a_camera_without_an_access_code_never_probes():
    camera = BambuChamberCamera(host="10.0.0.5", access_code="")

    assert camera.due_for_probe() is False
    with pytest.raises(BambuCameraError):
        asyncio.run(camera.frame())


def test_a_second_consumer_does_not_open_the_camera_port():
    """The desktop app polling the same printer must not take the stream away."""
    adapter = make_adapter()
    adapter.camera_probes_enabled = False

    asyncio.run(_state_once(adapter))

    assert adapter._camera_probe_task is None


async def _state_once(adapter: BambuAdapter) -> None:
    await adapter.get_state()


def test_the_capability_follows_the_probe_and_not_the_brand():
    """X1 firmware serves RTSP and nothing on this port; the brand cannot tell."""
    adapter = make_adapter()

    assert adapter.capabilities().camera is False

    adapter._camera._available = True

    assert adapter.capabilities().camera is True
