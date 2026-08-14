from __future__ import annotations

from pathlib import Path

from printer_agent.core.outbox import EventOutbox
from printer_agent.contracts import build_envelope


def test_outbox_persists_events_and_command_results(tmp_path: Path) -> None:
    db_path = tmp_path / "outbox.sqlite3"
    outbox = EventOutbox(db_path)
    msg_id = outbox.enqueue_event(build_envelope("event", {"printer_key": "printer-1"}, msg_id="msg-1"))
    assert msg_id == "msg-1"
    assert outbox.summary()["pending_events"] == 1

    reopened = EventOutbox(db_path)
    pending = reopened.list_pending_events()
    assert len(pending) == 1
    assert pending[0]["v"] == 1
    assert pending[0]["type"] == "event"
    assert pending[0]["msg_id"] == "msg-1"
    assert pending[0]["payload"] == {"printer_key": "printer-1"}
    reopened.ack_event("msg-1")
    assert reopened.summary()["pending_events"] == 0

    reopened.record_command_result("cmd-1", "printer-1", "done", "", {"ok": True})
    result = reopened.get_command_result("cmd-1")
    assert result is not None
    assert result["status"] == "done"
    assert result["response"] == {"ok": True}
