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
        t0 = time.perf_counter()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.perf_counter() - t0
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


def test_cancelling_a_wait_for_ends_it_promptly_and_the_others_still_wait() -> None:
    async def run() -> tuple[float, int, int]:
        obs = Observable(0)
        cancelled = asyncio.ensure_future(obs.wait_for(lambda v: v >= 2))
        other = asyncio.ensure_future(obs.wait_for(lambda v: v >= 2))
        await asyncio.sleep(0)
        t0 = time.perf_counter()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        elapsed = time.perf_counter() - t0
        obs.set(1)  # a publication after the cancel fails nobody
        await asyncio.sleep(0)
        obs.set(2)
        served = await asyncio.wait_for(other, 0.5)
        later = asyncio.ensure_future(obs.wait_for(lambda v: v >= 3))
        await asyncio.sleep(0)
        obs.set(3)
        return elapsed, served, await asyncio.wait_for(later, 0.5)

    elapsed, served, later = asyncio.run(run())
    assert elapsed < 0.05
    assert (served, later) == (2, 3)


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


def test_set_threadsafe_publishes_from_another_thread_onto_the_loop() -> None:
    async def run() -> tuple[int, int, bool]:
        obs = Observable(0)
        loop = asyncio.get_running_loop()
        changes = obs.changes()
        pending = asyncio.ensure_future(_next_within(changes))
        await asyncio.sleep(0)  # subscribed
        producer = threading.Thread(target=obs.set_threadsafe, args=(loop, 7))
        producer.start()
        producer.join()
        unchanged_yet = obs.value == 0  # queued on the loop, not applied on the thread
        got = await pending
        return got, obs.value, unchanged_yet

    assert asyncio.run(run()) == (7, 7, True)


def test_set_threadsafe_on_a_closed_loop_raises() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    obs = Observable(0)
    with pytest.raises(RuntimeError):
        obs.set_threadsafe(loop, 1)
    assert obs.value == 0


def test_an_every_subscriber_reads_a_whole_burst_in_order() -> None:
    async def run() -> list[int]:
        obs = Observable(0, history=8)
        changes = obs.changes(every=True)
        first = asyncio.ensure_future(_next_within(changes))
        await asyncio.sleep(0)
        obs.set(1)
        got = [await first]
        for value in (2, 3, 4):  # the subscriber is busy meanwhile
            obs.set(value)
        for _ in range(3):
            got.append(await _next_within(changes))
        with pytest.raises(TimeoutError):
            await _next_within(changes, timeout=0.05)  # nothing more than was published
        return got

    assert asyncio.run(run()) == [1, 2, 3, 4]


def test_every_on_a_history_of_one_reads_the_newest() -> None:
    async def run() -> list[int]:
        obs = Observable(0)  # the default history: the owner keeps nothing older
        changes = obs.changes(every=True)
        first = asyncio.ensure_future(_next_within(changes))
        await asyncio.sleep(0)
        obs.set(1)
        got = [await first]
        for value in (2, 3, 4):
            obs.set(value)
        got.append(await _next_within(changes))
        with pytest.raises(TimeoutError):
            await _next_within(changes, timeout=0.05)
        return got

    assert asyncio.run(run()) == [1, 4]


def test_a_burst_past_the_history_loses_its_oldest_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> list[int]:
        obs = Observable(0, history=3)
        changes = obs.changes(every=True)
        first = asyncio.ensure_future(_next_within(changes))
        await asyncio.sleep(0)
        obs.set(1)
        got = [await first]
        for value in range(2, 8):  # 6 values into a history of 3
            obs.set(value)
        for _ in range(3):
            got.append(await _next_within(changes))
        obs.set(8)  # the subscriber reads on after the gap
        got.append(await _next_within(changes))
        return got

    with caplog.at_level("WARNING", logger="reachy_mini_bridge.observable"):
        got = asyncio.run(run())
    assert got == [1, 5, 6, 7, 8]
    gaps = [r for r in caplog.records if r.name == "reachy_mini_bridge.observable"]
    assert len(gaps) == 1
    assert "3 oldest" in gaps[0].getMessage()


def test_a_default_and_an_every_subscriber_each_get_their_own_policy() -> None:
    async def run() -> tuple[list[int], list[int]]:
        obs = Observable(0, history=8)
        newest = obs.changes()
        every = obs.changes(every=True)
        firsts = [asyncio.ensure_future(_next_within(it)) for it in (newest, every)]
        await asyncio.sleep(0)
        obs.set(1)
        a = [await firsts[0]]
        b = [await firsts[1]]
        for value in (2, 3, 4):
            obs.set(value)
        a.append(await _next_within(newest))
        for _ in range(3):
            b.append(await _next_within(every))
        return a, b

    a, b = asyncio.run(run())
    assert a == [1, 4]
    assert b == [1, 2, 3, 4]


def test_a_history_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Observable(0, history=0)


def test_wait_for_finds_a_match_overwritten_in_the_same_step() -> None:
    async def run() -> int:
        obs = Observable(0, history=4)
        waiter = asyncio.ensure_future(obs.wait_for(lambda v: v == 2))
        await asyncio.sleep(0)
        obs.set(2)  # matches...
        obs.set(3)  # ...and is replaced before the waiter runs
        return await asyncio.wait_for(waiter, 0.5)

    assert asyncio.run(run()) == 2
