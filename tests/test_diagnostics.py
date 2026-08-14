"""Connectivity checks. Only the offline stages and a refused local port."""

from __future__ import annotations

import pytest

from printer_agent.config import AgentConfig, BackoffConfig, PrinterConfig
from printer_agent.uplink.connection import hub_wss_url
from printer_agent.uplink.diagnostics import check_hub, check_printer


def make_config(**overrides) -> AgentConfig:
    values = {
        "hub_url": "https://hub.example.com/api/printers/agent",
        "agent_token": "token",
        "location_key": "loc-1",
        "command_reconnect_backoff_s": BackoffConfig(),
        "printers": [],
    }
    values.update(overrides)
    return AgentConfig(**values)


@pytest.mark.parametrize(
    ("hub_url", "expected"),
    [
        ("https://hub.example.com/ws/agent", "wss://hub.example.com/ws/agent"),
        ("http://hub.example.com/ws/agent", "ws://hub.example.com/ws/agent"),
        ("wss://hub.example.com/ws/agent", "wss://hub.example.com/ws/agent"),
        # A bare host would hit the site root and get HTML, not a handshake.
        ("https://hub.example.com", "wss://hub.example.com/api/printers/agent"),
    ],
)
def test_hub_wss_url_derivation(hub_url, expected):
    assert hub_wss_url(hub_url) == expected


@pytest.mark.asyncio
async def test_check_hub_names_every_missing_field_at_once():
    result = await check_hub(make_config(hub_url="", agent_token="", location_key=""))

    assert result.ok is False
    assert "hub_url" in result.summary
    assert "agent_token" in result.summary
    assert "location_key" in result.summary
    # Later stages must stay unreached rather than reporting a false pass.
    assert result.step("connect").ok is None
    assert result.step("hello").ok is None


@pytest.mark.asyncio
async def test_check_hub_rejects_a_url_without_a_host():
    result = await check_hub(make_config(hub_url="not-a-url"))

    assert result.ok is False
    assert result.step("config").ok is False


@pytest.mark.asyncio
async def test_check_printer_requires_bambu_credentials():
    printer = PrinterConfig(key="p1", brand="bambu", host="192.168.1.4", port=8883, credentials={})

    result = await check_printer(printer)

    assert result.ok is False
    assert "access code" in result.summary.lower()
    assert "серийный номер" in result.summary.lower()
    assert result.step("tcp").ok is None


@pytest.mark.asyncio
async def test_check_printer_requires_a_host():
    printer = PrinterConfig(key="p1", brand="moonraker", host="", port=7125)

    result = await check_printer(printer)

    assert result.ok is False
    assert result.step("config").ok is False


@pytest.mark.asyncio
async def test_check_printer_reports_the_tcp_stage_on_a_refused_port():
    # Port 1 on loopback refuses immediately, so this stays fast and offline.
    printer = PrinterConfig(key="p1", brand="moonraker", host="127.0.0.1", port=1)

    result = await check_printer(printer, timeout_s=3)

    assert result.ok is False
    assert result.step("config").ok is True
    assert result.step("tcp").ok is False
    assert result.step("connect").ok is None


@pytest.mark.asyncio
async def test_check_printer_rejects_an_unknown_brand():
    printer = PrinterConfig(key="p1", brand="prusa-link", host="192.168.1.4")

    result = await check_printer(printer)

    assert result.ok is False
    assert "prusa-link" in result.summary
