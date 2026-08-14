# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

`printer-agent` is the edge service that runs inside a print location and bridges local 3D printers to the RD Control hub. Its only job: translate vendor printer protocols into one normalized contract and hold an outbound-only WSS session to the hub.

It is **not** a scheduler, UI, or business-logic service. See [README.md](README.md) and [AGENTS.md](AGENTS.md) for the project's stated boundaries; the rules that most affect code changes are repeated below.

## Commands

The repo uses a local venv at `.venv` (see [.vscode/settings.json](.vscode/settings.json)). Prefix commands with `.venv/Scripts/python.exe` on Windows.

```bash
python -m pip install -r requirements-dev.txt   # runtime + test deps
python -m pytest                                 # full suite (live tests auto-skip)
python -m pytest tests/test_config.py            # one file
python -m pytest tests/test_config.py::test_load_config_with_env_override  # one test
python -m printer_agent --config agent.yaml run
python -m printer_agent --config agent.yaml status
python -m printer_agent gui --config agent.yaml
```

`pytest` picks up `pythonpath = ["src"]` from [pyproject.toml](pyproject.toml), so no install is needed to run tests. There is no configured linter or formatter.

### Live printer tests

[tests/test_live_printers.py](tests/test_live_printers.py) is marked `live` and skips at module level unless `PRINTER_AGENT_LIVE_TESTS=1`. It connects to the real printers in `PRINTER_AGENT_LIVE_CONFIG` (default `agent.yaml`) and fails if a printer reports `offline`.

```bash
set PRINTER_AGENT_LIVE_TESTS=1
set PRINTER_AGENT_LIVE_CONFIG=agent.yaml
python -m pytest -m live
```

## Architecture

Data flows in one direction on each side: adapters normalize *up* into contract dataclasses, the hub sends commands *down* through the same adapters.

**Contract layer** — [src/printer_agent/contracts.py](src/printer_agent/contracts.py) holds the normalized vocabulary: `PrinterSnapshot` (+ `JobSnapshot`, `TemperatureSnapshot`, `ErrorSnapshot`, `PrinterCapabilities`), the `PrinterStatus`/`JobStatus`/`CommandStatus` enums, and `build_envelope()` for the `{v, type, msg_id, ts, payload}` wrapper. `to_dict()` strips `None` fields via `_clean_nested`, so absent data is omitted rather than sent as null. [docs/contracts/agent-hub-v1.md](docs/contracts/agent-hub-v1.md) is the source of truth for wire shapes — keep it in sync with any change here.

**Adapters** — [src/printer_agent/adapters/base.py](src/printer_agent/adapters/base.py) defines `PrinterAdapter`: `connect`/`disconnect`/`get_state` are abstract, every action method defaults to raising `UnsupportedCommandError`. Subclasses override only what the protocol supports; unimplemented actions surface as `status: "unsupported"` rather than failures. [moonraker.py](src/printer_agent/adapters/moonraker.py) polls JSON-RPC over HTTP and returns an offline snapshot on any request error; [bambu.py](src/printer_agent/adapters/bambu.py) holds a long-lived MQTT subscription with its own reconnect backoff and serves `get_state()` from a merged in-memory cache of `print`/`info`/`hms` payloads. Both map vendor statuses to `PrinterStatus` through a module-level dict with `maintenance` as the unknown-value fallback.

**Registry** — [src/printer_agent/core/registry.py](src/printer_agent/core/registry.py) maps brand string → adapter class. A new brand needs an entry here *and* in the `printer.brand not in {...}` check in `validate_config` ([config.py](src/printer_agent/config.py)) — the two lists must agree or valid configs get rejected.

**State diffing** — [src/printer_agent/core/state.py](src/printer_agent/core/state.py) keeps last-known snapshots and returns a `StateChange` only when status, job signature, or error signature differ. Temperature drift deliberately does not raise an event; temps ride along in lossy telemetry instead.

**Durable outbox** — [src/printer_agent/core/outbox.py](src/printer_agent/core/outbox.py) is a SQLite store (WAL, `synchronous=FULL`) with two tables: `events` (pending until the hub acks by `msg_id`) and `command_results` (idempotency by `command_id`). This is the crash-safety boundary — events survive restarts and a replayed command returns its stored result.

**Uplink** — [uplink/connection.py](src/printer_agent/uplink/connection.py) owns the WSS session: derives `wss://` from `hub_url`, sends `hello` with per-printer capabilities, handles `hello_ack`/`hello_reject`/`ack`/`command`, and reconnects with exponential backoff between `command_reconnect_backoff_s.min`/`.max`. [uplink/commands.py](src/printer_agent/uplink/commands.py) dispatches actions to adapters and maps exceptions onto `CommandStatus` — it checks the outbox for an existing result *before* executing, which is where idempotency is enforced.

**Config** — [config.py](src/printer_agent/config.py) loads YAML, applies uppercase env overrides (`HUB_URL`, `AGENT_TOKEN`, `LOCATION_KEY`, `TELEMETRY_INTERVAL_S`, `HEARTBEAT_INTERVAL_S`, `OUTBOX_DATABASE_PATH`, `UPDATE_FEED_URL`, `AUTO_UPDATE`, `UPDATE_CHECK_ON_STARTUP`), then validates. Validation collects *all* errors into `ConfigError.errors` rather than failing on the first. A missing config file is not an error — env-only configuration is supported.

**Entry points** — [cli.py](src/printer_agent/cli.py) (`run`, `status`, `gui`, `install-service`, `uninstall-service`, `update`, `publish-update`), [windows_service.py](src/printer_agent/windows_service.py) (pywin32 service; guards its imports so the module is importable on non-Windows), [gui.py](src/printer_agent/gui.py) (Tkinter config editor), [updates.py](src/printer_agent/updates.py) (manifest feed + `pip install --upgrade`).

### Known scaffold gap

The CLI `run` command currently logs a startup line and blocks on an empty `asyncio.Event` — it does **not** start `HubConnection` or any printer polling loop. Only `PrinterAgentService._run` in [windows_service.py](src/printer_agent/windows_service.py) wires the outbox and hub connection together. Wiring the CLI path is pending work, not an oversight to route around.

## Rules for changes here

- Protocol-specific code stays inside `src/printer_agent/adapters/`. Nothing outside that package should know Moonraker JSON-RPC shapes or Bambu MQTT topics.
- Preserve the outbound-only model — no inbound HTTP or listening ports in the agent service. The GUI is a local config surface only.
- No scheduling, queueing, or RD Control business logic; no persistence beyond the outbox and idempotency store.
- Update [docs/contracts/agent-hub-v1.md](docs/contracts/agent-hub-v1.md) alongside any message-shape change, and follow its evolution rules: add fields, never rename or remove them; breaking changes bump `PROTOCOL_VERSION`.
- Keep runtime dependencies minimal (currently `aiohttp`, `aiomqtt`, `PyYAML`); mirror any change across [requirements.txt](requirements.txt) and `pyproject.toml`.
- Never log `agent_token`, Bambu `access_code`, or Moonraker API keys.

## Repository skill conventions

[.github/skills/README.md](.github/skills/README.md) asks that these skills be preferred over ad-hoc reasoning: `managing-python-dependencies` for dependency/environment changes, `project-setup-info-local` for scaffolding, `python-fact-grounded-coding` for debugging grounded in runtime output, `pylance-refactoring` for refactors and import cleanup.
