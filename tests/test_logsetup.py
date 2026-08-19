"""Журнал: десять дней истории и потолок, за который он не вырастет.

Две крайности одинаково плохи. Ротация только по размеру меряет историю
байтами: одна шумная неделя стирает спокойный месяц перед собой, и на вопрос
«что изменилось во вторник» ответить нечем. Ротация только по времени ничем не
ограничивает плохой день — цикл переподключений пишет со скоростью опроса.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from printer_agent import logsetup
from printer_agent.logsetup import DailyLogFile


def record(message: str = "x" * 512) -> logging.LogRecord:
    return logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0, msg=message, args=None, exc_info=None
    )


@pytest.fixture
def handler(tmp_path: Path):
    h = DailyLogFile(tmp_path / "agent.log")
    try:
        yield h
    finally:
        h.close()


def age(path: Path, days: float) -> None:
    old = time.time() - days * 86400
    import os

    os.utime(path, (old, old))


def test_a_roll_older_than_the_retention_goes(handler, tmp_path: Path) -> None:
    stale = tmp_path / "agent.log.2020-01-01"
    stale.write_text("old", encoding="utf-8")
    age(stale, logsetup.LOG_RETENTION_DAYS + 1)

    assert str(stale) in handler.getFilesToDelete()


def test_a_roll_inside_the_retention_stays(handler, tmp_path: Path) -> None:
    """Десять дней — это обещание, а не пожелание."""
    fresh = tmp_path / "agent.log.2026-08-18"
    fresh.write_text("recent", encoding="utf-8")
    age(fresh, logsetup.LOG_RETENTION_DAYS - 1)

    assert handler.getFilesToDelete() == []


def test_the_live_file_is_never_deleted(handler, tmp_path: Path) -> None:
    """Он подходит под тот же шаблон, и удалить его — потерять текущий журнал."""
    live = Path(handler.baseFilename)
    live.write_text("now", encoding="utf-8")
    age(live, logsetup.LOG_RETENTION_DAYS + 5)

    assert str(live) not in handler.getFilesToDelete()


def test_the_budget_prunes_inside_the_retention_too(handler, tmp_path: Path, monkeypatch) -> None:
    """Возраст сам по себе размер не ограничивает.

    Журнал, забивший диск, уносит с собой службу — поэтому при переполнении
    старое уходит, даже если оно моложе десяти дней.
    """
    monkeypatch.setattr(logsetup, "LOG_TOTAL_BUDGET_BYTES", 1000)
    for day, size in ((3, 800), (2, 800), (1, 800)):
        path = tmp_path / f"agent.log.2026-08-1{day}"
        path.write_bytes(b"x" * size)
        age(path, day)

    doomed = handler.getFilesToDelete()

    # Уходит самое старое, и ровно столько, чтобы уложиться в бюджет.
    assert [Path(p).name for p in doomed] == ["agent.log.2026-08-13", "agent.log.2026-08-12"]


def test_a_second_roll_in_one_day_does_not_erase_the_first(handler, tmp_path: Path) -> None:
    """Иначе утро плохого дня стирается его же вечером.

    Штатный обработчик называет ротацию датой и удаляет одноимённый файл перед
    переименованием — а плохой день ротируется по размеру не один раз.
    """
    first = tmp_path / "agent.log.2026-08-19"
    first.write_text("morning", encoding="utf-8")

    second = handler.rotation_filename(str(first))

    assert Path(second) != first
    assert not Path(second).exists()


def test_size_rolls_the_file_before_midnight(tmp_path: Path, monkeypatch) -> None:
    """День — не ограниченный объём: цикл переподключений пишет непрерывно."""
    monkeypatch.setattr(logsetup, "MAX_LOG_BYTES", 2048)
    h = DailyLogFile(tmp_path / "agent.log")
    try:
        assert h.shouldRollover(record()) == 0
        for _ in range(8):
            h.emit(record())
        assert h.shouldRollover(record()) == 1
    finally:
        h.close()


def test_configure_logging_survives_an_unwritable_directory(monkeypatch, tmp_path: Path) -> None:
    """Неподнятый CLI не должен падать из-за журнала, который некуда писать."""

    def refuse(*_a, **_k):
        raise OSError("denied")

    monkeypatch.setattr(logsetup, "DailyLogFile", refuse)
    logsetup.configure_logging(log_file=tmp_path / "nope" / "agent.log")

    logging.getLogger("t").info("still works")
