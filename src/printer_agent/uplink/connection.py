from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from ..adapters.base import PrinterAdapter
from ..config import AgentConfig
from ..contracts import (
    COMMAND_BEARING_TYPES,
    PROTOCOL_VERSION,
    CommandStatus,
    ErrorSnapshot,
    JobSnapshot,
    MessageType,
    PrinterSnapshot,
    PrinterStatus,
    TemperatureSnapshot,
    build_envelope,
    is_retryable_hello_reject,
    utc_now_iso,
)
from ..core.outbox import EventOutbox
from ..core.registry import build_adapter
from ..core.state import PrinterStateStore
from .commands import CommandProcessor

logger = logging.getLogger(__name__)

DEFAULT_AGENT_PATH = "/api/printers/agent"
OUTBOX_FLUSH_LIMIT = 200
# Hub messages that expect a command_result but that this agent cannot execute.
UNSUPPORTED_MESSAGE_TYPES = frozenset(COMMAND_BEARING_TYPES - {MessageType.command.value})


class HubRejected(RuntimeError):
    """The hub refused this agent for a reason reconnecting cannot fix."""


def hub_wss_url(hub_url: str) -> str:
    """Derive the WebSocket URL the agent connects to from the configured hub URL."""
    parsed = urlparse(hub_url)
    scheme = "wss" if parsed.scheme == "https" else "ws" if parsed.scheme == "http" else parsed.scheme
    path = parsed.path
    if path in {"", "/"}:
        # A bare host would hit the site root and get HTML instead of a
        # WebSocket handshake; the endpoint path belongs in hub_url.
        logger.warning(
            "hub_url has no path; falling back to the default agent endpoint",
            extra={"action": "hub_connect", "path": DEFAULT_AGENT_PATH},
        )
        path = DEFAULT_AGENT_PATH
    return urlunparse((scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))


def hub_auth_headers(agent_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {agent_token}"}


def hello_payload(config: AgentConfig, adapters: list[PrinterAdapter], agent_version: str) -> dict[str, Any]:
    """The handshake payload, shared with the connectivity check."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "agent_version": agent_version,
        "location_key": config.location_key,
        "printers": [
            {
                "printer_key": adapter.printer_key,
                "brand": adapter.printer.brand,
                "capabilities": asdict(adapter.capabilities()),
            }
            for adapter in adapters
        ],
    }


@dataclass(slots=True)
class HubConnectionState:
    connected: bool = False
    last_hello_at: str | None = None
    last_heartbeat_at: str | None = None
    last_telemetry_at: str | None = None


class HubConnection:
    def __init__(self, config: AgentConfig, outbox: EventOutbox):
        self.config = config
        self.outbox = outbox
        self.state = HubConnectionState()
        self._stop_event = asyncio.Event()
        self._command_processor = CommandProcessor(outbox)
        self._adapters = {printer.key: build_adapter(printer) for printer in config.printers}
        self._state_store = PrinterStateStore()
        self._connected_adapters: set[str] = set()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()
        self._heartbeat_deadline = 0.0
        self._inflight_events: dict[str, float] = {}

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        poll_task = asyncio.create_task(self._poll_loop(), name="printer-agent-poll")
        try:
            await self._connection_loop()
        finally:
            poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await poll_task
            with suppress(Exception, asyncio.CancelledError):
                await self._disconnect_adapters()

    # -- hub session ---------------------------------------------------

    async def _connection_loop(self) -> None:
        backoff = self.config.command_reconnect_backoff_s.min_s
        async with aiohttp.ClientSession() as session:
            while not self._stop_event.is_set():
                try:
                    async with session.ws_connect(self._hub_wss_url(), headers=self._headers()) as ws:
                        try:
                            backoff = self.config.command_reconnect_backoff_s.min_s
                            self._inflight_events.clear()
                            self._touch_heartbeat_deadline()
                            logger.info("hub connected", extra={"action": "hub_connect"})
                            await self._send_hello(ws)
                            # Published to the poll loop only after hello, so no
                            # telemetry can overtake the handshake.
                            self._ws = ws
                            self.state.connected = True
                            await self._flush_outbox(ws)
                            await self._ws_loop(ws)
                        finally:
                            self._ws = None
                            self.state.connected = False
                except (asyncio.CancelledError, HubRejected):
                    raise
                except Exception as exc:  # pragma: no cover - integration path
                    logger.warning(
                        "hub connection failed", extra={"action": "hub_connect", "error": str(exc)}
                    )
                if self._stop_event.is_set():
                    break
                await self._sleep_unless_stopped(backoff)
                backoff = min(backoff * 2, self.config.command_reconnect_backoff_s.max_s)

    async def _ws_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            timeout = max(0.1, self._heartbeat_deadline - loop.time())
            try:
                message = await ws.receive(timeout=timeout)
            except asyncio.TimeoutError:
                # aiohttp raises on a receive timeout without closing the socket:
                # the idle window is our cue to send a heartbeat, not to reconnect.
                await self._send_heartbeat(ws)
                continue
            if message.type is aiohttp.WSMsgType.TEXT:
                await self._handle_message(ws, message.json())
            elif message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            }:
                logger.info("hub closed the session", extra={"action": "hub_message"})
                break
            elif message.type is aiohttp.WSMsgType.ERROR:
                raise RuntimeError("websocket error")

    async def _handle_message(self, ws: aiohttp.ClientWebSocketResponse, payload: dict[str, Any]) -> None:
        message_type = payload.get("type")
        message_payload = payload.get("payload") or {}
        if message_type == MessageType.hello_ack.value:
            self.state.last_hello_at = utc_now_iso()
            logger.info(
                "hub accepted the agent",
                extra={
                    "action": "hub_hello_ack",
                    "printers": str(message_payload.get("printers", "")),
                    "pending_commands": str(message_payload.get("pending_commands", "")),
                },
            )
            return
        if message_type == MessageType.hello_reject.value:
            reason = str(message_payload.get("reason", "hello rejected"))
            code = str(message_payload.get("code", ""))
            extra = {"action": "hub_hello_reject", "code": code, "reason": reason}
            if is_retryable_hello_reject(code):
                logger.warning("hub is temporarily unavailable", extra=extra)
                raise RuntimeError(reason)
            # A bad token or an unsupported protocol cannot be fixed by trying
            # again: stop instead of hammering the hub with the same handshake.
            logger.error("hub rejected the agent; stopping", extra=extra)
            self.stop()
            raise HubRejected(reason)
        if message_type == MessageType.ack.value:
            acked_msg_id = str(message_payload.get("msg_id", ""))
            if acked_msg_id:
                self.outbox.ack_event(acked_msg_id)
                self._inflight_events.pop(acked_msg_id, None)
            return
        if message_type == "error":
            # Hub extension over agent-hub-v1: a per-message refusal that keeps
            # the session open. Silence here would mean resending forever.
            logger.warning(
                "hub refused a message",
                extra={
                    "action": "hub_error",
                    "code": str(message_payload.get("code", "")),
                    "error": str(message_payload.get("message", "")),
                    "ref_msg_id": str(message_payload.get("msg_id", "")),
                },
            )
            return
        if message_type == MessageType.command.value:
            await self._handle_command(ws, message_payload)
            return
        if message_type in UNSUPPORTED_MESSAGE_TYPES:
            await self._reply_unsupported(ws, str(message_type), message_payload)
            return
        logger.info("ignored message", extra={"action": "hub_message", "message_type": str(message_type)})

    async def _handle_command(self, ws: aiohttp.ClientWebSocketResponse, command: dict[str, Any]) -> None:
        command_id = str(command.get("command_id", ""))
        printer_key = str(command.get("printer_key", ""))
        if not command_id:
            logger.warning("command without command_id", extra={"action": "hub_command"})
            return
        adapter = self._adapters.get(printer_key)
        if adapter is None:
            logger.warning(
                "command for an unknown printer",
                extra={"action": "hub_command", "printer_key": printer_key, "command_id": command_id},
            )
            result = {
                "command_id": command_id,
                "printer_key": printer_key,
                "status": CommandStatus.failed.value,
                "error_text": f"printer {printer_key} is not configured on this agent",
                "response": {},
            }
            self.outbox.record_command_result(
                command_id, printer_key, result["status"], result["error_text"], {}
            )
        else:
            result = await self._command_processor.dispatch(adapter, command)
        await self._send(ws, build_envelope(MessageType.command_result.value, result))

    async def _reply_unsupported(
        self, ws: aiohttp.ClientWebSocketResponse, message_type: str, message_payload: dict[str, Any]
    ) -> None:
        command_id = str(message_payload.get("command_id", ""))
        if not command_id:
            # A command_result without command_id references nothing and the hub
            # rejects it; there is nothing useful to answer with.
            logger.warning(
                "unsupported hub message without command_id",
                extra={"action": "hub_message", "message_type": message_type},
            )
            return
        await self._send(
            ws,
            build_envelope(
                MessageType.command_result.value,
                {
                    "command_id": command_id,
                    "printer_key": str(message_payload.get("printer_key", "")),
                    "status": CommandStatus.unsupported.value,
                    "error_text": f"{message_type} is not implemented yet",
                    "response": {},
                },
            ),
        )

    async def _send_hello(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        payload = hello_payload(self.config, list(self._adapters.values()), self._agent_version())
        await self._send(ws, build_envelope(MessageType.hello.value, payload))
        self.state.last_hello_at = utc_now_iso()

    async def _send_heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self._send(ws, build_envelope(MessageType.heartbeat.value, {"location_key": self.config.location_key}))
        self.state.last_heartbeat_at = utc_now_iso()

    async def _send(self, ws: aiohttp.ClientWebSocketResponse, envelope: dict[str, Any]) -> None:
        async with self._send_lock:
            await ws.send_json(envelope)
        self._touch_heartbeat_deadline()

    def _touch_heartbeat_deadline(self) -> None:
        # Per contract the heartbeat only fills silence: any send defers it.
        self._heartbeat_deadline = asyncio.get_running_loop().time() + self.config.heartbeat_interval_s

    # -- printer polling -----------------------------------------------

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshots = await self._collect_snapshots()
                self._record_events(snapshots)
                await self._flush_outbox()
                await self._send_telemetry(snapshots)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - integration path
                logger.warning("telemetry cycle failed", extra={"action": "telemetry", "error": str(exc)})
            await self._sleep_unless_stopped(self.config.telemetry_interval_s)

    async def _collect_snapshots(self) -> list[PrinterSnapshot]:
        if not self._adapters:
            return []
        timeout = max(5, self.config.telemetry_interval_s)
        results = await asyncio.gather(
            *(self._poll_adapter(key, adapter, timeout) for key, adapter in self._adapters.items())
        )
        return list(results)

    async def _poll_adapter(self, key: str, adapter: PrinterAdapter, timeout: float) -> PrinterSnapshot:
        await self._ensure_adapter_connected(key, adapter)
        try:
            return await asyncio.wait_for(adapter.get_state(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected_adapters.discard(key)
            logger.warning(
                "printer poll failed", extra={"action": "poll", "printer_key": key, "error": str(exc)}
            )
            return self._offline_snapshot(adapter, exc)

    async def _ensure_adapter_connected(self, key: str, adapter: PrinterAdapter) -> None:
        if key in self._connected_adapters:
            return
        try:
            await adapter.connect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "printer connect failed", extra={"action": "poll", "printer_key": key, "error": str(exc)}
            )
            return
        self._connected_adapters.add(key)

    def _record_events(self, snapshots: list[PrinterSnapshot]) -> None:
        queued = 0
        for snapshot in snapshots:
            change = self._state_store.update(snapshot)
            if change is None:
                continue
            snapshot_payload = snapshot.to_dict()
            for kind in change.kinds:
                msg_id = self.outbox.enqueue_event(
                    build_envelope(
                        MessageType.event.value,
                        {
                            "printer_key": snapshot.printer_key,
                            "kind": kind,
                            "snapshot": snapshot_payload,
                        },
                    )
                )
                queued += 1
                logger.info(
                    "event queued",
                    extra={
                        "action": "event",
                        "printer_key": snapshot.printer_key,
                        "kind": kind,
                        "msg_id": msg_id,
                    },
                )
        if queued:
            self.outbox.prune(self.config.outbox.max_events)

    async def _flush_outbox(self, ws: aiohttp.ClientWebSocketResponse | None = None) -> None:
        ws = ws or self._ws
        if ws is None or ws.closed:
            return
        now = asyncio.get_running_loop().time()
        resend_after = max(30.0, float(self.config.heartbeat_interval_s) * 2)
        for envelope in self.outbox.list_pending_events(limit=OUTBOX_FLUSH_LIMIT):
            msg_id = str(envelope.get("msg_id", ""))
            sent_at = self._inflight_events.get(msg_id)
            if sent_at is not None and now - sent_at < resend_after:
                continue
            await self._send(ws, envelope)
            self._inflight_events[msg_id] = now

    async def _send_telemetry(self, snapshots: list[PrinterSnapshot]) -> None:
        ws = self._ws
        if ws is None or ws.closed or not snapshots:
            return
        await self._send(
            ws, build_envelope(MessageType.telemetry.value, {"snapshots": [snapshot.to_dict() for snapshot in snapshots]})
        )
        self.state.last_telemetry_at = utc_now_iso()

    async def _disconnect_adapters(self) -> None:
        for key, adapter in self._adapters.items():
            self._connected_adapters.discard(key)
            try:
                await adapter.disconnect()
            except Exception as exc:  # pragma: no cover - shutdown path
                logger.warning(
                    "printer disconnect failed",
                    extra={"action": "shutdown", "printer_key": key, "error": str(exc)},
                )

    def _offline_snapshot(self, adapter: PrinterAdapter, exc: Exception) -> PrinterSnapshot:
        return PrinterSnapshot(
            printer_key=adapter.printer_key,
            status=PrinterStatus.offline,
            status_raw="offline",
            job=JobSnapshot(),
            temps=TemperatureSnapshot(),
            error=ErrorSnapshot(code="offline", message=str(exc) or adapter.printer.brand),
            capabilities=adapter.capabilities(),
        )

    async def _sleep_unless_stopped(self, delay: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)

    # -- helpers -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return hub_auth_headers(self.config.agent_token)

    def _hub_wss_url(self) -> str:
        return hub_wss_url(self.config.hub_url)

    @staticmethod
    def _agent_version() -> str:
        from .. import __version__

        return __version__
