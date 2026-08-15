"""The one asyncio entry point for every process that talks to a printer.

Windows picks the Proactor event loop by default, and Proactor has no
``add_reader``/``add_writer``. paho-mqtt drives its socket through exactly those
two calls, so an :mod:`aiomqtt` client on a Proactor loop connects, subscribes,
and then never delivers a byte — the failure surfaces much later as a plain
"Operation timed out" from a Bambu adapter whose cache stayed empty. Selecting
the loop here, once, keeps that trap out of every ``asyncio.run`` call site.

``asyncio.Runner`` (3.11+) is used rather than ``asyncio.run(..., loop_factory=)``
because the latter only exists from 3.12 on.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def new_event_loop() -> asyncio.AbstractEventLoop:
    """Return a loop that supports socket readers — selector-based on Windows."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Drop-in for :func:`asyncio.run` on a loop MQTT can actually use."""
    with asyncio.Runner(loop_factory=new_event_loop) as runner:
        return runner.run(coro)
