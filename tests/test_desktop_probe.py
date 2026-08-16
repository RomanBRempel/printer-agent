"""Adapter lifecycle in the desktop app's background poller.

The probe builds an adapter, connects it, and only then files it in the dict it
keeps between cycles. A `connect()` that times out never reaches the filing, so
the frame holds the only reference to an adapter that is already carrying an
open aiohttp session — and dropping it logged `Unclosed client session` and
stranded a socket, once per cycle, for as long as the printer stayed
unreachable. On a shop floor with one printer at a wrong address that is every
few seconds, all day.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("PySide6")

from printer_agent.adapters.base import PrinterAdapter
from printer_agent.config import PrinterConfig
from printer_agent.contracts import PrinterCapabilities, PrinterSnapshot, PrinterStatus
from printer_agent.desktop.probe import PrinterProbe


class HangingAdapter(PrinterAdapter):
    """Never finishes connecting, and remembers whether it was closed."""

    def __init__(self, printer: PrinterConfig, *, hang_on: str = "connect"):
        super().__init__(printer)
        self.hang_on = hang_on
        self.disconnected = 0

    async def connect(self) -> None:
        if self.hang_on == "connect":
            await asyncio.sleep(3600)

    async def disconnect(self) -> None:
        self.disconnected += 1

    async def get_state(self) -> PrinterSnapshot:
        if self.hang_on == "get_state":
            await asyncio.sleep(3600)
        return PrinterSnapshot(
            printer_key=self.printer.key, status=PrinterStatus.idle, status_raw="idle"
        )

    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()


@pytest.fixture()
def printer() -> PrinterConfig:
    return PrinterConfig(key="k1-0942", brand="moonraker", host="10.13.0.126", port=7125)


@pytest.fixture(autouse=True)
def instant_timeout(monkeypatch):
    """Keep the test off the real 12 s budget."""
    monkeypatch.setattr("printer_agent.desktop.probe.PROBE_TIMEOUT_S", 0.01)


@pytest.mark.asyncio
async def test_an_adapter_whose_connect_timed_out_is_closed(printer, monkeypatch):
    built: list[HangingAdapter] = []

    def fake_build(config: PrinterConfig) -> PrinterAdapter:
        adapter = HangingAdapter(config)
        built.append(adapter)
        return adapter

    monkeypatch.setattr("printer_agent.desktop.probe.build_adapter", fake_build)

    probe = PrinterProbe([printer])
    adapters: dict[str, PrinterAdapter] = {}
    result = await probe._probe(printer, adapters)

    assert result.error == "Таймаут опроса принтера"
    # Nothing else holds it, so the probe has to be the one that closes it.
    assert adapters == {}
    assert built[0].disconnected == 1


@pytest.mark.asyncio
async def test_a_filed_adapter_keeps_its_connection_when_the_state_query_times_out(
    printer, monkeypatch
):
    """Only the query timed out; the session is still good for the next cycle."""
    adapter = HangingAdapter(printer, hang_on="get_state")
    monkeypatch.setattr("printer_agent.desktop.probe.build_adapter", lambda config: adapter)

    probe = PrinterProbe([printer])
    adapters: dict[str, PrinterAdapter] = {}
    result = await probe._probe(printer, adapters)

    assert result.error == "Таймаут опроса принтера"
    assert adapters == {printer.key: adapter}
    assert adapter.disconnected == 0


@pytest.mark.asyncio
async def test_a_refused_adapter_is_closed_and_forgotten(printer, monkeypatch):
    class RefusingAdapter(HangingAdapter):
        async def connect(self) -> None:
            raise ConnectionRefusedError("connection refused")

    adapter = RefusingAdapter(printer)
    monkeypatch.setattr("printer_agent.desktop.probe.build_adapter", lambda config: adapter)

    probe = PrinterProbe([printer])
    adapters: dict[str, PrinterAdapter] = {}
    result = await probe._probe(printer, adapters)

    assert "refused" in result.error
    assert adapters == {}
    assert adapter.disconnected == 1
