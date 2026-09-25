from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from ..adapters.base import PrinterAdapter, UnsupportedCommandError
from ..config import (
    AgentConfig,
    PrinterConfig,
    config_from_dict,
    config_to_dict,
    load_config_file,
    parse_config,
    save_config,
    validate_config,
)
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
from ..core.discovery import find_printers, hosts_for, local_ipv4_networks
from ..core.filecache import PrintFileCache
from ..core.logtail import (
    UnknownLogFile,
    clip_to_budget,
    filter_by_level,
    list_log_files,
    resolve_log_file,
    scrub,
    tail_lines,
)
from ..core.outbox import EventOutbox
from ..core.recovery import plan_relocation, scan_networks, write_printer_fields
from ..core.registry import build_adapter
from ..logsetup import active_log_path
from ..core.state import PrinterStateStore
from .camera import CameraService
from .commands import CommandProcessor
from ..settings_bundle import (
    BundleError,
    apply_remote_settings,
    readonly_settings,
    redacted_settings,
)
from .files import PrintFileService

logger = logging.getLogger(__name__)

DEFAULT_AGENT_PATH = "/api/printers/agent"
OUTBOX_FLUSH_LIMIT = 200

#: How long a printer may take to answer one poll. Deliberately *not* the
#: telemetry interval: that says how often to ask, not how slow a machine is
#: allowed to be. A Creality K-series answers `printer.objects.query` in well
#: over the 5 s a default interval allowed, so every poll of a printing K1 was
#: cancelled and reported as offline while the desktop app — which waits 12 s —
#: showed the same printer running.
PRINTER_POLL_TIMEOUT_S = 20.0

#: Ceiling for the gap between recovery scans that keep finding nothing. A
#: printer that is switched off is indistinguishable from one that moved, and
#: it must not cost the location a subnet sweep every few minutes all weekend.
RECOVERY_MAX_INTERVAL_S = 3600.0

#: `error.code` of a printer whose address now answers as a different device.
IDENTITY_MISMATCH = "identity_mismatch"


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


def printer_roster(adapters: list[PrinterAdapter]) -> list[dict[str, Any]]:
    """The `printers[]` array, built once for both `hello` and `inventory`.

    The contract says the two carry the same array field for field, so the hub
    reads them with one parser; that only stays true if one function builds it.
    """
    return [
        {
            "printer_key": adapter.printer_key,
            "brand": adapter.printer.brand,
            "capabilities": asdict(adapter.capabilities()),
        }
        for adapter in adapters
    ]


def roster_stamp(printers: list[dict[str, Any]]) -> str:
    """Fingerprint of the roster as the hub last saw it.

    Capabilities are not all known at handshake time — a camera is found by
    probing the printer, which outlives `hello` — so the roster the agent sent
    can stop being true without anything in the config changing.
    """
    return json.dumps(printers, sort_keys=True)


def hello_payload(
    config: AgentConfig,
    adapters: list[PrinterAdapter],
    agent_version: str,
    update: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The handshake payload, shared with the connectivity check.

    `update` — что известно про доступную версию. Ключ ОТСУТСТВУЕТ, когда фид
    ещё не проверялся: «не знаем» и «обновлений нет» — разные утверждения, и
    хаб, показавший второе вместо первого, объявит агента актуальным, ничего об
    этом не зная.
    """
    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "agent_version": agent_version,
        "location_key": config.location_key,
        "printers": printer_roster(adapters),
    }
    if update:
        payload["update"] = update
    return payload


def inventory_payload(
    config: AgentConfig,
    adapters: list[PrinterAdapter],
    agent_version: str,
    request_msg_id: str = "",
    update: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The printer roster: who this agent is configured for.

    `request_msg_id` is omitted when the agent sends this on its own after the
    config changed — an absent value is left out rather than sent empty, as
    everywhere else on this wire.
    """
    payload: dict[str, Any] = {
        "location_key": config.location_key,
        "agent_version": agent_version,
        "printers": printer_roster(adapters),
    }
    if request_msg_id:
        payload["request_msg_id"] = request_msg_id
    if update:
        payload["update"] = update
    return payload


def _config_stamp(config: AgentConfig) -> tuple[int, int] | None:
    """Cheap fingerprint of the config file: (mtime_ns, size), or None.

    Size is in there because a same-second rewrite of the same length is what an
    editor that preserves mtime granularity produces, and losing that edit is
    worse than one extra reload.
    """
    if config.source_path is None:
        return None
    try:
        stat = config.source_path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _failure_reason(exc: Exception, timeout: float) -> str:
    """Name a poll failure in words an operator can act on.

    `asyncio.TimeoutError` carries an empty `str()`, so the obvious rendering
    yields nothing at all. This used to fall back to the printer's brand, which
    put `{"code": "offline", "message": "moonraker"}` on the hub's printer page —
    a word that names the adapter and says nothing about what went wrong. Say
    the budget instead: "the printer needed longer than we waited" is the one
    fact that distinguishes a slow machine from an absent one.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"no answer within {timeout:g}s"
    return str(exc) or exc.__class__.__name__


def _restart_required_changes(running: AgentConfig, incoming: AgentConfig) -> list[str]:
    """Settings a live agent cannot adopt, named so the log says which one.

    The session was opened with the hub URL, token and location, and the outbox
    file is already open: adopting any of them mid-flight would mean tearing
    down the very things that carry the change. The update channel is read once
    at service start. Printers, intervals and backoff are read every cycle and
    are applied without a restart.
    """
    changed: list[str] = []
    if running.hub_url != incoming.hub_url:
        changed.append("hub_url")
    if running.agent_token != incoming.agent_token:
        changed.append("agent_token")
    if running.location_key != incoming.location_key:
        changed.append("location_key")
    if running.outbox.database_path != incoming.outbox.database_path:
        changed.append("outbox.database_path")
    if running.updates != incoming.updates:
        changed.append("updates")
    return changed


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
        self._files = PrintFileService(
            PrintFileCache(
                config.print_files_directory(),
                max_age_h=config.print_files.max_age_h,
                max_total_mb=config.print_files.max_total_mb,
            ),
            config.agent_token,
        )
        self._camera = CameraService(config.agent_token)
        self._command_processor = CommandProcessor(outbox, self._files, self._camera)
        self._adapters = {printer.key: build_adapter(printer) for printer in config.printers}
        self._state_store = PrinterStateStore()
        self._connected_adapters: set[str] = set()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()
        self._heartbeat_deadline = 0.0
        self._inflight_events: dict[str, float] = {}
        self._config_stamp = _config_stamp(config)
        #: The roster as the hub last saw it, so a capability that only becomes
        #: true after the handshake is announced instead of waiting for one.
        self._roster_stamp = ""
        #: File transfers running outside the receive loop, kept referenced so
        #: the event loop cannot collect a task mid-download.
        self._transfers: set[asyncio.Task[None]] = set()
        #: Printers whose device was checked since they last came online. A
        #: printer is re-checked every time it reappears, because reappearing is
        #: exactly when DHCP may have put another machine at its address.
        self._verified: set[str] = set()
        #: printer_key -> why its address is answered by some other device.
        #: Such a printer reports offline and takes no commands.
        self._quarantined: dict[str, str] = {}
        #: printer_key -> loop time it was first seen offline in this stretch.
        self._offline_since: dict[str, float] = {}
        self._recovery_task: asyncio.Task[bool] | None = None
        self._recovery_last_at: float | None = None
        self._recovery_interval = float(config.recovery.min_interval_s)
        self._recovery_last_lost: frozenset[str] = frozenset()

    def stop(self) -> None:
        self._stop_event.set()

    def is_busy(self) -> bool:
        """True while stopping the agent would throw work away.

        Restarting does not disturb a print — the printer runs the job itself,
        and the outbox carries unacked events across — but it does destroy what
        the agent holds in memory: a file part-way to a printer, and a camera
        session someone is watching. Those are what an unattended update waits
        for.
        """
        return bool(self._transfers) or self._camera.has_sessions()

    async def run(self) -> None:
        poll_task = asyncio.create_task(self._poll_loop(), name="printer-agent-poll")
        try:
            await self._connection_loop()
        finally:
            poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await poll_task
            for transfer in list(self._transfers):
                transfer.cancel()
            if self._recovery_task is not None:
                self._recovery_task.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await self._recovery_task
            with suppress(Exception, asyncio.CancelledError):
                await self._camera.stop_all()
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
        if message_type == MessageType.error.value:
            # A per-message refusal that keeps the session open. The referenced
            # message stops being pending: the hub has answered about it, and
            # a refusal it will repeat forever — telemetry for a printer not
            # attached to this agent, say — would otherwise resend out of the
            # outbox for as long as the agent runs.
            refused_msg_id = str(message_payload.get("msg_id", ""))
            logger.warning(
                "hub refused a message",
                extra={
                    "action": "hub_error",
                    "code": str(message_payload.get("code", "")),
                    "error": str(message_payload.get("message", "")),
                    "ref_msg_id": refused_msg_id,
                },
            )
            if refused_msg_id:
                self.outbox.discard_event(refused_msg_id)
                self._inflight_events.pop(refused_msg_id, None)
            return
        if message_type == MessageType.inventory_request.value:
            await self._send_inventory(ws, str(payload.get("msg_id", "")))
            return
        if message_type == MessageType.log_request.value:
            await self._send_log(ws, str(payload.get("msg_id", "")), message_payload)
            return
        if message_type == MessageType.settings_request.value:
            await self._send_settings(ws, str(payload.get("msg_id", "")))
            return
        if message_type == MessageType.settings_update.value:
            await self._handle_settings_update(ws, message_payload)
            return
        if message_type == MessageType.update_request.value:
            await self._handle_update_request(ws, message_payload)
            return
        if message_type in COMMAND_BEARING_TYPES:
            await self._handle_command(ws, str(message_type), message_payload)
            return
        logger.info("ignored message", extra={"action": "hub_message", "message_type": str(message_type)})

    async def _handle_command(
        self, ws: aiohttp.ClientWebSocketResponse, message_type: str, command: dict[str, Any]
    ) -> None:
        """Answer every command-bearing hub message the same way.

        `command`, `file_offer`, `camera_request` and `camera_stop` differ only
        in what they ask an adapter to do: all four are answered with a
        `command_result` carrying their own `command_id`, and a message without
        one references nothing the hub could match an answer to, so it is dropped
        with a log line instead of being answered anonymously.
        """
        command_id = str(command.get("command_id", ""))
        printer_key = str(command.get("printer_key", ""))
        if not command_id:
            logger.warning(
                "hub message without command_id",
                extra={"action": "hub_command", "message_type": message_type},
            )
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
            await self._send(ws, build_envelope(MessageType.command_result.value, result))
            return

        refusal = self._quarantined.get(printer_key)
        if refusal and self.outbox.get_command_result(command_id) is None:
            # Checked after the stored-result lookup, so a replay of a command
            # that already ran still gets its real answer. Anything new is
            # refused outright: the device at this address is not the printer
            # the hub means, and a print started there is a print on the wrong
            # machine.
            logger.warning(
                "command refused: the printer's address answers as another device",
                extra={"action": "hub_command", "printer_key": printer_key, "command_id": command_id},
            )
            result = {
                "command_id": command_id,
                "printer_key": printer_key,
                "status": CommandStatus.failed.value,
                "error_text": refusal,
                "response": {},
            }
            self.outbox.record_command_result(command_id, printer_key, result["status"], refusal, {})
            await self._send(ws, build_envelope(MessageType.command_result.value, result))
            return

        if message_type == MessageType.file_offer.value:
            # Hundreds of megabytes over a shop-floor link take minutes. Running
            # that inline would stall the receive loop for the whole transfer —
            # no heartbeat, no acks, no second command — so it runs beside it and
            # answers when it is done.
            self._start_transfer(adapter, dict(command), command_id)
            return

        if message_type == MessageType.camera_request.value:
            result = await self._command_processor.dispatch_camera_request(adapter, command)
        elif message_type == MessageType.camera_stop.value:
            result = await self._command_processor.dispatch_camera_stop(adapter, command)
        else:
            result = await self._command_processor.dispatch(adapter, command)
        await self._send(ws, build_envelope(MessageType.command_result.value, result))

    def _start_transfer(self, adapter: PrinterAdapter, offer: dict[str, Any], command_id: str) -> None:
        task = asyncio.create_task(
            self._run_transfer(adapter, offer), name=f"printer-agent-file-offer-{command_id}"
        )
        self._transfers.add(task)
        task.add_done_callback(self._transfers.discard)

    async def _run_transfer(self, adapter: PrinterAdapter, offer: dict[str, Any]) -> None:
        try:
            result = await self._command_processor.dispatch_file_offer(adapter, offer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - the dispatcher maps its own failures
            logger.warning(
                "file transfer could not be dispatched",
                extra={"action": "file_offer", "error": str(exc) or exc.__class__.__name__},
            )
            return
        ws = self._ws
        if ws is None or ws.closed:
            # The result is already in the outbox: the hub redelivers the command
            # after the reconnect and gets this same answer without a second
            # download.
            logger.warning(
                "file transfer finished while the hub was unreachable",
                extra={
                    "action": "file_offer",
                    "command_id": str(result.get("command_id", "")),
                    "status": str(result.get("status", "")),
                },
            )
            return
        await self._send(ws, build_envelope(MessageType.command_result.value, result))

    async def _send_hello(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        payload = hello_payload(
            self.config,
            list(self._adapters.values()),
            self._agent_version(),
            self._update_status(),
        )
        await self._send(ws, build_envelope(MessageType.hello.value, payload))
        self._roster_stamp = roster_stamp(payload["printers"])
        self.state.last_hello_at = utc_now_iso()

    async def _send_inventory(self, ws: aiohttp.ClientWebSocketResponse, request_msg_id: str) -> None:
        payload = inventory_payload(
            self.config,
            list(self._adapters.values()),
            self._agent_version(),
            request_msg_id,
            self._update_status(),
        )
        # Which of the two cases this is has to be readable in the log. Both
        # send the same message, and reading an unsolicited announcement as an
        # answer to the hub is how an afternoon goes into looking for a request
        # the hub never made.
        logger.info(
            "answered the hub's roster request" if request_msg_id else "sent the printer roster unasked",
            extra={
                "action": "hub_inventory_request" if request_msg_id else "hub_inventory",
                "printers": str(len(payload["printers"])),
                "request_msg_id": request_msg_id,
            },
        )
        await self._send(ws, build_envelope(MessageType.inventory.value, payload))
        self._roster_stamp = roster_stamp(payload["printers"])

    async def _send_log(
        self, ws: aiohttp.ClientWebSocketResponse, request_msg_id: str, request: dict[str, Any]
    ) -> None:
        """Answer a `log_request` with a bounded, scrubbed tail.

        Reading the file happens in a worker thread: the live log is the one
        worth reading and also the largest, and the receive loop must not stop
        for it.
        """
        payload: dict[str, Any] = {
            "location_key": self.config.location_key,
            "agent_version": self._agent_version(),
        }
        if request_msg_id:
            payload["request_msg_id"] = request_msg_id
        try:
            payload.update(await asyncio.to_thread(self._read_log_tail, request))
        except UnknownLogFile as exc:
            payload["error"] = str(exc)
        except OSError as exc:
            payload["error"] = f"could not read the log: {exc}"
        logger.info(
            "sent a slice of the log",
            extra={
                "action": "hub_log",
                "file": str(payload.get("file", "")),
                "lines": str(len(payload.get("lines", []))),
                "error": str(payload.get("error", "")),
            },
        )
        await self._send(ws, build_envelope(MessageType.log.value, payload))

    def _read_log_tail(self, request: dict[str, Any]) -> dict[str, Any]:
        """Blocking half of :meth:`_send_log`."""
        live = active_log_path()
        folder = live.parent
        name = str(request.get("file") or "").strip() or live.name
        target = resolve_log_file(folder, name)

        try:
            wanted = int(request.get("lines") or 200)
        except (TypeError, ValueError):
            wanted = 200

        lines, truncated = tail_lines(target, wanted)
        lines = filter_by_level(lines, request.get("level"))
        lines = scrub(lines, self._log_secrets())
        lines, clipped = clip_to_budget(lines)
        return {
            "file": target.name,
            "files": [asdict(item) for item in list_log_files(folder)],
            "lines": lines,
            "truncated": truncated or clipped,
        }

    def _log_secrets(self) -> list[str]:
        """Every value that must never leave this machine in a log line.

        Collected from the running config rather than from a list of field
        names: the point is to catch a credential that reached the log through a
        field nobody thought to guard.
        """
        secrets = [self.config.agent_token]
        for printer in self.config.printers:
            secrets.extend(str(value) for value in (printer.credentials or {}).values())
        return secrets

    async def _send_settings(self, ws: aiohttp.ClientWebSocketResponse, request_msg_id: str) -> None:
        """Answer a `settings_request` with the config, secrets redacted."""
        config = self._config_on_disk()
        payload = {
            "location_key": self.config.location_key,
            "agent_version": self._agent_version(),
            "settings": redacted_settings(config),
            "readonly": readonly_settings(self.config),
        }
        if request_msg_id:
            payload["request_msg_id"] = request_msg_id
        logger.info(
            "sent the agent settings",
            extra={
                "action": "hub_settings",
                "printers": str(len(payload["settings"]["printers"])),
                "request_msg_id": request_msg_id,
            },
        )
        await self._send(ws, build_envelope(MessageType.settings.value, payload))

    def attach_updater(self, updater: Any) -> None:
        """Подключить апдейтер после создания соединения.

        Порядок вынужденный и не случайный: апдейтер берёт у соединения
        `is_busy`, поэтому создаётся вторым и в конструктор попасть не может.
        Соединение о его типе ничего не знает — ему нужны ровно два метода,
        `update_now()` и `known_status()`.
        """
        self._updater = updater

    def _update_status(self) -> dict[str, Any] | None:
        """Что известно про обновление — молча, если апдейтера нет вовсе."""
        updater = getattr(self, "_updater", None)
        if updater is None:
            return None
        try:
            return updater.known_status()
        except Exception:  # noqa: BLE001 — рукопожатие важнее сведений о версии
            logger.exception("could not read the update status")
            return None

    async def _run_update(self, target_version: str) -> dict[str, Any]:
        updater = getattr(self, "_updater", None)
        if updater is None:
            raise UnsupportedCommandError("this agent runs without an updater")
        return await updater.update_now(target_version=target_version)

    async def _handle_update_request(
        self, ws: aiohttp.ClientWebSocketResponse, payload: dict[str, Any]
    ) -> None:
        command_id = str(payload.get("command_id", ""))
        if not command_id:
            logger.warning(
                "hub message without command_id",
                extra={"action": "hub_command", "message_type": MessageType.update_request.value},
            )
            return
        result = await self._command_processor.dispatch_update(payload, self._run_update)
        await self._send(ws, build_envelope(MessageType.command_result.value, result))

    async def _handle_settings_update(
        self, ws: aiohttp.ClientWebSocketResponse, payload: dict[str, Any]
    ) -> None:
        command_id = str(payload.get("command_id", ""))
        if not command_id:
            logger.warning(
                "hub message without command_id",
                extra={"action": "hub_command", "message_type": MessageType.settings_update.value},
            )
            return
        result = await self._command_processor.dispatch_settings_update(
            payload, self._apply_remote_settings
        )
        await self._send(ws, build_envelope(MessageType.command_result.value, result))

    async def _apply_remote_settings(self, settings: Any) -> dict[str, Any]:
        """Write a hub change set into `agent.yaml`, or refuse without writing.

        Nothing here rebuilds an adapter or restarts anything: the file is the
        source of truth, and the poll loop's ordinary reload notices the change
        within one `telemetry_interval_s`, rebuilds what moved and announces the
        new roster. So the `command_result` says the change was accepted, and
        the `inventory` that follows says it took effect.
        """
        path = self.config.source_path
        if path is None:
            raise RuntimeError(
                "this agent has no config file to write; it is configured from the environment"
            )

        current = self._config_on_disk()
        try:
            merged, report = apply_remote_settings(settings, current)
        except BundleError as exc:
            raise RuntimeError(str(exc)) from exc

        errors = validate_config(self._with_session_identity(merged))
        if errors:
            # Nothing is written. A hub that can leave the file unloadable can
            # take a location off the air with one bad form submission, and the
            # agent would then be answering from a config nobody can see.
            raise RuntimeError("; ".join(errors))
        if not report.applied:
            refused = ", ".join(report.rejected)
            raise RuntimeError(
                f"nothing to apply: {refused} cannot be set from the hub"
                if refused
                else "nothing to apply: the change set is empty"
            )

        save_config(merged, path)
        logger.info(
            "applied settings from the hub",
            extra={
                "action": "hub_settings_update",
                "applied": ", ".join(report.applied),
                "rejected": ", ".join(report.rejected),
            },
        )
        return {
            "applied": report.applied,
            "kept_local": report.kept_local,
            "rejected": report.rejected,
            "missing": report.missing,
            # The fields a restart would be needed for are exactly the ones this
            # path refuses, so there is nothing left that needs one.
            "restart_required": False,
        }

    def _config_on_disk(self) -> AgentConfig:
        """The config as written, without environment overrides.

        `parse_config` would fold a temporary `HUB_URL` into what gets saved
        back — the same reason the settings transfer reads the file directly.
        """
        path = self.config.source_path
        return load_config_file(path) if path is not None else self.config

    def _with_session_identity(self, config: AgentConfig) -> AgentConfig:
        """`config` with the fields the hub cannot set taken from the running one.

        Validation has to judge the part the hub can affect. An agent configured
        entirely from the environment has no `hub_url` in its file, and checking
        the merged file as-is would refuse every remote change for a reason that
        has nothing to do with the change.
        """
        data = config_to_dict(config)
        data["hub_url"] = self.config.hub_url
        data["agent_token"] = self.config.agent_token
        data["location_key"] = self.config.location_key
        data["outbox"]["database_path"] = str(self.config.outbox.database_path)
        return config_from_dict(data)

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
                await self._reload_config_if_changed()
                snapshots = await self._collect_snapshots()
                self._maybe_start_recovery(snapshots)
                self._record_events(snapshots)
                await self._flush_outbox()
                await self._send_telemetry(snapshots)
                await self._announce_roster_changes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - integration path
                logger.warning("telemetry cycle failed", extra={"action": "telemetry", "error": str(exc)})
            await self._sleep_unless_stopped(self.config.telemetry_interval_s)

    async def _announce_roster_changes(self) -> None:
        """Re-send the roster when a capability changed under it.

        A camera is found by asking the printer, and that answer can arrive long
        after `hello` — or only once the operator closes Bambu Studio, which
        holds the camera port to itself. The flags ride along in every snapshot,
        but a hub that keyed its printer list on the handshake would keep showing
        a camera the agent has since learned to serve.
        """
        ws = self._ws
        if ws is None or ws.closed:
            return
        stamp = roster_stamp(printer_roster(list(self._adapters.values())))
        if stamp == self._roster_stamp:
            return
        logger.info("printer capabilities changed", extra={"action": "hub_inventory"})
        await self._send_inventory(ws, "")

    # -- config reload ---------------------------------------------------

    async def _reload_config_if_changed(self) -> None:
        """Pick up an edited printer list without restarting the agent.

        A printer added to `agent.yaml` used to reach the hub only after a
        service restart, because the roster is built once at startup — and an
        operator who added one and saw nothing had no way to tell that from a
        printer that was simply unreachable.
        """
        stamp = _config_stamp(self.config)
        if stamp is None or stamp == self._config_stamp:
            return
        # Stamped before parsing, so a file that cannot be read is reported once
        # rather than on every poll.
        self._config_stamp = stamp
        try:
            config, errors = parse_config(self.config.source_path)
        except Exception as exc:
            logger.warning("config reload failed", extra={"action": "config_reload", "error": str(exc)})
            return
        if errors:
            # The running config is known good; a half-edited file must not take
            # printers away from a working agent.
            logger.warning(
                "config on disk is not runnable; keeping the running one",
                extra={"action": "config_reload", "error": "; ".join(errors)},
            )
            return
        await self._apply_config(config)

    async def _apply_config(self, config: AgentConfig) -> None:
        previous = {printer.key: printer for printer in self.config.printers}
        incoming = {printer.key: printer for printer in config.printers}
        removed = [key for key in previous if key not in incoming]
        added = [key for key in incoming if key not in previous]
        # A learned `device_id` is written into the file by the agent itself;
        # that must not tear down a working connection to adopt it.
        rebuilt = [
            key
            for key, printer in incoming.items()
            if key in previous and replace(previous[key], device_id="") != replace(printer, device_id="")
        ]

        for name in _restart_required_changes(self.config, config):
            logger.warning(
                "config change needs an agent restart to take effect",
                extra={"action": "config_reload", "error": name},
            )

        for key in removed + rebuilt:
            adapter = self._adapters.pop(key, None)
            self._connected_adapters.discard(key)
            self._state_store.forget(key)
            self._verified.discard(key)
            self._quarantined.pop(key, None)
            self._offline_since.pop(key, None)
            # The frame loop holds the old adapter; leaving it running would keep
            # filming through a connection nothing else uses any more.
            with suppress(Exception):
                await self._camera.stop(key, "")
            if adapter is not None:
                with suppress(Exception):
                    await adapter.disconnect()
        for key in added + rebuilt:
            self._adapters[key] = build_adapter(incoming[key])
        for key, printer in incoming.items():
            if key in previous and previous[key].device_id != printer.device_id:
                # Someone changed or cleared it by hand: check the device, or
                # learn it again, on the next poll rather than the next outage.
                self._verified.discard(key)
                self._quarantined.pop(key, None)

        # Read fresh every cycle, so assigning them is all it takes.
        self.config.printers = list(config.printers)
        self.config.telemetry_interval_s = config.telemetry_interval_s
        self.config.heartbeat_interval_s = config.heartbeat_interval_s
        self.config.command_reconnect_backoff_s = config.command_reconnect_backoff_s
        self.config.outbox.max_events = config.outbox.max_events
        self.config.recovery = config.recovery
        if not (removed or added or rebuilt):
            return
        logger.info(
            "printer roster reloaded",
            extra={
                "action": "config_reload",
                "printers": str(len(self._adapters)),
                "added": ",".join(added),
                "removed": ",".join(removed),
                "rebuilt": ",".join(rebuilt),
            },
        )
        ws = self._ws
        if ws is not None and not ws.closed:
            # The hub keyed its list on the last roster it saw; telling it now is
            # the whole point of noticing the edit.
            await self._send_inventory(ws, "")

    async def _collect_snapshots(self) -> list[PrinterSnapshot]:
        if not self._adapters:
            return []
        timeout = max(PRINTER_POLL_TIMEOUT_S, self.config.telemetry_interval_s)
        results = await asyncio.gather(
            *(self._poll_adapter(key, adapter, timeout) for key, adapter in self._adapters.items())
        )
        return list(results)

    async def _poll_adapter(self, key: str, adapter: PrinterAdapter, timeout: float) -> PrinterSnapshot:
        await self._ensure_adapter_connected(key, adapter, timeout)
        try:
            snapshot = await asyncio.wait_for(adapter.get_state(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected_adapters.discard(key)
            reason = _failure_reason(exc, timeout)
            logger.warning(
                "printer poll failed", extra={"action": "poll", "printer_key": key, "error": reason}
            )
            return self._offline_snapshot(adapter, reason)
        return await self._check_identity(key, adapter, snapshot, timeout)

    async def _check_identity(
        self, key: str, adapter: PrinterAdapter, snapshot: PrinterSnapshot, timeout: float
    ) -> PrinterSnapshot:
        """Make sure the device answering is the printer this key means.

        After DHCP reshuffles a location, the address in the config can belong
        to a neighbour that speaks the same protocol. Its telemetry would arrive
        under the wrong key, and the hub's next print would start on it. So a
        printer is checked each time it (re)appears; a printer with no identity
        yet has the one it answers with recorded, and one that answers with a
        different identity is reported offline until recovery moves it.
        """
        if snapshot.status == PrinterStatus.offline:
            self._verified.discard(key)
            return snapshot
        if key in self._verified:
            return snapshot
        try:
            actual = await asyncio.wait_for(adapter.device_ids(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Unchecked this cycle, not trusted: the next poll asks again.
            logger.debug(
                "printer identity unavailable",
                extra={"action": "identity", "printer_key": key, "error": str(exc)},
            )
            return snapshot
        printer = self._printer_config(key) or adapter.printer
        expected = printer.identity()
        if not actual:
            self._verified.add(key)
            return snapshot
        if not expected:
            self._learn_identity(printer, min(actual))
            self._verified.add(key)
            return snapshot
        if expected in actual:
            self._verified.add(key)
            if self._quarantined.pop(key, None):
                logger.info(
                    "printer identity confirmed again",
                    extra={"action": "identity", "printer_key": key, "host": printer.host},
                )
            return snapshot

        reason = (
            f"{printer.host} now answers as {min(actual)}, not as this printer ({expected}); "
            "its address was probably reassigned by DHCP"
        )
        if key not in self._quarantined:
            logger.warning(
                "printer address answers as another device",
                extra={"action": "identity", "printer_key": key, "host": printer.host, "error": reason},
            )
        self._quarantined[key] = reason
        return self._offline_snapshot(adapter, reason, code=IDENTITY_MISMATCH)

    def _printer_config(self, key: str) -> PrinterConfig | None:
        return next((printer for printer in self.config.printers if printer.key == key), None)

    def _learn_identity(self, printer: PrinterConfig, device_id: str) -> None:
        """Record the identity a printer answered with, so it can be found again.

        Written to the file because it has to outlive the process: the moment it
        is needed is after a site-wide power cut, when the agent restarts and
        every address in the config is stale.
        """
        printer.device_id = device_id
        path = self.config.source_path
        if path is None or not path.exists():
            return
        try:
            write_printer_fields(path, {printer.key: {"device_id": device_id}})
        except Exception as exc:
            logger.warning(
                "could not record printer identity",
                extra={"action": "identity", "printer_key": printer.key, "error": str(exc)},
            )
            return
        logger.info(
            "recorded printer identity",
            extra={"action": "identity", "printer_key": printer.key, "device_id": device_id},
        )

    # -- recovery after readdressing -------------------------------------

    def _maybe_start_recovery(self, snapshots: list[PrinterSnapshot]) -> None:
        """Start a search for lost printers when one is due.

        Lost means: unreachable for `recovery.after_offline_s`, or answered by
        another device at its address. Only a printer with a known identity can
        be looked for. The scan runs beside the poll loop, which keeps
        reporting while it takes its tens of seconds.
        """
        now = asyncio.get_running_loop().time()
        for snapshot in snapshots:
            if snapshot.status == PrinterStatus.offline:
                self._offline_since.setdefault(snapshot.printer_key, now)
            else:
                self._offline_since.pop(snapshot.printer_key, None)

        recovery = self.config.recovery
        if not recovery.enabled:
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        lost = [
            printer
            for printer in self.config.printers
            if printer.identity()
            and (
                printer.key in self._quarantined
                or now - self._offline_since.get(printer.key, now) >= recovery.after_offline_s
            )
        ]
        if not lost:
            self._recovery_interval = float(recovery.min_interval_s)
            return
        keys = frozenset(printer.key for printer in lost)
        # A newly lost printer is worth a prompt look even while an older one,
        # probably switched off, has pushed the interval up.
        interval = (
            float(recovery.min_interval_s) if keys - self._recovery_last_lost else self._recovery_interval
        )
        if self._recovery_last_at is not None and now - self._recovery_last_at < interval:
            return
        self._recovery_last_at = now
        self._recovery_last_lost = keys
        self._recovery_task = asyncio.create_task(self._recover(lost), name="printer-agent-recovery")
        self._recovery_task.add_done_callback(self._recovery_done)

    def _recovery_done(self, task: asyncio.Task[bool]) -> None:
        moved = False
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("printer search failed", extra={"action": "recovery", "error": str(exc)})
            else:
                moved = bool(task.result())
        floor = float(self.config.recovery.min_interval_s)
        ceiling = max(RECOVERY_MAX_INTERVAL_S, floor)
        self._recovery_interval = floor if moved else min(max(self._recovery_interval * 2, floor), ceiling)

    async def _recover(self, lost: list[PrinterConfig]) -> bool:
        """Look for lost printers by identity and move the ones found.

        The move is a write to `agent.yaml`, not a change to a live adapter: the
        poll loop's reload rebuilds what changed and tells the hub, the same way
        an operator's edit would, and the new address survives a restart.
        """
        path = self.config.source_path
        if path is None or not path.exists():
            logger.warning(
                "cannot follow printers to new addresses without a config file",
                extra={"action": "recovery"},
            )
            return False
        local = await asyncio.to_thread(local_ipv4_networks)
        hosts = hosts_for(scan_networks(self.config.printers, self.config.recovery.networks, local))
        logger.info(
            "looking for printers at new addresses",
            extra={
                "action": "recovery",
                "printers": ",".join(printer.key for printer in lost),
                "hosts": str(len(hosts)),
            },
        )
        records = await find_printers(lost, hosts)
        # Planned against the roster as it is *now*: an edit may have landed
        # during the scan.
        plan = plan_relocation(self.config.printers, {printer.key for printer in lost}, records)
        for key in plan.not_found:
            logger.warning(
                "printer not found on the network",
                extra={"action": "recovery", "printer_key": key},
            )
        for reason in plan.refused:
            logger.warning("printer address left unchanged", extra={"action": "recovery", "error": reason})
        if not plan.moves:
            return False
        old_hosts = {printer.key: printer.host for printer in self.config.printers}
        changed = write_printer_fields(path, {key: {"host": host} for key, host in plan.moves.items()})
        for key in changed:
            logger.info(
                "printer found at a new address",
                extra={
                    "action": "recovery",
                    "printer_key": key,
                    "old_host": old_hosts.get(key, ""),
                    "host": plan.moves[key],
                },
            )
        return bool(changed)

    async def _ensure_adapter_connected(
        self, key: str, adapter: PrinterAdapter, timeout: float = PRINTER_POLL_TIMEOUT_S
    ) -> None:
        if key in self._connected_adapters:
            return
        try:
            # Bounded like the poll itself: this runs before it, so an unbounded
            # connect to a host that drops packets would hold up the whole
            # gathered cycle — every other printer's telemetry with it.
            await asyncio.wait_for(adapter.connect(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "printer connect failed",
                extra={"action": "poll", "printer_key": key, "error": _failure_reason(exc, timeout)},
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

    def _offline_snapshot(
        self, adapter: PrinterAdapter, reason: str, code: str = "offline"
    ) -> PrinterSnapshot:
        return PrinterSnapshot(
            printer_key=adapter.printer_key,
            status=PrinterStatus.offline,
            status_raw="offline",
            job=JobSnapshot(),
            temps=TemperatureSnapshot(),
            error=ErrorSnapshot(code=code, message=reason),
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
