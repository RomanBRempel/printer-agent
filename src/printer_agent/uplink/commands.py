from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..adapters.base import PrinterAdapter, UnsupportedCommandError
from ..contracts import CommandAction, CommandStatus
from ..core.outbox import EventOutbox
from .camera import CameraService
from .files import PrintFileService

logger = logging.getLogger(__name__)


class CommandProcessor:
    """Turns a hub message into a `command_result`, exactly once per command_id.

    Every hub message that carries a `command_id` comes through here — `command`,
    `file_offer`, `camera_request`, `camera_stop` — so idempotency and the
    exception-to-status mapping are written once. The stored result is consulted
    *before* anything runs: a command redelivered because the socket dropped
    while the result was in flight must not print a second part.
    """

    def __init__(
        self,
        outbox: EventOutbox,
        files: PrintFileService | None = None,
        camera: CameraService | None = None,
    ):
        self._outbox = outbox
        self._files = files
        self._camera = camera

    async def dispatch(self, adapter: PrinterAdapter, command: dict[str, Any]) -> dict[str, Any]:
        command_id = str(command["command_id"])
        printer_key = str(command["printer_key"])
        action = str(command["action"])
        args = command.get("args") or {}

        async def run() -> dict[str, Any]:
            if action == CommandAction.start_print.value:
                return await self._start_print(adapter, args)
            if action == CommandAction.pause.value:
                return await adapter.pause()
            if action == CommandAction.resume.value:
                return await adapter.resume()
            if action == CommandAction.cancel.value:
                return await adapter.cancel()
            if action == CommandAction.upload_file.value:
                return await adapter.upload_file(Path(args["local_path"]), str(args["remote_name"]))
            raise UnsupportedCommandError(f"unknown action {action}")

        return await self._run(command_id, printer_key, action, run)

    async def dispatch_file_offer(
        self, adapter: PrinterAdapter, offer: dict[str, Any]
    ) -> dict[str, Any]:
        command_id = str(offer["command_id"])
        printer_key = str(offer["printer_key"])

        async def run() -> dict[str, Any]:
            if self._files is None:
                raise UnsupportedCommandError("this agent has no print file cache configured")
            return await self._files.accept(adapter, offer)

        return await self._run(command_id, printer_key, "file_offer", run)

    async def dispatch_camera_request(
        self, adapter: PrinterAdapter, payload: dict[str, Any]
    ) -> dict[str, Any]:
        command_id = str(payload["command_id"])
        printer_key = str(payload["printer_key"])

        async def run() -> dict[str, Any]:
            if self._camera is None:
                raise UnsupportedCommandError("this agent cannot serve camera frames")
            return await self._camera.start(adapter, payload)

        return await self._run(command_id, printer_key, "camera_request", run)

    async def dispatch_camera_stop(
        self, adapter: PrinterAdapter, payload: dict[str, Any]
    ) -> dict[str, Any]:
        command_id = str(payload["command_id"])
        printer_key = str(payload["printer_key"])

        async def run() -> dict[str, Any]:
            if self._camera is None:
                raise UnsupportedCommandError("this agent cannot serve camera frames")
            return await self._camera.stop(printer_key, str(payload.get("session_id", "")).strip())

        return await self._run(command_id, printer_key, "camera_stop", run)

    # -- shared -------------------------------------------------------

    async def _start_print(self, adapter: PrinterAdapter, args: dict[str, Any]) -> dict[str, Any]:
        file_ref = str(args.get("file_ref", "")).strip()
        remote_name = str(args.get("remote_name", "")).strip() or None
        if not file_ref:
            raise RuntimeError("start_print without file_ref")
        if self._files is not None and not self._files.has_cached(file_ref):
            # The agent never goes looking for the file on its own: it has no
            # URL for it, and guessing one is not its business. The hub knows
            # this outcome and offers the file again.
            raise RuntimeError(
                f"print file {file_ref} is not in this agent's cache; send the file again"
            )
        return await adapter.start_print(file_ref, remote_name)

    async def _run(
        self,
        command_id: str,
        printer_key: str,
        action: str,
        run: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        existing = self._outbox.get_command_result(command_id)
        if existing is not None:
            logger.info(
                "replayed command answered from the stored result",
                extra={"action": "command", "command_id": command_id, "printer_key": printer_key},
            )
            return existing

        status = CommandStatus.done.value
        error_text = ""
        response: dict[str, Any] = {}
        try:
            response = await run() or {}
        except UnsupportedCommandError as exc:
            status = CommandStatus.unsupported.value
            error_text = str(exc)
        except TimeoutError as exc:
            status = CommandStatus.timeout.value
            error_text = str(exc) or "timed out"
        except Exception as exc:  # pragma: no cover - safety net for integration paths
            status = CommandStatus.failed.value
            error_text = str(exc) or exc.__class__.__name__

        if status != CommandStatus.done.value:
            logger.warning(
                "command did not succeed",
                extra={
                    "action": "command",
                    "command_id": command_id,
                    "printer_key": printer_key,
                    "command_action": action,
                    "status": status,
                    "error": error_text,
                },
            )

        payload = {
            "command_id": command_id,
            "printer_key": printer_key,
            "status": status,
            "error_text": error_text,
            "response": response,
        }
        self._outbox.record_command_result(command_id, printer_key, status, error_text, response)
        return payload
