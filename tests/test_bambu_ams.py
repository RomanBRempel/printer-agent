"""The feeding system in a snapshot, and flags that mean what they say.

The hub compares `state.ams.slots[]` against the filaments a print file asks for,
so an empty slot reported as absent, or a colour reported as the printer's raw
`RRGGBBAA`, both turn into a wrong answer for the operator.
"""

from __future__ import annotations

from printer_agent.adapters.bambu import BambuAdapter, bambu_ams_slots
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
    # Neither is implemented for this brand yet.
    assert capabilities.upload is False
    assert capabilities.camera is False
    assert capabilities.pause is True
