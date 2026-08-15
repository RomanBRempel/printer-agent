"""The camera stream: the hub sets the rate, and its answers end the session.

The loop is timing-driven, so these tests drive a real HTTP endpoint and wait for
frames to arrive rather than reaching into the service's state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from printer_agent.adapters.base import PrinterAdapter
from printer_agent.config import PrinterConfig
from printer_agent.contracts import PrinterCapabilities, PrinterSnapshot, PrinterStatus
from printer_agent.core.outbox import EventOutbox
from printer_agent.uplink import camera as camera_module
from printer_agent.uplink.camera import CameraService, frame_content_type
from printer_agent.uplink.commands import CommandProcessor

JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes" * 8
SESSION_ID = "cam_9f2c8a1b"
TOKEN = "secret-token"


class CameraAdapter(PrinterAdapter):
    def __init__(self, printer: PrinterConfig, *, camera: bool = True, frame: bytes = JPEG):
        super().__init__(printer)
        self._camera = camera
        self.frame = frame
        self.captures = 0

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_state(self) -> PrinterSnapshot:
        return PrinterSnapshot(printer_key=self.printer_key, status=PrinterStatus.printing, status_raw="printing")

    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(camera=self._camera)

    async def get_camera_frame(self) -> bytes:
        self.captures += 1
        return self.frame


class FrameHub:
    """The hub's frame endpoint: records what arrives, answers what it is told."""

    def __init__(self, status: int = 200, body: dict[str, Any] | None = None):
        self.status = status
        self.body = body if body is not None else {"ok": True, "continue": True, "interval_s": 0.02}
        self.frames: list[bytes] = []
        self.headers: list[dict[str, str]] = []
        self._server: TestServer | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/api/printers/camera/{session}", self._receive)
        self._server = TestServer(app)
        await self._server.start_server()
        return str(self._server.make_url(f"/api/printers/camera/{SESSION_ID}"))

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    async def _receive(self, request: web.Request) -> web.Response:
        self.frames.append(await request.read())
        self.headers.append(dict(request.headers))
        return web.json_response(self.body, status=self.status)


@pytest.fixture(autouse=True)
def quick_timings(monkeypatch):
    """Real waits, only short ones: the loop's shape is what is under test."""
    monkeypatch.setattr(camera_module, "MIN_INTERVAL_S", 0.01)
    monkeypatch.setattr(camera_module, "MIN_SILENCE_WINDOW_S", 0.3)
    monkeypatch.setattr(camera_module, "DEFAULT_SILENCE_WINDOW_S", 2.0)


@pytest.fixture
def adapter():
    return CameraAdapter(PrinterConfig(key="printer-1", brand="moonraker", host="127.0.0.1"))


def request_payload(upload_url: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "command_id": "1044",
        "printer_key": "printer-1",
        "session_id": SESSION_ID,
        "upload_url": upload_url,
        "interval_s": 0.02,
        "max_bytes": 2097152,
    }
    payload.update(overrides)
    return payload


async def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_frames_are_posted_with_the_token_and_their_own_type(adapter) -> None:
    service = CameraService(TOKEN)
    hub = FrameHub()
    url = await hub.start()
    try:
        answer = await service.start(adapter, request_payload(url))
        assert answer["streaming"] is True
        assert await wait_for(lambda: len(hub.frames) >= 2)
    finally:
        await service.stop_all()
        await hub.close()

    assert hub.frames[0] == JPEG
    headers = hub.headers[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["X-Captured-At"]


@pytest.mark.asyncio
async def test_camera_stop_ends_the_stream(adapter) -> None:
    service = CameraService(TOKEN)
    hub = FrameHub()
    url = await hub.start()
    try:
        await service.start(adapter, request_payload(url))
        assert await wait_for(lambda: len(hub.frames) >= 1)

        result = await service.stop("printer-1", SESSION_ID)
        sent_by_then = len(hub.frames)
        await asyncio.sleep(0.2)
    finally:
        await service.stop_all()
        await hub.close()

    assert result == {"session_id": SESSION_ID, "stopped": True}
    assert len(hub.frames) == sent_by_then
    assert service.active_session("printer-1") is None


@pytest.mark.asyncio
async def test_a_gone_session_stops_the_stream_without_being_told_twice(adapter) -> None:
    """`camera_stop` may never arrive; 409 is the answer that always does."""
    service = CameraService(TOKEN)
    hub = FrameHub(status=409, body={"ok": False, "continue": False, "reason": "session expired"})
    url = await hub.start()
    try:
        await service.start(adapter, request_payload(url))
        stopped = await wait_for(lambda: service.active_session("printer-1") is None)
    finally:
        await service.stop_all()
        await hub.close()

    assert stopped
    assert len(hub.frames) == 1


@pytest.mark.asyncio
async def test_an_unusable_frame_does_not_stop_the_stream(adapter) -> None:
    service = CameraService(TOKEN)
    hub = FrameHub(status=400, body={"ok": False, "continue": True, "reason": "empty frame"})
    url = await hub.start()
    try:
        await service.start(adapter, request_payload(url))
        kept_going = await wait_for(lambda: len(hub.frames) >= 3)
    finally:
        await service.stop_all()
        await hub.close()

    assert kept_going


@pytest.mark.asyncio
async def test_a_stop_for_another_session_leaves_the_stream_alone(adapter) -> None:
    """The viewer may have reopened the camera before the stop arrived."""
    service = CameraService(TOKEN)
    hub = FrameHub()
    url = await hub.start()
    try:
        await service.start(adapter, request_payload(url))
        result = await service.stop("printer-1", "cam_something_older")
        still_running = service.active_session("printer-1") is not None
    finally:
        await service.stop_all()
        await hub.close()

    assert result["stopped"] is False
    assert still_running


@pytest.mark.asyncio
async def test_an_oversize_frame_is_not_sent(adapter) -> None:
    service = CameraService(TOKEN)
    hub = FrameHub()
    url = await hub.start()
    try:
        await service.start(adapter, request_payload(url, max_bytes=4))
        # The loop keeps capturing; what it must not do is spend the uplink.
        captured = await wait_for(lambda: adapter.captures >= 2)
        await asyncio.sleep(0.05)
    finally:
        await service.stop_all()
        await hub.close()

    assert captured
    assert hub.frames == []


@pytest.mark.asyncio
async def test_a_silent_hub_ends_the_session_on_its_own(adapter) -> None:
    """A lost link must not leave a camera filming for the rest of the shift."""
    service = CameraService(TOKEN)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    try:
        await service.start(
            adapter,
            # Nothing listens there: every upload fails at the transport.
            request_payload("http://127.0.0.1:9/api/printers/camera/x", expires_at=expired),
        )
        stopped = await wait_for(lambda: service.active_session("printer-1") is None, timeout=5.0)
    finally:
        await service.stop_all()

    assert stopped


@pytest.mark.asyncio
async def test_a_printer_without_a_camera_is_answered_unsupported(tmp_path) -> None:
    outbox = EventOutbox(tmp_path / "outbox.sqlite3")
    service = CameraService(TOKEN)
    processor = CommandProcessor(outbox, None, service)
    adapter = CameraAdapter(
        PrinterConfig(key="printer-1", brand="creality", host="127.0.0.1"), camera=False
    )
    try:
        result = await processor.dispatch_camera_request(
            adapter, request_payload("https://hub.example.com/api/printers/camera/x")
        )
    finally:
        await service.stop_all()
        outbox.close()

    assert result["status"] == "unsupported"
    assert result["command_id"] == "1044"
    assert service.active_session("printer-1") is None


@pytest.mark.asyncio
async def test_camera_stop_for_an_idle_printer_is_still_answered(tmp_path) -> None:
    outbox = EventOutbox(tmp_path / "outbox.sqlite3")
    service = CameraService(TOKEN)
    processor = CommandProcessor(outbox, None, service)
    adapter = CameraAdapter(PrinterConfig(key="printer-1", brand="moonraker", host="127.0.0.1"))
    try:
        result = await processor.dispatch_camera_stop(
            adapter,
            {"command_id": "1045", "printer_key": "printer-1", "session_id": SESSION_ID},
        )
    finally:
        outbox.close()

    assert result["status"] == "done"
    assert result["response"]["stopped"] is False


def test_frame_type_comes_from_the_bytes() -> None:
    assert frame_content_type(JPEG) == "image/jpeg"
    assert frame_content_type(b"\x89PNG\r\n\x1a\n rest") == "image/png"
    assert frame_content_type(b"RIFF____WEBPVP8 ") == "image/webp"
    # An unknown blob is still offered as the type the hub expects most.
    assert frame_content_type(b"not an image") == "image/jpeg"
