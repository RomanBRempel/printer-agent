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
from .updates import UpdateManifest, apply_update, check_for_update

logger = logging.getLogger(__name__)


class UpdateUnavailable(RuntimeError):
    """This agent cannot act on a hub update request at all.

    Distinct from an install that fails: nothing was attempted, and repeating
    the request changes nothing until the machine's configuration does. It maps
    onto `unsupported` rather than `failed`.
    """

#: How often to re-ask whether the agent is idle enough to restart. Short, so an
#: update lands soon after a transfer ends rather than at the next daily check.
IDLE_POLL_S = 60.0

#: How long to keep waiting for an idle moment before giving up on this cycle.
#: A camera left open all day must not hold a release back forever — the next
#: check comes around anyway.
IDLE_WAIT_LIMIT_S = 6 * 3600.0


class AutoUpdater:
    """Polls the update feed and installs, on the schedule the config sets."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        is_busy: Callable[[], bool],
        restart: Callable[[], None],
        restarts_itself: bool = True,
    ):
        self.config = config
        self._is_busy = is_busy
        self._restart = restart
        self._restarts_itself = restarts_itself
        self._stop_event = asyncio.Event()
        #: Versions this process already failed to install. Retrying the same
        #: broken package every cycle only fills the log and the link.
        self._refused: set[str] = set()
        #: The install started by the hub, if one is still running. One at a
        #: time: a second request while the first is waiting for an idle moment
        #: must not start a parallel pip.
        self._requested_task: asyncio.Task[None] | None = None

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

    async def update_now(self, *, target_version: str = "") -> dict[str, Any]:
        """Check and install because the hub asked, not because the clock did.

        This returns as soon as the *decision* is made, not when the new version
        is running: installing waits for an idle moment and then restarts the
        process, so an answer sent afterwards would never reach the hub. The
        `command_result` therefore reports what was scheduled, and the `hello`
        of the restarted agent — which carries `agent_version` — is what says it
        took effect. Same shape as a `settings_update`, for the same reason.

        Two rules differ from the scheduled cycle, both because a person asked:
        `updates.auto_update` is not consulted (that flag governs the unattended
        path, and this *is* the operator installing it, from the hub instead of
        from the desktop app), and a version this process refused earlier is
        retried once — whatever broke the install may since have been fixed, and
        the alternative is an agent that can never be repaired from the hub.
        """
        if not self.config.updates.feed_url:
            raise UpdateUnavailable("this agent has no update feed configured")
        if self._requested_task is not None and not self._requested_task.done():
            raise UpdateUnavailable("an update requested earlier is still running")

        status = await asyncio.to_thread(check_for_update, self.config.updates.feed_url)
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
            # The hub named a version and the feed offers a different one. Doing
            # it anyway would install something nobody asked for, on a fleet
            # where the operator is watching for one specific number.
            raise UpdateUnavailable(
                f"the update feed offers {manifest.version}, not the requested {target_version}"
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
            # True means the install is queued behind a file transfer or an open
            # camera session, so the restart is minutes away rather than
            # seconds. It gives up after IDLE_WAIT_LIMIT_S and the scheduled
            # check picks it up later.
            "waiting_for_idle": busy,
            # False on a console run, where nobody is there to restart the
            # process: the package is installed and the old code keeps running
            # until someone starts it again. The hub must not wait for a new
            # `hello` that is not coming.
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
