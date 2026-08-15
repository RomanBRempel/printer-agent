"""Moving settings between installations.

The two failures worth guarding are silent ones: a bundle that leaks the agent
token when nobody asked for it, and an import that wipes credentials the target
machine already had because the bundle was redacted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from printer_agent import cli
from printer_agent.config import AgentConfig, BackoffConfig, OutboxConfig, PrinterConfig, UpdateConfig
from printer_agent.settings_bundle import (
    BUNDLE_KIND,
    BUNDLE_VERSION,
    MODE_FULL,
    MODE_PRINTERS,
    BundleError,
    apply_bundle,
    build_bundle,
    describe_bundle,
    read_bundle,
    write_bundle,
)


def make_config(**overrides) -> AgentConfig:
    config = AgentConfig(
        hub_url="https://hub.example.com/api/printers/agent",
        agent_token="token-from-source",
        location_key="loc-001",
        telemetry_interval_s=7,
        heartbeat_interval_s=21,
        command_reconnect_backoff_s=BackoffConfig(min_s=2, max_s=90),
        outbox=OutboxConfig(database_path="C:/ProgramData/printer-agent/outbox.sqlite3", max_events=4200),
        updates=UpdateConfig(feed_url="https://example.com/feed.json", auto_update=True),
        printers=[
            PrinterConfig(key="k1", brand="moonraker", host="10.0.0.11", port=7125),
            PrinterConfig(
                key="x1c",
                brand="bambu",
                host="10.0.0.12",
                credentials={"access_code": "12345678", "serial": "01P00A000000000"},
            ),
        ],
    )
    for name, value in overrides.items():
        setattr(config, name, value)
    return config


def empty_config() -> AgentConfig:
    return AgentConfig(hub_url="", agent_token="", location_key="")


# -- export ----------------------------------------------------------------- #

def test_export_leaves_secrets_out_by_default():
    bundle = build_bundle(make_config())

    settings = bundle["settings"]
    assert "agent_token" not in settings
    bambu = next(item for item in settings["printers"] if item["key"] == "x1c")
    assert "access_code" not in bambu["credentials"]
    # The serial identifies the printer rather than granting access to it.
    assert bambu["credentials"]["serial"] == "01P00A000000000"
    assert bundle["contains_secrets"] is False
    assert bundle["redacted"] == ["agent_token", "printers.x1c.credentials.access_code"]


def test_export_never_carries_the_outbox_path():
    """It names a folder on the source machine; the target may not own it."""
    bundle = build_bundle(make_config(), include_secrets=True)

    assert bundle["settings"]["outbox"] == {"max_events": 4200}


def test_export_with_secrets_is_labelled_as_such():
    bundle = build_bundle(make_config(), include_secrets=True, note="перенос на новый ПК")

    assert bundle["settings"]["agent_token"] == "token-from-source"
    assert bundle["contains_secrets"] is True
    assert bundle["note"] == "перенос на новый ПК"
    assert "redacted" not in bundle


def test_a_config_without_secrets_is_not_labelled_sensitive():
    bare = make_config(agent_token="", printers=[PrinterConfig(key="k1", brand="moonraker", host="10.0.0.11")])

    assert build_bundle(bare, include_secrets=True)["contains_secrets"] is False


# -- import ----------------------------------------------------------------- #

def test_full_import_onto_a_blank_agent_reproduces_the_source():
    source = make_config()
    merged, report = apply_bundle(build_bundle(source, include_secrets=True), empty_config())

    assert merged.hub_url == source.hub_url
    assert merged.location_key == source.location_key
    assert merged.agent_token == source.agent_token
    assert merged.telemetry_interval_s == 7
    assert merged.heartbeat_interval_s == 21
    assert merged.command_reconnect_backoff_s.max_s == 90
    assert merged.outbox.max_events == 4200
    assert merged.updates.auto_update is True
    assert [printer.key for printer in merged.printers] == ["k1", "x1c"]
    assert merged.printers[1].credentials["access_code"] == "12345678"
    assert report.missing == []


def test_import_keeps_the_local_outbox_path():
    merged, report = apply_bundle(
        build_bundle(make_config(), include_secrets=True),
        AgentConfig(
            hub_url="",
            agent_token="",
            location_key="",
            outbox=OutboxConfig(database_path="D:/agent/outbox.sqlite3"),
        ),
    )

    assert merged.outbox.database_path == Path("D:/agent/outbox.sqlite3")
    assert "outbox.database_path" in report.kept_local


def test_a_redacted_bundle_does_not_wipe_local_secrets():
    """Re-importing on a configured machine must not clear what is already there."""
    local = make_config(hub_url="https://old.example.com/api/printers/agent", agent_token="local-token")

    merged, report = apply_bundle(build_bundle(make_config()), local)

    assert merged.hub_url == "https://hub.example.com/api/printers/agent"
    assert merged.agent_token == "local-token"
    assert merged.printers[1].credentials["access_code"] == "12345678"
    assert "agent_token" in report.kept_local
    assert "printers.x1c.credentials.access_code" in report.kept_local
    assert report.missing == []


def test_missing_secrets_are_reported_rather_than_left_silently_blank():
    merged, report = apply_bundle(build_bundle(make_config()), empty_config())

    assert merged.agent_token == ""
    assert "agent_token" in report.missing
    assert "printers.x1c.credentials.access_code" in report.missing


def test_printers_only_mode_leaves_the_local_hub_alone():
    local = make_config(
        hub_url="https://local.example.com/api/printers/agent",
        location_key="loc-002",
        agent_token="local-token",
        printers=[],
    )

    merged, report = apply_bundle(build_bundle(make_config(), include_secrets=True), local, mode=MODE_PRINTERS)

    assert merged.hub_url == "https://local.example.com/api/printers/agent"
    assert merged.location_key == "loc-002"
    assert merged.agent_token == "local-token"
    assert [printer.key for printer in merged.printers] == ["k1", "x1c"]
    assert "printers[2]" in report.applied


def test_unknown_mode_is_refused():
    with pytest.raises(BundleError):
        apply_bundle(build_bundle(make_config()), empty_config(), mode="merge")


# -- file handling ---------------------------------------------------------- #

def test_round_trip_through_a_file(tmp_path):
    path = write_bundle(build_bundle(make_config(), include_secrets=True), tmp_path / "bundle.yaml")

    info = describe_bundle(read_bundle(path))

    assert info.version == BUNDLE_VERSION
    assert info.source_location_key == "loc-001"
    assert info.printer_keys == ["k1", "x1c"]
    assert info.contains_secrets is True


def test_a_foreign_yaml_file_is_refused(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text("hub_url: https://hub.example.com\nprinters: []\n", encoding="utf-8")

    with pytest.raises(BundleError, match="not a printer-agent settings bundle"):
        read_bundle(path)


def test_a_newer_bundle_version_is_refused(tmp_path):
    path = tmp_path / "bundle.yaml"
    path.write_text(
        yaml.safe_dump({"kind": BUNDLE_KIND, "version": BUNDLE_VERSION + 1, "settings": {}}),
        encoding="utf-8",
    )

    with pytest.raises(BundleError, match="newer than this agent understands"):
        read_bundle(path)


def test_an_unparseable_file_is_refused(tmp_path):
    path = tmp_path / "bundle.yaml"
    path.write_text("kind: [unclosed\n", encoding="utf-8")

    with pytest.raises(BundleError, match="not valid YAML"):
        read_bundle(path)


# -- CLI -------------------------------------------------------------------- #

def write_agent_yaml(path, config: AgentConfig):
    from printer_agent.config import save_config

    save_config(config, path)
    return path


def test_cli_export_then_import_moves_the_settings(tmp_path, capsys):
    source = write_agent_yaml(tmp_path / "source.yaml", make_config())
    bundle_path = tmp_path / "bundle.yaml"
    target = tmp_path / "target.yaml"

    assert cli.main(["--config", str(source), "export-settings", "--output", str(bundle_path), "--include-secrets"]) == 0
    assert "warning: this file carries secrets" in capsys.readouterr().out

    assert cli.main(["--config", str(target), "import-settings", str(bundle_path)]) == 0
    written = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert written["hub_url"] == "https://hub.example.com/api/printers/agent"
    assert written["agent_token"] == "token-from-source"
    assert [item["key"] for item in written["printers"]] == ["k1", "x1c"]
    assert "applied: hub_url" in capsys.readouterr().out


def test_cli_import_reports_an_unrunnable_result_and_still_writes(tmp_path, capsys):
    source = write_agent_yaml(tmp_path / "source.yaml", make_config())
    bundle_path = tmp_path / "bundle.yaml"
    target = tmp_path / "target.yaml"
    cli.main(["--config", str(source), "export-settings", "--output", str(bundle_path)])
    capsys.readouterr()

    # Redacted bundle onto a blank agent: usable inventory, no credentials yet.
    exit_code = cli.main(["--config", str(target), "import-settings", str(bundle_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert target.exists()
    assert "still missing:" in output
    assert "agent_token" in output
    assert "printers.x1c.credentials.access_code" in output
    assert "agent_token is required" in output


def test_cli_import_dry_run_writes_nothing(tmp_path, capsys):
    source = write_agent_yaml(tmp_path / "source.yaml", make_config())
    bundle_path = tmp_path / "bundle.yaml"
    target = tmp_path / "target.yaml"
    cli.main(["--config", str(source), "export-settings", "--output", str(bundle_path), "--include-secrets"])

    assert cli.main(["--config", str(target), "import-settings", str(bundle_path), "--dry-run"]) == 0
    assert not target.exists()
    assert "dry run" in capsys.readouterr().out


def test_cli_import_refuses_a_foreign_file(tmp_path):
    target = write_agent_yaml(tmp_path / "target.yaml", make_config())
    foreign = tmp_path / "notes.yaml"
    foreign.write_text("just: text\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--config", str(target), "import-settings", str(foreign)])

    assert excinfo.value.code == 2
    # The target config is untouched.
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["hub_url"]


def test_cli_export_does_not_bake_in_an_env_override(tmp_path, monkeypatch):
    """`HUB_URL` is a runtime override, not a setting the operator wrote down."""
    source = write_agent_yaml(tmp_path / "source.yaml", make_config())
    monkeypatch.setenv("HUB_URL", "https://temporary.example.com/api/printers/agent")
    bundle_path = tmp_path / "bundle.yaml"

    cli.main(["--config", str(source), "export-settings", "--output", str(bundle_path)])

    bundle = read_bundle(bundle_path)
    assert bundle["settings"]["hub_url"] == "https://hub.example.com/api/printers/agent"


def test_printers_only_import_from_the_cli(tmp_path):
    source = write_agent_yaml(tmp_path / "source.yaml", make_config())
    local = make_config(hub_url="https://local.example.com/api/printers/agent", printers=[])
    target = write_agent_yaml(tmp_path / "target.yaml", local)
    bundle_path = tmp_path / "bundle.yaml"
    cli.main(["--config", str(source), "export-settings", "--output", str(bundle_path), "--include-secrets"])

    cli.main(["--config", str(target), "import-settings", str(bundle_path), "--mode", "printers"])

    written = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert written["hub_url"] == "https://local.example.com/api/printers/agent"
    assert [item["key"] for item in written["printers"]] == ["k1", "x1c"]


def test_mode_full_is_the_default():
    assert MODE_FULL == "full"
