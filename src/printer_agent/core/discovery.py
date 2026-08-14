"""Find printers on the local network.

Protocol-specific probing lives in the adapters, as it must; this module only
enumerates the subnets worth scanning, fans out to the brand discoverers, and
merges what comes back into one normalized list.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any

from ..adapters.bambu import discover_bambu
from ..adapters.creality import discover_creality
from ..adapters.moonraker import discover_moonraker

#: Refuse to sweep anything larger than a /22 — a /16 is 65k probes and, on a
#: corporate network, indistinguishable from a port scan.
MAX_SCAN_HOSTS = 1024
DEFAULT_SCAN_TIMEOUT_S = 1.5
DEFAULT_LISTEN_TIMEOUT_S = 6.0


@dataclass(slots=True)
class DiscoveredPrinter:
    brand: str
    host: str
    port: int
    name: str = ""
    model: str = ""
    serial: str = ""
    source: str = ""

    @property
    def identity(self) -> tuple[str, str]:
        return self.brand, self.serial or self.host

    @property
    def suggested_key(self) -> str:
        """A printer_key that is stable, readable and safe for the contract."""
        base = self.name or self.serial or self.host
        cleaned = "".join(char if char.isalnum() else "-" for char in base.lower())
        cleaned = "-".join(part for part in cleaned.split("-") if part)
        return cleaned or f"{self.brand}-printer"

    @property
    def needs_credentials(self) -> bool:
        # Bambu's access code is only on the printer's screen; nothing on the
        # network will hand it over.
        return self.brand == "bambu"


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    """Every directly attached IPv4 subnet, assumed /24 where the mask is unknown."""
    networks: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    candidates: list[str] = []

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.append(info[4][0])
    except OSError:
        pass

    # getaddrinfo misses interfaces on some hosts; the route to a public address
    # reveals the primary one without sending anything.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
        candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    for address in candidates:
        try:
            parsed = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast:
            continue
        network = ipaddress.IPv4Network(f"{address}/24", strict=False)
        if str(network) not in seen:
            seen.add(str(network))
            networks.append(network)
    return networks


def hosts_for(networks: list[ipaddress.IPv4Network], limit: int = MAX_SCAN_HOSTS) -> list[str]:
    hosts: list[str] = []
    for network in networks:
        for address in network.hosts():
            if len(hosts) >= limit:
                return hosts
            hosts.append(str(address))
    return hosts


def parse_networks(values: list[str]) -> list[ipaddress.IPv4Network]:
    """Accept `192.168.1.0/24` or a bare `192.168.1.5` (treated as its /24)."""
    networks: list[ipaddress.IPv4Network] = []
    for value in values:
        text = value.strip()
        if not text:
            continue
        if "/" not in text:
            # A bare address parses as a /32, which would scan exactly itself.
            # Treating it as its /24 is what someone typing one address means.
            try:
                address = ipaddress.IPv4Address(text)
            except ValueError:
                continue
            networks.append(ipaddress.IPv4Network(f"{address}/24", strict=False))
            continue
        try:
            networks.append(ipaddress.IPv4Network(text, strict=False))
        except ValueError:
            continue
    return networks


def merge(records: list[dict[str, Any]], known_hosts: set[str] | None = None) -> list[DiscoveredPrinter]:
    """Normalize brand records, drop duplicates, and sort for a stable list.

    A Creality machine with Moonraker enabled answers both probes and would be
    offered twice. Moonraker wins that tie: it is the richer protocol, and the
    Creality socket is the fallback for firmware that keeps 7125 closed.
    """
    known = known_hosts or set()
    merged: dict[tuple[str, str], DiscoveredPrinter] = {}
    for record in records:
        printer = DiscoveredPrinter(
            brand=str(record.get("brand", "")),
            host=str(record.get("host", "")),
            port=int(record.get("port") or 0),
            name=str(record.get("name", "")),
            model=str(record.get("model", "")),
            serial=str(record.get("serial", "")),
            source=str(record.get("source", "")),
        )
        if not printer.brand or not printer.host:
            continue
        if printer.host in known:
            continue
        existing = merged.get(printer.identity)
        if existing is None or (not existing.serial and printer.serial):
            merged[printer.identity] = printer

    moonraker_hosts = {item.host for item in merged.values() if item.brand == "moonraker"}
    kept = [
        item
        for item in merged.values()
        if not (item.brand == "creality" and item.host in moonraker_hosts)
    ]
    return sorted(kept, key=lambda item: (item.brand, item.host))


async def discover(
    *,
    networks: list[ipaddress.IPv4Network] | None = None,
    known_hosts: set[str] | None = None,
    scan_timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
    listen_timeout_s: float = DEFAULT_LISTEN_TIMEOUT_S,
) -> list[DiscoveredPrinter]:
    """Run every brand discoverer concurrently and return one merged list."""
    targets = networks if networks is not None else local_ipv4_networks()
    hosts = hosts_for(targets)

    results = await asyncio.gather(
        discover_moonraker(hosts, timeout_s=scan_timeout_s),
        discover_creality(hosts, timeout_s=scan_timeout_s),
        discover_bambu(timeout_s=listen_timeout_s),
        return_exceptions=True,
    )
    records: list[dict[str, Any]] = []
    for result in results:
        if isinstance(result, list):
            records.extend(result)
    return merge(records, known_hosts)
