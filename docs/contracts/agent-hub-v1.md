# Agent-Hub Contract v1

This document is the repository contract between `printer-agent` and RD Control.
It is the source of truth for message shapes and versioning.

## Envelope

All messages use the same wrapper:

```json
{ "v": 1, "type": "hello", "msg_id": "uuid", "ts": "2026-08-14T12:00:00Z", "payload": {} }
```

Rules:

- `v` is the protocol version.
- `msg_id` is unique per message.
- `ts` is UTC ISO-8601.
- `payload` is message-specific.

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
      "error": null,
      "capabilities": { "pause": true }
    }
  ]
}
```

### `event`

Reliable event delivery. Events stay in the local outbox until hub ack.

```json
{
  "printer_key": "printer-1",
  "kind": "status_changed",
  "snapshot": {}
}
```

### `command_result`

```json
{ "command_id": "cmd-1", "status": "done", "error_text": "", "response": {} }
```

### `heartbeat`

Only when nothing else has been sent recently.

```json
{ "location_key": "loc-001" }
```

## Hub -> Agent

### `hello_ack`

Confirms protocol compatibility.

### `hello_reject`

Rejects the agent with a readable reason.

### `command`

```json
{ "command_id": "cmd-1", "printer_key": "printer-1", "action": "pause", "args": {} }
```

Idempotency is by `command_id`. Replayed commands must return the same result.

### `file_offer`

Pull-based file delivery from hub to agent.

```json
{ "url": "https://rd-control.example.com/files/abc.gcode", "sha256": "...", "remote_name": "job.gcode" }
```

### `camera_request`

Starts on-demand JPEG delivery for a printer.

### `camera_stop`

Stops the active camera session.

### `ack`

Acknowledges an `event` message by `msg_id`.

## Evolution Rules

- Hub must accept an agent that is one minor version newer.
- Breaking changes require a new protocol version.
- Existing fields stay backward compatible.
- New fields may be added, but existing fields are not renamed or removed.
