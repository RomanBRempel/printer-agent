"""Following printers to new addresses after DHCP hands them out again.

The field case this guards (loc-1, 2026-09-25): after the router restarted,
every printer at the location came back on a different address — K1C-B24E went
from .125 to .46, the H2D from .158 to .47 — and the agent kept polling the old
ones. Worse than silence is the swap: a neighbour that inherits an address
answers the same protocol, and its telemetry, and the hub's next print, would
go under the wrong printer's key.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

import pytest
import yaml

from printer_agent.adapters.bambu import bambu_certificate_serial
from printer_agent.adapters.moonraker import moonraker_device_ids
from printer_agent.config import (
    PrinterConfig,
    config_from_dict,
    config_to_dict,
    load_config,
    normalize_device_id,
    validate_config,
)
from printer_agent.contracts import AGENT_TO_HUB_TYPES, PrinterSnapshot, PrinterStatus, build_envelope
from printer_agent.core.discovery import DiscoveredPrinter
from printer_agent.core.outbox import EventOutbox
from printer_agent.core.recovery import (
    name_key,
    plan_relocation,
    scan_networks,
    unregistered_devices,
    write_printer_fields,
)
from printer_agent.settings_bundle import apply_remote_settings
from printer_agent.uplink import connection as connection_module
from printer_agent.uplink.connection import IDENTITY_MISMATCH, HubConnection

from test_uplink_connection import FakeAdapter, FakeWebSocket


def printer(key: str, host: str, device_id: str = "", brand: str = "moonraker", **extra: Any) -> PrinterConfig:
    return PrinterConfig(key=key, brand=brand, host=host, device_id=device_id, **extra)


def record(host: str, *device_ids: str, brand: str = "moonraker", name: str = "") -> dict[str, Any]:
    return {"brand": brand, "host": host, "port": 7125, "device_ids": list(device_ids), "name": name or host}


# -- identity ------------------------------------------------------------------


def test_a_mac_has_one_spelling_whatever_the_source() -> None:
    assert normalize_device_id("FC-EE-28-03-B2-4E") == "fc:ee:28:03:b2:4e"
    assert normalize_device_id(" fc:ee:28:03:b2:4e ") == "fc:ee:28:03:b2:4e"
    assert normalize_device_id("K1C-B24E") == "k1c-b24e"
    assert normalize_device_id(None) == ""


def test_a_bambu_is_identified_by_its_serial_not_by_a_learned_field() -> None:
    bambu = printer("h2d", "10.13.0.47", brand="bambu", credentials={"serial": "0948BB5B2400603"})
    assert bambu.identity() == "0948bb5b2400603"


def test_moonraker_identity_is_every_real_mac_it_reports() -> None:
    result = {
        "system_info": {
            "cpu_info": {"serial_number": "0"},
            "network": {
                "wlan0": {"mac_address": "FC:EE:28:0C:0E:C5"},
                "lo": {"mac_address": "00:00:00:00:00:00"},
                "eth0": {},
            },
        }
    }
    assert moonraker_device_ids(result) == {"fc:ee:28:0c:0e:c5"}
    assert moonraker_device_ids({}) == frozenset()


def test_the_bambu_serial_is_the_subject_common_name_not_the_issuer() -> None:
    def common_name(value: bytes, tag: int) -> bytes:
        return bytes((0x06, 0x03, 0x55, 0x04, 0x03, tag, len(value))) + value

    der = b"\x30\x82" + common_name(b"BBL CA", 0x0C) + b"\x17\x0d251126083330Z" + common_name(b"01P09C4B3000267", 0x13)
    assert bambu_certificate_serial(der) == "01P09C4B3000267"
    assert bambu_certificate_serial(b"not a certificate") == ""


# -- config --------------------------------------------------------------------


def base_config(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "hub_url": "https://hub.example.com/api/printers/agent",
        "agent_token": "token",
        "location_key": "loc-1",
        "printers": [{"key": "k1c-b24e", "brand": "creality", "host": "10.13.0.125", "device_id": "K1C-B24E"}],
    }
    data.update(overrides)
    return data


def test_device_id_and_recovery_settings_round_trip() -> None:
    config = config_from_dict(
        base_config(recovery={"after_offline_s": 30, "survey_interval_s": 0, "networks": "10.13.0.0/24"})
    )

    assert config.printers[0].device_id == "k1c-b24e"
    assert config.recovery.enabled is True
    assert config.recovery.after_offline_s == 30
    assert config.recovery.survey_interval_s == 0
    assert config.recovery.networks == ["10.13.0.0/24"]
    again = config_from_dict(config_to_dict(config))
    assert again.printers[0].device_id == "k1c-b24e"
    assert again.recovery == config.recovery


def test_two_entries_for_one_machine_are_refused() -> None:
    entry = {"brand": "creality", "host": "10.13.0.46", "device_id": "K1C-B24E"}
    config = config_from_dict(
        base_config(printers=[{"key": "a", **entry}, {"key": "b", **entry, "host": "10.13.0.48"}])
    )
    assert "printer b: same device as printer a (k1c-b24e)" in validate_config(config)


def test_a_bad_recovery_network_is_named() -> None:
    config = config_from_dict(base_config(recovery={"networks": ["10.13.0.0/33"]}))
    assert "recovery.networks: 10.13.0.0/33 is not an IPv4 network" in validate_config(config)


def test_a_hub_that_does_not_know_device_id_does_not_erase_it() -> None:
    current = config_from_dict(base_config())
    incoming = {"printers": [{"key": "k1c-b24e", "brand": "creality", "host": "10.13.0.125"}]}

    merged, _ = apply_remote_settings(incoming, current)
    assert merged.printers[0].device_id == "k1c-b24e"

    # Pointed elsewhere, the entry may now mean another machine: learn afresh.
    moved = {"printers": [{"key": "k1c-b24e", "brand": "creality", "host": "10.13.0.200"}]}
    merged, _ = apply_remote_settings(moved, current)
    assert merged.printers[0].device_id == ""


# -- planning ------------------------------------------------------------------


def test_a_lost_printer_follows_its_identity() -> None:
    printers = [printer("k1-0942", "10.13.0.126", "fc:ee:28:03:09:42")]
    records = [record("10.13.0.45", "fc:ee:28:03:09:42"), record("10.13.0.44", "fc:ee:28:0c:0e:c5")]

    plan = plan_relocation(printers, {"k1-0942"}, records)

    assert plan.moves == {"k1-0942": "10.13.0.45"}


def test_the_identity_must_come_from_the_protocol_the_printer_uses() -> None:
    """A Creality board answers Moonraker with a MAC and its socket with a
    hostname; a creality entry must not be moved by a Moonraker answer."""
    printers = [printer("ender", "10.13.0.129", "ender-5 max-549b", brand="creality")]
    records = [record("10.13.0.49", "ender-5 max-549b", brand="moonraker")]

    plan = plan_relocation(printers, {"ender"}, records)

    assert plan.moves == {}
    assert plan.not_found == ["ender"]


def test_an_identity_answering_twice_is_not_guessed() -> None:
    printers = [printer("p", "10.0.0.5", "aa:bb:cc:dd:ee:ff")]
    records = [record("10.0.0.6", "aa:bb:cc:dd:ee:ff"), record("10.0.0.7", "aa:bb:cc:dd:ee:ff")]

    plan = plan_relocation(printers, {"p"}, records)

    assert plan.moves == {}
    assert "10.0.0.6, 10.0.0.7" in plan.refused[0]


def test_an_address_held_by_a_working_printer_is_not_taken() -> None:
    printers = [printer("lost", "10.0.0.5", "aa:aa:aa:aa:aa:aa"), printer("fine", "10.0.0.9")]
    records = [record("10.0.0.9", "aa:aa:aa:aa:aa:aa")]

    plan = plan_relocation(printers, {"lost"}, records)

    assert plan.moves == {}
    assert "in use by printer fine" in plan.refused[0]


def test_two_printers_that_swapped_addresses_both_move() -> None:
    printers = [printer("a", "10.0.0.5", "aa:aa:aa:aa:aa:aa"), printer("b", "10.0.0.6", "bb:bb:bb:bb:bb:bb")]
    records = [record("10.0.0.6", "aa:aa:aa:aa:aa:aa"), record("10.0.0.5", "bb:bb:bb:bb:bb:bb")]

    plan = plan_relocation(printers, {"a", "b"}, records)

    assert plan.moves == {"a": "10.0.0.6", "b": "10.0.0.5"}


def test_a_printer_without_identity_is_found_by_the_name_it_was_added_under() -> None:
    """loc-1, 2026-09-25: the router restart came before the agent had ever
    reached its printers, so no identity had been learned — but every key was
    the device's own name, as the discovery dialog makes it."""
    printers = [printer("k1c-b24e", "10.13.0.125", brand="creality")]
    records = [
        record("10.13.0.46", "k1c-b24e", brand="creality", name="K1C-B24E"),
        record("10.13.0.48", "k1-022d", brand="creality", name="K1-022D"),
    ]

    plan = plan_relocation(printers, {"k1c-b24e"}, records)

    assert plan.moves == {"k1c-b24e": "10.13.0.46"}


def test_the_name_rule_is_the_one_the_discovery_dialog_uses() -> None:
    found = DiscoveredPrinter(brand="moonraker", host="10.13.0.44", port=7125, name="K2Plus-0EC5")
    assert found.suggested_key == name_key("K2Plus-0EC5") == "k2plus-0ec5"


def test_a_name_is_no_match_for_a_device_another_entry_owns() -> None:
    printers = [
        printer("k1-0942", "10.13.0.126"),
        printer("other", "10.13.0.200", "fc:ee:28:03:09:42"),
    ]
    records = [record("10.13.0.45", "fc:ee:28:03:09:42", name="K1-0942")]

    plan = plan_relocation(printers, {"k1-0942"}, records)

    assert plan.moves == {} and plan.not_found == ["k1-0942"]


def test_a_key_of_the_operators_own_choosing_is_not_guessed_at() -> None:
    printers = [printer("Ender5", "10.13.0.129", brand="creality")]
    records = [record("10.13.0.49", "ender-5 max-549b", brand="creality", name="Ender-5 Max-549B")]

    plan = plan_relocation(printers, {"Ender5"}, records)

    assert plan.moves == {} and plan.not_found == ["Ender5"]


# -- unregistered devices -------------------------------------------------------


def test_a_device_nobody_configured_is_unregistered_once_per_address() -> None:
    printers = [printer("k2plus-0ec5", "10.13.0.44", "fc:ee:28:0c:0e:c5")]
    records = [
        # the configured one, on both protocols its board speaks
        record("10.13.0.44", "fc:ee:28:0c:0e:c5", name="K2Plus-0EC5"),
        record("10.13.0.44", "k2plus-svr", brand="creality", name="K2Plus-SVR"),
        # a stranger answering two protocols
        record("10.13.0.49", "fc:ee:11:05:54:9b", name="Ender-5"),
        {**record("10.13.0.49", "ender-5 max-549b", brand="creality", name="Ender-5 Max-549B"), "model": "F004"},
        # a Bambu, which says nothing about itself but its serial
        {**record("10.13.0.47", "0948BB5B2400603", brand="bambu", name=""), "port": 8883},
    ]

    devices = unregistered_devices(printers, records)

    assert [device["host"] for device in devices] == ["10.13.0.47", "10.13.0.49"]
    assert devices[0]["protocols"] == [{"brand": "bambu", "port": 8883, "device_id": "0948bb5b2400603"}]
    assert devices[1]["model"] == "F004"
    assert {protocol["brand"] for protocol in devices[1]["protocols"]} == {"creality", "moonraker"}


def test_a_configured_printer_that_moved_is_lost_not_new() -> None:
    printers = [printer("k1-0942", "10.13.0.126", "fc:ee:28:03:09:42")]
    records = [record("10.13.0.45", "fc:ee:28:03:09:42", name="K1-0942")]

    assert unregistered_devices(printers, records) == []


def test_the_search_starts_in_the_subnets_the_printers_were_in() -> None:
    printers = [printer("a", "10.13.0.125"), printer("b", "10.13.0.200"), printer("c", "k1.local")]
    local = [ipaddress.IPv4Network("192.168.1.0/24"), ipaddress.IPv4Network("10.13.0.0/24")]

    assert [str(n) for n in scan_networks(printers, [], local)] == ["10.13.0.0/24"]
    assert scan_networks([printer("c", "k1.local")], [], local) == local
    assert [str(n) for n in scan_networks(printers, ["10.20.0.0/23"], local)] == ["10.20.0.0/23"]


def test_rewriting_a_host_keeps_what_this_agent_does_not_know(tmp_path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        yaml.safe_dump(base_config(future_setting={"x": 1}, printers=[
            {"key": "k1c-b24e", "brand": "creality", "host": "10.13.0.125", "future_field": True}
        ])),
        encoding="utf-8",
    )

    assert write_printer_fields(path, {"k1c-b24e": {"host": "10.13.0.46"}}) == ["k1c-b24e"]
    assert write_printer_fields(path, {"k1c-b24e": {"host": "10.13.0.46"}}) == []

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["future_setting"] == {"x": 1}
    assert data["printers"][0] == {
        "key": "k1c-b24e", "brand": "creality", "host": "10.13.0.46", "future_field": True
    }
    assert not (tmp_path / "agent.yaml.tmp").exists()


# -- the running agent ---------------------------------------------------------


class IdentifiedAdapter(FakeAdapter):
    def __init__(self, printer: PrinterConfig, ids: set[str]):
        super().__init__(printer)
        self.ids = frozenset(ids)
        self.identity_calls = 0

    async def device_ids(self) -> frozenset[str]:
        self.identity_calls += 1
        return self.ids


def write_agent_yaml(tmp_path, printers: list[dict[str, Any]], **extra: Any):
    path = tmp_path / "agent.yaml"
    data = base_config(printers=printers, telemetry_interval_s=1, **extra)
    data["outbox"] = {"database_path": (tmp_path / "outbox.sqlite3").as_posix()}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def agent(tmp_path):
    path = write_agent_yaml(
        tmp_path,
        [{"key": "k1-0942", "brand": "moonraker", "host": "10.13.0.126", "device_id": "fc:ee:28:03:09:42"}],
    )
    config = load_config(path)
    outbox = EventOutbox(config.outbox.database_path)
    hub = HubConnection(config, outbox)
    ws = FakeWebSocket()
    hub._ws = ws
    try:
        yield hub, ws, path
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_a_neighbour_at_the_printers_address_is_reported_offline(agent) -> None:
    hub, _ws, _path = agent
    neighbour = IdentifiedAdapter(hub.config.printers[0], {"fc:ee:28:0c:0e:c5"})
    hub._adapters = {"k1-0942": neighbour}

    [snapshot] = await hub._collect_snapshots()

    assert snapshot.status == PrinterStatus.offline
    assert snapshot.error.code == IDENTITY_MISMATCH
    assert "fc:ee:28:0c:0e:c5" in snapshot.error.message
    assert "k1-0942" in hub._quarantined


@pytest.mark.asyncio
async def test_no_command_reaches_a_device_that_is_not_the_printer(agent) -> None:
    hub, ws, _path = agent
    neighbour = IdentifiedAdapter(hub.config.printers[0], {"fc:ee:28:0c:0e:c5"})
    hub._adapters = {"k1-0942": neighbour}
    await hub._collect_snapshots()

    await hub._handle_message(
        ws, build_envelope("command", {"command_id": "cmd-1", "printer_key": "k1-0942", "action": "pause"})
    )

    result = ws.sent_of_type("command_result")[0]["payload"]
    assert result["status"] == "failed"
    assert "reassigned" in result["error_text"]
    assert neighbour.paused is False


@pytest.mark.asyncio
async def test_the_right_device_is_checked_once_per_appearance(agent) -> None:
    hub, _ws, _path = agent
    adapter = IdentifiedAdapter(hub.config.printers[0], {"fc:ee:28:03:09:42"})
    hub._adapters = {"k1-0942": adapter}

    await hub._collect_snapshots()
    await hub._collect_snapshots()
    assert adapter.identity_calls == 1

    adapter.snapshot = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.offline, status_raw="offline")
    await hub._collect_snapshots()
    adapter.snapshot = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.idle, status_raw="idle")
    [snapshot] = await hub._collect_snapshots()
    assert adapter.identity_calls == 2
    assert snapshot.status == PrinterStatus.idle


@pytest.mark.asyncio
async def test_an_unknown_identity_is_learned_into_the_config_file(tmp_path) -> None:
    path = write_agent_yaml(tmp_path, [{"key": "k1c-b24e", "brand": "creality", "host": "10.13.0.46"}])
    config = load_config(path)
    outbox = EventOutbox(config.outbox.database_path)
    try:
        hub = HubConnection(config, outbox)
        adapter = IdentifiedAdapter(config.printers[0], {"k1c-b24e"})
        hub._adapters = {"k1c-b24e": adapter}

        await hub._collect_snapshots()
        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert saved["printers"][0]["device_id"] == "k1c-b24e"

        # Adopting its own write must not reconnect a working printer.
        await hub._reload_config_if_changed()
        assert hub._adapters["k1c-b24e"] is adapter
    finally:
        outbox.close()


@pytest.mark.asyncio
async def test_a_lost_printer_is_found_and_moved(agent, monkeypatch) -> None:
    hub, ws, path = agent
    hub._adapters = {"k1-0942": IdentifiedAdapter(hub.config.printers[0], set())}
    searched: list[list[str]] = []

    async def fake_find(printers, hosts, **_: Any):
        searched.append([p.key for p in printers])
        return [record("10.13.0.45", "fc:ee:28:03:09:42")]

    monkeypatch.setattr(connection_module, "find_printers", fake_find)
    monkeypatch.setattr(connection_module, "local_ipv4_networks", lambda: [])

    offline = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.offline, status_raw="offline")
    hub._maybe_start_recovery([offline])
    assert hub._recovery_task is None  # not lost yet: just went quiet

    hub._offline_since["k1-0942"] -= hub.config.recovery.after_offline_s
    hub._maybe_start_recovery([offline])
    assert await hub._recovery_task is True

    # Every protocol the agent speaks is probed, not only the lost printer's.
    assert "k1-0942" in searched[0] and searched[0].count("") == 3
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["printers"][0]["host"] == "10.13.0.45"
    [report] = ws.sent_of_type("network_report")
    assert report["payload"]["moved"] == [
        {"printer_key": "k1-0942", "old_host": "10.13.0.126", "host": "10.13.0.45"}
    ]
    assert report["payload"]["lost"] == [] and report["payload"]["unregistered"] == []
    await hub._reload_config_if_changed()
    assert hub._adapters["k1-0942"].printer.host == "10.13.0.45"
    assert ws.sent_of_type("inventory")


@pytest.mark.asyncio
async def test_a_printer_that_stays_missing_is_searched_for_less_often(agent, monkeypatch) -> None:
    hub, _ws, _path = agent

    async def find_nothing(printers, hosts, **_: Any):
        return []

    monkeypatch.setattr(connection_module, "find_printers", find_nothing)
    monkeypatch.setattr(connection_module, "local_ipv4_networks", lambda: [])
    offline = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.offline, status_raw="offline")
    hub._offline_since["k1-0942"] = asyncio.get_running_loop().time() - 3600

    hub._maybe_start_recovery([offline])
    assert await hub._recovery_task is False
    assert hub._recovery_interval == 2 * hub.config.recovery.min_interval_s

    first = hub._recovery_task
    hub._maybe_start_recovery([offline])
    assert hub._recovery_task is first  # too soon for another sweep


# -- telling the hub -----------------------------------------------------------


def test_network_report_is_an_agent_message() -> None:
    assert "network_report" in AGENT_TO_HUB_TYPES


@pytest.mark.asyncio
async def test_the_hub_hears_about_a_printer_that_cannot_be_found(agent, monkeypatch) -> None:
    hub, ws, _path = agent

    async def find_a_stranger(printers, hosts, **_: Any):
        return [{**record("10.13.0.47", "0948BB5B2400603", brand="bambu", name=""), "port": 8883}]

    monkeypatch.setattr(connection_module, "find_printers", find_a_stranger)
    monkeypatch.setattr(connection_module, "local_ipv4_networks", lambda: [])
    offline = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.offline, status_raw="offline")
    hub._maybe_start_recovery([offline])
    hub._offline_since["k1-0942"] -= hub.config.recovery.after_offline_s
    hub._maybe_start_recovery([offline])
    assert await hub._recovery_task is False

    [report] = ws.sent_of_type("network_report")
    payload = report["payload"]
    assert payload["location_key"] == "loc-1"
    assert payload["networks"] == ["10.13.0.0/24"]
    [lost] = payload["lost"]
    assert lost["printer_key"] == "k1-0942"
    assert lost["reason"] == "not_found"
    assert lost["device_id"] == "fc:ee:28:03:09:42"
    assert lost["offline_since"].endswith("Z")
    assert [device["host"] for device in payload["unregistered"]] == ["10.13.0.47"]

    # The same news again is not sent again ...
    await hub._send_network_report()
    assert len(ws.sent_of_type("network_report")) == 1

    # ... but the printer answering again takes it back at once.
    online = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.idle, status_raw="idle")
    await hub._prune_network_report([online])
    latest = ws.sent_of_type("network_report")[-1]["payload"]
    assert latest["lost"] == [] and len(ws.sent_of_type("network_report")) == 2


@pytest.mark.asyncio
async def test_a_survey_runs_with_nothing_lost(agent, monkeypatch) -> None:
    hub, ws, _path = agent
    calls: list[int] = []

    async def find_a_stranger(printers, hosts, **_: Any):
        calls.append(1)
        return [record("10.13.0.49", "fc:ee:11:05:54:9b", name="Ender-5")]

    monkeypatch.setattr(connection_module, "find_printers", find_a_stranger)
    monkeypatch.setattr(connection_module, "local_ipv4_networks", lambda: [])
    online = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.idle, status_raw="idle")

    hub._maybe_start_recovery([online])
    assert hub._recovery_task is None  # the first survey waits after_offline_s
    hub._started_at -= hub.config.recovery.after_offline_s
    hub._maybe_start_recovery([online])
    assert await hub._recovery_task is False
    assert hub._recovery_interval == hub.config.recovery.min_interval_s  # a survey does not back off

    hub._maybe_start_recovery([online])
    assert len(calls) == 1  # not again before survey_interval_s

    [report] = ws.sent_of_type("network_report")
    assert report["payload"]["lost"] == []
    assert report["payload"]["unregistered"][0]["name"] == "Ender-5"


@pytest.mark.asyncio
async def test_a_report_made_while_the_hub_was_away_is_sent_when_it_is_back(agent, monkeypatch) -> None:
    hub, ws, _path = agent
    hub._ws = None

    async def find_moved(printers, hosts, **_: Any):
        return [record("10.13.0.45", "fc:ee:28:03:09:42")]

    monkeypatch.setattr(connection_module, "find_printers", find_moved)
    monkeypatch.setattr(connection_module, "local_ipv4_networks", lambda: [])
    hub._offline_since["k1-0942"] = asyncio.get_running_loop().time() - 3600
    offline = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.offline, status_raw="offline")
    hub._maybe_start_recovery([offline])
    assert await hub._recovery_task is True
    assert ws.sent_of_type("network_report") == []

    hub._ws = ws
    await hub._send_network_report()
    [report] = ws.sent_of_type("network_report")
    assert report["payload"]["moved"][0]["host"] == "10.13.0.45"


@pytest.mark.asyncio
async def test_a_printer_found_where_it_is_configured_is_not_reported_lost(agent, monkeypatch) -> None:
    hub, ws, _path = agent

    async def find_in_place(printers, hosts, **_: Any):
        return [record("10.13.0.126", "fc:ee:28:03:09:42"), record("10.13.0.126", "fc:ee:28:03:09:42")]

    monkeypatch.setattr(connection_module, "find_printers", find_in_place)
    monkeypatch.setattr(connection_module, "local_ipv4_networks", lambda: [])
    hub._offline_since["k1-0942"] = asyncio.get_running_loop().time() - 3600
    offline = PrinterSnapshot(printer_key="k1-0942", status=PrinterStatus.offline, status_raw="offline")
    hub._maybe_start_recovery([offline])
    assert await hub._recovery_task is False

    [report] = ws.sent_of_type("network_report")
    assert report["payload"]["lost"] == [] and report["payload"]["unregistered"] == []
