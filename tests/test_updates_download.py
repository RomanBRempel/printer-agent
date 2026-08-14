"""The downloaded package must keep the filename pip needs to parse."""

from __future__ import annotations

import hashlib
import io
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
