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

`brand` names the protocol the agent speaks to that printer, not the manufacturer:
`moonraker`, `bambu`, `creality`. It is an **open set** — a new adapter adds a value
without bumping `PROTOCOL_VERSION`, so receivers must store and pass through an
unknown `brand` rather than reject the printer. Note that one physical machine can be
reachable under more than one of them (Creality K-series firmware exposes Moonraker on
some builds and only the vendor socket on others), which is why `brand` is an input to
the identity hash and never an identity on its own.

**`capabilities` state what this adapter does, not what the machine could do.**
The hub gates commands on them *and* builds its interface from them, so a flag
raised ahead of the implementation shows the operator a button whose only
possible answer is `unsupported`. Two of them decide whole features:

| Flag | Opens | Raised when |
| --- | --- | --- |
| `upload` | `file_offer` for this printer, and the "send to printer" action | the adapter actually uploads files to this brand |
| `camera` | `camera_request` and the camera panel | the adapter can produce a still frame from this printer |

`camera: false` is a normal answer, not a defect: the hub says the printer
reports no camera. The same flags ride along in every snapshot, so an adapter
that learns the answer only after connecting (a snapshot URL it had to probe for)
corrects itself through `telemetry` without waiting for the next `hello`.

### `inventory`

The agent's printer roster: in answer to an `inventory_request`, and on its own
whenever the roster changes.

```json
{
  "location_key": "loc-001",
  "agent_version": "0.1.0",
  "request_msg_id": "uuid-of-the-inventory-request-envelope",
  "printers": [
    {
      "printer_key": "printer-1",
      "brand": "moonraker",
      "capabilities": { "pause": true, "resume": true, "cancel": true, "upload": true, "camera": false, "ams": false, "cfs": false }
    }
  ]
}
```

`printers[]` is the same array as in `hello`, field for field, so both messages
are read with one parser.

`request_msg_id` carries the `msg_id` of the envelope being answered. It is
**omitted** when the agent sends `inventory` on its own, which it does for two
reasons: its config file changed, so a printer added at the location reaches the
hub without waiting for a restart; or a **capability changed under an unchanged
roster** — a camera is found by asking the printer, and that answer can arrive
long after `hello`, or only once the client holding the printer's camera port
goes away. An unsolicited `inventory` is therefore normal
traffic, not a protocol error, and it replaces the roster wholesale: a printer
missing from it has been removed from that agent.

The roster is the set of printers the agent is **configured** for, not the set
currently answering: an unreachable printer stays in the list, and its state is
reported through `telemetry` as `offline` like any other.

### `telemetry`

Batch snapshots, lossy delivery.

```json
{
  "snapshots": [
    {
      "printer_key": "printer-1",
      "status": "printing",
      "status_raw": "printing",
      "job": { "name": "benchy", "progress_pct": 42.5, "filament_used_mm": 4123.5 },
      "temps": { "nozzle": 215.0, "bed": 60.0 },
      "error": {},
      "capabilities": { "pause": true },
      "state": { "ams": { "slots": [ { "index": 0, "material": "PLA", "color": "#000000", "remaining_pct": 87 } ] } },
      "ts": "2026-08-14T12:00:00Z"
    }
  ]
}
```

Absent values are omitted, never sent as `null`: a snapshot with no error carries
`"error": {}`, and unknown temperatures leave their keys out entirely. Receivers
must treat a missing key and an empty object as the same thing.

`state` holds slow-moving vendor state that has no place among the fixed fields.
It is free-form by design: a receiver reads the parts it knows and passes the
rest through unchanged. One block is defined today.

**`state.ams.slots[]`** — the feeding system, one entry per slot, in the order the
printer numbers them. The hub compares it against the filaments named in the
print file's header before sending a job, which is the only reason it exists.

| Field | Meaning |
| --- | --- |
| `index` | Slot number, flat across units (unit 1 tray 0 is index 4 on a four-tray system) |
| `material` | Filament type as the printer reports it (`PLA`, `PETG`). `type` is accepted as a synonym |
| `color` | `#RRGGBB`. `colour` is accepted as a synonym |
| `remaining_pct` | Remaining share of the spool, when the printer can tell |

Only `index` is mandatory: a slot whose material the printer cannot name is
reported as present and empty rather than left out, because a missing slot and an
unreadable one mean different things to a material check. A printer with no
feeding system sends no `ams` block at all, and `capabilities.ams` / `cfs` say so.

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

**`job.filament_used_mm` is a running counter of the current print, not a
total.** It reports how much filament the machine has extruded since this print
started, and it only grows while the print runs. It is optional: an adapter whose
firmware does not expose the figure omits the key rather than sending `0`, since
zero is a real value — a print that has just started. The hub keeps the largest
value it has seen for a job, so a firmware restart that resets the counter
mid-print cannot subtract material that was already spent. Grams are not part of
this field: converting length to mass needs the filament diameter and the
material density, and the printer reports neither. An adapter that gets a mass
straight from the firmware sends `job.filament_used_g` instead, and the hub
prefers it over its own estimate.

**`job.status` and `status` are separate vocabularies.** `idle`, `offline` and
`maintenance` describe a machine and have no job counterpart, so the agent omits
`job.status` instead of putting a printer value in it.

**`status` outranks the `job` block in the same snapshot.** Some firmware keeps
reporting the finished print — file name and progress included — after the
machine has gone back to `idle`; Creality's does. The receiver reads that as a
job that ended, not as one still running, and a leftover `job` block on a
printer that is not printing never starts a new one. There is deliberately **no
"print finished" message**: the transition is derivable from the snapshot, and an
event that must be produced exactly once at exactly the right moment is a thing
an edge agent cannot promise. Warm-up is part of printing, not idleness — the
status during it is `printing`.

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

### `inventory_request`

Asks the agent for its printer roster without waiting for a reconnect.

```json
{}
```

The payload is empty: the request is identified by its envelope `msg_id`, which
the agent echoes back as `inventory.request_msg_id`. This message exists because
the roster otherwise arrives only in `hello` — a hub that missed it, or that has
to reconcile after an operator changed something, had no way to ask for it short
of dropping the session.

It carries no `command_id` and is **not** answered with a `command_result`: it is
a request for state, not an action on a printer, and it never touches one. An
agent too old to know the type ignores it and logs the unknown type, so the hub
must treat a missing answer as "this agent predates the message", not as a
failure — send it, and fall back to the roster from `hello`.

### `command`

```json
{ "command_id": "cmd-1", "printer_key": "printer-1", "action": "pause", "args": {} }
```

`action` comes from the `command_action` vocabulary. Unknown actions are answered
with `command_result` of status `unsupported`, not dropped.

Idempotency is by `command_id`. Replayed commands must return the same result.

`start_print` takes both names of the same file:

```json
{ "command_id": "cmd-9", "printer_key": "printer-1", "action": "start_print",
  "args": { "file_ref": "pf_7f3a…", "remote_name": "BWB-20-D-001-R2.gcode",
            "ams_mapping": [2, 0] } }
```

`file_ref` is the file in the agent's cache, delivered earlier by `file_offer`;
`remote_name` is what it was stored as on the printer, which is how an adapter
that prints by printer-side name (Moonraker) addresses it. The duplication is
deliberate: reconstructing one from the other is where two implementations
diverge. A `file_ref` the agent no longer holds is answered `failed` with the ref
in `error_text` — the agent does **not** go looking for the file, it has no URL
for it and must not guess one. The hub answers that outcome by offering the file
again.

`ams_mapping` (optional) is which loaded slot each filament of the program goes
to, in the program's own filament order. The hub works it out by matching the
program against the slots the printer itself reported, so it is better informed
than the printer's own pick — an adapter that drops it hands the choice back to
the machine, and a job in the wrong material is scrap rather than a warning.
Adapters whose printers have no addressable feeding system ignore the field; its
absence means "do not involve the feeder", not "choose freely".

### `file_offer`

Pull-based file delivery from hub to agent.

```json
{
  "command_id": "cmd-2",
  "printer_key": "printer-1",
  "file_ref": "pf_7f3a91c2e5b04d18a6c2f0d9b41e77aa",
  "url": "https://rd-control.example.com/api/printers/files/pf_7f3a91c2e5b04d18a6c2f0d9b41e77aa",
  "remote_name": "BWB-20-D-001-R2.gcode",
  "sha256": "9f2c1f0a4d0c1e5b6a8f0d3b2c4e6a8f0d3b2c4e6a8f0d3b2c4e6a8f0d3b2c4e",
  "size_bytes": 24815064,
  "start_after_upload": true,
  "expires_at": "2026-08-14T12:30:00Z"
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `file_ref` | yes | The file's name in the agent's cache. **The hub assigns it**, and the follow-up `start_print` carries the same value |
| `url` | yes | Where to fetch it. Always on the hub |
| `remote_name` | yes | The name the file gets on the printer |
| `sha256` | yes | Verified by the agent **before** the file reaches the printer |
| `size_bytes` | yes | Expected length, checked alongside the checksum |
| `start_after_upload` | no | Informational: the hub will send `start_print` itself |
| `expires_at` | no | After this, retrying a `503` is pointless |

`file_ref` comes from the hub rather than the agent so that one file has one
name: an agent-assigned id would have to travel back in `command_result` and be
parsed out of it, which is a second naming path for the same file.

**`start_after_upload` is not an instruction to print.** The start arrives as its
own command with its own `command_id`, so its outcome has a command to belong to.
The flag only tells the agent that the file is about to be needed.

The agent answers `done` once the file is verified and on the printer,
`unsupported` when `capabilities.upload` is false for that adapter, and `failed`
with a readable `error_text` for everything else — a checksum mismatch included,
where nothing is sent to the printer at all.

### `camera_request`

Starts on-demand frame delivery for a printer.

```json
{
  "command_id": "cmd-3",
  "printer_key": "printer-1",
  "session_id": "cam_9f2c8a1b",
  "upload_url": "https://rd-control.example.com/api/printers/camera/cam_9f2c8a1b",
  "interval_s": 2,
  "max_bytes": 2097152,
  "expires_at": "2026-08-14T12:02:00Z"
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `session_id` | yes | Identifies the viewing session; echoed by `camera_stop` |
| `upload_url` | yes | Where frames are posted |
| `interval_s` | no | Seconds between frames. **The hub sets the rate**; the agent has none of its own |
| `max_bytes` | no | Per-frame ceiling |
| `expires_at` | no | The session's own lifetime, used by the agent as the longest it keeps filming with no answer from the hub |

The agent answers `done` once it has *started* filming — the result confirms the
session, not the delivery of a frame. Frames themselves go over HTTP, never
through this socket: the socket has a message-size ceiling and carries a durable
event stream that must not be filled with pictures.

Only one session runs per printer. The hub does not open a second one, and a
repeated `camera_request` replaces rather than doubles the stream.

### `camera_stop`

Stops the active camera session.

```json
{ "command_id": "cmd-4", "printer_key": "printer-1", "session_id": "cam_9f2c8a1b" }
```

`session_id` is **mandatory**. By the time the command arrives the viewer may
have closed the camera and opened it again, and a stop without a session would
put out a stream that someone is watching. A stop naming a session that is no
longer the active one is answered `done` and changes nothing.

`file_offer`, `camera_request` and `camera_stop` are answered with a
`command_result` carrying the same `command_id`, exactly like `command`, and
idempotency by `command_id` applies to them in the same way: a redelivered
`file_offer` returns the stored result rather than downloading and uploading a
second time. A message without `command_id` cannot be answered identifiably and
is dropped with a log line, so the field is mandatory for all three.

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

**Refusing an event discards it.** The agent settles the referenced message the
same way an `ack` settles it: the message is gone from the outbox and will not be
sent again, in this session or any later one. So `error` is the right answer to a
message the hub will *never* accept, and the wrong answer to one it cannot accept
*yet* — a printer awaiting attachment, a temporary storage failure. For those,
staying silent keeps the event pending and the agent resends it, which is the
behaviour that survives the wait.

## HTTP side channels

Two payloads do not belong on the control socket: print files, which run to
hundreds of megabytes, and camera frames, which are a lossy stream. Both use
plain HTTP against the hub, both are still **outbound only** — the agent opens no
port for either — and both authenticate with the same `Authorization: Bearer
<agent_token>` the WebSocket handshake uses. A `?token=` query parameter is not
accepted anywhere: it would end up in access logs.

Both addresses are named by the command that needs them. The agent never
constructs one.

### Fetching a print file

```
GET <file_offer.url>          →  200, body = the file
```

Answer headers: `Content-Length`, `X-Print-File-Sha256`, `X-Print-File-Name`,
`Content-Disposition`. The agent streams the body to a temporary file, hashes it
on the way past, and moves it into its cache under `file_ref` only after the
checksum and the length both match.

| Status | Meaning | Agent |
| --- | --- | --- |
| `401` | no header, or the token was refused | `failed`, reason in `error_text` |
| `403` | the file belongs to a printer of another agent | `failed`; a repeat cannot change it |
| `404` | unknown `file_ref`, or it was cleaned up | `failed`; the hub materialises the file again |
| `409` | the source file changed after the command was issued | `failed`. Printing it would put the wrong revision on the bed |
| `503` | the hub cannot serve it right now | retry with backoff, within `expires_at` |

An `X-Print-File-Sha256` that disagrees with the command's `sha256` is refused
before the body is read: the hub is serving a different revision than the command
was issued for.

### Posting a camera frame

```
POST <camera_request.upload_url>   body = the image bytes
```

`Content-Type` is `image/jpeg`, `image/png` or `image/webp`; `X-Captured-At`
(ISO-8601, by the agent's clock) is optional. The hub keeps only the newest
frame — there is no history, and none may be introduced.

**The answer to a frame outranks `camera_stop`:**

| Status | `continue` | Agent |
| --- | --- | --- |
| `200` | `true` | next frame after `interval_s` from the answer |
| `400` | `true` | this frame was unusable; log it and keep filming |
| `413` | `true` | frame above `max_bytes`; reduce it and keep filming |
| `409` / `404` | `false` | **stop immediately** — do not wait for `camera_stop` |
| `500` | `true` | a hub-side fault; carry on as normal |

The `continue` flag in the body decides; the status is what the agent falls back
on when there is no body. This pair is the primary way a stream ends: a stop
command may never arrive — the session can expire while the agent is
reconnecting — and a camera nobody is watching has to switch itself off.

The hub's session expires on its own, and **only a viewer extends it**. Frames do
not, or the stream would feed itself forever. An agent that gets no answer at all
for the length of `expires_at` stops filming: a lost link must not leave a camera
running.

Frames are never queued and never resent. Like telemetry, they are lossy on
purpose — a frame delivered a minute late describes a print that has since
changed, which is worse than no frame at all.

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
