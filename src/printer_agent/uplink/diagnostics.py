"""Connectivity checks: agent → hub and agent → printer.

Both checks report *stages* rather than a single boolean. "Не удалось
подключиться" is useless to an operator; "TLS-соединение установлено, hello
отклонён: invalid_token" tells them exactly what to change.

These open their own short-lived session and do not touch the running service.
"""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp

from ..config import AgentConfig, PrinterConfig
from ..contracts import MessageType, build_envelope
from ..core.registry import build_adapter
from .connection import hello_payload, hub_auth_headers, hub_wss_url

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_PORTS = {"moonraker": 7125, "bambu": 8883, "creality": 9999}


@dataclass(slots=True)
class CheckStep:
    key: str
    label: str
    #: True passed, False failed, None not reached.
    ok: bool | None = None
    detail: str = ""
    duration_ms: int | None = None


@dataclass(slots=True)
class CheckResult:
    ok: bool = False
    summary: str = ""
    steps: list[CheckStep] = field(default_factory=list)
    #: Anything worth showing beyond pass/fail — hub ack payload, printer status.
    info: dict[str, Any] = field(default_factory=dict)

    def step(self, key: str) -> CheckStep | None:
        for item in self.steps:
            if item.key == key:
                return item
        return None


class _Recorder:
    """Collects steps and stops the check at the first failure."""

    def __init__(self, result: CheckResult, plan: list[tuple[str, str]]):
        self.result = result
        for key, label in plan:
            result.steps.append(CheckStep(key=key, label=label))

    def start(self, key: str) -> float:
        return time.monotonic()

    def passed(self, key: str, started: float, detail: str = "") -> None:
        step = self.result.step(key)
        if step is not None:
            step.ok = True
            step.detail = detail
            step.duration_ms = int((time.monotonic() - started) * 1000)

    def failed(self, key: str, started: float | None, detail: str) -> CheckResult:
        step = self.result.step(key)
        if step is not None:
            step.ok = False
            step.detail = detail
            if started is not None:
                step.duration_ms = int((time.monotonic() - started) * 1000)
        self.result.ok = False
        self.result.summary = detail
        return self.result


# --------------------------------------------------------------------------- #
# agent -> hub
# --------------------------------------------------------------------------- #

HUB_PLAN: list[tuple[str, str]] = [
    ("config", "Проверка параметров"),
    ("dns", "Разрешение имени хоста"),
    ("connect", "TLS и WebSocket-рукопожатие"),
    ("hello", "Приветствие агента (hello)"),
]


async def check_hub(config: AgentConfig, timeout_s: float = DEFAULT_TIMEOUT_S) -> CheckResult:
    """Open a throwaway session to the hub and complete the `hello` handshake."""
    from .. import __version__

    result = CheckResult()
    recorder = _Recorder(result, HUB_PLAN)

    started = recorder.start("config")
    missing = [
        name
        for name, value in (
            ("hub_url", config.hub_url),
            ("agent_token", config.agent_token),
            ("location_key", config.location_key),
        )
        if not str(value).strip()
    ]
    if missing:
        return recorder.failed("config", started, "Не заполнено: " + ", ".join(missing))

    try:
        url = hub_wss_url(config.hub_url)
    except Exception as exc:
        return recorder.failed("config", started, f"Некорректный адрес хаба: {exc}")
    parsed = urlparse(url)
    if not parsed.hostname:
        return recorder.failed("config", started, f"В адресе «{config.hub_url}» нет имени хоста.")
    recorder.passed("config", started, url)
    result.info["url"] = url

    started = recorder.start("dns")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        return recorder.failed("dns", started, f"Имя «{parsed.hostname}» не разрешается: {exc}")
    resolved = sorted({item[4][0] for item in addresses})
    recorder.passed("dns", started, ", ".join(resolved))

    started = recorder.start("connect")
    client_timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.ws_connect(
                url, headers=hub_auth_headers(config.agent_token)
            ) as websocket:
                recorder.passed("connect", started)

                started = recorder.start("hello")
                adapters = [build_adapter(printer) for printer in config.printers]
                await websocket.send_json(
                    build_envelope(
                        MessageType.hello.value,
                        hello_payload(config, adapters, __version__),
                    )
                )
                return await _await_hello_reply(websocket, recorder, started, timeout_s, result)
    except aiohttp.WSServerHandshakeError as exc:
        hint = ""
        if exc.status in {401, 403}:
            hint = " Похоже на неверный agent_token."
        elif exc.status == 404:
            hint = " Проверьте путь в hub_url — конечная точка агента не найдена."
        return recorder.failed(
            "connect", started, f"Хаб ответил HTTP {exc.status}: {exc.message}.{hint}"
        )
    except aiohttp.ClientConnectorCertificateError as exc:
        return recorder.failed("connect", started, f"Сертификат хаба не принят: {exc}")
    except aiohttp.ClientError as exc:
        return recorder.failed("connect", started, f"Соединение не установлено: {exc}")
    except (TimeoutError, asyncio.TimeoutError):
        return recorder.failed("connect", started, f"Таймаут подключения ({timeout_s:.0f} с).")


async def _await_hello_reply(
    websocket: aiohttp.ClientWebSocketResponse,
    recorder: _Recorder,
    started: float,
    timeout_s: float,
    result: CheckResult,
) -> CheckResult:
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return recorder.failed("hello", started, "Хаб не ответил на hello в отведённое время.")
        try:
            message = await websocket.receive(timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            return recorder.failed("hello", started, "Хаб не ответил на hello в отведённое время.")

        if message.type is aiohttp.WSMsgType.TEXT:
            payload = message.json()
            message_type = payload.get("type")
            body = payload.get("payload") or {}
            if message_type == MessageType.hello_ack.value:
                result.info["hello_ack"] = body
                recorder.passed("hello", started, "Хаб принял агента.")
                result.ok = True
                result.summary = "Связь с хабом установлена."
                return result
            if message_type == MessageType.hello_reject.value:
                reason = str(body.get("reason") or "hello отклонён")
                code = str(body.get("code") or "")
                result.info["hello_reject"] = body
                return recorder.failed(
                    "hello", started, f"Хаб отклонил агента: {reason}" + (f" (код {code})" if code else "")
                )
            # Anything else is unrelated traffic; keep waiting for the reply.
            continue

        if message.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        }:
            return recorder.failed("hello", started, "Хаб закрыл соединение до ответа на hello.")
        if message.type is aiohttp.WSMsgType.ERROR:
            return recorder.failed("hello", started, f"Ошибка WebSocket: {websocket.exception()}")


# --------------------------------------------------------------------------- #
# agent -> printer
# --------------------------------------------------------------------------- #

PRINTER_PLAN: list[tuple[str, str]] = [
    ("config", "Проверка параметров"),
    ("tcp", "TCP-соединение с принтером"),
    ("connect", "Подключение адаптера"),
    ("state", "Чтение состояния"),
]


def _printer_issues(printer: PrinterConfig) -> list[str]:
    issues: list[str] = []
    if not printer.host.strip():
        issues.append("не указан хост")
    if printer.brand not in DEFAULT_PORTS:
        issues.append(f"неизвестный бренд «{printer.brand}»")
    if printer.brand == "bambu":
        credentials = printer.credentials or {}
        if not str(credentials.get("access_code", "")).strip():
            issues.append("не указан access code")
        if not str(credentials.get("serial", "")).strip():
            issues.append("не указан серийный номер")
    return issues


async def check_printer(printer: PrinterConfig, timeout_s: float = DEFAULT_TIMEOUT_S) -> CheckResult:
    """Connect to one printer through its adapter and read a snapshot."""
    result = CheckResult()
    recorder = _Recorder(result, PRINTER_PLAN)

    started = recorder.start("config")
    issues = _printer_issues(printer)
    if issues:
        return recorder.failed("config", started, "; ".join(issues).capitalize())
    port = printer.port or DEFAULT_PORTS[printer.brand]
    recorder.passed("config", started, f"{printer.host}:{port}")

    started = recorder.start("tcp")
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(printer.host, port), timeout=min(timeout_s, 6.0)
        )
        writer.close()
        with suppress(OSError, asyncio.TimeoutError):
            await writer.wait_closed()
    except (TimeoutError, asyncio.TimeoutError):
        return recorder.failed(
            "tcp", started, f"Порт {port} на {printer.host} не отвечает. Проверьте адрес и брандмауэр."
        )
    except OSError as exc:
        return recorder.failed("tcp", started, f"{printer.host}:{port} — {exc}")
    recorder.passed("tcp", started)

    adapter = build_adapter(printer)
    if hasattr(adapter, "client_identifier"):
        serial = str((printer.credentials or {}).get("serial", "")).strip()
        adapter.client_identifier = f"{serial}-agent-check" if serial else None
    # A check must not take the camera away from the running service: the Bambu
    # camera port serves one client at a time.
    if hasattr(adapter, "camera_probes_enabled"):
        adapter.camera_probes_enabled = False

    started = recorder.start("connect")
    try:
        await asyncio.wait_for(adapter.connect(), timeout=timeout_s)
    except (TimeoutError, asyncio.TimeoutError):
        await _close_quietly(adapter)
        return recorder.failed("connect", started, f"Адаптер не подключился за {timeout_s:.0f} с.")
    except Exception as exc:
        await _close_quietly(adapter)
        return recorder.failed("connect", started, str(exc) or exc.__class__.__name__)
    recorder.passed("connect", started)

    started = recorder.start("state")
    try:
        snapshot = await asyncio.wait_for(adapter.get_state(), timeout=timeout_s)
    except (TimeoutError, asyncio.TimeoutError):
        await _close_quietly(adapter)
        return recorder.failed("state", started, "Принтер не вернул состояние за отведённое время.")
    except Exception as exc:
        await _close_quietly(adapter)
        return recorder.failed("state", started, str(exc) or exc.__class__.__name__)

    await _close_quietly(adapter)

    status = str(snapshot.status)
    result.info["snapshot"] = snapshot
    result.info["status"] = status
    if status == "offline":
        message = (snapshot.error.message if snapshot.error else "") or "Принтер сообщает offline."
        return recorder.failed("state", started, message)

    recorder.passed("state", started, f"Статус: {status}")
    result.ok = True
    result.summary = f"Принтер отвечает. Статус: {status}."
    return result


async def _close_quietly(adapter) -> None:
    try:
        await asyncio.wait_for(adapter.disconnect(), timeout=5)
    except Exception:
        pass
