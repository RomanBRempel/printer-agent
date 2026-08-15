"""Local cache of print files delivered by `file_offer`.

The hub sends the file first and the start command second, with the upload to the
printer in between, so the file has to survive both the gap and a restart of the
agent — but not forever: a shop-floor box has a small disk and every job leaves a
copy behind. Hence a directory with an age and a size bound, pruned on write.

The name on disk is the hub's ``file_ref`` verbatim, because that is the only
name the follow-up `start_print` carries. That makes the hub the source of a
filesystem path, so :func:`validate_file_ref` is a security boundary, not a
formality.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: A file_ref names a file inside the cache and nothing else: no separators, no
#: parent references, no leading dot that would hide the entry.
FILE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Suffix of a download still in flight. Never a valid file_ref, so a partial
#: file cannot be mistaken for a delivered one.
PARTIAL_SUFFIX = ".part"

#: How long an interrupted download's leftovers are kept before being swept.
PARTIAL_MAX_AGE_S = 3600.0


class InvalidFileRef(ValueError):
    """The hub sent a file_ref that cannot be used as a file name."""


def validate_file_ref(file_ref: str) -> str:
    if not FILE_REF_PATTERN.match(file_ref or ""):
        raise InvalidFileRef(f"unusable file_ref {file_ref!r}")
    return file_ref


@dataclass(slots=True)
class PruneReport:
    removed: int = 0
    freed_bytes: int = 0


class PrintFileCache:
    def __init__(self, directory: str | Path, *, max_age_h: int = 72, max_total_mb: int = 2048):
        self.directory = Path(directory)
        self.max_age_s = max(1, int(max_age_h)) * 3600
        self.max_total_bytes = max(1, int(max_total_mb)) * 1024 * 1024

    def ensure_directory(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory

    def path_for(self, file_ref: str) -> Path:
        return self.directory / validate_file_ref(file_ref)

    def has(self, file_ref: str) -> bool:
        try:
            return self.path_for(file_ref).is_file()
        except InvalidFileRef:
            return False

    def size_of(self, file_ref: str) -> int:
        return self.path_for(file_ref).stat().st_size

    def partial_path_for(self, file_ref: str) -> Path:
        """Where a download is written before it is known to be complete."""
        self.ensure_directory()
        return self.directory / f"{validate_file_ref(file_ref)}{PARTIAL_SUFFIX}"

    def commit(self, partial_path: str | Path, file_ref: str) -> Path:
        """Move a verified download into place atomically.

        Downloading straight into the cache would leave a truncated file looking
        exactly like a delivered one after a power cut mid-transfer, and the next
        `start_print` would send half a part to the printer.
        """
        target = self.path_for(file_ref)
        self.ensure_directory()
        os.replace(Path(partial_path), target)
        return target

    def discard(self, path: str | Path) -> None:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            logger.warning(
                "could not remove a print file",
                extra={"action": "print_file_cache", "error": str(exc)},
            )

    def prune(self, *, now: float | None = None) -> PruneReport:
        """Enforce the age bound first, then the size bound, oldest first."""
        report = PruneReport()
        if not self.directory.is_dir():
            return report
        now = time.time() if now is None else now

        entries: list[tuple[float, int, Path]] = []
        for path in self.directory.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover - vanished between listing and stat
                continue
            age = now - stat.st_mtime
            if path.name.endswith(PARTIAL_SUFFIX):
                # Leftovers of a download that died; nothing will ever claim them.
                if age > PARTIAL_MAX_AGE_S:
                    self._remove(path, stat.st_size, report)
                continue
            if age > self.max_age_s:
                self._remove(path, stat.st_size, report)
                continue
            entries.append((stat.st_mtime, stat.st_size, path))

        total = sum(size for _mtime, size, _path in entries)
        for _mtime, size, path in sorted(entries):
            if total <= self.max_total_bytes:
                break
            self._remove(path, size, report)
            total -= size
        return report

    def _remove(self, path: Path, size: int, report: PruneReport) -> None:
        try:
            path.unlink()
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            logger.warning(
                "could not prune a print file",
                extra={"action": "print_file_cache", "error": str(exc)},
            )
            return
        report.removed += 1
        report.freed_bytes += size
