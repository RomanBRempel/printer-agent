# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

`printer-agent` is the edge service that runs inside a print location and bridges local 3D printers to the RD Control hub. Its only job: translate vendor printer protocols into one normalized contract and hold an outbound-only WSS session to the hub.

It is **not** a scheduler, UI, or business-logic service. See [README.md](README.md) and [AGENTS.md](AGENTS.md) for the project's stated boundaries; the rules that most affect code changes are repeated below.

## Commands

The repo uses a local venv at `.venv` (see [.vscode/settings.json](.vscode/settings.json)). Prefix commands with `.venv/Scripts/python.exe` on Windows.

```bash
python -m pip install -r requirements-dev.txt   # runtime + test deps
python -m pip install -r requirements-gui.txt   # + PySide6, needed only for the desktop app
python -m pytest                                 # full suite (live tests auto-skip)
python -m pytest tests/test_config.py            # one file
python -m pytest tests/test_config.py::test_load_config_with_env_override  # one test
python -m printer_agent --config agent.yaml run
python -m printer_agent --config agent.yaml status
python -m printer_agent --config agent.yaml gui  # desktop app
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

**Contract layer** — [src/printer_agent/contracts.py](src/printer_agent/contracts.py) holds the normalized vocabulary: `PrinterSnapshot` (+ `JobSnapshot`, `TemperatureSnapshot`, `ErrorSnapshot`, `PrinterCapabilities`), the `PrinterStatus`/`JobStatus`/`CommandStatus` enums, and `build_envelope()` for the `{v, type, msg_id, ts, payload}` wrapper. `to_dict()` strips `None` fields via `_clean_nested`, so absent data is omitted rather than sent as null. `PrinterSnapshot.state` is the free-form block for slow-moving vendor state; `ams_state(AmsSlot(...))` builds the only shape defined so far, `state.ams.slots[]`, which the hub compares against the filaments a print file asks for. [docs/contracts/agent-hub-v1.md](docs/contracts/agent-hub-v1.md) is the source of truth for wire shapes — keep it in sync with any change here.

**Adapters** — [src/printer_agent/adapters/base.py](src/printer_agent/adapters/base.py) defines `PrinterAdapter`: `connect`/`disconnect`/`get_state` are abstract, every action method defaults to raising `UnsupportedCommandError`. Subclasses override only what the protocol supports; unimplemented actions surface as `status: "unsupported"` rather than failures. [moonraker.py](src/printer_agent/adapters/moonraker.py) polls JSON-RPC over HTTP and returns an offline snapshot on any request error; [bambu.py](src/printer_agent/adapters/bambu.py) holds a long-lived MQTT subscription with its own reconnect backoff and serves `get_state()` from a merged in-memory cache of `print`/`info`/`hms` payloads; [creality.py](src/printer_agent/adapters/creality.py) does the same over the vendor WebSocket on port 9999, for K-series firmware that keeps Moonraker's 7125 closed — it must send an application-level `heart_beat` every 5 s or the firmware drops the client, and its message shapes are lifted from the printer's own web UI (`http://<printer>/static/js/app.*.js`), which is the only documentation that exists. All three map vendor statuses to `PrinterStatus` through a module-level dict with `maintenance` as the unknown-value fallback.

File delivery and camera frames exist only in the Moonraker adapter so far: `upload_file` is the multipart `POST /server/files/upload` every build has (never `/server/jsonrpc`, which Creality's fork lacks), `start_print` addresses the print by its *printer-side* name, and the camera is a still URL — `printer.camera_snapshot_url` if configured, otherwise probed once at `connect()` over the crowsnest defaults. `capabilities.upload` and `capabilities.camera` must state what the adapter *does*, not what the machine could do: the hub turns both into buttons, so a flag raised ahead of the implementation shows the operator a control whose only possible answer is `unsupported`. That is why Bambu reports `upload: false` and `camera: false` while its FTPS and RTSP paths are unwritten, and why `ams` is derived from the last report rather than hard-coded.

**Registry** — [src/printer_agent/core/registry.py](src/printer_agent/core/registry.py) maps brand string → adapter class. A new brand needs an entry here *and* in the `printer.brand not in {...}` check in `validate_config` ([config.py](src/printer_agent/config.py)) — the two lists must agree or valid configs get rejected.

**State diffing** — [src/printer_agent/core/state.py](src/printer_agent/core/state.py) keeps last-known snapshots and returns a `StateChange` only when status, job signature, or error signature differ. The job signature is `(name, status)` only: temperature drift, progress, layer and timing counters deliberately do not raise events — including them would write a durable outbox row on every poll of a running print. All of it rides along in lossy telemetry instead.

**Print file cache** — [src/printer_agent/core/filecache.py](src/printer_agent/core/filecache.py) keeps files delivered by `file_offer` under the hub's own `file_ref`, because that is the only name the follow-up `start_print` carries. That makes the hub the source of a path on this machine, so `validate_file_ref` is a security boundary and not a formality. A download lands in a `.part` file and is moved in with `os.replace` only after its checksum matches — an interrupted transfer must not leave something in the cache that looks whole. Retention is by age and by total size (`print_files` in the config), pruned after each successful delivery.

**Durable outbox** — [src/printer_agent/core/outbox.py](src/printer_agent/core/outbox.py) is a SQLite store (WAL, `synchronous=FULL`) with two tables: `events` (pending until the hub acks by `msg_id`) and `command_results` (idempotency by `command_id`). This is the crash-safety boundary — events survive restarts and a replayed command returns its stored result.

**Uplink** — [uplink/connection.py](src/printer_agent/uplink/connection.py) owns the WSS session: derives `wss://` from `hub_url`, sends `hello` with per-printer capabilities, handles `hello_ack`/`hello_reject`/`ack`/`command`/`error`, and reconnects with exponential backoff between `command_reconnect_backoff_s.min`/`.max`. It runs a second task, `_poll_loop`, for the whole life of `run()`: every `telemetry_interval_s` it polls all adapters concurrently, batches the snapshots into one `telemetry` message, feeds them through `PrinterStateStore` to queue `event`s in the outbox, and flushes unacked events (with a resend delay so a slow ack does not cause a send storm). The poll loop keeps running while the hub is unreachable — telemetry is dropped, events accumulate durably. Only a *retryable* `hello_reject` reconnects; every other code raises `HubRejected` out of `run()` and stops the agent, since retrying an unknown token cannot change the answer. Each poll cycle also stats `config.source_path` and, when the file changed, rebuilds the adapters for added/removed/edited printers and pushes an unsolicited `inventory` — a printer added at a location reaches the hub without a restart. Hub URL, token, location and outbox path are *not* hot: they are logged as needing a restart, because the session and the open database are the things that would have to be torn down to adopt them. An `error` from the hub now settles the referenced event in the outbox rather than only logging it, so a refusal the hub repeats forever cannot grow the queue forever. [uplink/commands.py](src/printer_agent/uplink/commands.py) dispatches all four command-bearing hub messages — `command`, `file_offer`, `camera_request`, `camera_stop` — to adapters and maps exceptions onto `CommandStatus`; every one of them checks the outbox for an existing result *before* executing, which is where idempotency is enforced, and every one answers with the `command_id` it was given (a `command_result` without one is refused by the hub as `command_id_required`). A `file_offer` runs in a task *beside* the receive loop: a gcode transfer takes minutes, and doing it inline would leave the session unable to send a heartbeat or answer anything else for the whole download.

**File delivery and camera** — [uplink/files.py](src/printer_agent/uplink/files.py) fetches an offered file with the same bearer token as the socket, streams it to disk while hashing it, checks `sha256` *and* `size_bytes`, and only then lets the adapter upload it; a mismatch deletes the download and nothing reaches the printer. Only `503` is retried — the other refusals (`401`/`403`/`404`/`409`) name a cause a repeat cannot change. [uplink/camera.py](src/printer_agent/uplink/camera.py) runs at most one frame loop per printer, posting stills to the address `camera_request` names. Two rules there are not obvious from the code shape: the upload's answer outranks `camera_stop` (`404`/`409` stop the stream immediately, because a stop command may never arrive), and frames are never queued or resent — a late frame shows a print that has since changed. A hub that answers nothing at all for the length of `expires_at` also stops the stream, so a lost link cannot leave a camera filming.

**Config** — [config.py](src/printer_agent/config.py) loads YAML, applies uppercase env overrides (`HUB_URL`, `AGENT_TOKEN`, `LOCATION_KEY`, `TELEMETRY_INTERVAL_S`, `HEARTBEAT_INTERVAL_S`, `OUTBOX_DATABASE_PATH`, `UPDATE_FEED_URL`, `AUTO_UPDATE`, `UPDATE_CHECK_ON_STARTUP`), then validates. Validation collects *all* errors into `ConfigError.errors` rather than failing on the first. A missing config file is not an error — env-only configuration is supported. A key written with no value after it parses as `None`, so the required fields read it as `data.get(key) or ""`: `str(None)` is the four-character token `"None"`, which passes every required-field check and fails only at the hub, as a bad credential. `print_files` bounds the delivered-file cache and `printers[].camera_snapshot_url` names a camera the adapter would not find by itself; neither is a secret, so both travel in a settings bundle. `hub_url` carries the full endpoint URL including the path (`https://host/api/printers/agent`); a bare host reaches the site root and gets HTML instead of a WebSocket handshake, so `_hub_wss_url()` falls back to `DEFAULT_AGENT_PATH` and logs a warning.

**Settings transfer** — [settings_bundle.py](src/printer_agent/settings_bundle.py) moves a setup between installations. A bundle is deliberately *not* a copy of `agent.yaml`: `outbox.database_path` never travels (it names a folder the target may not own), and secrets — `agent_token` plus the credential fields in `SECRET_CREDENTIAL_KEYS` — are stripped unless the export asks for them. A Bambu `serial` is *not* a secret and stays, or the entry would be useless. `apply_bundle` merges over the local config rather than replacing it, so re-importing a redacted bundle keeps credentials the target already had, and reports every field as applied / kept-local / still-missing instead of leaving blanks. Reading the config for this path goes through `load_config_file`, not `parse_config`: env overrides must not get baked into the saved file.

**Entry points** — [cli.py](src/printer_agent/cli.py) (`run`, `status`, `gui`, `install-service`, `uninstall-service`, `update`, `publish-update`, `export-settings`, `import-settings`), [windows_service.py](src/printer_agent/windows_service.py) (pywin32 service; guards its imports so the module is importable on non-Windows), [desktop/](src/printer_agent/desktop/) (the PySide6 app; [gui.py](src/printer_agent/gui.py) is a shim kept for pre-existing shortcuts), [updates.py](src/printer_agent/updates.py) (manifest feed + `pip install --upgrade`). Cutting a release has two traps that have already cost a shop floor its update — the manifest checksum must be read off the *published* wheel (`publish-update --sha256 from-url`), and a release marked pre-release is invisible to the feed, which resolves `releases/latest`. Both are written down in [docs/release.md](docs/release.md); follow it rather than reconstructing the order.

**Desktop app** — [src/printer_agent/desktop/](src/printer_agent/desktop/) is the operator UI and the only place Qt is imported; the service and CLI never touch it, and PySide6 lives in the `gui` extra rather than in runtime deps. [theme.py](src/printer_agent/desktop/theme.py) is pure data and string building — palettes, accent presets, and the QSS — so the colour system is testable without a display; only `apply_window_backdrop` touches Win32. [prefs.py](src/printer_agent/desktop/prefs.py) keeps theme/accent in `%APPDATA%\printer-agent\ui.json`, deliberately outside `agent.yaml`. [state.py](src/printer_agent/desktop/state.py) holds `AppState` plus a *tolerant* config reader: the editor must open on a config the service rejects, which is the whole point of the page. [probe.py](src/printer_agent/desktop/probe.py) runs adapter polling, service queries and blocking one-shots off the UI thread.

**Diagnostics and discovery** — [uplink/diagnostics.py](src/printer_agent/uplink/diagnostics.py) holds `check_hub` and `check_printer`; both return a staged `CheckResult` rather than a boolean, and both reuse the real handshake helpers (`hub_wss_url`, `hello_payload`) so a passing check means the service will connect too. [core/discovery.py](src/printer_agent/core/discovery.py) enumerates subnets and merges results; the actual probing is per-brand and lives in the adapters (`discover_moonraker`, `discover_creality`, `discover_bambu`/`parse_bambu_ssdp`), because a Moonraker HTTP probe and a Bambu SSDP datagram are vendor protocol details. `merge` drops a `creality` record when the same host also answered as `moonraker` — Creality firmware can expose both, and the richer adapter wins.

Two invariants worth keeping:

- Every `asyncio.run` in the package goes through [aio.py](src/printer_agent/aio.py). Windows defaults to the Proactor loop, which has no `add_reader`/`add_writer`; paho (under `aiomqtt`) needs both, so a Bambu adapter on a Proactor loop connects, subscribes and then receives nothing — surfacing only as an empty MQTT cache much later. [tests/test_bambu_transport.py](tests/test_bambu_transport.py) fails on a bare `asyncio.run` anywhere in `src/printer_agent/`.
- `cli.py` dispatches `gui` **before** `load_config`. Validating first is what made the old shortcut die silently under `pythonw.exe`, where `parser.exit()` writes to a `sys.stderr` that does not exist.
- `printer-agent-gui` belongs in `[project.gui-scripts]`, not `[project.scripts]` — that is what makes the launcher a GUI-subsystem exe and stops Windows opening a console.
- The Windows installer must ship the wheel it was built from. [build-gui-installer-exe.ps1](installer/windows/build-gui-installer-exe.ps1) builds one and bundles it; without that, `Resolve-InstallTarget` falls through to the update feed and installs the *previous* release, which is how an install once ended up running the old Tkinter editor behind a console window. [install.ps1](installer/windows/install.ps1) now force-reinstalls the package (a local build carries the same version string, so pip would otherwise keep the old code) and refuses to create shortcuts unless `printer_agent.desktop` imports and the launcher is PE subsystem 2.

Both entry points now run the same thing: `cli.run_agent` and `PrinterAgentService._run` in [windows_service.py](src/printer_agent/windows_service.py) each build an `EventOutbox`, hand it to `HubConnection`, and await `run()`.

## Rules for changes here

- Protocol-specific code stays inside `src/printer_agent/adapters/`. Nothing outside that package should know Moonraker JSON-RPC shapes or Bambu MQTT topics.
- Preserve the outbound-only model — no inbound HTTP or listening ports in the agent service. The desktop app is a local surface only; its printer polling is outbound and stops with the window.
- No scheduling, queueing, or RD Control business logic; no persistence beyond the outbox and idempotency store.
- Update [docs/contracts/agent-hub-v1.md](docs/contracts/agent-hub-v1.md) alongside any message-shape change, and follow its evolution rules: add fields, never rename or remove them; breaking changes bump `PROTOCOL_VERSION`.
- This repo is the *owner* of that contract and RD Control is the follower — cross-repository coordination rules (document before code, hub deployed before agents, handoff written as a file rather than agreed in chat) live in [docs/agent-collaboration.md](docs/agent-collaboration.md). Never edit the RD Control repository directly; write a handoff file under `docs/handoff/` instead.
- Keep runtime dependencies minimal (currently `aiohttp`, `aiomqtt`, `PyYAML`); mirror any change across [requirements.txt](requirements.txt) and `pyproject.toml`. PySide6 is a desktop-only dependency and belongs in the `gui` extra and [requirements-gui.txt](requirements-gui.txt), never in the runtime set.
- Never log `agent_token`, Bambu `access_code`, or Moonraker API keys.

## Repository skill conventions

[.github/skills/README.md](.github/skills/README.md) asks that skills be preferred over ad-hoc reasoning, and routes by which agent is running.

Available to Copilot / VS Code chat via the Pylance extension, **not** to the Claude Code CLI: `python-fact-grounded-coding` for debugging grounded in runtime output, `pylance-refactoring` for refactors and import cleanup, plus `pylance-python-profiling` and `pylance-docs`.

Earlier revisions also named `managing-python-dependencies` and `project-setup-info-local`; neither is installed anywhere in this workspace, so those references are stale.

The one skill vendored into `.github/skills/` — `premium-frontend-ui` — is out of scope here and should not be applied; see [.github/skills/CHANGELOG.md](.github/skills/CHANGELOG.md). That changelog is append-only and records every skill admission, rejection, and repair.

The `<!-- agent-ninja-START/END -->` markers in [AGENTS.md](AGENTS.md) and [.github/skills/README.md](.github/skills/README.md) fence a *generated* span. Never move hand-written rules inside them — a regeneration on 2026-08-14 deleted the entire AGENTS.md working-rules section that way.
