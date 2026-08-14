from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import agent_log_path

LOG_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s"
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5

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
        handlers.append(
            RotatingFileHandler(
                target, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
            )
        )
    except OSError:
        # An unwritable ProgramData (unelevated CLI run) is not a reason to fail.
        pass

    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
