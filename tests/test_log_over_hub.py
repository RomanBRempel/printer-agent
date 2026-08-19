"""Чтение журнала агента из хаба.

Каждый разбор на площадке упирался в одно: ответ лежит в `agent.log`, а
`agent.log` — на компьютере, у которого никто не стоит.

Опасных мест здесь два, и оба не про удобство. Имя файла присылает хаб — то есть
это недоверенный путь на нашей машине, ровно как `file_ref` у кэша печати. И
секреты: правило «не писать код доступа в лог» — про код, который есть сегодня,
а вычистка на выходе — про строку, которую кто-нибудь допишет через год.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from printer_agent.core import logtail
from printer_agent.core.logtail import (
    SCRUBBED,
    UnknownLogFile,
    clip_to_budget,
    filter_by_level,
    list_log_files,
    resolve_log_file,
    scrub,
    tail_lines,
)


def write_log(path: Path, count: int, level: str = "INFO") -> Path:
    path.write_text(
        "".join(f"ts=2026-08-19 12:00:{i:02d} level={level} message=line-{i}\n" for i in range(count)),
        encoding="utf-8",
    )
    return path


# ── Имя файла — граница ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["../agent.yaml", "..\\agent.yaml", "sub/agent.log", "/etc/passwd", "", "."],
)
def test_a_name_that_leaves_the_directory_is_refused(tmp_path: Path, name: str) -> None:
    """Хаб называет путь на чужой машине; проверка — по итоговому пути.

    Искать в строке `..` значит перечислять трюки одной операционной системы.
    Сравнение родителя после разрешения не требует их перечислять.
    """
    write_log(tmp_path / "agent.log", 3)

    with pytest.raises(UnknownLogFile):
        resolve_log_file(tmp_path, name)


def test_a_real_file_in_the_directory_is_served(tmp_path: Path) -> None:
    roll = write_log(tmp_path / "agent.log.2026-08-18", 3)

    assert resolve_log_file(tmp_path, "agent.log.2026-08-18") == roll.resolve()


def test_a_name_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UnknownLogFile):
        resolve_log_file(tmp_path, "agent.log.1999-01-01")


# ── Секреты ─────────────────────────────────────────────────────────────────


def test_a_secret_is_removed_wherever_it_appears() -> None:
    """По значению, а не по имени поля: имена ловят только то, что предусмотрели."""
    lines = [
        "ts=1 level=INFO message=connected access_code=8652d7bd",
        "ts=2 level=ERROR message=FTPS login failed for bblp:8652d7bd on 10.13.0.132",
    ]

    cleaned = scrub(lines, ["8652d7bd", "hub-token-value"])

    assert all("8652d7bd" not in line for line in cleaned)
    assert cleaned[0].endswith(f"access_code={SCRUBBED}")


def test_a_removed_secret_is_distinguishable_from_an_empty_field() -> None:
    """«Тут был секрет» и «поле пустое» — разные неисправности."""
    assert scrub(["access_code=8652d7bd"], ["8652d7bd"]) == [f"access_code={SCRUBBED}"]


def test_a_very_short_secret_is_not_used_as_a_needle() -> None:
    """Двухсимвольный «секрет» вычистил бы половину файла."""
    line = "ts=1 level=INFO message=printer on port 22 is idle"

    assert scrub([line], ["22", ""]) == [line]


# ── Объём ───────────────────────────────────────────────────────────────────


def test_the_tail_is_the_end_of_the_file(tmp_path: Path) -> None:
    path = write_log(tmp_path / "agent.log", 50)

    lines, truncated = tail_lines(path, 5)

    assert [line.split("message=")[1] for line in lines] == [f"line-{i}" for i in range(45, 50)]
    assert truncated is True


def test_a_short_file_comes_back_whole_and_untruncated(tmp_path: Path) -> None:
    path = write_log(tmp_path / "agent.log", 3)

    lines, truncated = tail_lines(path, 100)

    assert len(lines) == 3
    assert truncated is False


def test_a_partial_line_at_the_top_is_dropped(tmp_path: Path, monkeypatch) -> None:
    """Половина строки наверху читается как порча файла, а не как граница."""
    monkeypatch.setattr(logtail, "_CHUNK_BYTES", 64)
    path = write_log(tmp_path / "agent.log", 40)

    lines, _ = tail_lines(path, 5)

    assert all(line.startswith("ts=") for line in lines)


def test_the_agent_will_not_send_more_than_the_ceiling(tmp_path: Path) -> None:
    """Сессия та же, что у телеметрии и команд; журнал — мегабайты."""
    path = write_log(tmp_path / "agent.log", 5000)

    lines, truncated = tail_lines(path, 10_000)

    assert len(lines) == logtail.MAX_TAIL_LINES
    assert truncated is True


def test_the_byte_budget_drops_from_the_top(monkeypatch) -> None:
    """Сверху, потому что спрашивают про свежие строки."""
    monkeypatch.setattr(logtail, "MAX_TAIL_BYTES", 40)
    lines = [f"line-{i}-{'x' * 10}" for i in range(10)]

    kept, clipped = clip_to_budget(lines)

    assert clipped is True
    assert kept == lines[-len(kept):]


# ── Фильтр по уровню ────────────────────────────────────────────────────────


def test_the_level_filter_keeps_that_level_and_worse() -> None:
    lines = [
        "ts=1 level=DEBUG message=a",
        "ts=2 level=INFO message=b",
        "ts=3 level=WARNING message=c",
        "ts=4 level=ERROR message=d",
    ]

    assert [l[-1] for l in filter_by_level(lines, "WARNING")] == ["c", "d"]


def test_a_line_without_a_level_survives_the_filter() -> None:
    """Тело трассировки уровня не несёт, а исключение без тела бесполезно."""
    lines = ["ts=1 level=ERROR message=boom", "  File \"x.py\", line 3, in f", "    raise"]

    assert filter_by_level(lines, "ERROR") == lines


def test_an_unknown_level_filters_nothing() -> None:
    """Опечатка в запросе не должна выглядеть как пустой журнал."""
    lines = ["ts=1 level=INFO message=a"]

    assert filter_by_level(lines, "LOUD") == lines


# ── Список файлов ───────────────────────────────────────────────────────────


def test_the_files_are_listed_newest_first(tmp_path: Path) -> None:
    """Чтобы хаб мог предложить вчерашний файл, не угадывая его имя."""
    import os
    import time

    old = write_log(tmp_path / "agent.log.2026-08-17", 1)
    new = write_log(tmp_path / "agent.log", 1)
    os.utime(old, (time.time() - 86400, time.time() - 86400))

    assert [f.name for f in list_log_files(tmp_path)] == [new.name, old.name]


def test_listing_a_directory_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    assert list_log_files(tmp_path / "nope") == []


# ── Сквозь провод ───────────────────────────────────────────────────────────


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


@pytest.fixture
def hub(tmp_path: Path, monkeypatch):
    from printer_agent.config import AgentConfig, OutboxConfig, PrinterConfig
    from printer_agent.core.outbox import EventOutbox
    from printer_agent.uplink import connection as connection_module
    from printer_agent.uplink.connection import HubConnection

    logs = tmp_path / "logs"
    logs.mkdir()
    write_log(logs / "agent.log", 5)
    (logs / "agent.log").write_text(
        "ts=1 level=INFO message=hello agent_token=super-secret-token\n"
        "ts=2 level=WARNING message=printer refused access_code=8652d7bd\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(connection_module, "active_log_path", lambda: logs / "agent.log")

    config = AgentConfig(
        hub_url="https://hub.example.com/api/printers/agent",
        agent_token="super-secret-token",
        location_key="loc-1",
        outbox=OutboxConfig(database_path=tmp_path / "outbox.sqlite3"),
        printers=[
            PrinterConfig(key="a1", brand="bambu", host="10.0.0.5",
                          credentials={"access_code": "8652d7bd", "serial": "0309ABC"}),
        ],
    )
    outbox = EventOutbox(config.outbox.database_path)
    try:
        yield HubConnection(config, outbox), logs
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_a_log_request_is_answered_with_a_scrubbed_tail(hub) -> None:
    from printer_agent.contracts import build_envelope

    connection, _logs = hub
    ws = FakeWebSocket()

    await connection._handle_message(ws, build_envelope("log_request", {"lines": 10}))

    payload = ws.sent[0]["payload"]
    assert ws.sent[0]["type"] == "log"
    assert payload["file"] == "agent.log"
    assert payload["request_msg_id"]
    assert len(payload["lines"]) == 2
    body = "\n".join(payload["lines"])
    assert "super-secret-token" not in body
    assert "8652d7bd" not in body
    assert SCRUBBED in body
    assert [f["name"] for f in payload["files"]] == ["agent.log"]


@pytest.mark.asyncio
async def test_a_request_for_a_file_outside_the_directory_is_refused(hub) -> None:
    """И отвечает объяснением, а не молчанием: молчание читается как обрыв."""
    from printer_agent.contracts import build_envelope

    connection, _logs = hub
    ws = FakeWebSocket()

    await connection._handle_message(
        ws, build_envelope("log_request", {"file": "../agent.yaml"})
    )

    payload = ws.sent[0]["payload"]
    assert payload["error"]
    assert "lines" not in payload


@pytest.mark.asyncio
async def test_the_level_filter_runs_before_the_count(hub) -> None:
    """Иначе на болтливом агенте «200 строк уровня WARNING» — почти пустой ответ."""
    from printer_agent.contracts import build_envelope

    connection, logs = hub
    (logs / "agent.log").write_text(
        "".join(f"ts={i} level=INFO message=noise-{i}\n" for i in range(500))
        + "ts=999 level=ERROR message=the one that matters\n",
        encoding="utf-8",
    )
    ws = FakeWebSocket()

    await connection._handle_message(
        ws, build_envelope("log_request", {"lines": 5, "level": "WARNING"})
    )

    lines = ws.sent[0]["payload"]["lines"]
    assert lines == ["ts=999 level=ERROR message=the one that matters"]
