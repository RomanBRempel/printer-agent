"""Температура сопла у двухсопловой машины.

H2D печатал PETG, а система показывала 140° — температуру ВТОРОГО, простаивающего
сопла: плоское `nozzle_temper` называет одно фиксированное сопло, а печатать может
другое. Ошибка молчаливая и опасная в обе стороны — по такому показанию не отличить
и остывшее сопло от рабочего.

Раскладка полей взята у BambuStudio (`DevExtruderSystem.cpp`), не угадана.
"""

from __future__ import annotations

from printer_agent.adapters.bambu import active_nozzle_temps


def _dual(active: int, temps: dict[int, tuple[int, int]]) -> dict:
    """Отчёт H2D: два сопла, работает `active`."""
    return {
        "device": {
            "extruder": {
                # биты 0-3 — сколько сопел, биты 4-7 — какое работает
                "state": (active << 4) | 2,
                "info": [
                    {"id": i, "temp": (target << 16) | current}
                    for i, (current, target) in temps.items()
                ],
            }
        },
        "nozzle_temper": 140.0,
        "nozzle_target_temper": 0.0,
    }


def test_the_printing_nozzle_is_the_one_reported():
    """Ровно тот случай из цеха: печатает левое, плоское поле называет правое."""
    report = _dual(active=1, temps={0: (140, 0), 1: (250, 255)})

    assert active_nozzle_temps(report) == (250.0, 255.0)


def test_the_other_nozzle_does_not_leak_in_when_the_first_one_prints():
    report = _dual(active=0, temps={0: (250, 255), 1: (140, 0)})

    assert active_nozzle_temps(report) == (250.0, 255.0)


def test_a_single_nozzle_report_needs_no_active_index():
    """Одно описание — выбирать не из чего, и отказ здесь был бы придирчивостью."""
    report = {"device": {"extruder": {"info": [{"id": 0, "temp": (255 << 16) | 250}]}}}

    assert active_nozzle_temps(report) == (250.0, 255.0)


def test_two_nozzles_without_an_active_index_are_not_guessed():
    """Выбранное наугад сопло хуже честного отката к прежнему полю."""
    report = _dual(active=0, temps={0: (250, 255), 1: (140, 0)})
    report["device"]["extruder"].pop("state")

    assert active_nozzle_temps(report) is None


def test_a_hex_state_reads_the_same():
    """Прошивка шлёт разрядные поля то числом, то строкой — как у `ams.info`."""
    report = _dual(active=1, temps={0: (140, 0), 1: (250, 255)})
    report["device"]["extruder"]["state"] = hex(report["device"]["extruder"]["state"])

    assert active_nozzle_temps(report) == (250.0, 255.0)


def test_a_printer_that_says_nothing_about_separate_nozzles_falls_back():
    """P1/X1/A1 и прошивки постарше обязаны работать как раньше."""
    assert active_nozzle_temps({"nozzle_temper": 24.0}) is None
    assert active_nozzle_temps({"device": {"extruder": {"info": []}}}) is None
    assert active_nozzle_temps({"device": {"extruder": {"info": [{"id": 0}]}}}) is None


def test_the_snapshot_carries_the_printing_nozzle():
    """Правило обязано доезжать до снимка, а не жить отдельной функцией."""
    from printer_agent.adapters.bambu import BambuAdapter
    from printer_agent.config import PrinterConfig

    adapter = BambuAdapter(
        PrinterConfig(
            key="h2d",
            brand="bambu",
            host="10.0.0.5",
            credentials={"access_code": "12345678", "serial": "0309ABCDEF"},
        )
    )
    report = _dual(active=1, temps={0: (140, 0), 1: (250, 255)}) | {
        "gcode_state": "RUNNING"
    }

    snapshot = adapter._snapshot_from_state(report, None, None)

    assert snapshot.temps.nozzle == 250.0
    assert snapshot.temps.nozzle_target == 255.0
