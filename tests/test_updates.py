from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from printer_agent.updates import UpdateManifest, apply_update, check_for_update, manifest_to_dict, publish_manifest, write_manifest


def test_update_manifest_roundtrip(tmp_path: Path) -> None:
    manifest = UpdateManifest(
        version="0.2.0",
        package_url="https://downloads.example.com/printer-agent-0.2.0-py3-none-any.whl",
        sha256="abc123",
        notes="First auto-update build",
        published_at="2026-08-14T12:00:00Z",
    )

    destination = tmp_path / "update.json"
    write_manifest(manifest, destination)
    loaded = json.loads(destination.read_text(encoding="utf-8"))

    assert loaded == manifest_to_dict(manifest)


def test_check_for_update_uses_local_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "feed.json"
    publish_manifest(
        version="0.2.0",
        package_url="https://downloads.example.com/printer-agent-0.2.0-py3-none-any.whl",
        destination=manifest_path,
    )

    status = check_for_update(manifest_path)

    assert status.update_available is True
    assert status.manifest is not None
    assert status.latest_version == "0.2.0"


def test_apply_update_reports_success_for_mocked_pip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest = UpdateManifest(version="0.2.0", package_url=str(tmp_path / "printer-agent-0.2.0-py3-none-any.whl"))

    completed = SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("printer_agent.updates.subprocess.run", lambda *args, **kwargs: completed)

    status = apply_update(manifest)

    assert status.installed is True
    assert status.latest_version == "0.2.0"
