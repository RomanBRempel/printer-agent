"""Unattended updates: check on a schedule, install when it is safe to restart.

The update machinery already existed, but only ran once, at service start — a
box that stays up for weeks never saw a release. This is the part that makes it
happen without anyone opening the app: a task beside the hub session that polls
the feed, and applies a new version the moment nothing would be lost by
restarting.

What "safe" means here is narrow on purpose. A restart does **not** disturb a
print: the printer runs the job by itself, the outbox keeps unacked events
across the restart, and telemetry resumes within seconds of reconnecting. What a
restart does destroy is work the agent is holding in memory — a print file
mid-transfer, and a camera session someone is watching. So the update waits for
those, and for nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .config import AgentConfig
from .contracts import utc_now_iso
from .updates import UpdateManifest, apply_update, check_for_update

logger = logging.getLogger(__name__)

#: How often to re-ask whether the agent is idle enough to restart. Short, so an
#: update lands soon after a transfer ends rather than at the next daily check.
IDLE_POLL_S = 60.0

#: How long to keep waiting for an idle moment before giving up on this cycle.
#: A camera left open all day must not hold a release back forever — the next
#: check comes around anyway.
IDLE_WAIT_LIMIT_S = 6 * 3600.0


class UpdateUnavailable(RuntimeError):
    """Обновить по просьбе хаба нельзя, и причина человеку понятна.

    Отдельный класс, а не общий сбой: «фид не настроен» и «предыдущая установка
    ещё идёт» — состояния, а не аварии, и в ленте команд они обязаны читаться
    как ответ, а не как поломка агента.
    """


class AutoUpdater:
    """Polls the update feed and installs, on the schedule the config sets."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        is_busy: Callable[[], bool],
        restart: Callable[[], None],
        restarts_itself: bool = False,
    ):
        self.config = config
        self._is_busy = is_busy
        self._restart = restart
        #: Перезапустит ли себя этот процесс после установки. У службы — да, у
        #: консольного запуска — нет: там за клавиатурой сидит человек, и убить
        #: его сессию ради смены версии было бы неожиданностью. Хаб обязан это
        #: знать, иначе он будет ждать `hello` новой версии, который не придёт.
        self._restarts_itself = restarts_itself
        self._stop_event = asyncio.Event()
        #: Установка, запрошенная хабом. Держится, чтобы вторая просьба не
        #: запустила вторую установку поверх идущей.
        self._requested_task: asyncio.Task[None] | None = None
        #: Чем кончилась последняя проверка фида и КОГДА она была. Хаб
        #: показывает это рядом с агентом, поэтому отметка времени обязательна:
        #: «доступна 0.1.0a32» без неё выглядит свежим фактом и через трое суток
        #: — тот же класс молчания, что у ошибки принтера без времени.
        #: `None` означает «ещё не проверяли», и это НЕ то же самое, что
        #: «обновлений нет»: показать второе вместо первого значит объявить
        #: агента актуальным, ничего об этом не зная.
        self._last_status: dict[str, Any] | None = None
        #: Versions this process already failed to install. Retrying the same
        #: broken package every cycle only fills the log and the link.
        self._refused: set[str] = set()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        if not self.config.updates.feed_url:
            logger.info("no update feed configured", extra={"action": "auto_update"})
            return

        if self.config.updates.check_on_startup:
            await self._cycle()

        interval = self.config.updates.check_interval_h * 3600
        if interval <= 0:
            # Startup-only, which is what every install did before this existed.
            return
        while not self._stop_event.is_set():
            await self._sleep(interval)
            if self._stop_event.is_set():
                return
            await self._cycle()

    def _remember(self, status: Any) -> None:
        """Запомнить исход проверки фида — единственная точка записи."""
        self._last_status = {
            "current_version": str(getattr(status, "current_version", "") or ""),
            "latest_version": str(getattr(status, "latest_version", "") or ""),
            "update_available": bool(getattr(status, "update_available", False)),
            "checked_at": utc_now_iso(),
        }

    def known_status(self) -> dict[str, Any] | None:
        """Что известно про обновление — для `hello` и `inventory`.

        Возвращает КОПИЮ: снимок уезжает в сообщение, и вызывающий не должен
        мочь испортить состояние апдейтера, дописав в него поле.
        """
        return dict(self._last_status) if self._last_status else None

    async def update_now(self, *, target_version: str = "") -> dict[str, Any]:
        """Проверить и установить, потому что попросил человек, а не часы.

        Возвращает управление, как только принято РЕШЕНИЕ, а не когда новая
        версия поднялась: установка ждёт простоя и перезапускает процесс, и
        ответ, посланный после этого, до хаба уже не дойдёт. Поэтому
        `command_result` сообщает, что запланировано, а факт подтверждает
        `hello` перезапустившегося агента — он несёт `agent_version`. Та же
        форма и та же причина, что у `settings_update`.

        Два правила отличаются от планового цикла, и оба потому, что попросил
        человек: `updates.auto_update` не спрашивается вовсе (тот флаг управляет
        необслуживаемым путём, а здесь оператор ставит обновление сам, только из
        хаба, а не из десктопного приложения), и версия, которую этот процесс
        раньше отказался ставить, пробуется ещё раз — сломавшее установку могло
        с тех пор почини́ться, а иначе агента нельзя починить из хаба вообще.
        """
        if not self.config.updates.feed_url:
            raise UpdateUnavailable("у этого агента не настроен адрес обновлений")
        if self._requested_task is not None and not self._requested_task.done():
            raise UpdateUnavailable("установка, запрошенная раньше, ещё идёт")

        status = await asyncio.to_thread(check_for_update, self.config.updates.feed_url)
        self._remember(status)
        if not status.update_available or status.manifest is None:
            logger.info(
                "hub asked for an update; already on the latest version",
                extra={"action": "hub_update", "version": status.current_version},
            )
            return {
                "scheduled": False,
                "reason": "already_latest",
                "current_version": status.current_version,
                "latest_version": status.latest_version,
            }

        manifest = status.manifest
        if target_version and target_version != manifest.version:
            # Хаб назвал версию, а фид предлагает другую. Поставить всё равно
            # значит установить то, чего никто не просил, на парк, где оператор
            # ждёт одно конкретное число.
            raise UpdateUnavailable(
                f"фид предлагает {manifest.version}, а запрошена {target_version}"
            )

        self._refused.discard(manifest.version)
        self._requested_task = asyncio.create_task(
            self._apply_when_idle(manifest), name="printer-agent-update-request"
        )
        busy = self._is_busy()
        logger.info(
            "hub asked for an update",
            extra={
                "action": "hub_update",
                "version": status.current_version,
                "latest": manifest.version,
                "busy": str(busy),
            },
        )
        return {
            "scheduled": True,
            "current_version": status.current_version,
            "latest_version": manifest.version,
            # True означает, что установка стоит за передачей файла либо за
            # открытой камерой, то есть перезапуск в минутах, а не в секундах.
            "waiting_for_idle": busy,
            "restarts_itself": self._restarts_itself,
        }

    async def _cycle(self) -> None:
        try:
            status = await asyncio.to_thread(check_for_update, self.config.updates.feed_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A shop-floor link drops; that is not a reason to stop checking.
            logger.warning(
                "update check failed", extra={"action": "auto_update", "error": str(exc)}
            )
            return

        self._remember(status)
        if not status.update_available or status.manifest is None:
            logger.info(
                "agent is on the latest version",
                extra={"action": "auto_update", "version": status.current_version},
            )
            return

        logger.info(
            "update available",
            extra={
                "action": "auto_update",
                "version": status.current_version,
                "latest": status.latest_version,
            },
        )
        if not self.config.updates.auto_update:
            # Announced, not applied: the operator installs it from the app.
            return
        if status.manifest.version in self._refused:
            return
        await self._apply_when_idle(status.manifest)

    async def _apply_when_idle(self, manifest: UpdateManifest) -> None:
        if not await self._wait_until_idle(manifest):
            return

        logger.info(
            "installing update", extra={"action": "auto_update", "latest": manifest.version}
        )
        try:
            applied = await asyncio.to_thread(apply_update, manifest)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._refused.add(manifest.version)
            logger.error(
                "update install failed",
                extra={"action": "auto_update", "latest": manifest.version, "error": str(exc)},
            )
            return

        if not applied.installed:
            self._refused.add(manifest.version)
            logger.error(
                "update install failed",
                extra={
                    "action": "auto_update",
                    "latest": manifest.version,
                    "error": applied.message,
                },
            )
            return

        logger.info(
            "update installed; restarting to run it",
            extra={"action": "auto_update", "latest": manifest.version},
        )
        self._restart()

    async def _wait_until_idle(self, manifest: UpdateManifest) -> bool:
        waited = 0.0
        while self._is_busy():
            if self._stop_event.is_set():
                return False
            if waited >= IDLE_WAIT_LIMIT_S:
                logger.info(
                    "update postponed: the agent stayed busy",
                    extra={"action": "auto_update", "latest": manifest.version},
                )
                return False
            if waited == 0.0:
                logger.info(
                    "update waiting for a file transfer or camera session to finish",
                    extra={"action": "auto_update", "latest": manifest.version},
                )
            await self._sleep(IDLE_POLL_S)
            waited += IDLE_POLL_S
        return not self._stop_event.is_set()

    async def _sleep(self, delay: float) -> None:
        with suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
