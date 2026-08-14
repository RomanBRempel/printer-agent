from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..contracts import utc_now_iso


class EventOutbox:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._ensure_schema()

    def close(self) -> None:
        self._connection.close()

    def enqueue_event(self, envelope: dict[str, Any]) -> str:
        msg_id = str(envelope.get("msg_id") or uuid4().hex)
        stored = dict(envelope)
        stored["msg_id"] = msg_id
        payload_json = json.dumps(stored, separators=(",", ":"), ensure_ascii=False)
        self._connection.execute(
            """
            INSERT OR IGNORE INTO events (msg_id, payload_json, created_at)
            VALUES (?, ?, ?)
            """,
            (msg_id, payload_json, utc_now_iso()),
        )
        self._connection.commit()
        return msg_id

    def list_pending_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM events WHERE acknowledged_at IS NULL ORDER BY id ASC"
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        rows = self._connection.execute(query, params).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def ack_event(self, msg_id: str) -> None:
        self._connection.execute(
            "UPDATE events SET acknowledged_at = ? WHERE msg_id = ?",
            (utc_now_iso(), msg_id),
        )
        self._connection.commit()

    def record_command_result(
        self,
        command_id: str,
        printer_key: str,
        status: str,
        error_text: str = "",
        response: dict[str, Any] | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO command_results (command_id, printer_key, status, error_text, response_json, executed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(command_id) DO UPDATE SET
                printer_key = excluded.printer_key,
                status = excluded.status,
                error_text = excluded.error_text,
                response_json = excluded.response_json,
                executed_at = excluded.executed_at
            """,
            (
                command_id,
                printer_key,
                status,
                error_text,
                json.dumps(response or {}, separators=(",", ":"), ensure_ascii=False),
                utc_now_iso(),
            ),
        )
        self._connection.commit()

    def get_command_result(self, command_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT command_id, printer_key, status, error_text, response_json, executed_at FROM command_results WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "command_id": row["command_id"],
            "printer_key": row["printer_key"],
            "status": row["status"],
            "error_text": row["error_text"],
            "response": json.loads(row["response_json"] or "{}"),
            "executed_at": row["executed_at"],
        }

    def summary(self) -> dict[str, int]:
        pending_events = self._connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE acknowledged_at IS NULL"
        ).fetchone()["count"]
        command_results = self._connection.execute(
            "SELECT COUNT(*) AS count FROM command_results"
        ).fetchone()["count"]
        return {"pending_events": int(pending_events), "command_results": int(command_results)}

    def prune(self, max_events: int) -> int:
        if max_events <= 0:
            return 0
        removed = self._connection.execute(
            """
            DELETE FROM events
            WHERE id IN (
                SELECT id FROM events
                WHERE acknowledged_at IS NOT NULL
                ORDER BY id ASC
                LIMIT (
                    SELECT CASE WHEN COUNT(*) > ? THEN COUNT(*) - ? ELSE 0 END
                    FROM events
                    WHERE acknowledged_at IS NOT NULL
                )
            )
            """,
            (max_events, max_events),
        ).rowcount
        self._connection.commit()
        return int(removed or 0)

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                acknowledged_at TEXT
            );

            CREATE TABLE IF NOT EXISTS command_results (
                command_id TEXT PRIMARY KEY,
                printer_key TEXT NOT NULL,
                status TEXT NOT NULL,
                error_text TEXT NOT NULL DEFAULT '',
                response_json TEXT NOT NULL DEFAULT '{}',
                executed_at TEXT NOT NULL
            );
            """
        )
        self._connection.commit()
