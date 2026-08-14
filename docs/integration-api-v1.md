# Integration API v1 (accounting / ERP embedding)

Status: **specification**. This document defines the API surface that an external accounting system (ERP, MES, 1C, WMS) uses to embed printer state and printer control. The API is implemented **on the RD Control hub**, not in this repository.

## Краткое описание (RU)

`printer-agent` не имеет входящего API — это жёсткая граница проекта: агент держит только исходящую WSS-сессию к хабу и не открывает портов. Поэтому учётная система интегрируется **не с агентом, а с хабом RD Control**.

Модель встраивания:

- Учётная система читает состояние оборудования (`GET /printers`, `GET /printers/{id}/state`) — это проекция снимков, которые агент шлёт в хаб.
- Учётная система ставит задания и управляет печатью через команды (`POST /printers/{id}/commands`), хаб доставляет их агенту, агент — принтеру.
- Идемпотентность обеспечивает сама учётная система: `command_id` — это её собственный GUID документа. Повтор запроса с тем же `command_id` возвращает тот же результат, а не создаёт второе задание.
- События (смена статуса, ошибка, завершение печати) доставляются вебхуками или опросом `GET /events` с курсором, семантика — at-least-once, дедуп по `event_id`.

Словарь статусов, полей задания и команд полностью совпадает с [контрактом agent-hub v1](contracts/agent-hub-v1.md), идентификация принтеров — с [правилами дедупликации](data-storage-and-dedup.md).

## Why the API is hub-side

| Concern | Where it lives |
| --- | --- |
| Vendor protocol translation | `printer-agent` adapters |
| Durable delivery of events from the edge | `printer-agent` outbox |
| Authentication of external systems, history, aggregation | hub |
| Integration API for accounting systems | hub |

The agent must never be called directly by an accounting system: it has no inbound listener, no authentication surface for third parties, and its local SQLite store is an edge durability boundary — not a system of record.

## Base URL, versioning, auth

```
https://<hub-host>/api/integration/v1
```

- All requests: `Authorization: Bearer <integration_token>`.
- Token is scoped to a tenant and to a set of `location_key` values. Requests outside the scope return `403`.
- Version is in the path. Evolution follows the contract rules: fields may be added, never renamed or removed; a breaking change means `/v2`.
- All timestamps are UTC ISO-8601 with `Z` (`2026-08-14T12:00:00Z`), matching `utc_now_iso()` in the agent.
- Request and response bodies are `application/json; charset=utf-8`.

## Identity model

The accounting system must store one stable identifier per physical printer:

| Field | Meaning | Stability |
| --- | --- | --- |
| `printer_id` | canonical hub identity, derived as `sha256(location_key + brand + stable_device_id)` | stable for the physical device inside a location |
| `printer_key` | agent-local key from `agent.yaml` | may be an alias, may change on reconfiguration |
| `location_key` | print location | stable |

**Bind accounting records to `printer_id`, never to `printer_key`.** `printer_key` is a configuration label; two agents can report the same physical printer under different keys. See [data-storage-and-dedup.md](data-storage-and-dedup.md).

## Resources

### `GET /locations`

Lists locations visible to the token.

```json
{
  "items": [
    { "location_key": "loc-001", "name": "Shop 1", "agent_online": true, "last_seen_at": "2026-08-14T12:00:00Z" }
  ]
}
```

`agent_online` reflects the hub's view of the WSS session, driven by `hello`/`heartbeat`.

### `GET /printers`

Query parameters: `location_key`, `brand`, `status`, `cursor`, `limit` (default 50, max 200).

```json
{
  "items": [
    {
      "printer_id": "b6f1...c04",
      "printer_key": "printer-1",
      "location_key": "loc-001",
      "brand": "moonraker",
      "status": "printing",
      "capabilities": { "pause": true, "resume": true, "cancel": true, "upload": true, "camera": false, "ams": false, "cfs": false },
      "updated_at": "2026-08-14T12:00:03Z"
    }
  ],
  "next_cursor": null
}
```

Use `capabilities` before offering an action in the accounting UI. An action that the printer does not support returns `unsupported` rather than an error — it is a normal outcome, not a failure to retry.

### `GET /printers/{printer_id}/state`

Current normalized snapshot. Shape mirrors `PrinterSnapshot.to_dict()`; absent values are omitted, never sent as `null`.

```json
{
  "printer_id": "b6f1...c04",
  "printer_key": "printer-1",
  "location_key": "loc-001",
  "status": "printing",
  "status_raw": "printing",
  "job": {
    "name": "benchy",
    "progress_pct": 42.5,
    "layer": 120,
    "layers_total": 285,
    "time_elapsed_s": 1830,
    "time_remaining_s": 2470,
    "status": "printing"
  },
  "temps": { "nozzle": 215.0, "nozzle_target": 215.0, "bed": 60.0, "bed_target": 60.0 },
  "error": {},
  "capabilities": { "pause": true },
  "ts": "2026-08-14T12:00:03Z",
  "stale": false
}
```

- `status` is the normalized enum: `offline`, `idle`, `printing`, `paused`, `finished`, `error`, `maintenance`. Unknown vendor values normalize to `maintenance` — treat it as "state not interpretable", not as scheduled service.
- `status_raw` is the vendor string, for diagnostics only. Never branch accounting logic on it.
- `job.status` is the job enum: `queued`, `uploading`, `printing`, `paused`, `finished`, `failed`, `cancelled`.
- `ts` is when the agent observed the state, not when the hub answered. `stale` is true when the last snapshot is older than the hub's freshness threshold (agent disconnected or telemetry dropped).
- Telemetry is lossy by design: temperatures and progress may skip values. Do not use polled state to detect transitions — use events.

### `POST /files`

Registers a printable file so it can be referenced by a print command. The hub serves it and the agent pulls it, matching the `file_offer` model.

```json
{ "url": "https://erp.example.com/files/order-4711.gcode", "sha256": "9f2c...", "remote_name": "order-4711.gcode" }
```

Response:

```json
{ "file_ref": "file_01J8...", "sha256": "9f2c...", "size_bytes": 4821004, "created_at": "2026-08-14T11:59:00Z" }
```

The URL must be reachable by the hub. `sha256` is mandatory and verified before delivery to the agent.

### `POST /printers/{printer_id}/commands`

Issues a command. This is the only write path into the printer.

```json
{
  "command_id": "8f14e45f-ea6f-4b1c-9b2b-2f4c9a0d1e33",
  "action": "start_print",
  "args": { "file_ref": "file_01J8..." }
}
```

- `command_id` is supplied by the **accounting system** and must be a stable identifier of its own document or operation (order GUID, task GUID). It is the idempotency key.
- Supported actions and their `args`:

| Action | `args` | Notes |
| --- | --- | --- |
| `start_print` | `file_ref` (string, required) | file must be registered via `POST /files` |
| `pause` | — | requires `capabilities.pause` |
| `resume` | — | requires `capabilities.resume` |
| `cancel` | — | requires `capabilities.cancel` |
| `upload_file` | `file_ref`, `remote_name` | uploads without starting a print; requires `capabilities.upload` |

Response `202 Accepted`:

```json
{ "command_id": "8f14e45f-...", "printer_id": "b6f1...c04", "state": "accepted", "accepted_at": "2026-08-14T12:00:05Z" }
```

`202` means the hub accepted and queued the command for delivery. It does **not** mean the printer executed it. Poll `GET /commands/{command_id}` or wait for the `command.completed` event.

### `GET /commands/{command_id}`

```json
{
  "command_id": "8f14e45f-...",
  "printer_id": "b6f1...c04",
  "action": "start_print",
  "state": "completed",
  "status": "done",
  "error_text": "",
  "response": {},
  "accepted_at": "2026-08-14T12:00:05Z",
  "completed_at": "2026-08-14T12:00:07Z"
}
```

- `state` is the hub-side lifecycle: `accepted` → `dispatched` → `completed`, or `expired` if the agent never came back online within the command TTL.
- `status` is the agent-reported terminal result and appears only when `state` is `completed`. Values map one-to-one to `CommandStatus`:

| `status` | Meaning | Accounting system should |
| --- | --- | --- |
| `done` | executed by the printer | close the operation |
| `failed` | printer or adapter rejected it | surface `error_text`, allow a manual retry with a **new** `command_id` |
| `unsupported` | the printer cannot do this action | do not retry; hide the action for this printer |
| `timeout` | no result within the adapter timeout | reconcile against printer state before retrying |

## Idempotency

Two independent layers, and neither is optional for a correct integration:

1. **Command idempotency.** Repeating `POST /printers/{id}/commands` with an already-seen `command_id` returns the stored result with `200` instead of `202`, and does not execute anything a second time. This holds across agent restarts — the agent persists results in its `command_results` table keyed by `command_id`. A network timeout on the accounting side is therefore always safe to retry verbatim: retrying the same `command_id` can never start a second print.
2. **Event idempotency.** Event delivery is at-least-once. Deduplicate by `event_id` on receipt.

A retry with a *new* `command_id` is a new command and will execute again. Generate a new id only for a deliberate, operator-driven repeat.

## Events

### Polling: `GET /events`

Parameters: `cursor` (opaque, from the previous response), `location_key`, `printer_id`, `kind`, `limit`.

```json
{
  "items": [
    {
      "event_id": "evt_01J8...",
      "kind": "printer.status_changed",
      "printer_id": "b6f1...c04",
      "location_key": "loc-001",
      "occurred_at": "2026-08-14T12:00:03Z",
      "data": { "from": "idle", "to": "printing", "snapshot": {} }
    }
  ],
  "next_cursor": "eyJvIjoxNzMyfQ"
}
```

Persist `next_cursor` in the accounting system and resume from it. Events are retained for a bounded window (hub configuration); a consumer offline longer than the retention window must re-read current state via `GET /printers` instead of replaying.

### Webhooks

`POST /webhooks` registers an endpoint:

```json
{ "url": "https://erp.example.com/hooks/rd-control", "kinds": ["printer.status_changed", "command.completed"], "secret": "whsec_..." }
```

Delivery:

- Headers: `X-RD-Event-Id`, `X-RD-Timestamp`, `X-RD-Signature: sha256=<hex>`.
- Signature is `HMAC-SHA256(secret, timestamp + "." + raw_body)`. Verify it before parsing, and reject timestamps older than 5 minutes.
- Any `2xx` is an ack. Non-2xx or timeout is retried with exponential backoff; ordering across retries is not guaranteed.
- Body is one event object in the shape shown above.

### Event kinds

| Kind | Raised when | Key `data` fields |
| --- | --- | --- |
| `printer.status_changed` | normalized status changed | `from`, `to`, `snapshot` |
| `printer.job_changed` | job signature changed (new job, finished, cancelled) | `job`, `snapshot` |
| `printer.error` | error signature appeared or changed | `error.code`, `error.message` |
| `printer.online` / `printer.offline` | agent session or printer reachability changed | `location_key` |
| `command.completed` | terminal command result arrived | `command_id`, `status`, `error_text` |

Temperature drift raises **no** event by design — temperatures ride along in lossy telemetry only. An accounting system that needs thermal history must poll state.

## Errors

```json
{ "error": { "code": "printer_not_found", "message": "no printer with this id in scope", "details": {} } }
```

| HTTP | `code` examples | Retry |
| --- | --- | --- |
| 400 | `invalid_argument`, `missing_file_ref` | no, fix the request |
| 401 / 403 | `unauthenticated`, `location_out_of_scope` | no |
| 404 | `printer_not_found`, `command_not_found` | no |
| 409 | `command_id_conflict` (same id, different payload) | no |
| 422 | `capability_unsupported` | no |
| 429 | `rate_limited` (see `Retry-After`) | yes, with backoff |
| 503 | `agent_offline` | yes, or queue on the accounting side |

`agent_offline` on a command POST is not automatic — the hub may accept and hold the command until the agent reconnects, subject to the command TTL. Check `state: "expired"` rather than assuming delivery.

## Mapping to accounting entities

A suggested binding for a typical ERP data model:

| Accounting entity | API entity | Binding key |
| --- | --- | --- |
| Subdivision / warehouse | location | `location_key` |
| Equipment / fixed asset | printer | `printer_id` |
| Production order operation | command + job | `command_id` (= operation GUID) |
| Operation status | `command.status` + `job.status` | terminal states only |
| Equipment downtime record | `printer.status_changed` to `error` / `offline` | `event_id` |
| Attached print file | file | `file_ref`, `sha256` |

Recommended sequence for a production order:

1. Publish the G-code, register it: `POST /files` → `file_ref`.
2. Pick a printer: `GET /printers?location_key=...&status=idle`, check `capabilities`.
3. Start: `POST /printers/{printer_id}/commands` with `command_id` = operation GUID, `action: "start_print"`.
4. Wait for `command.completed` with `status: "done"`; treat anything else per the status table above.
5. Track progress from `printer.job_changed` events; write the completion fact when `job.status` becomes `finished`, `failed`, or `cancelled`.
6. Reconcile on restart: re-read `GET /commands/{command_id}` for every open operation before issuing anything new.

## Non-goals

- No scheduling, queueing, prioritization, or printer selection logic in this API. The accounting system decides which printer runs what.
- No pricing, costing, or material accounting.
- No inbound API on `printer-agent` itself — this remains a hard project boundary.
- No direct access to the agent's SQLite outbox or to vendor protocols (Moonraker JSON-RPC, Bambu MQTT) from outside the adapters.

## Keeping this document correct

Update this file when any of the following change:

1. `PrinterStatus`, `JobStatus`, or `CommandStatus` in [src/printer_agent/contracts.py](../src/printer_agent/contracts.py).
2. The action list in [src/printer_agent/uplink/commands.py](../src/printer_agent/uplink/commands.py) or the capability flags in `PrinterCapabilities`.
3. Message shapes in [docs/contracts/agent-hub-v1.md](contracts/agent-hub-v1.md).
4. The identity or dedupe rules in [docs/data-storage-and-dedup.md](data-storage-and-dedup.md).
