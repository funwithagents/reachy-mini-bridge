"""Functional tests for Observable[T] (specs/core/observable.md): pure asyncio, no robot."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator

import pytest

from reachy_mini_bridge.observable import Observable


async def _next_within(it: AsyncIterator[int], timeout: float = 0.5) -> int:
    return await asyncio.wait_for(anext(it), timeout)


def test_value_reads_the_initial_then_the_latest_set_or_updated() -> None:
    async def run() -> list[int]:
        obs = Observable(1)
        seen = [obs.value]
        obs.set(2)
        seen.append(obs.value)
        obs.update(3)
        seen.append(obs.value)
        return seen

    assert asyncio.run(run()) == [1, 2, 3]


def test_set_wakes_a_subscriber_and_update_does_not() -> None:
    async def run() -> list[int]:
        obs = Observable(0)
        received: list[int] = []

        async def subscriber() -> None:
            async for value in obs.changes():
                received.append(value)

        task = asyncio.create_task(subscriber())
        await asyncio.sleep(0)  # subscribed
        obs.update(1)
        await asyncio.sleep(0.05)
        obs.set(2)
        await asyncio.sleep(0.05)
        obs.update(3)
        await asyncio.sleep(0.05)
        task.cancel()
        return received

    assert asyncio.run(run()) == [2]


def test_a_subscriber_sees_only_values_published_after_it_subscribed() -> None:
    async def run() -> int:
        obs = Observable(0)
        obs.set(1)  # before anyone listens
        changes = obs.changes()
        pending = asyncio.ensure_future(_next_within(changes))
        await asyncio.sleep(0)
        obs.set(2)
        return await pending

    assert asyncio.run(run()) == 2


def test_two_subscribers_each_receive_every_published_value() -> None:
    async def run() -> tuple[list[int], list[int]]:
        obs = Observable(0)
        a: list[int] = []
        b: list[int] = []

        async def subscriber(into: list[int]) -> None:
            async for value in obs.changes():
                into.append(value)

        tasks = [asyncio.create_task(subscriber(x)) for x in (a, b)]
        await asyncio.sleep(0)
        for value in (1, 2, 3):
            obs.set(value)
            await asyncio.sleep(0)  # each subscriber keeps up
        for task in tasks:
            task.cancel()
        return a, b

    a, b = asyncio.run(run())
    assert a == [1, 2, 3]
    assert b == [1, 2, 3]


def test_a_slow_subscriber_gets_the_latest_of_a_burst() -> None:
    async def run() -> list[int]:
        obs = Observable(0)
        changes = obs.changes()
        first = asyncio.ensure_future(_next_within(changes))
        await asyncio.sleep(0)
        obs.set(1)
        got = [await first]
        for value in (2, 3, 4):  # nobody consumes meanwhile
            obs.set(value)
        got.append(await _next_within(changes))
        with pytest.raises(TimeoutError):
            await _next_within(changes, timeout=0.05)  # no backlog behind it
        return got

    assert asyncio.run(run()) == [1, 4]


def test_cancelling_a_subscriber_ends_it_promptly_and_detaches_it() -> None:
    async def run() -> tuple[float, list[int]]:
        obs = Observable(0)
        survivor: list[int] = []

        async def blocked() -> None:
            async for _ in obs.changes():
                pass

        async def other() -> None:
            async for value in obs.changes():
                survivor.append(value)

        task = asyncio.create_task(blocked())
        keeper = asyncio.create_task(other())
        await asyncio.sleep(0)
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - t0
        obs.set(1)  # a later publication does not fail on the detached subscriber
        await asyncio.sleep(0)
        keeper.cancel()
        return elapsed, survivor

    elapsed, survivor = asyncio.run(run())
    assert elapsed < 0.05
    assert survivor == [1]


def test_wait_for_returns_at_once_on_a_matching_current_value() -> None:
    async def run() -> int:
        obs = Observable(5)
        return await asyncio.wait_for(obs.wait_for(lambda v: v > 3), 0.1)

    assert asyncio.run(run()) == 5


def test_wait_for_returns_the_first_matching_publication() -> None:
    async def run() -> int:
        obs = Observable(0)
        waiter = asyncio.ensure_future(obs.wait_for(lambda v: v >= 2))
        await asyncio.sleep(0)
        obs.set(1)
        await asyncio.sleep(0)
        assert not waiter.done()
        obs.update(7)  # silent: not a publication
        await asyncio.sleep(0)
        assert not waiter.done()
        obs.set(3)
        return await asyncio.wait_for(waiter, 0.5)

    assert asyncio.run(run()) == 3


def test_set_and_update_from_a_plain_thread_raise() -> None:
    obs = Observable(0)
    errors: list[BaseException] = []

    def producer() -> None:
        for call in (obs.set, obs.update):
            try:
                call(1)
            except RuntimeError as e:
                errors.append(e)

    thread = threading.Thread(target=producer)
    thread.start()
    thread.join()
    assert len(errors) == 2
    assert obs.value == 0
