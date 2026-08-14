"""Creality vendor protocol: status mapping and snapshot building. No sockets.

The state frames below are captured verbatim from live printers (K1C mid-print,
K1 just finished, K2 Plus idle), trimmed to the fields the adapter reads.
"""

from __future__ import annotations

import pytest

from printer_agent.adapters.creality import (
    CREALITY_WS_PORT,
    CrealityAdapter,
    creality_discovery_record,
    normalize_creality_status,
)
from printer_agent.config import PrinterConfig
from printer_agent.contracts import JobStatus, PrinterStatus

PRINTING_FRAME = {
    "state": 1,
    "deviceState": 1,
    "hostname": "K1C-B24E",
    "model": "K1C",
    "printFileName": "/usr/data/printer_data/gcodes/камера_ascend_екне_PETG_9h35m.gcode",
    "printProgress": 81,
    "layer": 189,
    "TotalLayer": 354,
    "printJobTime": 27880,
    "printLeftTime": 4650,
    "nozzleTemp": "249.990000",
    "targetNozzleTemp": 250,
    "bedTemp0": "70.080000",
    "targetBedTemp0": 70,
    "boxTemp": 39,
    "cfsConnect": 0,
    "err": {"errcode": 0, "key": 0, "value": ""},
}

IDLE_FRAME = {
    "state": 0,
    "deviceState": 0,
    "hostname": "K2Plus-0EC5",
    "model": "F008",
    "printFileName": "/mnt/UDISK/printer_data/gcodes/top hatch.STEP_PETG_2h57m47s.gcode",
    "printProgress": 100,
    "cfsConnect": 1,
    "err": {"errcode": 0, "key": 0, "value": ""},
}


def build(frame: dict) -> CrealityAdapter:
    adapter = CrealityAdapter(PrinterConfig(key="k1c", brand="creality", host="10.0.0.5"))
    adapter._state = dict(frame)
    adapter._connected = True
    return adapter


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, PrinterStatus.idle),
        (1, PrinterStatus.printing),
        (2, PrinterStatus.finished),
        (3, PrinterStatus.error),
        (4, PrinterStatus.finished),
        (5, PrinterStatus.paused),
    ],
)
def test_status_map_follows_the_firmware_status_list(raw, expected) -> None:
    """Indexes into the web UI's own list, not a guess: 4 is "print abort"."""
    assert normalize_creality_status(raw) == expected


def test_unknown_and_missing_states_fall_back_to_maintenance() -> None:
    assert normalize_creality_status(99) == PrinterStatus.maintenance
    assert normalize_creality_status(None) == PrinterStatus.maintenance
    assert normalize_creality_status("nonsense") == PrinterStatus.maintenance


@pytest.mark.asyncio
async def test_snapshot_from_a_printing_frame() -> None:
    snapshot = await build(PRINTING_FRAME).get_state()

    assert snapshot.status == PrinterStatus.printing
    assert snapshot.status_raw == "state=1;deviceState=1"
    assert snapshot.job.name == "камера_ascend_екне_PETG_9h35m.gcode"
    assert snapshot.job.progress_pct == 81.0
    assert (snapshot.job.layer, snapshot.job.layers_total) == (189, 354)
    assert snapshot.job.time_remaining_s == 4650
    assert snapshot.job.status == JobStatus.printing
    assert snapshot.temps.nozzle == pytest.approx(249.99)
    assert snapshot.temps.bed_target == 70.0
    assert snapshot.temps.chamber == 39.0
    assert snapshot.error.code is None


@pytest.mark.asyncio
async def test_idle_frame_reports_no_job_status() -> None:
    """A finished job's leftovers stay readable, but idle is not a job state."""
    snapshot = await build(IDLE_FRAME).get_state()

    assert snapshot.status == PrinterStatus.idle
    assert snapshot.job.status is None
    assert snapshot.job.name == "top hatch.STEP_PETG_2h57m47s.gcode"


@pytest.mark.asyncio
async def test_aborted_print_is_a_finished_printer_with_a_cancelled_job() -> None:
    snapshot = await build({**PRINTING_FRAME, "state": 4}).get_state()

    assert snapshot.status == PrinterStatus.finished
    assert snapshot.job.status == JobStatus.cancelled


@pytest.mark.asyncio
async def test_error_code_is_carried_with_its_message() -> None:
    frame = {**PRINTING_FRAME, "state": 3, "err": {"errcode": 2001, "key": 7, "value": "heater fault"}}

    snapshot = await build(frame).get_state()

    assert snapshot.status == PrinterStatus.error
    assert (snapshot.error.code, snapshot.error.message) == ("2001", "heater fault")


@pytest.mark.asyncio
async def test_error_without_a_message_still_names_the_code() -> None:
    frame = {**PRINTING_FRAME, "err": {"errcode": 2001, "key": 7, "value": ""}}

    snapshot = await build(frame).get_state()

    assert snapshot.error.message == "Creality error 2001 (key 7)"


@pytest.mark.asyncio
async def test_empty_cache_reports_offline_with_the_reason() -> None:
    adapter = CrealityAdapter(PrinterConfig(key="k1c", brand="creality", host="10.0.0.5"))
    adapter._last_error = "Cannot connect to host 10.0.0.5:9999"

    snapshot = await adapter.get_state()

    assert snapshot.status == PrinterStatus.offline
    assert snapshot.error.message == "Cannot connect to host 10.0.0.5:9999"


@pytest.mark.asyncio
async def test_a_dropped_session_reports_offline_not_the_last_known_state() -> None:
    """A printer switched off mid-print must stop reporting `printing`."""
    adapter = build(PRINTING_FRAME)
    adapter._connected = False
    adapter._last_error = "Printer closed the session (CLOSED)"

    snapshot = await adapter.get_state()

    assert snapshot.status == PrinterStatus.offline
    assert snapshot.error.message == "Printer closed the session (CLOSED)"


@pytest.mark.asyncio
async def test_offline_without_a_recorded_reason_still_names_the_endpoint() -> None:
    adapter = CrealityAdapter(PrinterConfig(key="k1c", brand="creality", host="10.0.0.5"))

    snapshot = await adapter.get_state()

    assert snapshot.error.message == "No session with ws://10.0.0.5:9999"


def test_cfs_capability_follows_the_printer() -> None:
    assert build(IDLE_FRAME).capabilities().cfs is True
    assert build(PRINTING_FRAME).capabilities().cfs is False


def test_pause_resume_cancel_are_advertised_upload_is_not() -> None:
    capabilities = build(PRINTING_FRAME).capabilities()

    assert (capabilities.pause, capabilities.resume, capabilities.cancel) == (True, True, True)
    assert (capabilities.upload, capabilities.camera) == (False, False)


@pytest.mark.asyncio
async def test_commands_send_the_firmware_message_shapes() -> None:
    sent: list[str] = []

    class FakeSocket:
        closed = False

        async def send_str(self, data: str) -> None:
            sent.append(data)

    adapter = build(PRINTING_FRAME)
    adapter._websocket = FakeSocket()

    await adapter.pause()
    await adapter.resume()
    await adapter.cancel()

    assert sent == [
        '{"method": "set", "params": {"pause": 1}}',
        '{"method": "set", "params": {"pause": 0}}',
        '{"method": "set", "params": {"stop": 1}}',
    ]


@pytest.mark.asyncio
async def test_a_command_without_a_connection_fails_loudly() -> None:
    """Silently dropping a pause would report `done` on a printer that never got it."""
    with pytest.raises(RuntimeError):
        await build(PRINTING_FRAME).pause()


def test_discovery_record_uses_the_printer_hostname_and_model() -> None:
    record = creality_discovery_record(PRINTING_FRAME, "10.0.0.5")

    assert record == {
        "brand": "creality",
        "host": "10.0.0.5",
        "port": CREALITY_WS_PORT,
        "name": "K1C-B24E",
        "model": "K1C",
        "serial": "",
        "source": "ws",
    }


def test_discovery_record_falls_back_to_the_address() -> None:
    record = creality_discovery_record({"state": 0}, "10.0.0.5")

    assert record["name"] == "10.0.0.5"
    assert record["model"] == ""
