"""Following printers to new addresses after DHCP moved them.

Printers on a shop floor get their addresses from DHCP, and a router restart or
an expired lease hands them out again in a different order. The config names a
printer by address, so afterwards the agent polls an empty address — or, worse,
the neighbour that inherited it. What does not move is what the printer says it
is (see `PrinterConfig.identity`), and this module turns "that identity now
answers at another address" into a rewritten `host`.

Nothing here touches the network or the running agent: the probing lives in the
adapters and `core.discovery`, and the running agent adopts a changed `host`
through its ordinary config reload. What is left is the decision and the write.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import ConfigError, PrinterConfig, normalize_device_id


@dataclass(slots=True)
class RelocationPlan:
    #: printer_key -> the address its identity now answers at.
    moves: dict[str, str] = field(default_factory=dict)
    #: Lost printers whose identity answered nowhere.
    not_found: list[str] = field(default_factory=list)
    #: Matches that were found but not applied, and why.
    refused: list[str] = field(default_factory=list)


def plan_relocation(
    printers: list[PrinterConfig], lost: set[str], records: list[dict[str, Any]]
) -> RelocationPlan:
    """Decide which lost printers move where.

    Only a *unique* match on the printer's own identity, over the protocol it
    is configured for, moves a printer. Refused rather than guessed:

    - an identity answering at two addresses — a printer on both Wi-Fi and a
      cable, or two machines misconfigured alike; either way, not ours to pick;
    - an address two lost printers both claim;
    - an address held by a printer that is *not* lost. That one is being polled
      successfully where it is, so either it is the device there, or it has no
      identity to prove otherwise — and taking the address from under it would
      put two config entries on one machine.
    """
    plan = RelocationPlan()
    by_key = {printer.key: printer for printer in printers}

    for key in sorted(lost):
        printer = by_key.get(key)
        identity = printer.identity() if printer is not None else ""
        if printer is None or not identity:
            continue
        hosts = {
            str(record.get("host", ""))
            for record in records
            if record.get("brand") == printer.brand
            and identity in {normalize_device_id(value) for value in record.get("device_ids") or []}
        }
        hosts.discard("")
        if not hosts:
            plan.not_found.append(key)
        elif len(hosts) > 1:
            plan.refused.append(f"{key}: {identity} answers at {', '.join(sorted(hosts))}")
        elif printer.host not in hosts:
            plan.moves[key] = hosts.pop()
        # else: it answers where it is configured — it is back, nothing to do.

    claimed: dict[str, list[str]] = {}
    for key, host in plan.moves.items():
        claimed.setdefault(host, []).append(key)
    for host, keys in claimed.items():
        if len(keys) > 1:
            plan.refused.append(f"{', '.join(keys)}: all claim {host}")
            for key in keys:
                plan.moves.pop(key)

    held = {printer.host: printer.key for printer in printers if printer.key not in lost}
    for key, host in list(plan.moves.items()):
        holder = held.get(host)
        if holder is not None:
            plan.refused.append(f"{key}: {host} is in use by printer {holder}, which is online")
            plan.moves.pop(key)
    return plan


def scan_networks(
    printers: list[PrinterConfig], configured: list[str], local: list[ipaddress.IPv4Network]
) -> list[ipaddress.IPv4Network]:
    """Where to look: the configured networks, else every printer's /24.

    DHCP hands out addresses from the pool the printer was already in, so the
    subnet of the address it *had* is the one that matters. The agent's own
    networks are only the fallback for printers configured by name: a shop PC
    also carries Docker, VPN and overlay interfaces, and sweeping each of those
    for printers quadruples the scan for nothing.
    """
    if configured:
        return [ipaddress.IPv4Network(value, strict=False) for value in configured]
    networks: list[ipaddress.IPv4Network] = []
    for printer in printers:
        try:
            address = ipaddress.IPv4Address(printer.host)
        except ValueError:
            continue  # a DNS name: DHCP moving it is the DNS server's problem
        network = ipaddress.IPv4Network(f"{address}/24", strict=False)
        if network not in networks:
            networks.append(network)
    return networks or list(local)


def write_printer_fields(path: str | Path, changes: dict[str, dict[str, Any]]) -> list[str]:
    """Set fields on printer entries in the config file, keeping everything else.

    The file is edited as data rather than re-rendered from `AgentConfig`, so
    keys this version of the agent does not know survive — an operator's newer
    setting, or an older agent's — and it is replaced atomically, because this
    runs unattended inside the service and a half-written `agent.yaml` is a
    location that no longer starts. Returns the keys actually changed.
    """
    target = Path(path)
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) if target.exists() else None
    if not isinstance(raw, dict) or not isinstance(raw.get("printers"), list):
        raise ConfigError([f"{target} has no printers list to update"])

    changed: list[str] = []
    for entry in raw["printers"]:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        fields = changes.get(key)
        if not fields:
            continue
        if any(entry.get(name) != value for name, value in fields.items()):
            entry.update(fields)
            changed.append(key)
    if not changed:
        return []

    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(temporary, target)
    return changed
