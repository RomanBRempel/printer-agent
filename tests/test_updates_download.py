"""The downloaded package must keep the filename pip needs to parse."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from printer_agent import updates

WHEEL_URL = (
    "https://github.com/RomanBRempel/printer-agent/releases/download/"
    "v0.1.0a2/printer_agent-0.1.0a2-py3-none-any.whl"
)
PAYLOAD = b"not really a wheel, but the bytes do not matter here"


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


@pytest.fixture()
def fake_download(monkeypatch):
    monkeypatch.setattr(updates, "urlopen", lambda url: _FakeResponse(PAYLOAD))
    return PAYLOAD


def test_download_keeps_the_wheel_filename(fake_download):
    """A random temp stem makes pip fail with 'Invalid wheel filename'.

    pip reads the distribution name and version out of the filename, so the
    download cannot land in a NamedTemporaryFile.
    """
    path = Path(updates._download_package(WHEEL_URL, ""))

    assert path.name == "printer_agent-0.1.0a2-py3-none-any.whl"
    assert path.read_bytes() == PAYLOAD
    # The name pip parses: {distribution}-{version}-{python}-{abi}-{platform}
    assert len(path.stem.split("-")) == 5


def test_download_verifies_the_manifest_checksum(fake_download):
    digest = hashlib.sha256(PAYLOAD).hexdigest()

    path = Path(updates._download_package(WHEEL_URL, digest.upper()))

    assert path.exists()


def test_download_rejects_a_checksum_mismatch(fake_download):
    with pytest.raises(ValueError, match="sha256"):
        updates._download_package(WHEEL_URL, "0" * 64)


def test_download_falls_back_for_a_url_without_a_package_name(fake_download):
    path = Path(updates._download_package("https://example.com/latest", ""))

    assert path.suffix == ".whl"
    assert len(path.stem.split("-")) == 5


def test_download_strips_path_components_from_the_url(fake_download):
    path = Path(updates._download_package("https://example.com/a/b/pkg-1-py3-none-any.whl", ""))

    assert path.name == "pkg-1-py3-none-any.whl"


def test_local_paths_are_passed_through_untouched():
    assert updates._download_package(r"C:\builds\printer_agent-1-py3-none-any.whl", "") == (
        r"C:\builds\printer_agent-1-py3-none-any.whl"
    )


def test_pip_runs_under_the_console_interpreter(monkeypatch, tmp_path):
    """pip appends "w" to the running interpreter to name a gui launcher.

    Run it from pythonw.exe and it writes a shebang for `pythonww.exe`, which
    does not exist — every regenerated launcher then dies with
    "Unable to create process".
    """
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    monkeypatch.setattr(updates.sys, "executable", str(scripts / "pythonw.exe"))

    assert updates.pip_executable() == str(scripts / "python.exe")


def test_a_console_interpreter_is_used_as_is(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.sys, "executable", str(tmp_path / "python.exe"))

    assert updates.pip_executable() == str(tmp_path / "python.exe")


def test_a_missing_console_interpreter_falls_back(monkeypatch, tmp_path):
    """Nothing to switch to is better than pointing at a file that is not there."""
    monkeypatch.setattr(updates.sys, "executable", str(tmp_path / "pythonw.exe"))

    assert updates.pip_executable() == str(tmp_path / "pythonw.exe")


def test_the_manifest_checksum_comes_from_the_published_file(monkeypatch, tmp_path):
    """A wheel built twice from one commit is two different files.

    Zip timestamps differ, and on Windows so do the line endings git hands the
    build. A manifest hashed from the local copy therefore describes a download
    nobody will ever get: every agent refuses the update with "sha256 does not
    match manifest", and the release looks fine from the machine that published
    it and broken from the shop floor.
    """
    from printer_agent.cli import main

    published = b"the bytes the release actually serves"
    local_build = tmp_path / "printer_agent-9.9.9-py3-none-any.whl"
    local_build.write_bytes(b"the bytes this machine happened to build")
    monkeypatch.setattr(updates, "urlopen", lambda url: _FakeResponse(published))

    output = tmp_path / "printer-agent-update.json"
    exit_code = main(
        [
            "publish-update",
            "--version",
            "9.9.9",
            "--package-url",
            "https://example.com/printer_agent-9.9.9-py3-none-any.whl",
            "--sha256",
            "from-url",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["sha256"] == hashlib.sha256(published).hexdigest()
    assert manifest["sha256"] != hashlib.sha256(local_build.read_bytes()).hexdigest()


def test_a_local_package_path_is_hashed_from_disk(tmp_path):
    package = tmp_path / "printer_agent-9.9.9-py3-none-any.whl"
    package.write_bytes(PAYLOAD)

    assert updates.sha256_of_url(str(package)) == hashlib.sha256(PAYLOAD).hexdigest()
