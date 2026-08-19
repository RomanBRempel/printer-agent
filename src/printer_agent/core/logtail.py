"""Reading the agent's own log, for a hub that cannot walk up to the machine.

Every diagnosis on a shop floor so far has ended at the same wall: the answer is
in `agent.log`, and `agent.log` is on a computer nobody is standing at. This
module is the smallest thing that removes the wall — a bounded tail, by name,
with the secrets scrubbed.

Three rules shape it, and none of them are about convenience.

**Bounded, always.** The log is megabytes and the session it would travel on is
the same one carrying telemetry and commands. A request for "the log" returns a
tail with a hard ceiling on both lines and bytes, and says when it truncated.

**The name is a boundary, not a parameter.** The hub supplies a file name, which
makes it an untrusted path on this machine — the same reason `validate_file_ref`
exists for the print cache. Only plain names of files that are actually in the
log directory are served: no separators, no traversal, no symlink out.

**Secrets are scrubbed on the way out even though they are never logged.** The
rule against logging an access code is a rule about code that exists today; this
is about the line somebody adds next year. Known secret *values* are replaced
wherever they appear, which no future careless `extra=` can defeat.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: Most lines one answer may carry. A hub asking for more gets this many.
MAX_TAIL_LINES = 2000

#: And a byte ceiling, because a line has no length limit — a traceback or a
#: dumped payload can be enormous, and 2000 of those would stall the session.
MAX_TAIL_BYTES = 256 * 1024

#: What the tail is read through. Reading a 100 MB file to keep its last page
#: should not put 100 MB in memory.
_CHUNK_BYTES = 64 * 1024

#: Levels in the order the format writes them, for "this and worse".
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_LEVEL_IN_LINE = re.compile(r"\blevel=([A-Z]+)")

#: Replaces a scrubbed value. Distinctive on purpose: an operator reading the
#: hub's log view should be able to tell "there was a secret here" from "the
#: field was empty", because those are different faults.
SCRUBBED = "***"


class UnknownLogFile(ValueError):
    """The name does not belong to a file in this agent's log directory."""


@dataclass(slots=True)
class LogFile:
    name: str
    size_bytes: int
    modified: str


def list_log_files(directory: str | Path) -> list[LogFile]:
    """Every log file, newest first, so the hub can offer a choice."""
    from datetime import datetime, timezone

    folder = Path(directory)
    if not folder.is_dir():
        return []
    found = [path for path in folder.iterdir() if path.is_file()]
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        LogFile(
            name=path.name,
            size_bytes=path.stat().st_size,
            modified=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        )
        for path in found
    ]


def resolve_log_file(directory: str | Path, name: str) -> Path:
    """Turn a name from the hub into a path, or refuse it.

    The check is by *identity of the resolved path*, not by inspecting the
    string for `..`: a name can reach outside a directory in more ways than one
    operating system's worth of tricks, and comparing parents afterwards is the
    only version that does not need to enumerate them.
    """
    folder = Path(directory).resolve()
    if not name or name != Path(name).name:
        raise UnknownLogFile(f"unusable log file name {name!r}")
    candidate = (folder / name).resolve()
    if candidate.parent != folder or not candidate.is_file():
        raise UnknownLogFile(f"no log file named {name!r}")
    return candidate


def tail_lines(path: str | Path, limit: int) -> tuple[list[str], bool]:
    """The last `limit` lines, and whether anything was left out.

    Read backwards in chunks rather than with `readlines()`: the live log is the
    one most worth reading and also the largest, and the point of a tail is not
    to pay for the head.
    """
    limit = max(1, min(int(limit), MAX_TAIL_LINES))
    target = Path(path)
    size = target.stat().st_size
    collected: list[str] = []
    with target.open("rb") as handle:
        position = size
        buffer = b""
        while position > 0 and len(collected) <= limit:
            step = min(_CHUNK_BYTES, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
            collected = buffer.split(b"\n")
        # rstrip of the carriage return: the file is written in text mode,
        # so on Windows every line ends CRLF and splitting on the LF leaves
        # the CR behind — a stray character at the end of every line in the
        # hub's log view.
        text = [line.decode("utf-8", "replace").rstrip("\r") for line in buffer.split(b"\n")]
    # A read that stopped mid-line drops that fragment: half a line at the top
    # of the view reads as corruption rather than as a boundary.
    if position > 0 and text:
        text = text[1:]
    text = [line for line in text if line.strip()]
    truncated = len(text) > limit or position > 0
    return text[-limit:], truncated


def filter_by_level(lines: Iterable[str], level: str | None) -> list[str]:
    """Keep lines at `level` or worse; keep everything if it is not a level.

    Unparseable lines stay. A traceback's continuation carries no `level=`, and
    dropping it would hand the reader an exception with no body.
    """
    if not level:
        return list(lines)
    wanted = level.strip().upper()
    if wanted not in _LEVELS:
        return list(lines)
    floor = _LEVELS.index(wanted)
    kept = []
    for line in lines:
        match = _LEVEL_IN_LINE.search(line)
        if match is None or match.group(1) not in _LEVELS:
            kept.append(line)
        elif _LEVELS.index(match.group(1)) >= floor:
            kept.append(line)
    return kept


def scrub(lines: Iterable[str], secrets: Iterable[str]) -> list[str]:
    """Replace known secret values wherever they appear.

    By value and not by field name: a field name only catches the shapes we
    thought of, and this exists precisely for the ones we did not. Short strings
    are skipped — a two-character "secret" would blank half the file.
    """
    real = sorted({str(s) for s in secrets if s and len(str(s)) >= 6}, key=len, reverse=True)
    if not real:
        return list(lines)
    scrubbed = []
    for line in lines:
        for secret in real:
            line = line.replace(secret, SCRUBBED)
        scrubbed.append(line)
    return scrubbed


def clip_to_budget(lines: list[str]) -> tuple[list[str], bool]:
    """Drop from the top until the answer fits :data:`MAX_TAIL_BYTES`.

    From the top because the newest lines are the ones being asked about.
    """
    total = 0
    kept: list[str] = []
    for line in reversed(lines):
        total += len(line.encode("utf-8")) + 1
        if total > MAX_TAIL_BYTES:
            return list(reversed(kept)), True
        kept.append(line)
    return list(reversed(kept)), False
