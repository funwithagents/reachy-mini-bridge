"""``Observable[T]`` — a value a caller reads directly and can subscribe to
(specs/core/observable.md).

``value`` is the current state, readable from any thread; ``changes()`` is an async
iterator woken on every *published* value; ``wait_for`` waits for a value matching a
predicate. The owner decides what counts as a change: ``set`` replaces and publishes,
``update`` replaces silently, ``set_threadsafe`` is the ``set`` of a producer on another
thread. The owner also sets how many published values a subscriber may fall behind by
(``history``); a subscriber reads the newest by default, or every value in order with
``changes(every=True)``. Nothing blocks the producer.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

__all__ = ["CHANGE_HISTORY", "Observable"]

_logger = logging.getLogger(__name__)

# The history of the bridge's state observables (`faces`, `head_tracking`): published on
# meaningful changes only, a few a minute, so an `every` subscriber reads each in order
# through any slow step it takes.
CHANGE_HISTORY = 64


class _Subscriber[T]:
    """One ``changes()`` iterator's pending values: at most ``capacity``, the oldest
    dropped first — counted when the subscriber reads every value."""

    __slots__ = ("capacity", "every", "lost", "pending", "ready")

    def __init__(self, capacity: int, every: bool) -> None:
        self.capacity = capacity
        self.every = every
        self.pending: deque[T] = deque()
        self.ready = asyncio.Event()
        self.lost = 0

    def push(self, value: T) -> None:
        if len(self.pending) >= self.capacity:
            self.pending.popleft()
            if self.every:
                self.lost += 1
        self.pending.append(value)
        self.ready.set()


class Observable[T]:
    """A value read directly and subscribed to (specs/core/observable.md).

    ``history`` (at least 1) is how many published values a subscriber may fall behind
    by: ``changes()`` yields the newest, ``changes(every=True)`` every value in order up
    to that history. ``set`` / ``update`` run on the event-loop thread (they raise
    ``RuntimeError`` elsewhere); a producer on another thread publishes through
    :meth:`set_threadsafe`, which marshals the ``set`` onto the loop it is given.
    """

    def __init__(self, initial: T, *, history: int = 1) -> None:
        if history < 1:
            raise ValueError(f"an Observable's history is at least 1, got {history}")
        self._value = initial
        self._history = history
        # One record per live subscriber, in subscription order.
        self._subscribers: list[_Subscriber[T]] = []

    @property
    def value(self) -> T:
        """The current value; no await."""
        return self._value

    @property
    def history(self) -> int:
        """How many published values a subscriber may fall behind by."""
        return self._history

    def set(self, value: T) -> None:
        """Replace the value and publish it to every subscriber."""
        asyncio.get_running_loop()  # fails loudly off the event-loop thread
        self._value = value
        for subscriber in self._subscribers:
            subscriber.push(value)

    def update(self, value: T) -> None:
        """Replace the value silently: readers of :attr:`value` see it, nobody wakes."""
        asyncio.get_running_loop()
        self._value = value

    def set_threadsafe(self, loop: asyncio.AbstractEventLoop, value: T) -> None:
        """:meth:`set` from another thread: the publication is queued on ``loop`` and runs
        there, so subscribers wake in publication order and :attr:`value` changes on the
        loop's thread. Raises ``RuntimeError`` when ``loop`` is closed, as
        ``call_soon_threadsafe`` does; the caller decides whether that matters."""
        loop.call_soon_threadsafe(self.set, value)

    def changes(self, *, every: bool = False) -> AsyncIterator[T]:
        """Yield values published from the moment the iterator is first driven: the
        newest whenever the subscriber comes back (default), or with ``every`` each one in
        order, up to the owner's history — a subscriber further behind loses the oldest,
        with one ``WARNING`` per gap.

        Cancelling the task blocked in ``async for`` ends the iteration and detaches the
        subscriber (specs/core/bridge.md "Cancellation").
        """
        return self._subscribe(every)

    async def _subscribe(self, every: bool) -> AsyncGenerator[T]:
        subscriber: _Subscriber[T] = _Subscriber(self._history if every else 1, every)
        self._subscribers.append(subscriber)
        try:
            while True:
                while not subscriber.pending:
                    subscriber.ready.clear()
                    await subscriber.ready.wait()
                if subscriber.lost:
                    _logger.warning(
                        "observable: a subscriber fell more than %d values behind; "
                        "its %d oldest unread values were dropped",
                        subscriber.capacity,
                        subscriber.lost,
                    )
                    subscriber.lost = 0
                yield subscriber.pending.popleft()
        finally:
            self._subscribers.remove(subscriber)

    async def wait_for(self, predicate: Callable[[T], bool]) -> T:
        """The current value when ``predicate`` holds for it, else the first published
        value that matches — every published value is checked, up to the history."""
        if predicate(self._value):
            return self._value
        changes = self._subscribe(every=True)
        try:
            async for value in changes:
                if predicate(value):
                    return value
        finally:
            await changes.aclose()
        raise AssertionError("unreachable: changes() never ends on its own")
