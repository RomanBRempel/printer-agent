"""Regression cover for the startup failure the desktop app replaced.

The old GUI shortcut ran `pythonw -m printer_agent gui`, which loaded and
validated the config *before* opening a window. A config the service rejects —
the one the installer writes, with an empty printer list — made argparse write
to a `sys.stderr` that does not exist under pythonw, so the process died with no
window and no message.
"""

from __future__ import annotations

import sys

import pytest

from printer_agent import cli
from printer_agent.config import load_config, ConfigError
from printer_agent.desktop.state import read_config
from printer_agent.logging import configure_logging

INSTALLER_DEFAULT_CONFIG = """
hub_url: wss://rd-control.example.com/ws/agent
agent_token: change-me
location_key: location-1
telemetry_interval_s: 5
heartbeat_interval_s: 15
outbox:
  database_path: data/outbox.sqlite3
  max_events: 5000
updates:
  feed_url: ""
  auto_update: false
  check_on_startup: true
printers: []
"""


@pytest.fixture()
def installer_config(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(INSTALLER_DEFAULT_CONFIG, encoding="utf-8")
    return path


def test_installer_default_config_is_still_invalid_for_the_service(installer_config):
    with pytest.raises(ConfigError) as excinfo:
        load_config(installer_config)

    assert "printers must not be empty" in str(excinfo.value)


def test_gui_command_launches_without_validating_the_config(installer_config, monkeypatch):
    launched: list[list[str]] = []
    monkeypatch.setattr(
        "printer_agent.desktop.main", lambda argv=None: launched.append(list(argv or [])) or 0
    )

    exit_code = cli.main(["--config", str(installer_config), "gui"])

    assert exit_code == 0
    assert launched == [["--config", str(installer_config)]]


def test_gui_command_launches_even_when_the_file_is_unparseable(tmp_path, monkeypatch):
    broken = tmp_path / "agent.yaml"
    broken.write_text("hub_url: [unclosed\n", encoding="utf-8")
    launched: list[list[str]] = []
    monkeypatch.setattr(
        "printer_agent.desktop.main", lambda argv=None: launched.append(list(argv or [])) or 0
    )

    assert cli.main(["--config", str(broken), "gui"]) == 0
    assert launched


def test_config_flag_is_accepted_after_the_subcommand(installer_config, monkeypatch):
    """`install-service --config X` is the natural form and the installer used it.

    It used to be rejected as an unrecognized argument, the installer ignored the
    non-zero exit, and the service was never registered.
    """
    launched: list[list[str]] = []
    monkeypatch.setattr(
        "printer_agent.desktop.main", lambda argv=None: launched.append(list(argv or [])) or 0
    )

    assert cli.main(["gui", "--config", str(installer_config)]) == 0
    assert launched == [["--config", str(installer_config)]]


def test_config_before_the_subcommand_still_wins(installer_config, monkeypatch):
    launched: list[list[str]] = []
    monkeypatch.setattr(
        "printer_agent.desktop.main", lambda argv=None: launched.append(list(argv or [])) or 0
    )

    assert cli.main(["--config", str(installer_config), "gui"]) == 0
    assert launched == [["--config", str(installer_config)]]


def test_install_service_registers_despite_an_unrunnable_config(installer_config, monkeypatch, capsys):
    """A fresh install writes a template with no printers.

    Refusing to register the service on it would leave the operator with nothing
    to configure — which is exactly what happened on the first install.
    """
    monkeypatch.setattr(cli.os, "name", "nt")
    commands: list[str] = []
    monkeypatch.setattr(cli, "_run_windows_service_command", commands.append)
    monkeypatch.setattr("printer_agent.windows_service.CONFIG_PATH", installer_config)

    exit_code = cli.main(["install-service", "--config", str(installer_config)])

    assert exit_code == 0
    assert commands == ["install"]
    output = capsys.readouterr().out
    assert "printers must not be empty" in output
    assert "not runnable yet" in output


def test_parse_config_returns_errors_instead_of_raising(installer_config):
    from printer_agent.config import parse_config

    config, errors = parse_config(installer_config)

    assert config.hub_url  # the parsed values are still usable
    assert "printers must not be empty" in errors


def test_read_config_tolerates_a_broken_file(tmp_path):
    broken = tmp_path / "agent.yaml"
    broken.write_text("hub_url: [unclosed\n", encoding="utf-8")

    config, error = read_config(broken)

    assert error
    assert config.printers == []


def test_read_config_tolerates_a_missing_file(tmp_path):
    config, error = read_config(tmp_path / "absent.yaml")

    assert error == ""
    assert config.hub_url == ""


def test_read_config_rejects_a_non_mapping_document(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")

    config, error = read_config(path)

    assert "должен быть словарём" in error
    assert config.printers == []


def test_logging_survives_a_missing_stdout(tmp_path, monkeypatch):
    """pythonw.exe gives the process no stdout and no stderr."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log_file = tmp_path / "agent.log"

    configure_logging(log_file=log_file)
    import logging

    logging.getLogger("printer_agent.test").info("startup", extra={"action": "startup"})
    logging.shutdown()

    assert "startup" in log_file.read_text(encoding="utf-8")


def test_logging_falls_back_when_the_log_file_is_unwritable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    # A path whose parent is a file cannot be created; this must not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    configure_logging(log_file=blocker / "nested" / "agent.log")
