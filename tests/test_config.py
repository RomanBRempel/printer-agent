from __future__ import annotations

from pathlib import Path

import pytest

from printer_agent.config import (
    AgentConfig,
    BackoffConfig,
    ConfigError,
    OutboxConfig,
    PrinterConfig,
    UpdateConfig,
    config_to_dict,
    load_config,
    parse_config,
    save_config,
)


def test_load_config_with_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
hub_url: https://rd-control.example.com
agent_token: file-token
location_key: loc-file
printers:
  - key: printer-1
    brand: moonraker
    host: 192.168.1.10
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_TOKEN", "env-token")
    config = load_config(config_path)
    assert config.agent_token == "env-token"
    assert config.hub_url == "https://rd-control.example.com"
    assert config.printers[0].brand == "moonraker"


def test_invalid_config_reports_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)
    assert "hub_url is required" in str(exc_info.value)
    assert "printers must not be empty" in str(exc_info.value)


def test_config_roundtrip_through_yaml(tmp_path: Path) -> None:
    config = AgentConfig(
        hub_url="https://rd-control.example.com",
        agent_token="token-123",
        location_key="loc-001",
        telemetry_interval_s=5,
        heartbeat_interval_s=15,
        command_reconnect_backoff_s=BackoffConfig(min_s=1, max_s=30),
        outbox=OutboxConfig(database_path=tmp_path / "outbox.sqlite3", max_events=1000),
        updates=UpdateConfig(feed_url="https://updates.example.com/printer-agent.json", auto_update=True, check_on_startup=False),
        printers=[
            PrinterConfig(
                key="printer-1",
                brand="moonraker",
                host="192.168.1.10",
                port=7125,
                credentials={"access_code": "abc", "serial": "def"},
            )
        ],
    )

    config_path = tmp_path / "agent.yaml"
    save_config(config, config_path)
    reloaded = load_config(config_path)

    assert config_to_dict(reloaded) == config_to_dict(config)


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(body, encoding="utf-8")
    return path


BLANK_SCALARS = """
hub_url: https://rd-control.example.com/api/printers/agent
agent_token: token
location_key: loc-1
printers:
  - key: printer-1
    brand:
    host:
"""


def test_a_key_with_no_value_reads_as_empty_not_as_none(tmp_path: Path) -> None:
    """`host:` parses as None, and `str(None)` is the token "None": a value that
    looks set, satisfies every required-field check, and fails much later as a
    hostname nothing can resolve."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, BLANK_SCALARS))

    assert "printer printer-1: host is required" in excinfo.value.errors


def test_a_blank_brand_falls_back_to_the_default(tmp_path: Path) -> None:
    config, _errors = parse_config(write(tmp_path, BLANK_SCALARS))

    assert config.printers[0].brand == "moonraker"


def test_a_blank_optional_path_is_absent_rather_than_a_path_named_none(tmp_path: Path) -> None:
    config, _errors = parse_config(
        write(
            tmp_path,
            BLANK_SCALARS.replace("printers:", "print_files:\n  directory:\nprinters:"),
        )
    )

    assert config.print_files.directory is None


def test_a_non_numeric_setting_names_itself(tmp_path: Path) -> None:
    """`int()` raising on its own says which type failed, not which setting."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, BLANK_SCALARS.replace("location_key: loc-1", "location_key: loc-1\ntelemetry_interval_s: every 5s")))

    assert excinfo.value.errors == ["telemetry_interval_s must be a whole number"]


def test_a_blank_interval_takes_the_default(tmp_path: Path) -> None:
    config, _errors = parse_config(
        write(tmp_path, BLANK_SCALARS.replace("location_key: loc-1", "location_key: loc-1\ntelemetry_interval_s:"))
    )

    assert config.telemetry_interval_s == 5
