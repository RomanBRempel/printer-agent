# Agent-Hub Contract v1

This document is the repository contract between `printer-agent` and RD Control.
It is the source of truth for message shapes and versioning.

`printer-agent` owns this document; RD Control follows it and keeps no second
description of the same wire. Roles, rollout order and how cross-repository work
is handed off are in [docs/agent-collaboration.md](../agent-collaboration.md).

## Transport

- Endpoint: `wss://<host>/api/printers/agent`. The agent holds one outbound
  session and opens no ports of its own.
- Authentication: the `Authorization: Bearer <token>` handshake header. The
  token is never passed as a query parameter, where it would land in access logs.
- `hub_url` in `agent.yaml` carries the full endpoint URL including that path.

## Envelope

All messages use the same wrapper:

```json
{ "v": 1, "type": "hello", "msg_id": "uuid", "ts": "2026-08-14T12:00:00Z", "payload": {} }
```

Rules:

- `v` is the protocol version. It equals `PROTOCOL_VERSION` in
  `src/printer_agent/contracts.py`.
- `msg_id` is unique per message.
- `ts` is UTC ISO-8601.
- `payload` is message-specific.
- A receiver that gets a higher `v` than it implements must refuse readably
  instead of processing the message: the hub answers `hello_reject`, the agent
  logs the mismatch and closes the session with a stated reason.

Identity and duplicate-prevention notes:

- `msg_id` uniqueness is transport-level and only guarantees message idempotency.
- Printer-level deduplication must use a stable physical identity key scoped by `location_key`.
- Recommended identity input is `location_key + brand + stable_device_id` (for example Bambu serial), hashed if needed for compact transport.
- In multi-agent overlap scenarios, only one active owner should publish state for a given printer identity at a time.

## Agent -> Hub

### `hello`

Sent on connect.

```json
{
  "protocol_version": 1,
  "agent_version": "0.1.0",
  "location_key": "loc-001",
  "printers": [
    {
      "printer_key": "printer-1",
      "brand": "moonraker",
      "capabilities": { "pause": true, "resume": true, "cancel": true, "upload": true, "camera": false, "ams": false, "cfs": false }
    }
  ]
}
```

`printer_key` must be stable for the same physical printer inside one location. If a config-local alias is used, map it to a canonical identity before publishing.

### `telemetry`

Batch snapshots, lossy delivery.

```json
{
  "snapshots": [
    {
      "printer_key": "printer-1",
      "status": "printing",
      "status_raw": "printing",
      "job": { "name": "benchy", "progress_pct": 42.5 },
      "temps": { "nozzle": 215.0, "bed": 60.0 },
      "error": {},
      "capabilities": { "pause": true },
      "ts": "2026-08-14T12:00:00Z"
    }
  ]
}
```

Absent values are omitted, never sent as `null`: a snapshot with no error carries
`"error": {}`, and unknown temperatures leave their keys out entirely. Receivers
must treat a missing key and an empty object as the same thing.

### `event`

Reliable event delivery. Events stay in the local outbox until hub ack.

```json
{
  "printer_key": "printer-1",
  "kind": "status_changed",
  "snapshot": {}
}
```

`kind` comes from the `event_kind` vocabulary below. A single state transition can
produce several kinds at once; the agent sends one event per kind. `snapshot` is
the first observation of a printer after agent start — it carries no previous
state to compare against.

### `command_result`

```json
{ "command_id": "cmd-1", "printer_key": "printer-1", "status": "done", "error_text": "", "response": {} }
```

`status` comes from the `command_status` vocabulary. An action the printer's
protocol does not implement is `unsupported`, not `failed`.

### `heartbeat`

Only when nothing else has been sent recently.

```json
{ "location_key": "loc-001" }
```

## Snapshot semantics

`telemetry.snapshots[]` and `event.snapshot` carry the same object. Three of its
properties do not follow from the shape alone.

**A job has no identifier.** The snapshot carries `job.name` only, so the hub
keys a job on `(printer_key, job.name)`. Two prints of the same file in a row are
indistinguishable unless the job reached a terminal `job_status` in between.
`job.external_job_id` is reserved for adapters that can supply a vendor job
number; no adapter sends it yet, and it is optional when one does.

**`time_remaining_s` counts from the snapshot's `ts`, not from arrival.** A
snapshot can wait in the outbox for the whole length of an outage. A receiver
that reads the value as relative to processing time overstates the remaining
time by the age of the message.

**`job.status` and `status` are separate vocabularies.** `idle`, `offline` and
`maintenance` describe a machine and have no job counterpart, so the agent omits
`job.status` instead of putting a printer value in it.

## Hub -> Agent

### `hello_ack`

Confirms protocol compatibility.

```json
{ "location_key": "loc-001", "protocol_version": 1 }
```

### `hello_reject`

Rejects the agent with a readable reason.

```json
{ "code": "protocol_unsupported", "reason": "agent speaks v1, hub requires v2" }
```

`code` comes from the `hello_reject_code` vocabulary and decides agent behaviour;
`reason` is human text for logs and is never parsed. Only
`temporarily_unavailable` is retryable — the agent reconnects with its normal
backoff. Every other code, an unknown code, and a missing `code` field stop the
session: the agent logs the reason and does not reconnect, because retrying
cannot change the answer. This is the rule that keeps an incompatible or
deauthorised agent from looking like a network failure and looping forever.

### `command`

```json
{ "command_id": "cmd-1", "printer_key": "printer-1", "action": "pause", "args": {} }
```

`action` comes from the `command_action` vocabulary. Unknown actions are answered
with `command_result` of status `unsupported`, not dropped.

Idempotency is by `command_id`. Replayed commands must return the same result.

### `file_offer`

Pull-based file delivery from hub to agent.

```json
{ "command_id": "cmd-2", "printer_key": "printer-1", "url": "https://rd-control.example.com/files/abc.gcode", "sha256": "...", "remote_name": "job.gcode" }
```

### `camera_request`

Starts on-demand JPEG delivery for a printer.

```json
{ "command_id": "cmd-3", "printer_key": "printer-1" }
```

### `camera_stop`

Stops the active camera session.

```json
{ "command_id": "cmd-4", "printer_key": "printer-1" }
```

`file_offer`, `camera_request` and `camera_stop` are answered with a
`command_result` carrying the same `command_id`, exactly like `command`. They are
not implemented yet and currently answer `unsupported`. A message without
`command_id` cannot be answered identifiably and is dropped with a log line, so
the field is mandatory for all three. Delivery-specific fields (frame rate,
resolution, transport) are not specified yet and may be added later.

### `ack`

Acknowledges an `event` message by `msg_id`.

```json
{ "msg_id": "uuid-of-the-event-envelope" }
```

### `error`

Refuses a single agent message without ending the session.

```json
{ "code": "command_id_required", "message": "command_id is missing", "msg_id": "uuid-of-the-refused-envelope" }
```

Used when one message is malformed but the agent itself is fine — the opposite of
`hello_reject`, which ends the session. `msg_id` refers to the refused envelope so
the agent can stop resending it; without this message a rejected event would be
retried out of the outbox forever. `code` is free-form for now and is only logged.

## Vocabularies

These lists are the closed sets used on the wire. Both sides pin them with a
test; adding a value is a contract change like any other.

### `printer_status`

- `offline`
- `idle`
- `printing`
- `paused`
- `finished`
- `error`
- `maintenance`

Unknown vendor states map to `maintenance`, never to `error`.

### `job_status`

- `queued`
- `uploading`
- `printing`
- `paused`
- `finished`
- `failed`
- `cancelled`

### `command_status`

- `done`
- `failed`
- `unsupported`
- `timeout`

### `event_kind`

- `snapshot`
- `status_changed`
- `job_changed`
- `error_changed`

### `command_action`

- `start_print`
- `pause`
- `resume`
- `cancel`
- `upload_file`

### `hello_reject_code`

- `protocol_unsupported`
- `auth_rejected`
- `location_unknown`
- `temporarily_unavailable`

## Evolution Rules

- Hub must accept an agent that is one minor version newer.
- Breaking changes require a new protocol version.
- Existing fields stay backward compatible.
- New fields may be added, but existing fields are not renamed or removed.
- Adding a value to a vocabulary above is a wire change: the receiving side ships
  first, and the sending side may only start using the value once the receiver is
  deployed everywhere.
- Incompatibility must produce a readable refusal, never a silent disconnect.
- Any change starts in this document, in both repositories, before code.
