"""The live tier's building blocks — importable by consumers of the bridge.

Two ways a live test opts out cleanly instead of failing in an environment that can't
run it:

- `require_env(...)` — a live test needs real credentials; rather than fail when
  they're absent, it *skips* cleanly (you only exercise services you hold keys for).
- `requires_caps(...)` — a test needs a robot capability (`motion`, `audio`, `camera`,
  …); it *skips* when the current target didn't probe that capability. The probed set
  rides along in the `live_bridge` fixture value, so a test passes that value in:
  `requires_caps(live_bridge, "audio")` — see specs/testing/testing_support.md.

And the loop a bridge's lifecycle runs on across a test module:

- `BridgeLoop` — one event loop on a background thread, from the bridge's `start()` to
  its `stop()`. The bridge is loop-bound (its detection loop is an asyncio task, its
  observables publish on the loop thread), so a module-scoped fixture keeps the loop
  that started the bridge running, and a test runs its coroutines on it through
  `run(...)` rather than under an `asyncio.run` of its own.
- `LiveBridge` — what `live_bridge` yields: the bridge, its probed capabilities (it
  unpacks as `bridge, caps = live_bridge`) and `run(...)` on that loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

import pytest

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

    from reachy_mini_bridge.bridge import ReachyMiniBridge

_STOP_TIMEOUT_S = 10.0


def require_env(name: str) -> str:
    """Return env var `name`, or skip the calling test if it's unset/empty."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set; skipping live test")
    return value


class BridgeLoop:
    """One event loop on a background thread for a bridge's whole lifecycle
    (specs/testing/testing_support.md "Public surface").

    ``start()`` spins the loop up, ``run(coro)`` executes a coroutine on it and blocks
    until it returns (its exception propagates; a timeout or a ``KeyboardInterrupt``
    while waiting cancels it), ``stop()`` cancels whatever is still running there and
    joins the thread. A context manager over the pair.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._loop is not None

    def start(self) -> None:
        if self._loop is not None:
            raise RuntimeError("BridgeLoop is already running")
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=loop.run_forever, name="bridge-loop", daemon=True
        )
        thread.start()
        self._loop, self._thread = loop, thread

    def run[T](self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coro`` on the loop and return its result, waiting at most ``timeout``
        seconds (``None``: as long as it takes)."""
        loop = self._loop
        if loop is None:
            coro.close()
            raise RuntimeError("BridgeLoop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"the coroutine did not finish within {timeout} s"
            ) from None
        except BaseException:
            future.cancel()
            raise

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        self._loop = self._thread = None
        if loop is None or thread is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(_cancel_everything(), loop).result(
                _STOP_TIMEOUT_S
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(_STOP_TIMEOUT_S)
            if not thread.is_alive():
                loop.close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


async def _cancel_everything() -> None:
    """Cancel every other task on the loop and wait for them, then close the async
    generators — nothing of a stopped bridge should be left, but a test that was
    interrupted mid-verb may have left one."""
    current = asyncio.current_task()
    others = [t for t in asyncio.all_tasks() if t is not current]
    for task in others:
        task.cancel()
    if others:
        await asyncio.gather(*others, return_exceptions=True)
    await asyncio.get_running_loop().shutdown_asyncgens()


@dataclass(frozen=True)
class LiveBridge:
    """The ``live_bridge`` fixture's value: ``bridge``, its probed ``capabilities``, and
    ``run(coro)`` on the loop the bridge lives on. Unpacks as ``(bridge, capabilities)``."""

    bridge: ReachyMiniBridge
    capabilities: frozenset[str]
    loop: BridgeLoop

    def run[T](self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coro`` on the bridge's event loop and return its result."""
        return self.loop.run(coro, timeout)

    def __iter__(self) -> Iterator[Any]:
        return iter((self.bridge, self.capabilities))


def requires_caps(live: LiveBridge | tuple[object, frozenset[str]], *caps: str) -> None:
    """Skip the calling test unless the live target probed every capability in `caps`.

    Pass the `live_bridge` fixture value; the probed set travels in it, so no ambient
    state is needed.
    """
    available = live.capabilities if isinstance(live, LiveBridge) else live[1]
    missing = sorted(set(caps) - available)
    if missing:
        pytest.skip(f"target lacks required capability/ies: {', '.join(missing)}")
