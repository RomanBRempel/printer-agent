from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..config import PrinterConfig
from ..contracts import PrinterCapabilities, PrinterSnapshot


class UnsupportedCommandError(RuntimeError):
    pass


class PrinterAdapter(ABC):
    def __init__(self, printer: PrinterConfig):
        self.printer = printer

    @property
    def printer_key(self) -> str:
        return self.printer.key

    @abstractmethod
    async def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_state(self) -> PrinterSnapshot:
        raise NotImplementedError

    def capabilities(self) -> PrinterCapabilities:
        raise NotImplementedError

    async def start_print(self, file_ref: str, remote_name: str | None = None) -> dict[str, Any]:
        """Print a file already delivered to this printer.

        ``file_ref`` names the file in the agent's own cache; ``remote_name`` is
        what it was stored as on the printer. Adapters that address a print by
        the printer-side name need the second one, and the hub sends both rather
        than making every adapter reconstruct one from the other.
        """
        raise UnsupportedCommandError(f"start_print is not supported for {self.printer.brand}")

    async def pause(self) -> dict[str, Any]:
        raise UnsupportedCommandError(f"pause is not supported for {self.printer.brand}")

    async def resume(self) -> dict[str, Any]:
        raise UnsupportedCommandError(f"resume is not supported for {self.printer.brand}")

    async def cancel(self) -> dict[str, Any]:
        raise UnsupportedCommandError(f"cancel is not supported for {self.printer.brand}")

    async def upload_file(self, local_path: str | Path, remote_name: str) -> dict[str, Any]:
        raise UnsupportedCommandError(f"upload_file is not supported for {self.printer.brand}")

    async def get_camera_frame(self) -> bytes:
        raise UnsupportedCommandError(f"get_camera_frame is not supported for {self.printer.brand}")

    async def open_camera_stream(self) -> None:
        raise UnsupportedCommandError(f"open_camera_stream is not supported for {self.printer.brand}")

    async def close_camera_stream(self) -> None:
        raise UnsupportedCommandError(f"close_camera_stream is not supported for {self.printer.brand}")
