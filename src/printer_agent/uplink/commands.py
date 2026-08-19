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

    async def dispatch_settings_update(
        self,
        payload: dict[str, Any],
        apply: Callable[[Any], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """A command about the agent rather than about one printer.

        It goes through :meth:`_run` like every other command, for the one
        reason that matters here: the stored result is consulted first, so a
        `settings_update` redelivered because the socket dropped while its
        answer was in flight does not get applied twice. `printer_key` is empty
        because there is no printer in it.
        """
        command_id = str(payload["command_id"])

        async def run() -> dict[str, Any]:
            return await apply(payload.get("settings"))

        return await self._run(command_id, "", "settings_update", run)

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
        # Which loaded slot each filament of the program goes to. The hub has
        # already matched the program against the slots this printer reports, so
        # dropping the answer here would hand the choice back to the printer —
        # which knows less about the program than the hub does. Anything that is
        # not a list of slot numbers is ignored rather than passed on: a bad
        # mapping prints in the wrong material, which is scrap, not a warning.
        ams_mapping = _ams_mapping(args.get("ams_mapping"))
        if not file_ref:
            raise RuntimeError("start_print without file_ref")
        if self._files is not None and not self._files.has_cached(file_ref):
            # The agent never goes looking for the file on its own: it has no
            # URL for it, and guessing one is not its business. The hub knows
            # this outcome and offers the file again.
            raise RuntimeError(
                f"print file {file_ref} is not in this agent's cache; send the file again"
            )
        return await adapter.start_print(
            file_ref,
            remote_name,
            ams_mapping=ams_mapping,
            # The agent's own copy, when the cache still holds it. Some protocols
            # need what is inside the file and the printer cannot be asked.
            local_path=self._files.cached_path(file_ref) if self._files is not None else None,
        )

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


def _ams_mapping(value: Any) -> dict[int, int] | None:
    """The hub's slot mapping as `{filament index: slot index}`.

    Two shapes are accepted, and both mean the same thing. `[{"filament": 0,
    "slot": 1}]` is what the hub sends and is the one to write new code against:
    it can leave a filament out, which a program with an unnamed material
    genuinely does. `[1, 0]` is the positional form the contract carried first —
    still read, because an older hub is still allowed to send it.

    Anything unreadable returns `None` **and says so in the log**. The previous
    version dropped a mapping it could not parse without a word, and the print
    went out as `use_ams: false` — a two-colour plate came off the bed in one
    colour, hours later, reported to the hub as done.
    """
    if value in (None, [], {}):
        return None
    mapping: dict[int, int] = {}
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        for item in value:
            try:
                mapping[int(item["filament"])] = int(item["slot"])
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "unreadable ams_mapping entry; the feeder will not be used",
                    extra={"action": "command", "error": repr(item)},
                )
                return None
        return mapping or None
    if isinstance(value, list):
        for index, slot in enumerate(value):
            if isinstance(slot, bool) or not isinstance(slot, (int, str)):
                logger.warning(
                    "unreadable ams_mapping entry; the feeder will not be used",
                    extra={"action": "command", "error": repr(slot)},
                )
                return None
            try:
                mapping[index] = int(slot)
            except ValueError:
                logger.warning(
                    "unreadable ams_mapping entry; the feeder will not be used",
                    extra={"action": "command", "error": repr(slot)},
                )
                return None
        return mapping or None
    logger.warning(
        "ams_mapping is not a list; the feeder will not be used",
        extra={"action": "command", "error": repr(value)},
    )
    return None
