"""Every command-bearing hub message is answered, and by its own command_id.

A `command_result` without one references nothing: the hub refuses it with
`command_id_required` and the command closes as undelivered. That used to be
harmless because the hub sent none of these types; it now sends all three.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from printer_agent.adapters.base import PrinterAdapter
from printer_agent.config import AgentConfig, OutboxConfig, PrintFilesConfig, PrinterConfig
from printer_agent.contracts import PrinterCapabilities, PrinterSnapshot, PrinterStatus, build_envelope
from printer_agent.core.outbox import EventOutbox
from printer_agent.uplink.connection import HubConnection


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def sent_of_type(self, message_type: str) -> list[dict[str, Any]]:
        return [envelope for envelope in self.sent if envelope.get("type") == message_type]


class QuietAdapter(PrinterAdapter):
    """Supports nothing: every command has a knowable answer without a printer."""

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_state(self) -> PrinterSnapshot:
        return PrinterSnapshot(printer_key=self.printer_key, status=PrinterStatus.idle, status_raw="idle")

    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()


@pytest.fixture
def connection(tmp_path):
    config = AgentConfig(
        hub_url="https://hub.example.com/api/printers/agent",
        agent_token="secret-token",
        location_key="loc-001",
        outbox=OutboxConfig(database_path=tmp_path / "outbox.sqlite3"),
        print_files=PrintFilesConfig(directory=tmp_path / "print-files"),
        printers=[PrinterConfig(key="printer-1", brand="moonraker", host="127.0.0.1")],
    )
    outbox = EventOutbox(config.outbox.database_path)
    hub = HubConnection(config, outbox)
    hub._adapters = {"printer-1": QuietAdapter(config.printers[0])}
    try:
        yield hub, outbox
    finally:
        outbox.close()


async def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def offer(**overrides: Any) -> dict[str, Any]:
    payload = {
        "command_id": "1042",
        "printer_key": "printer-1",
        "file_ref": "pf_7f3a",
        "url": "https://hub.example.com/api/printers/files/pf_7f3a",
        "remote_name": "job.gcode",
        "sha256": "0" * 64,
        "size_bytes": 12,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_a_file_offer_is_answered_beside_the_receive_loop(connection) -> None:
    """A transfer takes minutes; the session cannot go deaf for that long."""
    hub, _outbox = connection
    ws = FakeWebSocket()
    hub._ws = ws
    hub._touch_heartbeat_deadline()

    await hub._handle_message(ws, build_envelope("file_offer", offer()))

    # The receive loop is free again before the transfer has answered.
    assert ws.sent == []
    assert await wait_for(lambda: ws.sent_of_type("command_result"))
    result = ws.sent_of_type("command_result")[0]["payload"]
    assert result["command_id"] == "1042"
    # This adapter cannot take files at all, so nothing was ever fetched.
    assert result["status"] == "unsupported"


@pytest.mark.asyncio
async def test_a_file_offer_without_command_id_is_dropped(connection) -> None:
    hub, _outbox = connection
    ws = FakeWebSocket()
    hub._ws = ws

    await hub._handle_message(ws, build_envelope("file_offer", offer(command_id="")))
    await asyncio.sleep(0.05)

    assert ws.sent == []


@pytest.mark.asyncio
async def test_camera_messages_are_answered_with_their_command_id(connection) -> None:
    hub, _outbox = connection
    ws = FakeWebSocket()
    hub._touch_heartbeat_deadline()

    await hub._handle_message(
        ws,
        build_envelope(
            "camera_request",
            {
                "command_id": "1044",
                "printer_key": "printer-1",
                "session_id": "cam_1",
                "upload_url": "https://hub.example.com/api/printers/camera/cam_1",
            },
        ),
    )
    await hub._handle_message(
        ws,
        build_envelope(
            "camera_stop",
            {"command_id": "1045", "printer_key": "printer-1", "session_id": "cam_1"},
        ),
    )

    results = [envelope["payload"] for envelope in ws.sent_of_type("command_result")]
    assert [item["command_id"] for item in results] == ["1044", "1045"]
    # No camera on this adapter: refused as unsupported, not as a failure.
    assert results[0]["status"] == "unsupported"
    assert results[1]["status"] == "done"


@pytest.mark.asyncio
async def test_a_command_for_an_unknown_printer_is_still_answered(connection) -> None:
    hub, _outbox = connection
    ws = FakeWebSocket()
    hub._touch_heartbeat_deadline()

    await hub._handle_message(
        ws, build_envelope("file_offer", offer(printer_key="ghost", command_id="1046"))
    )

    result = ws.sent_of_type("command_result")[0]["payload"]
    assert result["command_id"] == "1046"
    assert result["status"] == "failed"
