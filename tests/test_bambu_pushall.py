"""Полный отчёт принтера — тот, в котором есть состав подающей системы.

Заведено по цеховому случаю: A1, час назад показывавший пять мест заправки,
отвечал «принтер не сообщает состав подающей системы» при живом соединении и
идущей телеметрии. Причина не в хабе: `pushall` уходит один раз на подключение,
с qos 0 и без подтверждения, а всё, что принтер шлёт дальше, — изменения, и
состава слотов в них нет. Потерянная публикация (или принтер, занятый в этот
момент другим клиентом) означала, что состав не появится до перезапуска службы.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from printer_agent.adapters import bambu
from printer_agent.adapters.bambu import BambuAdapter
from printer_agent.config import PrinterConfig


def make_adapter() -> BambuAdapter:
    return BambuAdapter(
        PrinterConfig(
            key="p1",
            brand="bambu",
            host="127.0.0.1",
            credentials={"serial": "01P00A000000000", "access_code": "12345678"},
        )
    )


class RecordingClient:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.published.append({"topic": topic, "payload": json.loads(payload), "qos": qos})


def test_a_report_without_slots_is_a_report_we_have_not_received() -> None:
    """У любой Bambu есть хотя бы внешний держатель.

    Поэтому «слотов нет вовсе» — это не машина без подающей системы, а отчёт,
    который до нас не доехал.
    """
    adapter = make_adapter()
    assert adapter._needs_full_report() is True

    adapter._latest_print = {"gcode_state": "RUNNING", "mc_percent": 42}
    assert adapter._needs_full_report() is True

    adapter._latest_print = {"vt_tray": {"id": "254", "tray_type": ""}}
    assert adapter._needs_full_report() is False


@pytest.mark.asyncio
async def test_the_full_report_is_asked_for_again_until_it_arrives(monkeypatch) -> None:
    """Один запрос на подключение — это одна попытка, а ответ не подтверждается."""
    monkeypatch.setattr(bambu, "BAMBU_PUSHALL_RETRY_S", 0.01)
    monkeypatch.setattr(bambu, "BAMBU_PUSHALL_RESYNC_S", 30.0)
    adapter = make_adapter()
    client = RecordingClient()

    task = asyncio.create_task(adapter._keep_report_full(client, "01P00A000000000"))
    await asyncio.sleep(0.05)
    asked_before = len(client.published)
    # Отчёт доехал — переспрашивать больше незачем, дальше только редкое
    # освежение, до которого этот тест не доживает.
    adapter._latest_print = {"vt_tray": {"id": "254", "tray_type": "PLA"}}
    # Одна уже начатая попытка успевает добежать — это не переспрашивание.
    await asyncio.sleep(0.03)
    settled = len(client.published)
    await asyncio.sleep(0.05)
    adapter._stop_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert asked_before >= 2, "повторных запросов не было"
    assert len(client.published) == settled, "переспрашиваем уже полученное"
    assert all(item["payload"]["pushing"]["command"] == "pushall" for item in client.published)
    # qos 0 — принтер не подтверждает этот ответ, и ожидание PUBACK рвало бы
    # только что оформленную подписку.
    assert all(item["qos"] == 0 for item in client.published)


@pytest.mark.asyncio
async def test_a_failed_request_does_not_take_the_connection_down(monkeypatch) -> None:
    """Публикация — не чтение отчётов: её сбой останавливает только её саму."""
    monkeypatch.setattr(bambu, "BAMBU_PUSHALL_RETRY_S", 0.01)
    adapter = make_adapter()

    class Broken:
        async def publish(self, *_args, **_kwargs):
            raise RuntimeError("broker went away")

    await asyncio.wait_for(adapter._keep_report_full(Broken(), "01P00A000000000"), timeout=1.0)
