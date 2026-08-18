"""Что агент говорит про Bambu, когда с ним что-то не так.

Оба правила здесь про одно: молчание принтера не должно выглядеть как норма.

Кэш MQTT — это всё состояние Bambu у агента, и сам он не протухает: принтер
шлёт отчёты по изменению, поэтому машина, которую выдернули из розетки, просто
перестаёт что-либо говорить. Без явного признака устаревания адаптер продолжал
отдавать последний отчёт бесконечно — хаб показывал свободный принтер с
температурой сопла, и единственным признаком беды было то, что ничего не
менялось.

Второе: код ошибки должен совпадать с тем, что оператор видит на экране
принтера. `83902467` не находится нигде, `0500-4003` находится сразу.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from printer_agent.adapters.bambu import (
    BAMBU_OFFLINE_GRACE_S,
    BAMBU_USER_CANCELLED,
    BambuAdapter,
)
from printer_agent.config import PrinterConfig
from printer_agent.contracts import PrinterStatus


def make_adapter() -> BambuAdapter:
    return BambuAdapter(
        PrinterConfig(
            key="a1-mini-sancho",
            brand="bambu",
            host="10.13.0.126",
            credentials={"access_code": "12345678", "serial": "0309ABCDEF"},
        )
    )


def connected_with_a_report(adapter: BambuAdapter) -> None:
    """Приводит адаптер в состояние «принтер на связи и отчитался»."""
    adapter._set_connected(True)
    adapter._latest_print = {"gcode_state": "IDLE", "nozzle_temper": 24.0}
    adapter._latest_snapshot = None


# ── Пропавший принтер ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_connected_printer_reports_what_it_said(monkeypatch) -> None:
    monkeypatch.setattr(BambuAdapter, "_schedule_camera_probe", lambda self: None)
    adapter = make_adapter()
    connected_with_a_report(adapter)

    snapshot = await adapter.get_state()

    assert snapshot.status is PrinterStatus.idle
    assert snapshot.temps.nozzle == 24.0


@pytest.mark.asyncio
async def test_a_printer_pulled_from_the_wall_goes_offline(monkeypatch) -> None:
    """Иначе хаб бесконечно показывает состояние на момент смерти машины."""
    monkeypatch.setattr(BambuAdapter, "_schedule_camera_probe", lambda self: None)
    adapter = make_adapter()
    connected_with_a_report(adapter)

    adapter._set_connected(False)
    adapter._last_error = "Connection lost"
    adapter._offline_since = time.monotonic() - BAMBU_OFFLINE_GRACE_S - 1

    snapshot = await adapter.get_state()

    assert snapshot.status is PrinterStatus.offline
    assert snapshot.error.code == "offline"
    assert "Connection lost" in (snapshot.error.message or "")
    # Температуры мёртвой машины не отдаём: их читают как живые.
    assert snapshot.temps.nozzle is None


@pytest.mark.asyncio
async def test_a_reconnect_inside_the_grace_does_not_flap(monkeypatch) -> None:
    """Разрыв на секунду — не событие.

    Каждый переход online↔offline пишет в outbox долговечную строку, а сессия
    отваливается и возвращается сама: Wi-Fi моргнул, брокер выбросил дубль
    client id.
    """
    monkeypatch.setattr(BambuAdapter, "_schedule_camera_probe", lambda self: None)
    adapter = make_adapter()
    connected_with_a_report(adapter)

    adapter._set_connected(False)
    adapter._offline_since = time.monotonic() - 1.0

    assert (await adapter.get_state()).status is PrinterStatus.idle


@pytest.mark.asyncio
async def test_reconnecting_clears_the_clock(monkeypatch) -> None:
    """Иначе второй разрыв отсчитывался бы от первого и срабатывал мгновенно."""
    monkeypatch.setattr(BambuAdapter, "_schedule_camera_probe", lambda self: None)
    adapter = make_adapter()
    connected_with_a_report(adapter)

    adapter._set_connected(False)
    adapter._offline_since = time.monotonic() - BAMBU_OFFLINE_GRACE_S - 1
    adapter._set_connected(True)

    assert adapter._offline_for() == 0.0
    assert (await adapter.get_state()).status is PrinterStatus.idle


@pytest.mark.asyncio
async def test_a_printer_that_never_answered_says_so_differently(monkeypatch) -> None:
    """«Не отвечает с такого-то момента» и «не отвечал никогда» — разные работы."""
    monkeypatch.setattr(BambuAdapter, "_schedule_camera_probe", lambda self: None)
    adapter = make_adapter()

    snapshot = await adapter.get_state()

    assert snapshot.status is PrinterStatus.offline
    assert "stopped answering" not in (snapshot.error.message or "")


# ── Код ошибки ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "printed"),
    [
        (83902467, "0500-4003"),
        (83902466, "0500-4002"),
        (BAMBU_USER_CANCELLED, "0300-400C"),
    ],
)
def test_an_error_code_is_printed_the_way_the_printer_prints_it(raw: int, printed: str) -> None:
    """Оператор стоит у принтера и видит вторую форму, а не первую."""
    assert BambuAdapter._bambu_error_code(raw) == printed


def test_a_confirmed_code_is_explained_and_an_unknown_one_is_not_invented() -> None:
    """Выдуманный перевод хуже голого кода: он отправляет искать не ту поломку."""
    assert "parse" in BambuAdapter._bambu_error_message(83902467)
    assert BambuAdapter._bambu_error_message(83902466) == "Bambu error 0500-4002"


def test_cancelling_a_print_still_reads_as_a_cancellation() -> None:
    """Поведение, которое было до перевода кодов в печатную форму."""
    assert BambuAdapter._bambu_error_message(BAMBU_USER_CANCELLED) == (
        "0300-400C: Print cancelled by user"
    )


def test_the_snapshot_carries_the_printed_code_not_the_integer() -> None:
    adapter = make_adapter()
    state: dict[str, Any] = {"gcode_state": "FAILED", "print_error": 83902467}

    snapshot = adapter._snapshot_from_state(state, None, None)

    assert snapshot.error.code == "0500-4003"
