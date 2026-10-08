"""Mic feed (specs/audio/microphone.md): the one reader of the microphone, its ring, and
``audio_input()`` as one subscriber of many over it.

Most tests drive a ``MicFeed`` bound to an indexed reader — chunk ``i`` carries ``i``
in its sample values — so a subscriber's output decodes back to the chunks it got, and
"every chunk, in order" is checked exactly. The bridge-level tests run on the fake.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import ReachyMiniBridge
from reachy_mini_bridge import microphone as microphone_module
from reachy_mini_bridge.audio import MediaSession
from reachy_mini_bridge.errors import BridgeError
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.microphone import MicChunk, MicFeed

_PERIOD_S = 0.01  # one chunk, as upstream's capture delivers them
_FRAMES = 4


class _Indexed:
    """A paced reader whose chunk ``i`` holds ``i / 1000`` on the left channel and
    ``right_scale * i / 1000`` on the right."""

    def __init__(self, *, right_scale: float = 1.0) -> None:
        self.i = 0
        self.right_scale = right_scale

    def __call__(self) -> npt.NDArray[np.float32]:
        time.sleep(_PERIOD_S)
        v = (self.i % 1000) / 1000.0
        self.i += 1
        return np.tile(
            np.array([v, v * self.right_scale], dtype=np.float32), (_FRAMES, 1)
        )


def _index(chunk: bytes) -> int:
    """The index an indexed mono chunk carries."""
    pcm = np.frombuffer(chunk, dtype=np.int16)
    return round(float(pcm[0]) * 1000.0 / 32767.0)


def _feed(
    reader: Callable[[], npt.NDArray[np.float32] | None] | None = None,
) -> MicFeed:
    return MicFeed(reader or _Indexed(), channels=2, sample_rate=16000)


async def _take(stream: AsyncIterator[bytes], n: int) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in stream:
        out.append(chunk)
        if len(out) == n:
            break
    return out


def _run_with(feed: MicFeed, body: Callable[[], Awaitable[object]]) -> object:
    async def run() -> object:
        await feed.start()
        try:
            return await body()
        finally:
            await feed.stop()

    return asyncio.run(run())


def test_two_subscribers_each_receive_every_chunk_in_order() -> None:
    feed = _feed()

    async def body() -> tuple[list[int], list[int]]:
        await asyncio.sleep(0.05)
        a, b = feed.subscribe(), feed.subscribe()  # the same starting cursor
        got_a, got_b = await asyncio.gather(_take(a, 30), _take(b, 30))
        return [_index(c) for c in got_a], [_index(c) for c in got_b]

    a, b = _run_with(feed, body)  # type: ignore[misc]
    assert a == b
    assert a == list(range(a[0], a[0] + 30))  # consecutive: no chunk split or skipped


def test_mono_and_raw_subscribers_side_by_side_over_the_same_chunks() -> None:
    feed = _feed(_Indexed(right_scale=0.5))

    async def body() -> tuple[list[bytes], list[bytes]]:
        await asyncio.sleep(0.03)
        mono, raw = feed.subscribe(mono=True), feed.subscribe(mono=False)
        return await asyncio.gather(_take(mono, 5), _take(raw, 5))

    mono, raw = _run_with(feed, body)  # type: ignore[misc]
    for m, r in zip(mono, raw, strict=True):
        left_right = np.frombuffer(r, dtype=np.int16).reshape(-1, 2)
        assert left_right.shape == (_FRAMES, 2)
        down = np.frombuffer(m, dtype=np.int16)
        assert down.shape == (_FRAMES,)
        # The mono chunk is the average of the raw chunk's two channels.
        expected = (left_right.astype(np.float64).mean(axis=1)).astype(np.int64)
        assert np.all(np.abs(down.astype(np.int64) - expected) <= 1)
        assert left_right[0, 0] > 0 and left_right[0, 1] == pytest.approx(
            left_right[0, 0] / 2, abs=1
        )


def test_a_lapped_subscriber_reports_one_gap_and_costs_no_one_else(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(microphone_module, "MIC_RING_CHUNKS", 8)
    feed = _feed()

    async def body() -> tuple[list[int], list[int]]:
        slow, fast = feed.subscribe(), feed.subscribe()

        async def stall() -> list[int]:
            first = await _take(slow, 1)
            await asyncio.sleep(0.25)  # ~25 chunks: lapped by the 8-chunk ring
            rest = await _take(slow, 5)
            return [_index(c) for c in first + rest]

        async def keep_up() -> list[int]:
            return [_index(c) for c in await _take(fast, 30)]

        return await asyncio.gather(stall(), keep_up())

    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.microphone"):
        slow, fast = _run_with(feed, body)  # type: ignore[misc]
    gaps = [r for r in caplog.records if "behind the mic" in r.getMessage()]
    assert len(gaps) == 1
    assert slow[1] - slow[0] > 8  # resumed past the overwritten chunks
    assert slow[1:] == list(range(slow[1], slow[1] + 5))  # then every chunk again
    assert fast == list(range(fast[0], fast[0] + 30))  # the fast one lost nothing


def test_preroll_starts_a_subscriber_in_the_past() -> None:
    feed = _feed()

    async def body() -> tuple[int, int]:
        await asyncio.sleep(0.15)
        now = feed.published_count
        first = _index((await _take(feed.subscribe(preroll_s=0.05), 1))[0])
        return now, first

    published, first = _run_with(feed, body)  # type: ignore[misc]
    # About 5 chunks before the call: in the past, and no further than asked (slack for
    # a slow machine's late chunks).
    assert published - 8 <= first < published


def test_preroll_beyond_the_ring_starts_at_the_oldest_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(microphone_module, "MIC_RING_CHUNKS", 8)
    feed = _feed()

    async def body() -> tuple[int, int]:
        await asyncio.sleep(0.2)
        stream = feed.subscribe(preroll_s=10.0)
        head = feed.published_count
        return head, _index((await _take(stream, 1))[0])

    head, first = _run_with(feed, body)  # type: ignore[misc]
    assert head - 8 <= first <= head - 7  # the oldest the ring held at the call


def test_negative_preroll_raises_at_the_call() -> None:
    feed = _feed()

    async def body() -> None:
        with pytest.raises(ValueError, match="preroll_s"):
            feed.subscribe(preroll_s=-1.0)

    _run_with(feed, body)


def test_subscribe_outside_a_running_feed_raises() -> None:
    with pytest.raises(BridgeError):
        _feed().subscribe()


def test_cancelling_one_subscriber_leaves_the_others_streaming() -> None:
    feed = _feed()

    async def body() -> list[int]:
        async def drain(stream: AsyncIterator[bytes]) -> None:
            async for _ in stream:
                pass

        doomed = asyncio.create_task(drain(feed.subscribe()))
        survivor = feed.subscribe()
        await asyncio.sleep(0.05)
        doomed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await doomed
        got = [_index(c) for c in await _take(survivor, 20)]
        # A fresh subscriber still works after the cancel.
        assert len(await _take(feed.subscribe(), 2)) == 2
        return got

    got = _run_with(feed, body)
    assert got == list(range(got[0], got[0] + 20))  # type: ignore[index]


def test_subscribers_end_when_the_feed_stops_and_never_cross_sessions() -> None:
    feed = _feed()

    async def run() -> tuple[int, list[bytes]]:
        chunks = 0

        async def drain(stream: AsyncIterator[bytes]) -> None:
            nonlocal chunks
            async for _ in stream:
                chunks += 1

        await feed.start()
        task = asyncio.create_task(drain(feed.subscribe()))
        stale = feed.subscribe()  # made in session 1, iterated in session 2
        await asyncio.sleep(0.05)
        await feed.stop()
        await asyncio.wait_for(task, 1.0)  # ends on its own
        await feed.start()
        try:
            await asyncio.sleep(0.05)
            leaked = await asyncio.wait_for(_take(stale, 1), 1.0)
        finally:
            await feed.stop()
        return chunks, leaked

    chunks, leaked = asyncio.run(run())
    assert chunks > 0
    assert leaked == []


def test_latest_and_published_count_across_sessions() -> None:
    feed = _feed()

    async def run() -> tuple[
        MicChunk | None, MicChunk | None, MicChunk | None, int, int
    ]:
        before = feed.latest()
        await feed.start()
        await asyncio.sleep(0.1)
        during = feed.latest()
        await feed.stop()
        after = feed.latest()
        first_session = feed.published_count
        await feed.start()
        await asyncio.sleep(0.05)
        second = feed.latest()
        await feed.stop()
        assert second is not None
        return before, during, after, first_session, second.seq

    before, during, after, first_session, second_seq = asyncio.run(run())
    assert before is None and after is None
    assert during is not None and during.samples.shape == (_FRAMES, 2)
    assert during.seq <= first_session - 1
    assert second_seq >= first_session  # seq counts on across sessions


def test_a_failing_reader_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microphone_module, "MIC_RETRY_S", 0.01)
    indexed = _Indexed()
    failures = 3

    def flaky() -> npt.NDArray[np.float32]:
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("pipeline hiccup")
        return indexed()

    feed = _feed(flaky)

    async def body() -> int:
        return len(await asyncio.wait_for(_take(feed.subscribe(), 5), 2.0))

    assert _run_with(feed, body) == 5


def test_an_empty_reader_is_paced_not_spun() -> None:
    calls = 0

    def empty() -> None:
        nonlocal calls
        calls += 1

    feed = _feed(empty)

    async def body() -> None:
        await asyncio.sleep(0.2)

    _run_with(feed, body)
    # ~20 reads at MIC_EMPTY_S; a spinning loop makes thousands. Upper bound only.
    assert calls < 50


def test_the_fake_capture_is_paced_at_a_hundred_chunks_a_second() -> None:
    async def run() -> int:
        async with ReachyMiniBridge("fake") as bridge:
            start = bridge.mic.published_count
            await asyncio.sleep(0.5)
            return bridge.mic.published_count - start

    published = asyncio.run(run())
    assert 30 <= published <= 55  # 50 at real time; slack below for a loaded machine


def test_bridge_mic_reads_and_two_audio_input_subscribers() -> None:
    bridge = ReachyMiniBridge("fake")

    async def run() -> tuple[MicChunk | None, MicChunk | None, int, int]:
        before = bridge.mic.latest()
        async with bridge:
            a, b = await asyncio.gather(
                _take(bridge.audio_input(), 10),
                _take(bridge.audio_input(mono=False), 10),
            )
            during = bridge.mic.latest()
        return before, during, len(a), len(b)

    before, during, a, b = asyncio.run(run())
    assert before is None
    assert during is not None and during.samples.shape == (160, 2)
    assert (a, b) == (10, 10)
    assert bridge.mic.latest() is None


def test_media_session_runs_its_own_feed_when_none_is_given() -> None:
    session = MediaSession(FakeReachyMini())

    async def run() -> MicChunk | None:
        await session.start()
        try:
            await _take(session.audio_input(), 1)
            return session.mic.latest()
        finally:
            await session.stop()

    assert asyncio.run(run()) is not None
    assert session.mic.latest() is None
