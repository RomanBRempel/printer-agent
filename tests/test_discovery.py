"""Network discovery: parsing, scoping and merging. No sockets are opened."""

from __future__ import annotations

import ipaddress

import pytest

from printer_agent.adapters.bambu import BAMBU_MQTT_PORT, parse_bambu_ssdp
from printer_agent.core.discovery import (
    MAX_SCAN_HOSTS,
    DiscoveredPrinter,
    hosts_for,
    merge,
    parse_networks,
)

# Printers allow a user-assigned name, so the parser has to survive non-ASCII.
BAMBU_NOTIFY = (
    "NOTIFY * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:2021\r\n"
    "Location: 192.168.1.44\r\n"
    "NT: urn:bambulab-com:device:3dprinter:1\r\n"
    "USN: 01P00A123456789\r\n"
    "DevModel.bambu.com: BL-P001\r\n"
    "DevName.bambu.com: Цех-1 P1S\r\n"
    "DevSignal.bambu.com: -50\r\n\r\n"
).encode("utf-8")


def test_parse_networks_accepts_cidr():
    assert [str(item) for item in parse_networks(["192.168.1.0/24"])] == ["192.168.1.0/24"]


def test_parse_networks_widens_a_bare_address_to_its_subnet():
    """A bare address as a /32 would scan only itself, which is never the intent."""
    assert [str(item) for item in parse_networks(["192.168.1.55"])] == ["192.168.1.0/24"]


def test_parse_networks_skips_garbage():
    assert parse_networks(["", "not-an-ip", "999.1.1.1"]) == []


def test_hosts_for_excludes_network_and_broadcast():
    hosts = hosts_for([ipaddress.IPv4Network("192.168.1.0/24")])

    assert len(hosts) == 254
    assert "192.168.1.0" not in hosts
    assert "192.168.1.255" not in hosts


def test_hosts_for_caps_the_sweep():
    """A /16 is 65k probes and reads as a port scan on a corporate network."""
    hosts = hosts_for([ipaddress.IPv4Network("10.0.0.0/16")])

    assert len(hosts) == MAX_SCAN_HOSTS


def test_merge_normalizes_and_sorts():
    records = [
        {"brand": "moonraker", "host": "192.168.1.9", "port": 7125, "name": "voron", "source": "http"},
        {"brand": "bambu", "host": "192.168.1.4", "port": 8883, "serial": "S1", "source": "ssdp"},
    ]

    merged = merge(records)

    assert [item.brand for item in merged] == ["bambu", "moonraker"]
    assert merged[0].serial == "S1"


def test_merge_drops_duplicates_by_serial():
    records = [
        {"brand": "bambu", "host": "192.168.1.4", "port": 8883, "serial": "S1"},
        {"brand": "bambu", "host": "192.168.1.4", "port": 8883, "serial": "S1", "name": "later"},
    ]

    assert len(merge(records)) == 1


def test_merge_hides_already_configured_hosts():
    records = [{"brand": "moonraker", "host": "192.168.1.9", "port": 7125}]

    assert merge(records, known_hosts={"192.168.1.9"}) == []


def test_merge_rejects_records_without_brand_or_host():
    records = [{"brand": "", "host": "192.168.1.9"}, {"brand": "bambu", "host": ""}]

    assert merge(records) == []


@pytest.mark.parametrize(
    ("name", "serial", "host", "expected"),
    [
        ("Цех-1 P1S", "S1", "192.168.1.4", "цех-1-p1s"),
        ("", "01P00A99", "192.168.1.4", "01p00a99"),
        ("", "", "192.168.1.4", "192-168-1-4"),
    ],
)
def test_suggested_key_is_readable_and_contract_safe(name, serial, host, expected):
    printer = DiscoveredPrinter(brand="bambu", host=host, port=8883, name=name, serial=serial)

    assert printer.suggested_key == expected


def test_bambu_needs_credentials_moonraker_does_not():
    assert DiscoveredPrinter(brand="bambu", host="h", port=8883).needs_credentials
    assert not DiscoveredPrinter(brand="moonraker", host="h", port=7125).needs_credentials


def test_parse_bambu_ssdp_extracts_identity():
    record = parse_bambu_ssdp(BAMBU_NOTIFY, "192.168.1.44")

    assert record == {
        "brand": "bambu",
        "host": "192.168.1.44",
        "port": BAMBU_MQTT_PORT,
        "name": "Цех-1 P1S",
        "model": "BL-P001",
        "serial": "01P00A123456789",
        "source": "ssdp",
    }


def test_parse_bambu_ssdp_falls_back_to_the_sender_address():
    datagram = b"NOTIFY * HTTP/1.1\r\nUSN: SERIAL1\r\n\r\n"

    record = parse_bambu_ssdp(datagram, "10.0.0.7")

    assert record is not None
    assert record["host"] == "10.0.0.7"
    assert record["name"] == "SERIAL1"


def test_parse_bambu_ssdp_ignores_unrelated_datagrams():
    assert parse_bambu_ssdp(b"random udp noise", "10.0.0.7") is None
    assert parse_bambu_ssdp(b"", "10.0.0.7") is None
