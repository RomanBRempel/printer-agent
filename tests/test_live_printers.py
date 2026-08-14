from __future__ import annotations

import os
from pathlib import Path

import pytest

from printer_agent.config import load_config
from printer_agent.core.registry import build_adapter
from printer_agent.contracts import PrinterStatus


LIVE_TESTS_ENABLED = os.getenv("PRINTER_AGENT_LIVE_TESTS", "0") == "1"
LIVE_CONFIG_PATH = Path(os.getenv("PRINTER_AGENT_LIVE_CONFIG", "agent.yaml"))


pytestmark = pytest.mark.live

if not LIVE_TESTS_ENABLED:
    pytest.skip("live printer tests are disabled; set PRINTER_AGENT_LIVE_TESTS=1 to enable", allow_module_level=True)


@pytest.mark.asyncio
async def test_real_printers_connect_and_report_state() -> None:
    config = load_config(LIVE_CONFIG_PATH)
    adapters = [build_adapter(printer) for printer in config.printers]

    try:
        for adapter in adapters:
            await adapter.connect()
            snapshot = await adapter.get_state()
            assert snapshot.printer_key == adapter.printer_key
            assert snapshot.status != PrinterStatus.offline
            assert snapshot.status_raw
    finally:
        for adapter in reversed(adapters):
            await adapter.disconnect()
