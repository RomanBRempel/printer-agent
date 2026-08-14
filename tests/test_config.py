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
