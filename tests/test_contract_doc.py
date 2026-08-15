"""The contract document and the code must not drift apart.

This test reads `docs/contracts/agent-hub-v1.md` instead of restating it. A second
description of the same wire would be a divergence waiting to happen: it fails
silently in the field rather than in CI, and review does not catch it because
plausible field names look right in both copies.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import pytest

from printer_agent.adapters.base import PrinterAdapter
from printer_agent.config import AgentConfig, PrinterConfig
from printer_agent.contracts import (
    AGENT_TO_HUB_TYPES,
    HUB_TO_AGENT_TYPES,
    PROTOCOL_VERSION,
    CommandAction,
    CommandStatus,
    EventKind,
    HelloRejectCode,
    JobStatus,
    MessageType,
    PrinterCapabilities,
    PrinterSnapshot,
    PrinterStatus,
    build_envelope,
)
from printer_agent.core.outbox import EventOutbox
from printer_agent.uplink.commands import CommandProcessor
from printer_agent.uplink.connection import inventory_payload

CONTRACT_DOC = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "agent-hub-v1.md"

AGENT_TO_HUB_SECTION = "Agent -> Hub"
HUB_TO_AGENT_SECTION = "Hub -> Agent"
VOCABULARY_SECTION = "Vocabularies"


@dataclass(slots=True)
class ParsedContract:
    """What the document says, in a form the code can be compared against."""

    message_types: dict[str, set[str]] = field(default_factory=dict)
    vocabularies: dict[str, list[str]] = field(default_factory=dict)
    json_blocks: dict[str, list[Any]] = field(default_factory=dict)

    def one_block(self, heading: str) -> Any:
        blocks = self.json_blocks.get(heading, [])
        assert blocks, f"the document has no JSON example under `{heading}`"
        return blocks[0]


def _parse_contract(text: str) -> ParsedContract:
    parsed = ParsedContract()
    section = ""
    heading = ""
    in_fence = False
    in_json = False
    buffer: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if in_fence:
            if stripped == "```":
                if in_json:
                    parsed.json_blocks.setdefault(heading, []).append(json.loads("\n".join(buffer)))
                in_fence = in_json = False
                buffer = []
            elif in_json:
                buffer.append(line)
            continue
        if stripped.startswith("```"):
            in_fence = True
            in_json = stripped == "```json"
            buffer = []
            continue
        if line.startswith("## "):
            section = heading = line[3:].strip()
            continue
        if line.startswith("### "):
            heading = line[4:].strip().strip("`")
            if section in {AGENT_TO_HUB_SECTION, HUB_TO_AGENT_SECTION}:
                parsed.message_types.setdefault(section, set()).add(heading)
            elif section == VOCABULARY_SECTION:
                parsed.vocabularies.setdefault(heading, [])
            continue
        if section == VOCABULARY_SECTION and heading in parsed.vocabularies and stripped.startswith("- `"):
            parsed.vocabularies[heading].append(stripped.split("`")[1])

    return parsed


@pytest.fixture(scope="module")
def contract() -> ParsedContract:
    return _parse_contract(CONTRACT_DOC.read_text(encoding="utf-8"))


class _StubAdapter(PrinterAdapter):
    """Concrete adapter that supports nothing: enough to build a command_result."""

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def get_state(self) -> PrinterSnapshot:
        return PrinterSnapshot(printer_key=self.printer_key, status=PrinterStatus.idle, status_raw="idle")

    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()


def _nulls(value: Any, path: str = "") -> list[str]:
    if value is None:
        return [path or "<root>"]
    if isinstance(value, dict):
        return [item for key, nested in value.items() for item in _nulls(nested, f"{path}.{key}")]
    if isinstance(value, list):
        return [item for index, nested in enumerate(value) for item in _nulls(nested, f"{path}[{index}]")]
    return []


def test_documented_agent_messages_match_the_code(contract: ParsedContract) -> None:
    assert contract.message_types[AGENT_TO_HUB_SECTION] == set(AGENT_TO_HUB_TYPES)


def test_documented_hub_messages_match_the_code(contract: ParsedContract) -> None:
    assert contract.message_types[HUB_TO_AGENT_SECTION] == set(HUB_TO_AGENT_TYPES)


def test_no_message_type_exists_outside_the_document(contract: ParsedContract) -> None:
    documented = contract.message_types[AGENT_TO_HUB_SECTION] | contract.message_types[HUB_TO_AGENT_SECTION]
    assert documented == {member.value for member in MessageType}


@pytest.mark.parametrize(
    ("name", "enum"),
    [
        ("printer_status", PrinterStatus),
        ("job_status", JobStatus),
        ("command_status", CommandStatus),
        ("event_kind", EventKind),
        ("command_action", CommandAction),
        ("hello_reject_code", HelloRejectCode),
    ],
)
def test_vocabularies_match_the_enums(contract: ParsedContract, name: str, enum: Any) -> None:
    documented = contract.vocabularies.get(name)
    assert documented is not None, f"the document has no `{name}` vocabulary"
    assert documented == [member.value for member in enum]


def test_envelope_example_uses_the_protocol_version(contract: ParsedContract) -> None:
    envelope = contract.one_block("Envelope")
    assert envelope["v"] == PROTOCOL_VERSION
    assert set(envelope) == set(build_envelope(MessageType.heartbeat.value, {}))


def test_hello_capabilities_match_the_dataclass(contract: ParsedContract) -> None:
    documented = contract.one_block("hello")["printers"][0]["capabilities"]
    assert set(documented) == {item.name for item in fields(PrinterCapabilities)}


def test_hello_protocol_version_matches_the_envelope(contract: ParsedContract) -> None:
    assert contract.one_block("hello")["protocol_version"] == PROTOCOL_VERSION


def test_telemetry_example_omits_absent_values_instead_of_nulls(contract: ParsedContract) -> None:
    # `_clean_nested` can never produce a null, so an example with one would send
    # the hub looking for a value that never arrives.
    assert _nulls(contract.one_block("telemetry")) == []


def test_event_example_uses_a_documented_kind(contract: ParsedContract) -> None:
    assert contract.one_block("event")["kind"] in contract.vocabularies["event_kind"]


def test_hello_reject_example_uses_a_documented_code(contract: ParsedContract) -> None:
    assert contract.one_block("hello_reject")["code"] in contract.vocabularies["hello_reject_code"]


def test_command_example_uses_a_documented_action(contract: ParsedContract) -> None:
    assert contract.one_block("command")["action"] in contract.vocabularies["command_action"]


def test_inventory_shape_matches_what_the_agent_sends(contract: ParsedContract) -> None:
    config = AgentConfig(hub_url="https://hub.example.com/api/printers/agent", agent_token="t", location_key="loc-001")
    adapter = _StubAdapter(PrinterConfig(key="printer-1", brand="moonraker", host="127.0.0.1"))

    payload = inventory_payload(config, [adapter], "0.1.0", "msg-1")

    assert set(contract.one_block("inventory")) == set(payload)


def test_inventory_repeats_the_hello_printer_entry(contract: ParsedContract) -> None:
    # The document promises one parser reads both arrays. An entry that drifts
    # here is exactly the divergence that shows up as a hub quietly storing a
    # printer with half its fields.
    hello_entry = contract.one_block("hello")["printers"][0]
    inventory_entry = contract.one_block("inventory")["printers"][0]

    assert set(hello_entry) == set(inventory_entry)
    assert set(hello_entry["capabilities"]) == set(inventory_entry["capabilities"])


def test_inventory_request_carries_no_command_id(contract: ParsedContract) -> None:
    # It acts on no printer, so it is answered with `inventory`, not with a
    # `command_result` — and a command_id would imply the opposite.
    assert "command_id" not in contract.one_block("inventory_request")


def test_command_bearing_hub_messages_all_carry_a_command_id(contract: ParsedContract) -> None:
    # Without it the agent cannot answer identifiably and has to drop the message.
    for heading in (
        MessageType.command.value,
        MessageType.file_offer.value,
        MessageType.camera_request.value,
        MessageType.camera_stop.value,
    ):
        assert "command_id" in contract.one_block(heading), f"`{heading}` has no command_id"


def test_command_result_shape_matches_what_the_agent_sends(contract: ParsedContract, tmp_path) -> None:
    outbox = EventOutbox(tmp_path / "outbox.sqlite3")
    try:
        adapter = _StubAdapter(PrinterConfig(key="printer-1", brand="moonraker", host="127.0.0.1"))
        result = asyncio.run(
            CommandProcessor(outbox).dispatch(
                adapter,
                {"command_id": "cmd-1", "printer_key": "printer-1", "action": CommandAction.pause.value},
            )
        )
    finally:
        outbox.close()

    assert set(contract.one_block("command_result")) == set(result)
    assert result["status"] in contract.vocabularies["command_status"]
