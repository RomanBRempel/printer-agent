from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.base import PrinterAdapter, UnsupportedCommandError
from ..core.outbox import EventOutbox
from ..contracts import CommandStatus


class CommandProcessor:
    def __init__(self, outbox: EventOutbox):
        self._outbox = outbox

    async def dispatch(self, adapter: PrinterAdapter, command: dict[str, Any]) -> dict[str, Any]:
        command_id = str(command["command_id"])
        printer_key = str(command["printer_key"])
        existing = self._outbox.get_command_result(command_id)
        if existing is not None:
            return existing

        action = str(command["action"])
        args = command.get("args") or {}
        status = CommandStatus.done.value
        error_text = ""
        response: dict[str, Any] = {}

        try:
            if action == "start_print":
                response = await adapter.start_print(str(args["file_ref"]))
            elif action == "pause":
                response = await adapter.pause()
            elif action == "resume":
                response = await adapter.resume()
            elif action == "cancel":
                response = await adapter.cancel()
            elif action == "upload_file":
                response = await adapter.upload_file(Path(args["local_path"]), str(args["remote_name"]))
            else:
                raise UnsupportedCommandError(f"unknown action {action}")
        except UnsupportedCommandError as exc:
            status = CommandStatus.unsupported.value
            error_text = str(exc)
        except TimeoutError as exc:
            status = CommandStatus.timeout.value
            error_text = str(exc)
        except Exception as exc:  # pragma: no cover - safety net for integration paths
            status = CommandStatus.failed.value
            error_text = str(exc)

        payload = {
            "command_id": command_id,
            "printer_key": printer_key,
            "status": status,
            "error_text": error_text,
            "response": response,
        }
        self._outbox.record_command_result(command_id, printer_key, status, error_text, response)
        return payload
