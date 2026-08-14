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

- `adapters/` — интеграции с протоколами Moonraker (JSON-RPC поверх HTTP) и Bambu (MQTT).
- `core/` — нормализованное состояние, вычисление изменений (diff) и надёжное локальное хранилище на SQLite: очередь событий (outbox) и идемпотентность команд.
- `uplink/` — исходящее WSS-соединение, `hello`/`heartbeat` и маршрутизация команд.
- `docs/contracts/agent-hub-v1.md` — версионированный контракт между агентом и RD Control, источник истины для формата сообщений.

**Эксплуатация**

- Секреты (`agent_token`, `access_code`, ключи API) не должны попадать в логи.
- Состояние хранится на диске, поэтому перезапуск сохраняет неотправленные события и результаты команд.
- Настройка на Windows выполняется через локальный GUI и службу; входящих портов при этом не появляется.

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

- `src/printer_agent/adapters/` protocol integrations for Moonraker and Bambu.
- `src/printer_agent/core/` normalized state, diffing, and durable local storage.
- `src/printer_agent/uplink/` outbound WSS connection, hello/heartbeat, and command routing.
- `docs/contracts/agent-hub-v1.md` versioned contract between the agent and RD Control.

## What is already scaffolded

- Python 3.11+ asyncio package layout.
- normalized contract models and message envelope helpers.
- config loading from file plus environment overrides.
- crash-safe local outbox and command idempotency storage in SQLite.
- adapter registry for Moonraker and Bambu.
- WSS uplink and command dispatch skeletons.
- Dockerfile and systemd unit template.
- Windows configuration GUI backed by Tkinter.
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

## Windows configuration

The GUI opens with `python -m printer_agent gui --config agent.yaml` or through the Start Menu shortcut created by the installer.
The installer writes the shared service configuration to `ProgramData\printer-agent\agent.yaml` and registers the `printer-agent` Windows service.

## Contract source of truth

- [Agent-Hub contract](docs/contracts/agent-hub-v1.md)
- [Data storage and duplicate prevention](docs/data-storage-and-dedup.md)
- [Integration API v1 for accounting systems](docs/integration-api-v1.md) (hub-side specification; the agent itself exposes no inbound API)

## Operational notes

- Secrets must stay out of logs.
- Runtime state is disk-backed so restarts preserve pending events and command idempotency.
- Only adapter modules may contain vendor protocol logic.
- The GUI is a local configuration surface only; it does not expose inbound ports.
- In multi-agent deployments, deduplicate by stable printer identity per location. Do not rely on `msg_id` as the only duplicate prevention key.
