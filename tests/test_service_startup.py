"""Two ways the Windows service died before it could say anything useful."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from printer_agent import windows_service
from printer_agent.config import parse_config

CONFIG_WITH_RELATIVE_OUTBOX = """
hub_url: ws://hub.example.com/api/printers/agent
agent_token: t
location_key: loc-1
outbox:
  database_path: data/outbox.sqlite3
  max_events: 5000
printers:
  - key: p1
    brand: moonraker
    host: 192.168.1.5
"""


@pytest.fixture()
def config_file(tmp_path):
    path = tmp_path / "conf" / "agent.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(CONFIG_WITH_RELATIVE_OUTBOX, encoding="utf-8")
    return path


def test_relative_outbox_anchors_to_the_config_not_the_cwd(config_file):
    """The service starts in System32; `data/` there is access-denied."""
    config, errors = parse_config(config_file)

    assert errors == []
    assert config.outbox.database_path == (config_file.parent / "data" / "outbox.sqlite3").resolve()
    assert config.outbox.database_path.is_absolute()


def test_an_absolute_outbox_path_is_left_alone(tmp_path):
    path = tmp_path / "agent.yaml"
    absolute = tmp_path / "queue" / "outbox.sqlite3"
    path.write_text(
        CONFIG_WITH_RELATIVE_OUTBOX.replace("data/outbox.sqlite3", absolute.as_posix()),
        encoding="utf-8",
    )

    config, _ = parse_config(path)

    assert config.outbox.database_path == absolute


def test_the_resolved_path_is_writable(config_file):
    """Anchoring is only useful if the directory can actually be created."""
    from printer_agent.core.outbox import EventOutbox

    config, _ = parse_config(config_file)
    outbox = EventOutbox(config.outbox.database_path)
    try:
        assert outbox.summary() == {"pending_events": 0, "command_results": 0}
    finally:
        outbox.close()


def test_the_service_reports_running_before_doing_any_work():
    """Without this the SCM kills the start after 30s with error 1053.

    The config read, the update check and the hub dial all happen after the
    handshake and can each exceed the timeout on their own. Read the module
    source rather than the class: the class only exists where pywin32 does,
    and this invariant has to hold everywhere it is edited.
    """
    source = Path(inspect.getfile(windows_service)).read_text(encoding="utf-8")
    body = source[source.index("def SvcDoRun") : source.index("async def _run")]

    assert "SERVICE_RUNNING" in body
    assert body.index("ReportServiceStatus") < body.index("asyncio.run")
