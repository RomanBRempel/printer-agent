from __future__ import annotations

from printer_agent.adapters.bambu import normalize_bambu_status
from printer_agent.adapters.moonraker import normalize_moonraker_status
from printer_agent.contracts import ErrorSnapshot, JobSnapshot, PrinterSnapshot, PrinterStatus
from printer_agent.core.state import PrinterStateStore


def test_status_normalization_tables() -> None:
    assert normalize_moonraker_status("printing") == PrinterStatus.printing
    assert normalize_moonraker_status("shutdown") == PrinterStatus.maintenance
    assert normalize_bambu_status("pause") == PrinterStatus.paused
    assert normalize_bambu_status("unknown") == PrinterStatus.maintenance


def test_state_store_detects_changes() -> None:
    store = PrinterStateStore()
    first = PrinterSnapshot(printer_key="printer-1", status=PrinterStatus.idle, status_raw="idle")
    change = store.update(first)
    assert change is not None
    assert change.kinds == ("snapshot",)

    second = PrinterSnapshot(
        printer_key="printer-1",
        status=PrinterStatus.printing,
        status_raw="printing",
        job=JobSnapshot(name="benchy", progress_pct=10.0),
        error=ErrorSnapshot(code="E1", message="warmup"),
    )
    change = store.update(second)
    assert change is not None
    assert "status_changed" in change.kinds
    assert "job_changed" in change.kinds
    assert "error_changed" in change.kinds
