from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from . import __version__


#: Filename endings pip can install from a local path.
PACKAGE_SUFFIXES = (".whl", ".tar.gz", ".zip")


@dataclass(slots=True)
class UpdateManifest:
    version: str
    package_url: str
    sha256: str = ""
    notes: str = ""
    published_at: str = ""


@dataclass(slots=True)
class UpdateStatus:
    current_version: str
    latest_version: str
    update_available: bool
    manifest: UpdateManifest | None = None
    installed: bool = False
    message: str = ""


def current_version() -> str:
    return __version__


def manifest_from_dict(data: dict[str, object]) -> UpdateManifest:
    version = str(data.get("version", "")).strip()
    package_url = str(data.get("package_url", "")).strip()
    if not version:
        raise ValueError("update manifest is missing version")
    if not package_url:
        raise ValueError("update manifest is missing package_url")
    return UpdateManifest(
        version=version,
        package_url=package_url,
        sha256=str(data.get("sha256", "")).strip(),
        notes=str(data.get("notes", "")).strip(),
        published_at=str(data.get("published_at", "")).strip(),
    )


def manifest_to_dict(manifest: UpdateManifest) -> dict[str, str]:
    data = {
        "version": manifest.version,
        "package_url": manifest.package_url,
    }
    if manifest.sha256:
        data["sha256"] = manifest.sha256
    if manifest.notes:
        data["notes"] = manifest.notes
    if manifest.published_at:
        data["published_at"] = manifest.published_at
    return data


def load_manifest(source: str | Path) -> UpdateManifest:
    source_text = str(source)
    parsed = urlparse(source_text)
    if parsed.scheme in {"http", "https"}:
        with urlopen(source_text) as response:  # nosec: B310 - update manifests are operator-controlled
            payload = response.read().decode("utf-8")
    else:
        payload = Path(source_text).read_text(encoding="utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("update manifest must be a JSON object")
    return manifest_from_dict(data)


def write_manifest(manifest: UpdateManifest, destination: str | Path) -> None:
    target_path = Path(destination)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(manifest_to_dict(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_for_update(feed_url: str | Path | None) -> UpdateStatus:
    if not feed_url:
        return UpdateStatus(
            current_version=current_version(),
            latest_version=current_version(),
            update_available=False,
            message="No update feed configured.",
        )

    manifest = load_manifest(feed_url)
    update_available = is_newer_version(current_version(), manifest.version)
    message = "Update available." if update_available else "Already up to date."
    return UpdateStatus(
        current_version=current_version(),
        latest_version=manifest.version,
        update_available=update_available,
        manifest=manifest,
        message=message,
    )


def pip_executable() -> str:
    """The interpreter pip should run under.

    pip derives a gui-script's launcher from the running interpreter by
    appending "w" to its name. Run it from pythonw.exe — which is what the
    desktop app is — and it writes a shebang pointing at `pythonww.exe`, a file
    that does not exist, leaving every regenerated launcher dead with
    "Unable to create process".
    """
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


def apply_update(manifest: UpdateManifest) -> UpdateStatus:
    package_reference = _download_package(manifest.package_url, manifest.sha256)
    completed = subprocess.run(
        [pip_executable(), "-m", "pip", "install", "--upgrade", package_reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "pip upgrade failed"
        return UpdateStatus(
            current_version=current_version(),
            latest_version=manifest.version,
            update_available=True,
            manifest=manifest,
            installed=False,
            message=stderr,
        )
    return UpdateStatus(
        current_version=current_version(),
        latest_version=manifest.version,
        update_available=True,
        manifest=manifest,
        installed=True,
        message="Update installed successfully.",
    )


def publish_manifest(version: str, package_url: str, destination: str | Path, *, sha256: str = "", notes: str = "", published_at: str = "") -> UpdateManifest:
    manifest = UpdateManifest(
        version=version.strip(),
        package_url=package_url.strip(),
        sha256=sha256.strip(),
        notes=notes.strip(),
        published_at=published_at.strip(),
    )
    if not manifest.version:
        raise ValueError("version is required")
    if not manifest.package_url:
        raise ValueError("package_url is required")
    write_manifest(manifest, destination)
    return manifest


def is_newer_version(current: str, candidate: str) -> bool:
    return _version_key(candidate) > _version_key(current)


def _version_key(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", value)]
    return tuple(parts) if parts else (0,)


def _download_package(package_url: str, expected_sha256: str) -> str:
    parsed = urlparse(package_url)
    if parsed.scheme not in {"http", "https"}:
        return package_url

    with urlopen(package_url) as response:  # nosec: B310 - update packages are operator-controlled
        payload = response.read()
    if expected_sha256:
        digest = hashlib.sha256(payload).hexdigest()
        if digest.lower() != expected_sha256.lower():
            raise ValueError("downloaded package sha256 does not match manifest")

    # pip reads the distribution name and version out of the *filename*, so the
    # download has to keep the one from the URL. Writing to a NamedTemporaryFile
    # gives it a random stem and pip refuses the install with
    # "Invalid wheel filename (wrong number of parts)".
    name = Path(parsed.path).name
    if not name.endswith(PACKAGE_SUFFIXES):
        name = "printer_agent-0-py3-none-any.whl"
    directory = Path(tempfile.mkdtemp(prefix="printer-agent-update-"))
    target = directory / Path(name).name  # drop any path components from the URL
    target.write_bytes(payload)
    return str(target)
