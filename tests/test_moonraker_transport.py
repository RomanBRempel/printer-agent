"""Moonraker speaks two dialects; the adapter has to survive both.

Creality's K-series firmware ships an older Moonraker fork whose
`/server/jsonrpc` answers 404. Its REST API is the one every build has.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from printer_agent.adapters.moonraker import REST_ROUTES, MoonrakerAdapter
from printer_agent.config import PrinterConfig


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None, status: int = 200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status, message="Not Found"
            )

    async def json(self, content_type: str | None = "application/json"):
        return self._payload


class _FakeSession:
    """Records calls and answers JSON-RPC with a configurable status."""

    def __init__(self, jsonrpc_status: int = 200):
        self.jsonrpc_status = jsonrpc_status
        self.posts: list[str] = []
        self.requests: list[tuple[str, str]] = []

    def post(self, url: str, json: dict[str, Any] | None = None):
        self.posts.append(url)
        if self.jsonrpc_status >= 400:
            return _FakeResponse(None, self.jsonrpc_status)
        return _FakeResponse({"result": {"state": "ready", "source": "jsonrpc"}})

    def request(self, verb: str, url: str):
        self.requests.append((verb, url))
        return _FakeResponse({"result": {"state": "ready", "source": "rest"}})


def make_adapter(session: _FakeSession) -> MoonrakerAdapter:
    adapter = MoonrakerAdapter(PrinterConfig(key="p1", brand="moonraker", host="10.0.0.5", port=7125))
    adapter._ensure_session = lambda: session  # type: ignore[method-assign]
    return adapter


@pytest.mark.asyncio
async def test_modern_moonraker_stays_on_jsonrpc():
    session = _FakeSession()
    adapter = make_adapter(session)

    result = await adapter._call("printer.info")

    assert result["source"] == "jsonrpc"
    assert adapter._prefers_rest is False
    assert session.requests == []


@pytest.mark.asyncio
async def test_a_404_switches_the_adapter_to_rest():
    session = _FakeSession(jsonrpc_status=404)
    adapter = make_adapter(session)

    result = await adapter._call("printer.info")

    assert result["source"] == "rest"
    assert adapter._prefers_rest is True
    assert session.requests == [("GET", "http://10.0.0.5:7125/printer/info")]


@pytest.mark.asyncio
async def test_the_transport_choice_sticks():
    """Re-probing JSON-RPC on every poll would waste a request per cycle."""
    session = _FakeSession(jsonrpc_status=404)
    adapter = make_adapter(session)

    await adapter._call("printer.info")
    await adapter._call("printer.info")

    assert len(session.posts) == 1  # only the first call probed JSON-RPC
    assert len(session.requests) == 2


@pytest.mark.asyncio
async def test_other_http_errors_are_not_treated_as_a_missing_endpoint():
    session = _FakeSession(jsonrpc_status=500)
    adapter = make_adapter(session)

    with pytest.raises(aiohttp.ClientResponseError):
        await adapter._call("printer.info")

    assert adapter._prefers_rest is False


@pytest.mark.asyncio
async def test_object_query_becomes_a_rest_query_string():
    session = _FakeSession(jsonrpc_status=404)
    adapter = make_adapter(session)

    await adapter._call("printer.objects.query", {"objects": {"webhooks": None, "extruder": None}})

    verb, url = session.requests[0]
    assert verb == "GET"
    # Bare names: a value after `=` would restrict the fields returned.
    assert url == "http://10.0.0.5:7125/printer/objects/query?webhooks&extruder"


@pytest.mark.asyncio
async def test_an_unmapped_method_fails_loudly():
    session = _FakeSession(jsonrpc_status=404)
    adapter = make_adapter(session)

    with pytest.raises(RuntimeError, match="no REST equivalent"):
        await adapter._call("printer.gcode.script")


@pytest.mark.asyncio
async def test_get_state_reports_the_real_error_when_offline():
    class _Failing(_FakeSession):
        def post(self, url: str, json: dict[str, Any] | None = None):
            raise OSError("no route to host 10.0.0.5")

    adapter = make_adapter(_Failing())

    snapshot = await adapter.get_state()

    assert str(snapshot.status) == "offline"
    # "Moonraker request failed" would name nothing an operator can act on.
    assert "no route" in (snapshot.error.message or "")


def test_every_method_the_adapter_calls_has_a_rest_route():
    used = {"printer.info", "printer.objects.query", "printer.print.pause",
            "printer.print.resume", "printer.print.cancel"}

    assert used <= set(REST_ROUTES)
