from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import pytest

from printer_agent.adapters.base import PrinterAdapter
from printer_agent.config import AgentConfig, OutboxConfig, PrinterConfig, load_config
from printer_agent.contracts import (
    ErrorSnapshot,
    JobSnapshot,
    JobStatus,
    PrinterCapabilities,
    PrinterSnapshot,
    PrinterStatus,
    build_envelope,
)
from printer_agent.core.outbox import EventOutbox
from printer_agent.uplink.connection import (
    DEFAULT_AGENT_PATH,
    PRINTER_POLL_TIMEOUT_S,
    HubConnection,
    HubRejected,
)


class FakeMessage:
    def __init__(self, message_type: aiohttp.WSMsgType, payload: dict[str, Any] | None = None):
        self.type = message_type
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeWebSocket:
    """Stands in for aiohttp's client websocket: records sends, scripts receives."""

    def __init__(self, incoming: list[Any] | None = None):
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._incoming = list(incoming or [])

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def receive(self, timeout: float | None = None) -> FakeMessage:
        if not self._incoming:
            return FakeMessage(aiohttp.WSMsgType.CLOSED)
        item = self._incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def sent_of_type(self, message_type: str) -> list[dict[str, Any]]:
        return [envelope for envelope in self.sent if envelope.get("type") == message_type]


class FakeAdapter(PrinterAdapter):
    def __init__(self, printer: PrinterConfig, snapshot: PrinterSnapshot | None = None):
        super().__init__(printer)
        self.snapshot = snapshot or PrinterSnapshot(
            printer_key=printer.key, status=PrinterStatus.idle, status_raw="idle"
        )
        self.raise_on_get_state: Exception | None = None
        self.connect_calls = 0
        self.paused = False

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        return None

    async def get_state(self) -> PrinterSnapshot:
        if self.raise_on_get_state is not None:
            raise self.raise_on_get_state
        return self.snapshot

    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(pause=True, resume=True, cancel=True)

    async def pause(self) -> dict[str, Any]:
        self.paused = True
        return {"ok": True}


def build_config(tmp_path, hub_url: str = "https://hub.example.com/api/printers/agent") -> AgentConfig:
    return AgentConfig(
        hub_url=hub_url,
        agent_token="secret-token",
        location_key="loc-001",
        telemetry_interval_s=1,
        heartbeat_interval_s=15,
        outbox=OutboxConfig(database_path=tmp_path / "outbox.sqlite3"),
        printers=[PrinterConfig(key="printer-1", brand="moonraker", host="127.0.0.1")],
    )


@pytest.fixture
def connection(tmp_path):
    config = build_config(tmp_path)
    outbox = EventOutbox(config.outbox.database_path)
    hub = HubConnection(config, outbox)
    adapter = FakeAdapter(config.printers[0])
    hub._adapters = {adapter.printer_key: adapter}
    try:
        yield hub, adapter, outbox
    finally:
        outbox.close()


def test_hub_wss_url_keeps_the_configured_path(tmp_path) -> None:
    config = build_config(tmp_path)
    hub = HubConnection(config, EventOutbox(config.outbox.database_path))
    assert hub._hub_wss_url() == "wss://hub.example.com/api/printers/agent"
    hub.outbox.close()


def test_hub_wss_url_falls_back_to_the_agent_endpoint(tmp_path) -> None:
    config = build_config(tmp_path, hub_url="https://hub.example.com")
    hub = HubConnection(config, EventOutbox(config.outbox.database_path))
    assert hub._hub_wss_url() == f"wss://hub.example.com{DEFAULT_AGENT_PATH}"
    hub.outbox.close()


@pytest.mark.asyncio
async def test_hello_reports_every_printer_with_capabilities(connection) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    await hub._send_hello(ws)

    hello = ws.sent_of_type("hello")[0]
    assert hello["v"] == 1
    payload = hello["payload"]
    assert payload["location_key"] == "loc-001"
    assert payload["printers"] == [
        {
            "printer_key": "printer-1",
            "brand": "moonraker",
            "capabilities": {
                "camera": False,
                "ams": False,
                "cfs": False,
                "pause": True,
                "resume": True,
                "cancel": True,
                "upload": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_receive_timeout_sends_heartbeat_and_keeps_the_session(connection) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket([asyncio.TimeoutError(), FakeMessage(aiohttp.WSMsgType.CLOSED)])
    hub._touch_heartbeat_deadline()

    await hub._ws_loop(ws)

    heartbeats = ws.sent_of_type("heartbeat")
    assert len(heartbeats) == 1
    assert heartbeats[0]["payload"] == {"location_key": "loc-001"}
    assert hub.state.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_telemetry_batches_all_snapshots_in_one_message(connection) -> None:
    hub, adapter, _outbox = connection
    adapter.snapshot = PrinterSnapshot(
        printer_key="printer-1",
        status=PrinterStatus.printing,
        status_raw="printing",
        job=JobSnapshot(name="benchy", progress_pct=42.5),
    )
    ws = FakeWebSocket()
    hub._ws = ws
    hub._touch_heartbeat_deadline()

    snapshots = await hub._collect_snapshots()
    await hub._send_telemetry(snapshots)

    telemetry = ws.sent_of_type("telemetry")[0]
    assert [item["printer_key"] for item in telemetry["payload"]["snapshots"]] == ["printer-1"]
    assert telemetry["payload"]["snapshots"][0]["job"]["name"] == "benchy"
    assert adapter.connect_calls == 1


@pytest.mark.asyncio
async def test_unreachable_printer_reports_offline_instead_of_failing(connection) -> None:
    hub, adapter, _outbox = connection
    adapter.raise_on_get_state = RuntimeError("connection refused")

    snapshots = await hub._collect_snapshots()

    assert [snapshot.status for snapshot in snapshots] == [PrinterStatus.offline]


@pytest.mark.asyncio
async def test_poll_budget_does_not_follow_the_telemetry_interval(connection) -> None:
    """How often we ask must not decide how long a printer may take to answer.

    The budget used to be `max(5, telemetry_interval_s)`. A printing Creality K1
    needs longer than that for one state query, so every poll of it was
    cancelled and the hub was told the printer was offline — while the desktop
    app, which waits 12 s, showed the same machine printing.
    """
    hub, _adapter, _outbox = connection
    seen: list[float] = []
    original = hub._poll_adapter

    async def spy(key, adapter, timeout):
        seen.append(timeout)
        return await original(key, adapter, timeout)

    hub._poll_adapter = spy

    hub.config.telemetry_interval_s = 1
    await hub._collect_snapshots()
    assert seen == [PRINTER_POLL_TIMEOUT_S]

    # A slower cadence still widens it: the interval is a floor, never a ceiling.
    seen.clear()
    hub.config.telemetry_interval_s = 60
    await hub._collect_snapshots()
    assert seen == [60]


@pytest.mark.asyncio
async def test_a_poll_timeout_names_the_budget_rather_than_the_brand(connection) -> None:
    """`asyncio.TimeoutError` has an empty `str()`.

    The fallback used to be the printer's brand, which put
    `{"code": "offline", "message": "moonraker"}` on the hub's printer page — the
    name of the adapter, telling an operator nothing about what went wrong.
    """
    hub, adapter, _outbox = connection
    adapter.raise_on_get_state = asyncio.TimeoutError()

    snapshot = (await hub._collect_snapshots())[0]

    assert snapshot.status is PrinterStatus.offline
    assert snapshot.error is not None
    assert snapshot.error.message != adapter.printer.brand
    assert snapshot.error.message == f"no answer within {PRINTER_POLL_TIMEOUT_S:g}s"


@pytest.mark.asyncio
async def test_events_are_queued_flushed_and_cleared_on_ack(connection) -> None:
    hub, adapter, outbox = connection
    ws = FakeWebSocket()
    hub._ws = ws
    hub._touch_heartbeat_deadline()

    hub._record_events(await hub._collect_snapshots())
    await hub._flush_outbox()

    events = ws.sent_of_type("event")
    assert len(events) == 1
    assert events[0]["payload"]["kind"] == "snapshot"
    assert events[0]["payload"]["snapshot"]["printer_key"] == "printer-1"
    assert len(outbox.list_pending_events()) == 1

    await hub._handle_message(ws, build_envelope("ack", {"msg_id": events[0]["msg_id"]}))
    assert outbox.list_pending_events() == []

    # A second flush must not resend an acknowledged event.
    await hub._flush_outbox()
    assert len(ws.sent_of_type("event")) == 1


@pytest.mark.asyncio
async def test_a_refused_event_stops_being_resent(connection) -> None:
    """The hub refuses telemetry for a printer not attached to this agent, and
    it will keep refusing: resending it forever is how the outbox grows without
    bound on a location with one unattached printer."""
    hub, _adapter, outbox = connection
    ws = FakeWebSocket()
    hub._ws = ws
    hub._touch_heartbeat_deadline()

    hub._record_events(await hub._collect_snapshots())
    await hub._flush_outbox()
    refused = ws.sent_of_type("event")[0]

    await hub._handle_message(
        ws,
        build_envelope(
            "error",
            {"code": "printer_not_found", "message": "printer is not attached", "msg_id": refused["msg_id"]},
        ),
    )
    await hub._flush_outbox()

    assert outbox.list_pending_events() == []
    assert len(ws.sent_of_type("event")) == 1


@pytest.mark.asyncio
async def test_an_error_about_an_unknown_message_leaves_the_outbox_alone(connection) -> None:
    hub, _adapter, outbox = connection
    ws = FakeWebSocket()
    hub._ws = ws
    hub._touch_heartbeat_deadline()
    hub._record_events(await hub._collect_snapshots())

    await hub._handle_message(
        ws, build_envelope("error", {"code": "command_id_required", "message": "no command_id", "msg_id": ""})
    )

    assert len(outbox.list_pending_events()) == 1


@pytest.mark.asyncio
async def test_progress_drift_does_not_queue_events(connection) -> None:
    hub, adapter, outbox = connection
    adapter.snapshot = PrinterSnapshot(
        printer_key="printer-1",
        status=PrinterStatus.printing,
        status_raw="printing",
        job=JobSnapshot(name="benchy", progress_pct=10.0, layer=3, status=JobStatus.printing),
    )
    hub._record_events(await hub._collect_snapshots())
    assert len(outbox.list_pending_events()) == 1

    adapter.snapshot = PrinterSnapshot(
        printer_key="printer-1",
        status=PrinterStatus.printing,
        status_raw="printing",
        job=JobSnapshot(name="benchy", progress_pct=11.0, layer=4, status=JobStatus.printing),
    )
    hub._record_events(await hub._collect_snapshots())
    assert len(outbox.list_pending_events()) == 1

    adapter.snapshot = PrinterSnapshot(
        printer_key="printer-1",
        status=PrinterStatus.error,
        status_raw="error",
        job=JobSnapshot(name="benchy", progress_pct=11.0, status=JobStatus.failed),
        error=ErrorSnapshot(code="E1", message="thermal runaway"),
    )
    hub._record_events(await hub._collect_snapshots())
    kinds = {event["payload"]["kind"] for event in outbox.list_pending_events()}
    assert {"status_changed", "job_changed", "error_changed"} <= kinds


@pytest.mark.asyncio
async def test_poll_loop_sends_telemetry_and_queues_events(connection) -> None:
    hub, _adapter, outbox = connection
    ws = FakeWebSocket()
    hub._ws = ws
    hub._touch_heartbeat_deadline()

    task = asyncio.create_task(hub._poll_loop())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ws.sent_of_type("telemetry"):
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ws.sent_of_type("telemetry")
    assert ws.sent_of_type("event")
    assert len(outbox.list_pending_events()) == 1


@pytest.mark.asyncio
async def test_command_is_dispatched_and_answered(connection) -> None:
    hub, adapter, _outbox = connection
    ws = FakeWebSocket()

    await hub._handle_message(
        ws,
        build_envelope("command", {"command_id": "cmd-1", "printer_key": "printer-1", "action": "pause"}),
    )

    result = ws.sent_of_type("command_result")[0]["payload"]
    assert adapter.paused is True
    assert result["command_id"] == "cmd-1"
    assert result["status"] == "done"


def write_config_file(
    tmp_path, printer_keys: list[str], agent_token: str = "secret-token", brand: str = "moonraker"
) -> Any:
    """A config file on disk, which is what the reload path actually watches."""
    printers = "\n".join(
        f"  - key: {key}\n    brand: {brand}\n    host: 127.0.0.1\n    port: 7125" for key in printer_keys
    )
    path = tmp_path / "agent.yaml"
    path.write_text(
        "hub_url: https://hub.example.com/api/printers/agent\n"
        f"agent_token: {agent_token}\n"
        "location_key: loc-001\n"
        "telemetry_interval_s: 1\n"
        "outbox:\n"
        f"  database_path: {(tmp_path / 'outbox.sqlite3').as_posix()}\n"
        "printers:\n" + printers + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def file_backed_connection(tmp_path):
    path = write_config_file(tmp_path, ["printer-1"])
    config = load_config(path)
    outbox = EventOutbox(config.outbox.database_path)
    hub = HubConnection(config, outbox)
    ws = FakeWebSocket()
    hub._ws = ws
    try:
        yield hub, ws, path
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_config_reload_picks_up_a_new_printer_and_tells_the_hub(file_backed_connection, tmp_path) -> None:
    """A printer added at the location used to reach the hub only after a
    service restart, which is indistinguishable from a printer that is simply
    unreachable."""
    hub, ws, path = file_backed_connection
    write_config_file(tmp_path, ["printer-1", "printer-2"])

    await hub._reload_config_if_changed()

    assert sorted(hub._adapters) == ["printer-1", "printer-2"]
    payload = ws.sent_of_type("inventory")[0]["payload"]
    assert [entry["printer_key"] for entry in payload["printers"]] == ["printer-1", "printer-2"]
    assert "request_msg_id" not in payload


@pytest.mark.asyncio
async def test_config_reload_forgets_a_removed_printer(file_backed_connection, tmp_path) -> None:
    hub, ws, _path = file_backed_connection
    hub._record_events(await hub._collect_snapshots())
    write_config_file(tmp_path, ["printer-2"])

    await hub._reload_config_if_changed()

    assert sorted(hub._adapters) == ["printer-2"]
    assert hub._state_store.get("printer-1") is None
    assert ws.sent_of_type("inventory")[0]["payload"]["printers"][0]["printer_key"] == "printer-2"


@pytest.mark.asyncio
async def test_a_broken_config_leaves_the_running_roster_alone(file_backed_connection, tmp_path) -> None:
    """Half-typed edits happen; taking a working agent's printers away because
    the file is mid-save is worse than ignoring the save."""
    hub, ws, _path = file_backed_connection
    write_config_file(tmp_path, ["printer-1", "printer-2"], brand="mooonraker")

    await hub._reload_config_if_changed()

    assert sorted(hub._adapters) == ["printer-1"]
    assert ws.sent_of_type("inventory") == []


@pytest.mark.asyncio
async def test_an_unchanged_config_is_not_reapplied(file_backed_connection) -> None:
    hub, ws, _path = file_backed_connection

    await hub._reload_config_if_changed()
    await hub._reload_config_if_changed()

    assert ws.sent_of_type("inventory") == []


@pytest.mark.asyncio
async def test_hub_wiring_changes_do_not_apply_to_a_live_session(file_backed_connection, tmp_path, caplog) -> None:
    """The session was opened with that token; adopting a new one mid-flight
    would mean tearing down the thing carrying the change."""
    hub, _ws, _path = file_backed_connection
    write_config_file(tmp_path, ["printer-1"], agent_token="rotated-token")

    with caplog.at_level(logging.WARNING):
        await hub._reload_config_if_changed()

    assert hub.config.agent_token == "secret-token"
    assert "agent_token" in [getattr(record, "error", "") for record in caplog.records]


@pytest.mark.asyncio
async def test_inventory_request_is_answered_with_the_roster(connection) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()
    request = build_envelope("inventory_request", {})

    await hub._handle_message(ws, request)

    payload = ws.sent_of_type("inventory")[0]["payload"]
    assert payload["request_msg_id"] == request["msg_id"]
    assert payload["location_key"] == hub.config.location_key
    assert [entry["printer_key"] for entry in payload["printers"]] == ["printer-1"]


@pytest.mark.asyncio
async def test_inventory_carries_the_same_roster_as_hello(connection) -> None:
    """The contract promises one parser reads both; that holds only if one
    function builds the array."""
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    await hub._send_hello(ws)
    await hub._handle_message(ws, build_envelope("inventory_request", {}))

    hello = ws.sent_of_type("hello")[0]["payload"]
    inventory = ws.sent_of_type("inventory")[0]["payload"]
    assert hello["printers"] == inventory["printers"]


@pytest.mark.asyncio
async def test_inventory_request_is_not_answered_with_a_command_result(connection) -> None:
    """It touches no printer, so answering it as a command would invent a
    command_id the hub never sent."""
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    await hub._handle_message(ws, build_envelope("inventory_request", {}))

    assert ws.sent_of_type("command_result") == []


@pytest.mark.asyncio
async def test_command_for_an_unknown_printer_fails_without_dropping_the_session(connection) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    await hub._handle_message(
        ws,
        build_envelope("command", {"command_id": "cmd-2", "printer_key": "ghost", "action": "pause"}),
    )

    result = ws.sent_of_type("command_result")[0]["payload"]
    assert result["command_id"] == "cmd-2"
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_file_offer_without_command_id_is_not_answered(connection) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    await hub._handle_message(ws, build_envelope("file_offer", {"url": "https://hub/f.gcode"}))

    assert ws.sent == []


@pytest.mark.asyncio
async def test_camera_request_with_command_id_is_answered_unsupported(connection) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    await hub._handle_message(
        ws, build_envelope("camera_request", {"command_id": "cmd-3", "printer_key": "printer-1"})
    )

    result = ws.sent_of_type("command_result")[0]["payload"]
    assert result["command_id"] == "cmd-3"
    assert result["status"] == "unsupported"


@pytest.mark.asyncio
async def test_terminal_hello_reject_stops_the_agent(connection) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    with pytest.raises(HubRejected):
        await hub._handle_message(
            ws,
            build_envelope("hello_reject", {"code": "auth_rejected", "reason": "unknown principal"}),
        )

    assert hub._stop_event.is_set()


@pytest.mark.asyncio
async def test_temporary_hello_reject_only_triggers_a_reconnect(connection) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    with pytest.raises(RuntimeError) as exc_info:
        await hub._handle_message(
            ws,
            build_envelope("hello_reject", {"code": "temporarily_unavailable", "reason": "restarting"}),
        )

    assert not isinstance(exc_info.value, HubRejected)
    assert not hub._stop_event.is_set()


@pytest.mark.asyncio
async def test_hub_error_is_logged_with_its_code(connection, caplog) -> None:
    hub, _adapter, _outbox = connection
    ws = FakeWebSocket()

    with caplog.at_level(logging.WARNING):
        await hub._handle_message(
            ws, build_envelope("error", {"code": "command_id_required", "message": "no command_id"})
        )

    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert any(getattr(record, "code", "") == "command_id_required" for record in caplog.records)
