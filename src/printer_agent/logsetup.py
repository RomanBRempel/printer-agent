from __future__ import annotations

import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .paths import agent_log_path

LOG_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s"

#: How far back the log goes. Ten days covers "it started misbehaving last week"
#: without asking the operator to have noticed at the time.
LOG_RETENTION_DAYS = 10

#: Ceiling on one file. Rotation is by day, but a day is not a bounded amount of
#: logging: a reconnect loop or a printer flapping writes at the speed of the
#: poll, and a single day can outgrow the disk. Rolling early keeps each file
#: readable and, with the budget below, keeps the whole directory bounded.
MAX_LOG_BYTES = 8 * 1024 * 1024

#: Ceiling on everything kept. Age alone does not bound size, and the point of
#: the exercise is a log that cannot grow without limit — so when a noisy period
#: pushes past this, the oldest files go even if they are inside the ten days.
LOG_TOTAL_BUDGET_BYTES = 128 * 1024 * 1024

_RESERVED_RECORD_FIELDS = frozenset(
    vars(logging.LogRecord(name="", level=0, pathname="", lineno=0, msg="", args=None, exc_info=None))
) | {"asctime", "message", "taskName"}


class KeyValueFormatter(logging.Formatter):
    """Appends `extra=...` fields to the line so failures name their cause."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = [
            f"{key}={value}"
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_FIELDS
        ]
        return " ".join([line, *extras])


class DailyLogFile(TimedRotatingFileHandler):
    """One file per day, kept for :data:`LOG_RETENTION_DAYS`, bounded in total.

    The stdlib offers rotation by time *or* by size, and neither alone is what a
    shop-floor log needs. By size only, retention is measured in bytes: a busy
    week erases the quiet fortnight before it, and "what changed last Tuesday"
    is unanswerable. By time only, nothing bounds a bad day — a reconnect loop
    writes at the speed of the poll.

    So both triggers roll the file, and the pruning is by *age* rather than by
    the stdlib's file count: rolling twice in one day must not shorten the ten
    days. A total budget prunes further when even that is too much, oldest
    first, because a log that fills the disk takes the service with it.
    """

    def __init__(self, filename: str | Path, *, encoding: str = "utf-8") -> None:
        super().__init__(
            str(filename), when="midnight", backupCount=0, encoding=encoding, delay=False
        )
        self._directory = Path(filename).parent
        self._stem = Path(filename).name

    def shouldRollover(self, record: logging.LogRecord) -> int:  # noqa: N802 - stdlib name
        if super().shouldRollover(record):
            return 1
        if self.stream is None:  # pragma: no cover - only with delay=True
            return 0
        self.stream.seek(0, 2)
        return 1 if self.stream.tell() >= MAX_LOG_BYTES else 0

    def rotation_filename(self, default_name: str) -> str:  # noqa: N802 - stdlib name
        """Never overwrite an existing roll.

        The stdlib names a roll after the date, and a second roll on the same
        day would land on the same name — which `doRollover` deletes first. That
        is the morning of a bad day being erased by its afternoon.
        """
        candidate = Path(super().rotation_filename(default_name))
        if not candidate.exists():
            return str(candidate)
        for ordinal in range(1, 1000):
            numbered = candidate.with_name(f"{candidate.name}.{ordinal}")
            if not numbered.exists():
                return str(numbered)
        return str(candidate)  # pragma: no cover - a thousand rolls in one day

    def getFilesToDelete(self) -> list[str]:  # noqa: N802 - stdlib name
        """Prune by age first, then by total size. Never the file in use."""
        now = time.time()
        cutoff = now - LOG_RETENTION_DAYS * 86400
        current = Path(self.baseFilename).resolve()
        rolls = [
            path
            for path in self._directory.glob(f"{self._stem}.*")
            if path.is_file() and path.resolve() != current
        ]

        doomed = [path for path in rolls if path.stat().st_mtime < cutoff]
        kept = sorted(
            (path for path in rolls if path not in doomed), key=lambda p: p.stat().st_mtime
        )
        total = sum(path.stat().st_size for path in kept)
        while kept and total > LOG_TOTAL_BUDGET_BYTES:
            oldest = kept.pop(0)
            total -= oldest.stat().st_size
            doomed.append(oldest)
        return [str(path) for path in doomed]


def configure_logging(level: int = logging.INFO, log_file: str | Path | None = None) -> None:
    """Log to stdout when there is one, and always try the rotating log file.

    The Windows service has no console and the desktop app runs under
    pythonw.exe, where ``sys.stdout`` is ``None`` — a StreamHandler on a missing
    stream raises on the first record. The file is what the app's Logs page
    reads, so failing to open it must stay non-fatal.
    """
    formatter = KeyValueFormatter(LOG_FORMAT)
    handlers: list[logging.Handler] = []

    stream = sys.stdout if sys.stdout is not None else sys.stderr
    if stream is not None:
        handlers.append(logging.StreamHandler(stream))

    target = Path(log_file) if log_file else agent_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(DailyLogFile(target))
    except OSError:
        # An unwritable ProgramData (unelevated CLI run) is not a reason to fail.
        pass

    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
