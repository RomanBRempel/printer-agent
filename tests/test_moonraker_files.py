"""Moonraker: putting a file on the printer, starting it, and reading a frame.

Against a real HTTP server, because what is being checked is the request shape —
multipart fields, the query parameter a print start is addressed by, and whether
a snapshot endpoint answers at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from printer_agent.adapters import moonraker as moonraker_module
from printer_agent.adapters.base import UnsupportedCommandError
from printer_agent.adapters.moonraker import MoonrakerAdapter
from printer_agent.config import PrinterConfig

GCODE = b"; sliced by a human\nG28\n"
JPEG = b"\xff\xd8\xff\xe0" + b"snapshot" * 4


class FakeMoonraker:
    """Enough of Moonraker to check what the adapter sends it."""

    def __init__(self, *, jsonrpc: bool = False, webcam: bool = True, stored_as: str | None = None):
        self.jsonrpc = jsonrpc
        self.webcam = webcam
        #: What the upload reports as the stored path. Real Moonraker is free to
        #: normalise the offered name, and a print is addressed by what it made.
        self.stored_as = stored_as
        self.uploads: list[dict[str, Any]] = []
        self.starts: list[str] = []
        self.jsonrpc_calls: list[dict[str, Any]] = []
        self.snapshot_requests = 0
        self._server: TestServer | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/server/jsonrpc", self._jsonrpc)
        app.router.add_post("/server/files/upload", self._upload)
        app.router.add_post("/printer/print/start", self._start)
        app.router.add_get("/webcam/", self._snapshot)
        self._server = TestServer(app)
        await self._server.start_server()
        return str(self._server.make_url("")).rstrip("/")

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    def snapshot_url(self) -> str:
        assert self._server is not None
        return str(self._server.make_url("/webcam/?action=snapshot"))

    async def _jsonrpc(self, request: web.Request) -> web.Response:
        if not self.jsonrpc:
            # Creality's fork: the endpoint simply is not there.
            return web.json_response({"error": "not found"}, status=404)
        body = await request.json()
        self.jsonrpc_calls.append(body)
        if body.get("method") == "printer.print.start":
            self.starts.append(str((body.get("params") or {}).get("filename", "")))
            return web.json_response({"result": "ok"})
        return web.json_response({"result": {"state": "ready"}})

    async def _upload(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        fields: dict[str, Any] = {}
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                fields["filename"] = part.filename
                fields["content"] = await part.read(decode=False)
            else:
                fields[str(part.name)] = (await part.read()).decode()
        self.uploads.append(fields)
        return web.json_response(
            {
                "item": {
                    "path": self.stored_as or fields.get("filename", ""),
                    "root": "gcodes",
                },
                "print_started": False,
            },
            status=201,
        )

    async def _start(self, request: web.Request) -> web.Response:
        self.starts.append(request.query.get("filename", ""))
        return web.json_response({"result": "ok"})

    async def _snapshot(self, request: web.Request) -> web.Response:
        self.snapshot_requests += 1
        if not self.webcam:
            return web.Response(status=404)
        return web.Response(body=JPEG, content_type="image/jpeg")


async def make_adapter(hub: FakeMoonraker, printer: PrinterConfig | None = None) -> MoonrakerAdapter:
    base = await hub.start()
    adapter = MoonrakerAdapter(
        printer or PrinterConfig(key="p1", brand="moonraker", host="127.0.0.1", port=7125)
    )
    adapter._base_url = base
    return adapter


@pytest.mark.asyncio
async def test_upload_sends_the_file_and_does_not_start_it(tmp_path) -> None:
    """The start is a separate command with its own result to belong to."""
    hub = FakeMoonraker()
    adapter = await make_adapter(hub)
    source = tmp_path / "cached-file"
    source.write_bytes(GCODE)
    try:
        result = await adapter.upload_file(source, "BWB-20-D-001-R2.gcode")
    finally:
        await adapter.disconnect()
        await hub.close()

    assert result["ok"] is True
    assert hub.uploads == [
        {
            "root": "gcodes",
            "print": "false",
            "filename": "BWB-20-D-001-R2.gcode",
            "content": GCODE,
        }
    ]


@pytest.mark.asyncio
async def test_start_print_addresses_the_printer_side_name() -> None:
    hub = FakeMoonraker()
    adapter = await make_adapter(hub)
    try:
        result = await adapter.start_print("pf_7f3a", "BWB-20-D-001-R2.gcode")
    finally:
        await adapter.disconnect()
        await hub.close()

    # The cache's file_ref never reaches the machine.
    assert hub.starts == ["BWB-20-D-001-R2.gcode"]
    assert result["filename"] == "BWB-20-D-001-R2.gcode"


@pytest.mark.asyncio
async def test_start_print_works_on_a_build_with_jsonrpc() -> None:
    """`printer.print.start` answers with a bare "ok", not an object."""
    hub = FakeMoonraker(jsonrpc=True)
    adapter = await make_adapter(hub)
    try:
        result = await adapter.start_print("pf_7f3a", "job.gcode")
    finally:
        await adapter.disconnect()
        await hub.close()

    assert result["ok"] is True
    assert hub.starts == ["job.gcode"]


@pytest.mark.asyncio
async def test_start_print_falls_back_to_the_file_ref_when_no_name_is_given() -> None:
    hub = FakeMoonraker()
    adapter = await make_adapter(hub)
    try:
        await adapter.start_print("job.gcode")
    finally:
        await adapter.disconnect()
        await hub.close()

    assert hub.starts == ["job.gcode"]


@pytest.mark.asyncio
async def test_the_camera_capability_follows_a_probe(monkeypatch) -> None:
    hub = FakeMoonraker()
    adapter = await make_adapter(hub)
    monkeypatch.setattr(moonraker_module, "CAMERA_SNAPSHOT_CANDIDATES", (hub.snapshot_url(),))
    try:
        assert adapter.capabilities().camera is False
        await adapter._probe_camera()
        found = adapter.capabilities().camera
        frame = await adapter.get_camera_frame()
    finally:
        await adapter.disconnect()
        await hub.close()

    assert found is True
    assert frame == JPEG


@pytest.mark.asyncio
async def test_a_printer_without_a_snapshot_endpoint_keeps_the_flag_down(monkeypatch) -> None:
    """A raised flag would show the operator a button that leads nowhere."""
    hub = FakeMoonraker(webcam=False)
    adapter = await make_adapter(hub)
    monkeypatch.setattr(moonraker_module, "CAMERA_SNAPSHOT_CANDIDATES", (hub.snapshot_url(),))
    try:
        await adapter._probe_camera()
        assert adapter.capabilities().camera is False
        with pytest.raises(UnsupportedCommandError):
            await adapter.get_camera_frame()
    finally:
        await adapter.disconnect()
        await hub.close()


@pytest.mark.asyncio
async def test_a_configured_snapshot_url_is_trusted_without_probing(monkeypatch) -> None:
    hub = FakeMoonraker()
    base = await hub.start()
    printer = PrinterConfig(
        key="p1",
        brand="moonraker",
        host="127.0.0.1",
        port=7125,
        camera_snapshot_url=hub.snapshot_url(),
    )
    adapter = MoonrakerAdapter(printer)
    adapter._base_url = base
    monkeypatch.setattr(moonraker_module, "CAMERA_SNAPSHOT_CANDIDATES", ())
    try:
        assert adapter.capabilities().camera is True
        await adapter._probe_camera()
        assert await adapter.get_camera_frame() == JPEG
    finally:
        await adapter.disconnect()
        await hub.close()


@pytest.mark.asyncio
async def test_start_uses_the_path_the_upload_made(tmp_path) -> None:
    """A print is addressed by what Moonraker stored, not by what it was offered.

    Moonraker normalises the offered name, and Klipper answers a request for a
    file it does not have with "Internal error on command:SDCARD_PRINT_FILE" —
    an error that names neither the file nor the reason, so the operator reads
    it as a broken printer rather than a renamed file.
    """
    hub = FakeMoonraker(stored_as="PQ-000011.gcode")
    adapter = await make_adapter(hub)
    source = tmp_path / "pf_7f3a"
    source.write_bytes(GCODE)
    try:
        upload = await adapter.upload_file(source, "законцовка левая^TRK.gcode")
        result = await adapter.start_print("pf_7f3a", "законцовка левая^TRK.gcode")
    finally:
        await adapter.disconnect()
        await hub.close()

    assert upload["path"] == "PQ-000011.gcode"
    assert hub.starts == ["PQ-000011.gcode"]
    # Обе величины остаются в ответе: по одной ищут файл на принтере, по другой
    # — заявку в хабе.
    assert result["filename"] == "PQ-000011.gcode"
    assert result["requested_name"] == "законцовка левая^TRK.gcode"


@pytest.mark.asyncio
async def test_start_without_a_preceding_upload_uses_the_offered_name() -> None:
    """Перезапуск агента между загрузкой и стартом — прежнее поведение.

    Соответствие «имя → путь» живёт в памяти адаптера: после перезапуска его
    нет, и обращаться остаётся по имени от хаба. Это не регрессия, а то, как
    работало всегда; выдумывать путь агент не станет.
    """
    hub = FakeMoonraker(stored_as="normalised.gcode")
    adapter = await make_adapter(hub)
    try:
        await adapter.start_print("pf_7f3a", "job.gcode")
    finally:
        await adapter.disconnect()
        await hub.close()

    assert hub.starts == ["job.gcode"]

