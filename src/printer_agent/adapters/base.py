from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Mapping
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

    async def start_print(
        self,
        file_ref: str,
        remote_name: str | None = None,
        ams_mapping: Mapping[int, int] | None = None,
        local_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Print a file already delivered to this printer.

        ``file_ref`` names the file in the agent's own cache; ``remote_name`` is
        what it was stored as on the printer. Adapters that address a print by
        the printer-side name need the second one, and the hub sends both rather
        than making every adapter reconstruct one from the other.

        ``local_path`` is the agent's own copy, when it still has one. A printer
        addressed only by a name cannot be asked what is inside the file, and
        for some protocols the command depends on that — a Bambu project has to
        name the plate to run, and the plate is a fact about the archive. Most
        adapters ignore it; none may require it, because the copy is a cache and
        the cache is prunable.

        ``ams_mapping`` is which loaded slot each filament of the program goes
        to. The hub works it out when it matches the program against the slots
        the printer reports, and passing it on is the point of having matched:
        dropping it would leave the choice to a printer that knows less about
        the program than the hub does. Adapters whose printers have no feeding
        system ignore it — an optional argument, not a contract change.
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
