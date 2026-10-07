"""Owning asynchronous work past a caller's cancel (specs/core/bridge.md "Cancellation").

A command the bridge has accepted finishes as a whole, whatever cancels reach the
caller meanwhile: the work runs as a task of the bridge's own, the caller's cancel is
absorbed until the work ends, and the caller is told so to re-raise it afterwards.
Used by the session teardown, the detection loop's stop and the bring-up unwind.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine


async def owned(work: Coroutine[Any, Any, object]) -> bool:
    """Run ``work`` as a task of the bridge's own, awaited through any cancel of the
    caller: a cancel arriving meanwhile is absorbed and reported (``True``) instead of
    cutting the work short; the work's failure propagates once it has ended."""
    task = asyncio.ensure_future(work)
    interrupted = False
    while not task.done():
        with suppress(asyncio.CancelledError):
            await asyncio.shield(task)
            continue
        interrupted = True
    task.result()
    return interrupted
