"""On-demand camera frames, pushed to a hub address the command names.

The agent opens no ports, and a shop-floor camera speaks HTTP while the portal
speaks HTTPS, so nobody can pull a stream through the agent. The contract turns
that around: the hub hands out a session URL, the agent posts still frames to it
at the interval the hub chose, and the hub keeps only the last one.

Two properties of that arrangement matter more than the loop itself:

- **the upload response outranks `camera_stop`.** A stop command may never
  arrive — the session can expire while the agent is reconnecting — so `404`
  and `409` end the stream on the spot, without waiting to be told twice;
- **frames are lossy, like telemetry.** Nothing is queued, nothing is resent.
  A frame delivered a minute late shows the past, and showing the past on a
  print that has since gone wrong is worse than showing nothing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..adapters.base import PrinterAdapter, UnsupportedCommandError
from ..contracts import utc_now_iso
from .files import parse_iso8601

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 2.0
MIN_INTERVAL_S = 0.5
MAX_INTERVAL_S = 60.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

#: How long the stream keeps running without a single answer from the hub. The
#: hub's own session TTL arrives as `expires_at`; this is the floor under it, so
#: a lost link cannot leave a camera filming forever.
MIN_SILENCE_WINDOW_S = 30.0
DEFAULT_SILENCE_WINDOW_S = 120.0

#: Consecutive capture failures before the session gives up. A camera that has
#: been unplugged should stop the loop, not fill the log until `expires_at`.
MAX_CAPTURE_FAILURES = 5

#: Statuses that mean the session is gone: stop immediately.
SESSION_GONE_STATUSES = {404, 409}


class CameraError(RuntimeError):
    """The camera command could not be carried out."""


@dataclass(slots=True)
class CameraSession:
    session_id: str
    printer_key: str
    upload_url: str
    interval_s: float = DEFAULT_INTERVAL_S
    max_bytes: int = DEFAULT_MAX_BYTES
    silence_window_s: float = DEFAULT_SILENCE_WINDOW_S
    frames_sent: int = 0
    task: asyncio.Task[None] | None = field(default=None, compare=False)


@dataclass(slots=True)
class _UploadOutcome:
    reached_hub: bool
    should_continue: bool
    interval_s: float | None = None
    status: int = 0
    reason: str = ""


class CameraService:
    """Owns at most one frame loop per printer."""

    def __init__(self, agent_token: str):
        self._agent_token = agent_token
        self._sessions: dict[str, CameraSession] = {}

    def active_session(self, printer_key: str) -> CameraSession | None:
        return self._sessions.get(printer_key)

    async def start(self, adapter: PrinterAdapter, payload: dict[str, Any]) -> dict[str, Any]:
        if not adapter.capabilities().camera:
            raise UnsupportedCommandError(
                f"{adapter.printer.brand} printer {adapter.printer_key} has no camera on this agent"
            )
        session_id = str(payload.get("session_id", "")).strip()
        upload_url = str(payload.get("upload_url", "")).strip()
        if not session_id:
            raise CameraError("camera_request without session_id")
        if not upload_url:
            raise CameraError(f"camera_request {session_id} without upload_url")

        session = CameraSession(
            session_id=session_id,
            printer_key=adapter.printer_key,
            upload_url=upload_url,
            interval_s=_interval(payload.get("interval_s")),
            max_bytes=_max_bytes(payload.get("max_bytes")),
            silence_window_s=_silence_window(payload.get("expires_at")),
        )

        # The hub does not open a second session for the same printer, but a
        # replayed command must not leave two loops filming into one URL.
        await self._cancel(adapter.printer_key, reason="replaced by a new session")

        self._sessions[adapter.printer_key] = session
        session.task = asyncio.create_task(
            self._run(adapter, session), name=f"printer-agent-camera-{adapter.printer_key}"
        )
        logger.info(
            "camera session started",
            extra={
                "action": "camera",
                "printer_key": adapter.printer_key,
                "session_id": session_id,
                "interval_s": str(session.interval_s),
            },
        )
        return {
            "session_id": session_id,
            "streaming": True,
            "interval_s": session.interval_s,
        }

    async def stop(self, printer_key: str, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(printer_key)
        if session is None:
            return {"session_id": session_id, "stopped": False, "reason": "no active camera session"}
        if session_id and session.session_id != session_id:
            # A stop for a session that has already been replaced must not put
            # out the stream someone is watching right now.
            return {
                "session_id": session_id,
                "stopped": False,
                "reason": f"another session is active ({session.session_id})",
            }
        await self._cancel(printer_key, reason="camera_stop")
        return {"session_id": session.session_id, "stopped": True}

    async def stop_all(self) -> None:
        for printer_key in list(self._sessions):
            await self._cancel(printer_key, reason="agent shutting down")

    async def _cancel(self, printer_key: str, *, reason: str) -> None:
        session = self._sessions.pop(printer_key, None)
        if session is None:
            return
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        logger.info(
            "camera session stopped",
            extra={
                "action": "camera",
                "printer_key": printer_key,
                "session_id": session.session_id,
                "reason": reason,
                "frames": str(session.frames_sent),
            },
        )

    async def _run(self, adapter: PrinterAdapter, session: CameraSession) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + session.silence_window_s
        failures = 0
        interval = session.interval_s
        stop_reason = "expired"
        timeout = aiohttp.ClientTimeout(total=max(30.0, session.interval_s * 5))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                while loop.time() < deadline:
                    try:
                        frame = await asyncio.wait_for(
                            adapter.get_camera_frame(), timeout=max(10.0, session.interval_s * 3)
                        )
                    except asyncio.CancelledError:
                        raise
                    except UnsupportedCommandError as exc:
                        stop_reason = f"adapter has no camera: {exc}"
                        break
                    except Exception as exc:
                        failures += 1
                        logger.warning(
                            "camera frame capture failed",
                            extra={
                                "action": "camera",
                                "printer_key": session.printer_key,
                                "session_id": session.session_id,
                                "error": str(exc) or exc.__class__.__name__,
                            },
                        )
                        if failures >= MAX_CAPTURE_FAILURES:
                            stop_reason = "the camera stopped answering"
                            break
                        await asyncio.sleep(interval)
                        continue

                    failures = 0
                    if len(frame) > session.max_bytes:
                        # The hub would answer 413 and ask for a smaller frame;
                        # sending it first only wastes the uplink.
                        logger.warning(
                            "camera frame exceeds the hub's limit",
                            extra={
                                "action": "camera",
                                "printer_key": session.printer_key,
                                "session_id": session.session_id,
                                "bytes": str(len(frame)),
                                "max_bytes": str(session.max_bytes),
                            },
                        )
                        await asyncio.sleep(interval)
                        continue

                    outcome = await self._upload(http, session, frame)
                    if outcome.reached_hub:
                        deadline = loop.time() + session.silence_window_s
                        session.frames_sent += 1
                    if not outcome.should_continue:
                        stop_reason = f"hub closed the session (HTTP {outcome.status})"
                        break
                    interval = outcome.interval_s or session.interval_s
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - integration path
            stop_reason = f"camera loop failed: {exc}"
        finally:
            if self._sessions.get(session.printer_key) is session:
                del self._sessions[session.printer_key]
                logger.info(
                    "camera session ended",
                    extra={
                        "action": "camera",
                        "printer_key": session.printer_key,
                        "session_id": session.session_id,
                        "reason": stop_reason,
                        "frames": str(session.frames_sent),
                    },
                )

    async def _upload(
        self, http: aiohttp.ClientSession, session: CameraSession, frame: bytes
    ) -> _UploadOutcome:
        headers = {
            "Authorization": f"Bearer {self._agent_token}",
            "Content-Type": frame_content_type(frame),
            "X-Captured-At": utc_now_iso(),
        }
        try:
            async with http.post(session.upload_url, data=frame, headers=headers) as response:
                body: Any = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    body = {}
                return _read_outcome(response.status, body if isinstance(body, dict) else {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Unreachable hub is not a reason to stop: the silence window is.
            logger.warning(
                "camera frame upload failed",
                extra={
                    "action": "camera",
                    "printer_key": session.printer_key,
                    "session_id": session.session_id,
                    "error": str(exc) or exc.__class__.__name__,
                },
            )
            return _UploadOutcome(reached_hub=False, should_continue=True)


def _read_outcome(status: int, body: dict[str, Any]) -> _UploadOutcome:
    """Trust the body's `continue` flag, and the status when there is no body."""
    should_continue = status not in SESSION_GONE_STATUSES
    if "continue" in body:
        should_continue = bool(body["continue"])
    interval = body.get("interval_s")
    return _UploadOutcome(
        reached_hub=True,
        should_continue=should_continue,
        interval_s=_interval(interval) if interval is not None else None,
        status=status,
        reason=str(body.get("reason", "")),
    )


def frame_content_type(frame: bytes) -> str:
    """Name the image type from its own bytes rather than from a file extension."""
    if frame.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if frame.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if frame.startswith(b"RIFF") and frame[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _interval(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    if parsed <= 0:
        return DEFAULT_INTERVAL_S
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, parsed))


def _max_bytes(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return parsed if parsed > 0 else DEFAULT_MAX_BYTES


def _silence_window(expires_at: Any) -> float:
    """How long to keep filming with no word from the hub.

    `expires_at` is the hub's session TTL. It is used as a duration rather than
    as a wall-clock instant on purpose: a viewer who keeps watching extends the
    session hub-side, and the extension never reaches the agent — only the next
    successful upload does.
    """
    deadline = parse_iso8601(str(expires_at or ""))
    if deadline is None:
        return DEFAULT_SILENCE_WINDOW_S
    window = (deadline - datetime.now(timezone.utc)).total_seconds()
    return max(MIN_SILENCE_WINDOW_S, min(window, 3600.0))
