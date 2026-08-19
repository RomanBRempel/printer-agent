from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

from ..adapters.base import PrinterAdapter
from ..config import (
    AgentConfig,
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


def hello_payload(config: AgentConfig, adapters: list[PrinterAdapter], agent_version: str) -> dict[str, Any]:
    """The handshake payload, shared with the connectivity check."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "agent_version": agent_version,
        "location_key": config.location_key,
        "printers": printer_roster(adapters),
    }


def inventory_payload(
    config: AgentConfig, adapters: list[PrinterAdapter], agent_version: str, request_msg_id: str = ""
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
        payload = hello_payload(self.config, list(self._adapters.values()), self._agent_version())
        await self._send(ws, build_envelope(MessageType.hello.value, payload))
        self._roster_stamp = roster_stamp(payload["printers"])
        self.state.last_hello_at = utc_now_iso()

    async def _send_inventory(self, ws: aiohttp.ClientWebSocketResponse, request_msg_id: str) -> None:
        payload = inventory_payload(
            self.config, list(self._adapters.values()), self._agent_version(), request_msg_id
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
        rebuilt = [key for key, printer in incoming.items() if key in previous and previous[key] != printer]

        for name in _restart_required_changes(self.config, config):
            logger.warning(
                "config change needs an agent restart to take effect",
                extra={"action": "config_reload", "error": name},
            )

        for key in removed + rebuilt:
            adapter = self._adapters.pop(key, None)
            self._connected_adapters.discard(key)
            self._state_store.forget(key)
            # The frame loop holds the old adapter; leaving it running would keep
            # filming through a connection nothing else uses any more.
            with suppress(Exception):
                await self._camera.stop(key, "")
            if adapter is not None:
                with suppress(Exception):
                    await adapter.disconnect()
        for key in added + rebuilt:
            self._adapters[key] = build_adapter(incoming[key])

        # Read fresh every cycle, so assigning them is all it takes.
        self.config.printers = list(config.printers)
        self.config.telemetry_interval_s = config.telemetry_interval_s
        self.config.heartbeat_interval_s = config.heartbeat_interval_s
        self.config.command_reconnect_backoff_s = config.command_reconnect_backoff_s
        self.config.outbox.max_events = config.outbox.max_events
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
            return await asyncio.wait_for(adapter.get_state(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected_adapters.discard(key)
            reason = _failure_reason(exc, timeout)
            logger.warning(
                "printer poll failed", extra={"action": "poll", "printer_key": key, "error": reason}
            )
            return self._offline_snapshot(adapter, reason)

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

    def _offline_snapshot(self, adapter: PrinterAdapter, reason: str) -> PrinterSnapshot:
        return PrinterSnapshot(
            printer_key=adapter.printer_key,
            status=PrinterStatus.offline,
            status_raw="offline",
            job=JobSnapshot(),
            temps=TemperatureSnapshot(),
            error=ErrorSnapshot(code="offline", message=reason),
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
