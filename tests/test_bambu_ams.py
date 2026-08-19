"""The feeding system in a snapshot, and flags that mean what they say.

The hub compares `state.ams.slots[]` against the filaments a print file asks for,
so an empty slot reported as absent, or a colour reported as the printer's raw
`RRGGBBAA`, both turn into a wrong answer for the operator.
"""

from __future__ import annotations

from printer_agent.adapters.bambu import BambuAdapter, bambu_ams_slots, describe_report_shape
from printer_agent.config import PrinterConfig

#: Shape of a pushall `print` report, trimmed to what is read here.
PRINT_STATE = {
    "gcode_state": "RUNNING",
    "mc_percent": 42,
    "ams": {
        "tray_now": "0",
        "ams": [
            {
                "id": "0",
                "humidity": "4",
                "tray": [
                    {"id": "0", "tray_type": "PLA", "tray_color": "000000FF", "remain": 87},
                    {"id": "1", "tray_type": "PETG", "tray_color": "ff6a13ff", "remain": -1},
                    {"id": "2", "tray_type": "", "tray_color": "", "remain": -1},
                ],
            },
            {"id": "1", "tray": [{"id": "0", "tray_type": "ABS", "tray_color": "1A1A1AFF", "remain": 40}]},
        ],
    },
}


def make_adapter() -> BambuAdapter:
    return BambuAdapter(
        PrinterConfig(
            key="p1",
            brand="bambu",
            host="127.0.0.1",
            credentials={"serial": "01P00A000000000", "access_code": "12345678"},
        )
    )


def test_slots_are_numbered_flat_across_units() -> None:
    slots = bambu_ams_slots(PRINT_STATE)

    assert [slot.index for slot in slots] == [0, 1, 2, 4]
    assert [slot.material for slot in slots] == ["PLA", "PETG", None, "ABS"]


def test_colours_become_hex_and_unknown_values_are_dropped() -> None:
    slots = {slot.index: slot for slot in bambu_ams_slots(PRINT_STATE)}

    assert slots[0].color == "#000000"
    assert slots[1].color == "#FF6A13"
    # An empty slot is still a slot: reported, with nothing claimed about it.
    assert slots[2].color is None
    assert slots[2].material is None
    # `remain: -1` is the printer saying it cannot tell, not "empty".
    assert slots[0].remaining_pct == 87
    assert slots[1].remaining_pct is None


def test_a_printer_without_a_feeding_system_reports_no_block() -> None:
    assert bambu_ams_slots({"gcode_state": "IDLE"}) == []
    assert bambu_ams_slots({"ams": {}}) == []
    assert bambu_ams_slots({"ams": {"ams": "unexpected"}}) == []


def test_the_snapshot_carries_the_slots_without_nulls() -> None:
    adapter = make_adapter()

    payload = adapter._snapshot_from_state(PRINT_STATE, None, None).to_dict()

    slots = payload["state"]["ams"]["slots"]
    assert slots[0] == {"index": 0, "material": "PLA", "color": "#000000", "remaining_pct": 87.0}
    # Absent values are omitted rather than sent as null, like everywhere else.
    assert slots[2] == {"index": 2}


def test_an_empty_snapshot_state_stays_empty() -> None:
    adapter = make_adapter()

    payload = adapter._snapshot_from_state({"gcode_state": "IDLE"}, None, None).to_dict()

    assert payload["state"] == {}


def test_capabilities_report_what_this_adapter_actually_does() -> None:
    """A flag raised early shows the operator a button that can only refuse."""
    adapter = make_adapter()
    assert adapter.capabilities().ams is False

    adapter._latest_print = dict(PRINT_STATE)
    capabilities = adapter.capabilities()

    assert capabilities.ams is True
    # Upload is implemented and the access code is set, so the flag is up. It
    # follows the access code rather than the brand: FTPS authenticates with it,
    # and a printer configured without one can only refuse the transfer.
    assert capabilities.upload is True
    assert capabilities.camera is False
    assert capabilities.pause is True


# ── Внешняя катушка ─────────────────────────────────────────────────────────


def test_the_external_spool_is_a_slot_like_any_other() -> None:
    """Печать в два цвета с одним из них на держателе — обычное дело.

    `vt_tray` не входит в `ams.ams[]`, поэтому в цикле по юнитам его не видно.
    Хаб сверяет материалы задания с тем, что мы прислали, и без этой записи
    отказывается отправлять файл — печать не начинается, а причина выглядит как
    «не удалось отправить».
    """
    slots = bambu_ams_slots(
        {
            "ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA",
                                                  "tray_color": "FF6910FF", "remain": 100}]}]},
            "vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "C12E1EFF", "remain": 0},
        }
    )

    assert [s.index for s in slots] == [0, 254]
    assert slots[-1].material == "PLA"
    assert slots[-1].color == "#C12E1E"


def test_the_spool_holder_reports_no_remaining_share() -> None:
    """У держателя нет RFID, а принтер шлёт `remain: 0`.

    Как процент это читается «пусто» — для катушки, которая может быть полной.
    """
    slots = bambu_ams_slots({"vt_tray": {"id": "254", "tray_type": "PLA", "remain": 0}})

    assert slots[0].remaining_pct is None


def test_a_printer_without_an_ams_still_reports_its_spool() -> None:
    """A1 без AMS Lite печатает с держателя, и это единственный его материал."""
    slots = bambu_ams_slots({"vt_tray": {"id": "254", "tray_type": "PETG", "tray_color": "A4AAACFF"}})

    assert [(s.index, s.material) for s in slots] == [(254, "PETG")]


def test_an_unnamed_spool_holder_is_still_a_slot() -> None:
    """Незаполненный `tray_type` у держателя — «состав не назван», а не «пусто».

    У держателя нет RFID: материал на экране принтера задаёт человек, и обычно
    не задаёт вовсе. Пока пустое значение читалось как «катушки нет», держатель
    не попадал в снапшот, а вместе с ним пропадала и возможность печатать с
    него: хаб предлагает выбрать из тех мест заправки, о которых ему сказали.
    Ровно так вёл себя H2D — четыре слота AMS в списке и ни одного держателя.

    Ничего о материале при этом не утверждается (`material=None`), поэтому
    автоматика такой слот не выберет — его выбирает человек.
    """
    slots = bambu_ams_slots({"vt_tray": {"id": "254", "tray_type": ""}})

    assert [(s.index, s.material) for s in slots] == [(254, None)]


def test_the_holder_keeps_the_number_the_printer_gave_it() -> None:
    """Ноль — тоже номер, и подменять его на 254 нельзя.

    Номер уезжает обратно в `ams_mapping`, и принтер не узнает тот, который мы
    придумали за него. Отсутствие номера — другое дело: там 254 это известный
    номер держателя, а не догадка.
    """
    assert bambu_ams_slots({"vt_tray": {"id": "0", "tray_type": "PLA"}})[0].index == 0
    assert bambu_ams_slots({"vt_tray": {"tray_type": "PLA"}})[0].index == 254


def test_the_holder_is_found_one_level_deeper_too() -> None:
    """H-серия несёт часть отчёта в `device`, и форма там та же.

    Промах здесь молчит: держателя просто нет в снапшоте — ни ошибки, ни
    записи в журнале, только принтер, с катушки которого нельзя напечатать.
    """
    slots = bambu_ams_slots({"device": {"vt_tray": {"id": "254", "tray_type": "PETG"}}})

    assert [(s.index, s.material) for s in slots] == [(254, "PETG")]

    nested_ams = bambu_ams_slots(
        {"device": {"ams": {"ams": [{"id": "0", "tray": [{"id": "1", "tray_type": "PLA"}]}]}}}
    )
    assert [(s.index, s.material) for s in nested_ams] == [(1, "PLA")]


def test_two_holders_are_two_slots_and_neither_is_renumbered() -> None:
    """У двухсопловой машины держатель на каждый экструдер."""
    slots = bambu_ams_slots(
        {"vt_tray": [{"id": "254", "tray_type": "PLA"}, {"id": "255", "tray_type": "PETG"}]}
    )

    assert [(s.index, s.material) for s in slots] == [(254, "PLA"), (255, "PETG")]


def test_the_same_slot_reported_twice_is_reported_once() -> None:
    """Отчёт может нести обе формы сразу — дубль слота хаб принял бы за второй."""
    slots = bambu_ams_slots(
        {
            "vt_tray": {"id": "254", "tray_type": "PLA"},
            "device": {"vt_tray": {"id": "254", "tray_type": "PLA"}},
        }
    )

    assert [s.index for s in slots] == [254]


def test_the_holder_is_found_inside_the_ams_block_too() -> None:
    """Держатель — не юнит AMS, но лежать рядом с ними он тоже может.

    H2D на 0.1.0a27 не отдал держателя ни сверху, ни из `device`, при том что
    четыре лотка AMS приехали как обычно. Смотреть в известные места дёшево;
    не смотреть — значит оставить принтер, с собственной катушки которого
    напечатать нельзя.
    """
    slots = bambu_ams_slots(
        {
            "ams": {
                "ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PETG"}]}],
                "vt_tray": {"id": "254", "tray_type": ""},
            }
        }
    )

    assert [(s.index, s.material) for s in slots] == [(0, "PETG"), (254, None)]


def test_a_report_without_a_feeding_system_names_what_it_does_carry() -> None:
    """Молчание — та самая неисправность: снимок без состава выглядит нормально.

    Строка уходит в журнал агента, который хаб умеет читать, и отвечает на
    вопрос «куда делся держатель» без похода в чужой репозиторий. Значений в
    ней нет — вопрос про раскладку отчёта, а не про данные принтера.
    """
    shape = describe_report_shape(
        {"gcode_state": "RUNNING", "mc_percent": 42, "device": {"extruder": {}, "vt_slot": {}}}
    )

    assert "print: device, gcode_state, mc_percent" in shape
    assert "device: extruder, vt_slot" in shape
    # Значения не печатаются — только имена ключей.
    assert "42" not in shape


def test_the_missing_holder_is_what_gets_reported(caplog) -> None:
    """Условие — нет ДЕРЖАТЕЛЯ, а не «нет состава вовсе».

    У H2D четыре лотка AMS приехали как обычно, поэтому проверка «состава нет»
    молчала бы ровно на том принтере, ради которого заведена.
    """
    adapter = make_adapter()
    adapter._latest_print = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PETG"}]}]}}

    with caplog.at_level("INFO"):
        adapter._log_missing_feeding_system()

    assert "no external spool holder" in caplog.text

    # А у принтера, который держателя прислал, спрашивать нечего.
    quiet = make_adapter()
    quiet._latest_print = {"vt_tray": {"id": "254", "tray_type": ""}}
    caplog.clear()
    with caplog.at_level("INFO"):
        quiet._log_missing_feeding_system()
    assert caplog.text == ""


def test_the_shape_is_reported_once_per_connection() -> None:
    """Отчёты идут секундами: строка на каждый сделала бы журнал нечитаемым."""
    adapter = make_adapter()
    adapter._latest_print = {"gcode_state": "IDLE"}

    adapter._log_missing_feeding_system()
    assert adapter._feed_shape_logged is True

    # Второй раз — уже молча; сбрасывается только новым подключением.
    adapter._log_missing_feeding_system()
