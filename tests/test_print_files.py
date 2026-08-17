"""Delivering a print file: verify before the printer, never twice.

These tests run against a real HTTP server rather than a stubbed session: the
download streams, hashes and re-checks a body, and a fake that returns bytes in
one piece would exercise none of that.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from printer_agent.adapters.base import PrinterAdapter, UnsupportedCommandError
from printer_agent.config import PrinterConfig
from printer_agent.contracts import PrinterCapabilities, PrinterSnapshot, PrinterStatus
from printer_agent.core.filecache import InvalidFileRef, PrintFileCache
from printer_agent.core.outbox import EventOutbox
from printer_agent.uplink.commands import CommandProcessor
from printer_agent.uplink.files import PrintFileService

PAYLOAD = b"G28 ; home\n" * 5000
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
FILE_REF = "pf_7f3a91c2e5b04d18a6c2f0d9b41e77aa"
TOKEN = "secret-token"


class RecordingAdapter(PrinterAdapter):
    def __init__(self, printer: PrinterConfig, *, upload: bool = True):
        super().__init__(printer)
        self._upload = upload
        self.uploads: list[tuple[Path, str]] = []
        self.prints: list[tuple[str, str | None]] = []
        #: Slot mapping the hub worked out, kept separately so a test can assert
        #: it arrived — dropping it silently is the failure mode that matters.
        self.print_mappings: list[list[int] | None] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_state(self) -> PrinterSnapshot:
        return PrinterSnapshot(printer_key=self.printer_key, status=PrinterStatus.idle, status_raw="idle")

    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(upload=self._upload)

    async def upload_file(self, local_path: str | Path, remote_name: str) -> dict[str, Any]:
        self.uploads.append((Path(local_path), remote_name))
        return {"ok": True}

    async def start_print(
        self,
        file_ref: str,
        remote_name: str | None = None,
        ams_mapping: list[int] | None = None,
    ) -> dict[str, Any]:
        self.prints.append((file_ref, remote_name))
        self.print_mappings.append(ams_mapping)
        return {"ok": True}


class FileHub:
    """The hub's file endpoint, with the answers the contract names."""

    def __init__(self, payload: bytes = PAYLOAD, sha256: str = SHA256, status: int = 200):
        self.payload = payload
        self.sha256 = sha256
        self.status = status
        self.requests: list[str] = []
        self.authorizations: list[str] = []
        self._server: TestServer | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/api/printers/files/{ref}", self._serve)
        self._server = TestServer(app)
        await self._server.start_server()
        return str(self._server.make_url(f"/api/printers/files/{FILE_REF}"))

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    async def _serve(self, request: web.Request) -> web.Response:
        self.requests.append(request.match_info["ref"])
        self.authorizations.append(request.headers.get("Authorization", ""))
        if self.status != 200:
            return web.json_response({"error": "no"}, status=self.status)
        return web.Response(
            body=self.payload,
            headers={"X-Print-File-Sha256": self.sha256, "X-Print-File-Name": "job.gcode"},
        )


def build_offer(url: str, **overrides: Any) -> dict[str, Any]:
    offer = {
        "command_id": "1042",
        "printer_key": "printer-1",
        "file_ref": FILE_REF,
        "url": url,
        "remote_name": "BWB-20-D-001-R2.gcode",
        "sha256": SHA256,
        "size_bytes": len(PAYLOAD),
        "start_after_upload": True,
    }
    offer.update(overrides)
    return offer


@pytest.fixture
def processor(tmp_path):
    outbox = EventOutbox(tmp_path / "outbox.sqlite3")
    cache = PrintFileCache(tmp_path / "print-files")
    files = PrintFileService(cache, TOKEN)
    try:
        yield CommandProcessor(outbox, files), cache, outbox
    finally:
        outbox.close()


@pytest.fixture
def adapter():
    return RecordingAdapter(PrinterConfig(key="printer-1", brand="moonraker", host="127.0.0.1"))


# -- the cache -------------------------------------------------------------


def test_a_file_ref_cannot_escape_the_cache_directory(tmp_path) -> None:
    """The hub names a file on this machine's disk; that is a trust boundary."""
    cache = PrintFileCache(tmp_path / "print-files")
    for ref in ("../agent.yaml", "a/b", r"a\b", "", ".hidden", "/etc/passwd"):
        with pytest.raises(InvalidFileRef):
            cache.path_for(ref)


def test_prune_drops_files_past_the_age_bound(tmp_path) -> None:
    cache = PrintFileCache(tmp_path / "print-files", max_age_h=1)
    cache.ensure_directory()
    old = cache.path_for("old_file")
    old.write_bytes(b"x")
    import os

    stale = time.time() - 7200
    os.utime(old, (stale, stale))
    fresh = cache.path_for("fresh_file")
    fresh.write_bytes(b"x")

    report = cache.prune()

    assert report.removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_prune_enforces_the_size_bound_oldest_first(tmp_path) -> None:
    cache = PrintFileCache(tmp_path / "print-files", max_total_mb=1)
    cache.ensure_directory()
    import os

    now = time.time()
    for index in range(3):
        path = cache.path_for(f"file_{index}")
        path.write_bytes(b"0" * (600 * 1024))
        os.utime(path, (now - (10 - index), now - (10 - index)))

    cache.prune()

    assert not cache.has("file_0")
    assert cache.has("file_2")


# -- file_offer ------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_offer_downloads_verifies_and_uploads(processor, adapter) -> None:
    command_processor, cache, _outbox = processor
    hub = FileHub()
    url = await hub.start()
    try:
        result = await command_processor.dispatch_file_offer(adapter, build_offer(url))
    finally:
        await hub.close()

    assert result["status"] == "done"
    assert result["command_id"] == "1042"
    assert hub.authorizations == [f"Bearer {TOKEN}"]
    assert cache.path_for(FILE_REF).read_bytes() == PAYLOAD
    # The printer gets the cached file under the name the hub chose for it.
    assert adapter.uploads == [(cache.path_for(FILE_REF), "BWB-20-D-001-R2.gcode")]


@pytest.mark.asyncio
async def test_a_checksum_mismatch_sends_nothing_to_the_printer(processor, adapter) -> None:
    """Printing what the checksum does not cover would put the wrong part on the bed."""
    command_processor, cache, _outbox = processor
    hub = FileHub()
    url = await hub.start()
    try:
        result = await command_processor.dispatch_file_offer(
            adapter, build_offer(url, sha256="0" * 64)
        )
    finally:
        await hub.close()

    assert result["status"] == "failed"
    assert SHA256 in result["error_text"] or "0" * 64 in result["error_text"]
    assert adapter.uploads == []
    assert not cache.has(FILE_REF)
    assert list(cache.directory.glob("*")) == []


@pytest.mark.asyncio
async def test_a_replayed_offer_answers_from_the_stored_result(processor, adapter) -> None:
    """A reconnect while the result was in flight must not print a second part."""
    command_processor, _cache, _outbox = processor
    hub = FileHub()
    url = await hub.start()
    try:
        first = await command_processor.dispatch_file_offer(adapter, build_offer(url))
        second = await command_processor.dispatch_file_offer(adapter, build_offer(url))
    finally:
        await hub.close()

    assert first["status"] == second["status"] == "done"
    assert second["command_id"] == first["command_id"]
    assert len(hub.requests) == 1
    assert len(adapter.uploads) == 1


@pytest.mark.asyncio
async def test_an_adapter_without_upload_answers_unsupported(processor) -> None:
    command_processor, _cache, _outbox = processor
    adapter = RecordingAdapter(
        PrinterConfig(key="printer-1", brand="creality", host="127.0.0.1"), upload=False
    )
    hub = FileHub()
    url = await hub.start()
    try:
        result = await command_processor.dispatch_file_offer(adapter, build_offer(url))
    finally:
        await hub.close()

    assert result["status"] == "unsupported"
    # Nothing was fetched: the refusal is knowable before spending the link.
    assert hub.requests == []


@pytest.mark.asyncio
async def test_a_refusal_names_its_reason(processor, adapter) -> None:
    command_processor, _cache, _outbox = processor
    hub = FileHub(status=403)
    url = await hub.start()
    try:
        result = await command_processor.dispatch_file_offer(adapter, build_offer(url))
    finally:
        await hub.close()

    assert result["status"] == "failed"
    assert "403" in result["error_text"]
    assert "another agent" in result["error_text"]


@pytest.mark.asyncio
async def test_a_wrong_length_fails_even_when_the_body_arrives(processor, adapter) -> None:
    command_processor, cache, _outbox = processor
    hub = FileHub()
    url = await hub.start()
    try:
        result = await command_processor.dispatch_file_offer(
            adapter, build_offer(url, size_bytes=len(PAYLOAD) + 1)
        )
    finally:
        await hub.close()

    assert result["status"] == "failed"
    assert not cache.has(FILE_REF)


@pytest.mark.asyncio
async def test_a_second_offer_of_a_cached_file_does_not_download_it_again(processor, adapter) -> None:
    command_processor, _cache, _outbox = processor
    hub = FileHub()
    url = await hub.start()
    try:
        await command_processor.dispatch_file_offer(adapter, build_offer(url))
        result = await command_processor.dispatch_file_offer(
            adapter, build_offer(url, command_id="1043")
        )
    finally:
        await hub.close()

    assert result["status"] == "done"
    assert result["response"]["reused_cached_file"] is True
    assert len(hub.requests) == 1


# -- start_print -----------------------------------------------------------


@pytest.mark.asyncio
async def test_start_print_passes_both_names_to_the_adapter(processor, adapter) -> None:
    command_processor, cache, _outbox = processor
    cache.ensure_directory()
    cache.path_for(FILE_REF).write_bytes(PAYLOAD)

    result = await command_processor.dispatch(
        adapter,
        {
            "command_id": "1043",
            "printer_key": "printer-1",
            "action": "start_print",
            "args": {"file_ref": FILE_REF, "remote_name": "BWB-20-D-001-R2.gcode"},
        },
    )

    assert result["status"] == "done"
    assert adapter.prints == [(FILE_REF, "BWB-20-D-001-R2.gcode")]


@pytest.mark.asyncio
async def test_start_print_without_the_cached_file_fails_naming_the_ref(processor, adapter) -> None:
    """The agent has no URL for the file and must not invent one."""
    command_processor, _cache, _outbox = processor

    result = await command_processor.dispatch(
        adapter,
        {
            "command_id": "1044",
            "printer_key": "printer-1",
            "action": "start_print",
            "args": {"file_ref": FILE_REF},
        },
    )

    assert result["status"] == "failed"
    assert FILE_REF in result["error_text"]
    assert adapter.prints == []


@pytest.mark.asyncio
async def test_an_unimplemented_action_is_unsupported_not_failed(processor) -> None:
    command_processor, _cache, _outbox = processor
    adapter = RecordingAdapter(PrinterConfig(key="printer-1", brand="bambu", host="127.0.0.1"))

    with pytest.raises(UnsupportedCommandError):
        await adapter.pause()

    result = await command_processor.dispatch(
        adapter, {"command_id": "1045", "printer_key": "printer-1", "action": "pause"}
    )
    assert result["status"] == "unsupported"
