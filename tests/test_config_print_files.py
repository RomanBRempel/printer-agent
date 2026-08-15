"""The two settings the print-file and camera work added to `agent.yaml`."""

from __future__ import annotations

from pathlib import Path

from printer_agent.config import (
    AgentConfig,
    OutboxConfig,
    PrinterConfig,
    config_from_dict,
    config_to_dict,
    parse_config,
    validate_config,
)

BASE = """
hub_url: https://hub.example.com/api/printers/agent
agent_token: secret-token
location_key: loc-001
outbox:
  database_path: data/outbox.sqlite3
printers:
  - key: printer-1
    brand: moonraker
    host: 127.0.0.1
"""


def write(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(BASE + extra, encoding="utf-8")
    return path


def test_the_cache_defaults_next_to_the_outbox(tmp_path) -> None:
    """One folder to point at a data disk, not two."""
    config, errors = parse_config(write(tmp_path), env={})  # type: ignore[arg-type]

    assert errors == []
    assert config.print_files_directory() == config.outbox.database_path.parent / "print-files"


def test_a_relative_cache_path_is_anchored_to_the_config_file(tmp_path) -> None:
    """The service starts in System32; a relative path must not mean "there"."""
    config, _errors = parse_config(
        write(tmp_path, "print_files:\n  directory: spool\n"), env={}  # type: ignore[arg-type]
    )

    assert config.print_files_directory() == (tmp_path / "spool").resolve()


def test_the_camera_url_survives_a_round_trip(tmp_path) -> None:
    config, _errors = parse_config(
        write(tmp_path, "    camera_snapshot_url: http://127.0.0.1:8080/?action=snapshot\n"),
        env={},  # type: ignore[arg-type]
    )

    assert config.printers[0].camera_snapshot_url == "http://127.0.0.1:8080/?action=snapshot"
    # Saving and reading back must not drop it: the desktop editor rewrites the
    # whole file every time a printer is edited.
    again = config_from_dict(config_to_dict(config))
    assert again.printers[0].camera_snapshot_url == config.printers[0].camera_snapshot_url


def test_a_camera_url_that_is_not_a_url_is_named_as_an_error() -> None:
    config = AgentConfig(
        hub_url="https://hub.example.com/api/printers/agent",
        agent_token="secret-token",
        location_key="loc-001",
        outbox=OutboxConfig(database_path=Path("outbox.sqlite3")),
        printers=[
            PrinterConfig(
                key="printer-1", brand="moonraker", host="127.0.0.1", camera_snapshot_url="192.168.1.9"
            )
        ],
    )

    errors = validate_config(config)

    assert any("camera_snapshot_url" in error for error in errors)


def test_retention_bounds_must_be_positive(tmp_path) -> None:
    config, errors = parse_config(
        write(tmp_path, "print_files:\n  max_age_h: 0\n  max_total_mb: -1\n"), env={}  # type: ignore[arg-type]
    )

    assert config.print_files.max_age_h == 0
    assert [error for error in errors if "max_age_h" in error]
    assert [error for error in errors if "max_total_mb" in error]
