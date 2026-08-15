# printer-agent

`printer-agent` is the edge service that runs inside each print location and bridges local printers to RD Control.
It has one job: translate vendor-specific printer protocols into a single normalized agent-hub contract and keep an outbound-only WSS session to the hub.

## Описание проекта (RU)

`printer-agent` — краевой (edge) сервис, который работает внутри площадки печати и связывает локальные 3D-принтеры с хабом RD Control.
У него одна задача: приводить вендорские протоколы принтеров к единому нормализованному контракту agent-hub и держать исходящую WSS-сессию к хабу.

**Чем он не является:** это не планировщик, не UI и не сервис бизнес-логики. Агент опрашивает принтеры своей площадки, нормализует их состояние, отправляет телеметрию и события в хаб и выполняет команды, пришедшие от хаба.

**Жёсткие границы**

- Никаких входящих портов и пользовательского интерфейса.
- Никакой логики очередей и планирования.
- Никакого прямого знания о хранилище и внутренних сервисах RD Control.
- Код, специфичный для протоколов, живёт только в `src/printer_agent/adapters/`.

**Как устроен**

- `adapters/` — интеграции с протоколами Moonraker (JSON-RPC поверх HTTP), Bambu (MQTT) и Creality (WebSocket на порту 9999 — для прошивок K-серии, где Moonraker закрыт).
- `core/` — нормализованное состояние, вычисление изменений (diff) и надёжное локальное хранилище на SQLite: очередь событий (outbox) и идемпотентность команд.
- `uplink/` — исходящее WSS-соединение, `hello`/`heartbeat` и маршрутизация команд.
- `docs/contracts/agent-hub-v1.md` — версионированный контракт между агентом и RD Control, источник истины для формата сообщений.

**Эксплуатация**

- Секреты (`agent_token`, `access_code`, ключи API) не должны попадать в логи.
- Состояние хранится на диске, поэтому перезапуск сохраняет неотправленные события и результаты команд.
- Настройка на Windows выполняется через локальное приложение Printer Agent и службу; входящих портов при этом не появляется.

**Исключение дублей (multi-agent)**

- Один физический принтер должен иметь один канонический идентификатор `printer_identity` на уровне локации.
- Рекомендуемая стратегия: `printer_identity = sha256(location_key + brand + stable_device_id)`, где `stable_device_id` это Bambu serial или устойчивый идентификатор для Moonraker.
- `msg_id` и `command_id` обеспечивают идемпотентность сообщений, но не решают дубль одного и того же принтера при двух сервисах.
- При одновременной видимости одного принтера несколькими агентами должен действовать один активный owner (lease/claim), остальные инстансы не публикуют дублирующие snapshots/events.
- На стороне БД дедуп должен опираться на `(location_key, printer_identity)` и сигнатуру состояния, а не на транспортные `msg_id`.

**Встраивание в учётную систему**

- У агента нет входящего API — это жёсткая граница. Учётная система (ERP/1C/MES) интегрируется с хабом RD Control, а не с агентом.
- Спецификация: [docs/integration-api-v1.md](docs/integration-api-v1.md) — чтение состояния принтеров, команды печати, вебхуки событий, идемпотентность по `command_id` из документа учётной системы.

Подробности по установке, запуску, обновлению и деплою — в английских разделах ниже.

## What this project is

This repository is the standalone agent for a distributed 3D printer farm. It is not a scheduler, not a UI, and not a business-logic service.
The agent discovers and polls printers in its location, normalizes their state, forwards telemetry and events to the hub, and executes hub-issued commands.

## Hard boundaries

- No inbound ports and no user-facing UI.
- No queueing or scheduling logic.
- No direct knowledge of RD Control storage or internal services.
- Protocol-specific code must stay inside `src/printer_agent/adapters/`.

## Current architecture

- `src/printer_agent/adapters/` protocol integrations for Moonraker, Bambu and Creality.
- `src/printer_agent/core/` normalized state, diffing, and durable local storage.
- `src/printer_agent/uplink/` outbound WSS connection, hello/heartbeat, printer polling, telemetry and event delivery, and command routing.
- `docs/contracts/agent-hub-v1.md` versioned contract between the agent and RD Control.

## What is already scaffolded

- Python 3.11+ asyncio package layout.
- normalized contract models and message envelope helpers.
- config loading from file plus environment overrides.
- crash-safe local outbox and command idempotency storage in SQLite.
- adapter registry for Moonraker, Bambu and Creality.
- WSS uplink that polls printers on `telemetry_interval_s`, batches snapshots into
  one `telemetry` message, queues state changes as durable `event`s, and clears
  them from the outbox on hub `ack`.
- command dispatch with idempotency by `command_id`.
- Dockerfile and systemd unit template.
- Native Windows desktop app (PySide6, Fluent styling) with light/dark/system themes and accent colours.
- Windows installer scripts that create a service, configuration file, and launch shortcuts.

## Local setup

1. Create a virtual environment.
2. Install runtime dependencies with `python -m pip install -r requirements.txt`.
3. Install test dependencies with `python -m pip install -r requirements-dev.txt`.
4. Copy `agent.example.yaml` to `agent.yaml` and fill location-specific settings.

## Run locally

```bash
python -m printer_agent --config agent.yaml run
```

## Check local status

```bash
python -m printer_agent --config agent.yaml status
```

## Move settings to another installation

A *settings bundle* carries the tedious part of a setup — hub wiring, intervals, update
channel and the printer inventory — from one agent to another. It is not a copy of
`agent.yaml`: the outbox database path stays on the machine that owns it, and secrets
(`agent_token`, printer `access_code`) are left out unless `--include-secrets` is given.

```bash
python -m printer_agent --config agent.yaml export-settings --output printer-agent-settings.yaml
python -m printer_agent --config agent.yaml export-settings --output bundle.yaml --include-secrets --note "new PC"
python -m printer_agent --config agent.yaml import-settings printer-agent-settings.yaml
python -m printer_agent --config agent.yaml import-settings bundle.yaml --mode printers --dry-run
```

Import merges the bundle over the local config instead of overwriting the file, so a
redacted bundle applied to an already-configured agent keeps the token and access codes
that agent already had. Everything else is reported by name — what was applied, what was
kept local, and which secrets still have to be filled in. `import-settings` exits `1` when
the result is not runnable yet, so an installer script can tell a partial import from a
finished one; the file is still written.

`--mode printers` replaces only the printer inventory and leaves the local hub wiring,
intervals and update settings untouched — the form to use when the two agents serve
different locations.

The same operation is on the **Перенос настроек** page of the desktop app.

## Update the app

Use the update feed configured under `updates.feed_url` to check or install a newer package build.

```bash
python -m printer_agent --config agent.yaml update
python -m printer_agent --config agent.yaml update --apply
python -m printer_agent publish-update --version 0.2.1 --package-url https://downloads.example.com/printer-agent-0.2.1-py3-none-any.whl --output update.json
```

For GitHub Releases, the feed file should point to a release asset like `https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json` or a known release URL. A ready template is in [docs/printer-agent-update.example.json](docs/printer-agent-update.example.json).

Direct install from a release artifact:

```bash
python -m pip install --upgrade "https://github.com/RomanBRempel/printer-agent/releases/download/v0.1.0a1/printer_agent-0.1.0a1-py3-none-any.whl"
```

Alpha publish flow for GitHub Releases:

```bash
git checkout main
git pull --ff-only
python -m build
git tag v0.1.0a1
git push origin main
git push origin v0.1.0a1
```

This triggers the release workflow, which builds the wheel, generates `printer-agent-update.json`, and publishes both artifacts to the GitHub Release.

Alpha installer flow for Windows:

```powershell
powershell -ExecutionPolicy Bypass -File installer\windows\install.ps1 -PackageSpec "https://github.com/RomanBRempel/printer-agent/releases/download/v0.1.0a1/printer_agent-0.1.0a1-py3-none-any.whl" -UpdateFeedUrl "https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json" -AutoUpdate $true
```

If `updates.auto_update` is enabled, the Windows service checks the feed on startup and installs a newer package before it starts the printer loops.

## Run live printer tests

Set `PRINTER_AGENT_LIVE_TESTS=1` and, if needed, `PRINTER_AGENT_LIVE_CONFIG` to point at the config file that contains your real network printers.

```bash
set PRINTER_AGENT_LIVE_TESTS=1
set PRINTER_AGENT_LIVE_CONFIG=agent.yaml
python -m pytest -m live
```

The live test suite connects to the configured printers, waits for a live snapshot, and fails if the printer stays offline.

## Deploy

- Docker image: `docker build -t printer-agent .`
- systemd: use `systemd/printer-agent.service` as the template unit.
- Windows: run `installer/windows/install.cmd` from an elevated prompt.

## Windows desktop app

The desktop app is the operator surface: service state, live printer status, configuration, updates and logs.

```bash
python -m pip install -r requirements-gui.txt   # or: pip install "printer-agent[gui]"
printer-agent-gui --config agent.yaml           # installed launcher, no console window
python -m printer_agent --config agent.yaml gui # equivalent, from a source checkout
```

`printer-agent-gui` is declared under `[project.gui-scripts]`, so setuptools builds it against
`pythonw.exe` and Windows starts it without a terminal. The installer points both shortcuts at it.

Pages:

| Page | What it does |
|------|--------------|
| Обзор | Windows service state with start/stop/restart, outbox counters, hub wiring, config validation |
| Хаб | Hub URL, agent token, location key, intervals, backoff, outbox — **and the agent → hub check** |
| Принтеры | Printer inventory, network discovery, add/edit/remove, live status, **agent → printer check** |
| Перенос настроек | Export the settings to a bundle file and import one from another installation |
| Обновления | Update feed URL, check and apply a release, auto-update toggles |
| Логи | Tail of the rotating log files under `ProgramData\printer-agent\logs` |
| Оформление | Light / dark / follow-system theme, eight accent colours, poll interval |

The hub link and the printer setup are separate pages on purpose: nothing on **Хаб** knows what a
printer is, and nothing on **Принтеры** knows the hub exists.

### Connectivity checks

[uplink/diagnostics.py](src/printer_agent/uplink/diagnostics.py) reports *stages*, not a boolean —
"could not connect" is useless to an operator, while "TLS established, hello rejected: invalid_token"
names the field to fix.

- **Агент → хаб** (Хаб page): validates the fields, resolves the host, opens WSS, completes the
  `hello` handshake and reports what the hub answered. It checks the values *in the form*, so a URL
  can be tried before it is saved, and it opens its own short-lived session — the running service is
  untouched.
- **Агент → принтер** (Принтеры page): validates the entry, opens a TCP connection, connects the
  adapter, then reads a snapshot and reports the printer's status.

### Network discovery

**Найти в сети** on the Принтеры page sweeps the locally attached subnets:

- **Moonraker** has nothing to announce itself with, so discovery asks each address whether it
  answers `/printer/info`, TCP-preflighted so non-printers fail fast.
- **Bambu Lab** announces itself over SSDP on UDP 2021; the app listens for those datagrams and
  reads the serial, model and user-assigned name straight out of the vendor headers.
- **Creality** is probed the same way as Moonraker but on the vendor WebSocket (port 9999), which
  answers with the hostname and model. A machine that answers both probes is offered once, as
  Moonraker: that adapter is the richer one, and the Creality socket is the fallback for firmware
  that keeps 7125 closed.

The sweep is capped at 1024 addresses — a `/16` is 65k probes and, on a corporate network, is
indistinguishable from a port scan. Already-configured hosts are hidden from the results, and Bambu
entries still require the access code from the printer's own screen; nothing on the network hands
that over. Protocol-specific probing lives in the adapters
([moonraker](src/printer_agent/adapters/moonraker.py), [bambu](src/printer_agent/adapters/bambu.py),
[creality](src/printer_agent/adapters/creality.py));
[core/discovery.py](src/printer_agent/core/discovery.py) only enumerates subnets and merges results.

Theme and accent live in `%APPDATA%\printer-agent\ui.json` — per user, deliberately not in `agent.yaml`,
which is the service's contract and is admin-owned.

The app opens even when `agent.yaml` is missing, unparseable, or rejected by `validate_config` — it is the
tool you use to fix exactly that, and it reports the errors on the Обзор page. When the config lives under
`ProgramData` and the app runs unelevated, saving falls back to a UAC-elevated copy.

Live status is polled by the app itself, independently of the service, only while the window is open, and can
be turned off in Оформление. For Bambu Lab printers the app uses its own MQTT client id so it does not evict
the service's broker session.

The installer writes the shared service configuration to `ProgramData\printer-agent\agent.yaml` and registers
the `printer-agent` Windows service.

## Contract source of truth

- [Agent-Hub contract](docs/contracts/agent-hub-v1.md)
- [Data storage and duplicate prevention](docs/data-storage-and-dedup.md)
- [Integration API v1 for accounting systems](docs/integration-api-v1.md) (hub-side specification; the agent itself exposes no inbound API)

## Operational notes

- Secrets must stay out of logs.
- Runtime state is disk-backed so restarts preserve pending events and command idempotency.
- Only adapter modules may contain vendor protocol logic.
- The desktop app is a local surface only; it does not expose inbound ports.
- In multi-agent deployments, deduplicate by stable printer identity per location. Do not rely on `msg_id` as the only duplicate prevention key.
