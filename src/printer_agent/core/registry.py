from __future__ import annotations

from ..adapters.base import PrinterAdapter
from ..adapters.bambu import BambuAdapter
from ..adapters.creality import CrealityAdapter
from ..adapters.moonraker import MoonrakerAdapter
from ..config import PrinterConfig

ADAPTERS: dict[str, type[PrinterAdapter]] = {
    "moonraker": MoonrakerAdapter,
    "bambu": BambuAdapter,
    "creality": CrealityAdapter,
}


def build_adapter(printer: PrinterConfig) -> PrinterAdapter:
    try:
        adapter_type = ADAPTERS[printer.brand.lower()]
    except KeyError as exc:
        raise KeyError(f"unsupported printer brand: {printer.brand}") from exc
    return adapter_type(printer)
