# Data Storage and Duplicate Prevention

This document defines where data is stored in the project and how duplicate records are prevented when one or more agent instances observe the same physical printers.

## Scope

- Applies to local agent persistence.
- Applies to message publication rules.
- Applies to hub-side canonical storage requirements.

## Where data is stored

### Local agent (edge)

- Configuration file:
  - `agent.yaml` (default local config).
  - Windows installer scenario typically uses `ProgramData\\printer-agent\\agent.yaml`.
- Durable local database:
  - Path from `outbox.database_path` in config.
  - Default value: `data/outbox.sqlite3`.
- SQLite tables:
  - `events`: reliable event outbox, pending until ack by `msg_id`.
  - `command_results`: idempotent command execution results by `command_id`.

Important: local SQLite is an edge durability boundary, not a global source of truth for printer identity.

## Hub-side canonical storage

- The hub data store must enforce printer-level uniqueness.
- Canonical key must be based on physical identity, not transport IDs.

## Identity model for deduplication

Use one stable key for one physical printer inside one location:

- `printer_identity = sha256(location_key + brand + stable_device_id)`

Where `stable_device_id` is:

- Bambu: serial number.
- Moonraker: stable hardware ID if available; otherwise a documented fallback such as host:port.

Notes:

- `msg_id` is message-level idempotency only.
- `command_id` is command replay idempotency only.
- Neither replaces printer-level deduplication.

## Multi-agent overlap rule

If two services can see the same printer, only one service may publish state as active owner at a time.

Recommended mechanism:

- Lease/claim record by `printer_identity`.
- Fields:
  - `printer_identity`
  - `owner_agent_id`
  - `lease_expires_at`

Behavior:

- Owner publishes `telemetry` and `event` messages.
- Non-owner instances do not publish duplicate state for the same identity.

## Database constraints (canonical side)

Minimum constraints for dedupe:

- Unique printer record by `(location_key, printer_identity)`.
- Optional state-level dedupe by `(printer_identity, snapshot_signature)`.

`snapshot_signature` should be built from normalized state fields, for example:

- status
- job signature
- error signature
- capabilities

Temperature-only jitter should not generate duplicate event records.

## Contract and payload requirements

- `printer_key` sent by agent must be stable for the same physical printer in one location.
- If local config uses alias names, map alias to canonical identity before publishing.

## Operational checklist

When adding a new adapter or changing identity fields:

1. Keep adapter-specific identity extraction inside `src/printer_agent/adapters/`.
2. Keep dedupe logic protocol-agnostic outside adapters.
3. Update `docs/contracts/agent-hub-v1.md` when payload identity behavior changes.
4. Update this document if storage paths, tables, or dedupe constraints change.

## Non-goals

- No inbound API for ownership negotiation in the edge agent.
- No RD Control business logic in the edge agent.
