"""Bambu MQTT transport rules, learned from an A1 mini that went silent.

Two of them cost a live printer its telemetry: the printer never PUBACKs a
publish on ``device/<serial>/request``, and a second client on the subscription's
own id makes the broker evict the session feeding the cache. Neither shows up as
an error the operator can read — the snapshot just stays empty — so they are
pinned here instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from printer_agent.adapters import bambu
from printer_agent.adapters.bambu import BambuAdapter
from printer_agent.config import PrinterConfig
from printer_agent.contracts import PrinterStatus


def make_adapter() -> BambuAdapter:
    return BambuAdapter(
        PrinterConfig(
            key="a1-mini",
            brand="bambu",
            host="10.0.0.5",
            port=8883,
            credentials={"serial": "0309DA4B0803132", "access_code": "12345678"},
        )
    )


class FakeClient:
    """Records what the adapter publishes and with which client id."""

    published: list[dict] = []
    identifiers: list[str] = []

    def __init__(self, **kwargs):
        FakeClient.identifiers.append(kwargs.get("identifier", ""))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def publish(self, topic, payload, qos=0):
        FakeClient.published.append({"topic": topic, "payload": payload, "qos": qos})


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.published = []
    FakeClient.identifiers = []
    monkeypatch.setattr(bambu, "Client", FakeClient)
    return FakeClient


def test_commands_publish_at_qos_zero(fake_client):
    """qos above 0 waits for an ack the printer never sends, then times out."""
    adapter = make_adapter()
    asyncio.run(adapter.pause())

    assert [entry["qos"] for entry in fake_client.published] == [0]
    assert json.loads(fake_client.published[0]["payload"])["print"]["command"] == "pause"


def test_commands_use_their_own_client_id(fake_client):
    """Reusing the subscription's id has the broker evict the state feed."""
    adapter = make_adapter()
    adapter.client_identifier = "0309DA4B0803132-agent-ui"
    asyncio.run(adapter.cancel())

    assert fake_client.identifiers == ["0309DA4B0803132-agent-ui-cmd"]


def test_an_empty_cache_reports_why_it_is_empty():
    """"Cache is empty" alone sent an operator hunting the wrong problem."""
    adapter = make_adapter()
    adapter._last_error = "Operation timed out"

    snapshot = asyncio.run(adapter.get_state())

    assert snapshot.status is PrinterStatus.offline
    assert "Operation timed out" in snapshot.error.message


def test_every_asyncio_run_goes_through_the_selector_loop_helper():
    """paho needs add_reader, which the Windows Proactor loop does not have.

    A bare ``asyncio.run`` anywhere in the package silently kills Bambu polling
    on Windows: the client connects, subscribes and then receives nothing.
    """
    package = Path(bambu.__file__).parent.parent
    offenders = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if path.name != "aio.py" and "asyncio.run(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
