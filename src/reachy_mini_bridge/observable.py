"""``Observable[T]`` — a value a caller reads directly and can subscribe to
(specs/core/observable.md).

``value`` is the current state, readable from any thread; ``changes()`` is an async
iterator woken on every *published* value; ``wait_for`` waits for a value matching a
predicate. The owner decides what counts as a change: ``set`` replaces and publishes,
``update`` replaces silently. A slow subscriber gets the latest value, never a backlog,
and nothing blocks the producer.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

__all__ = ["Observable"]


class Observable[T]:
    """A value read directly and subscribed to (specs/core/observable.md).

    ``set`` / ``update`` run on the event-loop thread (they raise ``RuntimeError``
    elsewhere); a producer on another thread marshals through
    ``loop.call_soon_threadsafe(observable.set, value)``.
    """

    def __init__(self, initial: T) -> None:
        self._value = initial
        # One bounded queue per live subscriber, in subscription order.
        self._subscribers: list[asyncio.Queue[T]] = []

    @property
    def value(self) -> T:
        """The current value; no await."""
        return self._value

    def set(self, value: T) -> None:
        """Replace the value and publish it to every subscriber."""
        asyncio.get_running_loop()  # fails loudly off the event-loop thread
        self._value = value
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()  # latest wins: drop the value not yet consumed
            queue.put_nowait(value)

    def update(self, value: T) -> None:
        """Replace the value silently: readers of :attr:`value` see it, nobody wakes."""
        asyncio.get_running_loop()
        self._value = value

    def changes(self) -> AsyncIterator[T]:
        """Yield each value published from the moment the iterator is first driven.

        Cancelling the task blocked in ``async for`` ends the iteration and detaches the
        subscriber (specs/core/bridge.md "Cancellation").
        """
        return self._subscribe()

    async def _subscribe(self) -> AsyncGenerator[T]:
        queue: asyncio.Queue[T] = asyncio.Queue(maxsize=1)
        self._subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.remove(queue)

    async def wait_for(self, predicate: Callable[[T], bool]) -> T:
        """The current value when ``predicate`` holds for it, else the first published
        value that matches."""
        if predicate(self._value):
            return self._value
        changes = self._subscribe()
        try:
            async for value in changes:
                if predicate(value):
                    return value
        finally:
            await changes.aclose()
        raise AssertionError("unreachable: changes() never ends on its own")
