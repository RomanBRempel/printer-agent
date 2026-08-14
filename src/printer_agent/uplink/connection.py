from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from ..config import AgentConfig
from ..contracts import build_envelope, utc_now_iso
from ..core.outbox import EventOutbox
from ..core.registry import build_adapter
from .commands import CommandProcessor

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HubConnectionState:
    connected: bool = False
    last_hello_at: str | None = None
    last_heartbeat_at: str | None = None


class HubConnection:
    def __init__(self, config: AgentConfig, outbox: EventOutbox):
        self.config = config
        self.outbox = outbox
        self.state = HubConnectionState()
        self._stop_event = asyncio.Event()
        self._command_processor = CommandProcessor(outbox)
        self._adapters = {printer.key: build_adapter(printer) for printer in config.printers}

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        backoff = self.config.command_reconnect_backoff_s.min_s
        async with aiohttp.ClientSession() as session:
            while not self._stop_event.is_set():
                try:
                    async with session.ws_connect(self._hub_wss_url(), headers=self._headers()) as ws:
                        self.state.connected = True
                        backoff = self.config.command_reconnect_backoff_s.min_s
                        await self._send_hello(ws)
                        await self._ws_loop(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - integration path
                    self.state.connected = False
                    logger.exception("hub connection failed", extra={"action": "hub_connect", "error": str(exc)})
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.config.command_reconnect_backoff_s.max_s)

    async def _ws_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        loop = asyncio.get_running_loop()
        heartbeat_deadline = loop.time() + self.config.heartbeat_interval_s
        while not self._stop_event.is_set():
            timeout = max(0.1, heartbeat_deadline - loop.time())
            message = await ws.receive(timeout=timeout)
            if message.type == aiohttp.WSMsgType.TEXT:
                await self._handle_message(ws, message.json())
                heartbeat_deadline = loop.time() + self.config.heartbeat_interval_s
            elif message.type == aiohttp.WSMsgType.CLOSED:
                break
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise RuntimeError("websocket error")
            else:
                if loop.time() >= heartbeat_deadline:
                    await self._send_heartbeat(ws)
                    heartbeat_deadline = loop.time() + self.config.heartbeat_interval_s

    async def _handle_message(self, ws: aiohttp.ClientWebSocketResponse, payload: dict[str, Any]) -> None:
        message_type = payload.get("type")
        if message_type == "hello_ack":
            self.state.last_hello_at = utc_now_iso()
            return
        if message_type == "hello_reject":
            self.state.connected = False
            raise RuntimeError(str(payload.get("payload", {}).get("reason", "hello rejected")))
        if message_type == "ack":
            acked_msg_id = str(payload.get("payload", {}).get("msg_id", ""))
            if acked_msg_id:
                self.outbox.ack_event(acked_msg_id)
            return
        if message_type == "command":
            command_payload = payload.get("payload") or {}
            printer_key = str(command_payload["printer_key"])
            adapter = self._adapters[printer_key]
            result = await self._command_processor.dispatch(adapter, command_payload)
            await ws.send_json(build_envelope("command_result", result))
            return
        if message_type in {"file_offer", "camera_request", "camera_stop"}:
            await ws.send_json(build_envelope("command_result", {"status": "unsupported", "error_text": f"{message_type} is not implemented yet"}))
            return
        logger.info("ignored message", extra={"action": "hub_message", "message_type": str(message_type)})

    async def _send_hello(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        payload = {
            "protocol_version": 1,
            "agent_version": self._agent_version(),
            "location_key": self.config.location_key,
            "printers": [
                {
                    "printer_key": printer.printer_key,
                    "brand": printer.printer.brand,
                    "capabilities": asdict(adapter.capabilities()),
                }
                for printer, adapter in ((self._adapters[key].printer, self._adapters[key]) for key in self._adapters)
            ],
        }
        await ws.send_json(build_envelope("hello", payload))
        self.state.last_hello_at = utc_now_iso()

    async def _send_heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_json(build_envelope("heartbeat", {"location_key": self.config.location_key}))
        self.state.last_heartbeat_at = utc_now_iso()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.agent_token}"}

    def _hub_wss_url(self) -> str:
        parsed = urlparse(self.config.hub_url)
        scheme = "wss" if parsed.scheme == "https" else "ws" if parsed.scheme == "http" else parsed.scheme
        return urlunparse((scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

    @staticmethod
    def _agent_version() -> str:
        from .. import __version__

        return __version__
