"""Mic feed: ``MicFeed`` / ``MicChunk``, the one reader of the robot's microphone
(specs/audio/microphone.md).

Upstream hands capture out one chunk at a time — ``media.get_audio_sample()`` returns
each 10 ms chunk once, then the next — so two readers in one process split the capture
between them, silently. The feed owns the read: one thread loops the robot's
``get_audio_sample`` (the only call site of it in the bridge), stamps every chunk with
its sequence number and arrival time, and keeps the last ``MIC_RING_CHUNKS`` in a ring.
Every ``audio_input()`` is a subscriber of its own over that ring, holding a cursor on
the chunk it yields next: each receives every chunk, in order, at its own pace, and a
subscriber lapped by the ring loses only its own chunks, with a warning.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from .audio import downmix_to_mono, float32_to_int16
from .errors import BridgeError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import numpy.typing as npt

__all__ = [
    "MIC_DOWN_S",
    "MIC_EMPTY_S",
    "MIC_RETRY_S",
    "MIC_RING_CHUNKS",
    "MicChunk",
    "MicFeed",
    "SampleReader",
]

_logger = logging.getLogger(__name__)

# The ring's length in chunks (specs/audio/microphone.md "The ring"): 2 s at upstream's
# 10 ms chunks. Read at start(), so tests can shorten it.
MIC_RING_CHUNKS = 200
# The reader's pacing and failure rules, the camera feed's (specs/audio/microphone.md
# "The feed"): an empty read sooner than MIC_EMPTY_S waits out the difference; a read
# that raises is retried after MIC_RETRY_S, and one that keeps raising for MIC_DOWN_S
# logs one WARNING (and one INFO when chunks return). Read at run time.
MIC_EMPTY_S = 0.01
MIC_RETRY_S = 0.1
MIC_DOWN_S = 5.0

# What the feed reads: the next capture chunk, float32 ``(frames, channels)``, or
# ``None`` when nothing is queued yet.
type SampleReader = Callable[[], npt.NDArray[np.float32] | None]
# A waiting subscriber: its event, set from the reader thread through its loop.
type _Waiter = tuple[asyncio.AbstractEventLoop, asyncio.Event]


@dataclass(frozen=True)
class MicChunk:
    """One published capture chunk (specs/audio/microphone.md "The chunk")."""

    seq: int  # 0, 1, 2, … per feed — the subscribers' cursor counts these
    ts: float  # time.monotonic() when the reader received the chunk
    # (frames, channels) interleaved float32, the array upstream returned — shared by
    # reference with every subscriber, so read-only by convention.
    samples: npt.NDArray[np.float32] = field(compare=False)


class MicFeed:
    """The one reader of the robot's microphone (specs/audio/microphone.md "The feed"):
    ``start()`` spawns the reader thread, ``subscribe()`` makes an ``audio_input()``
    stream over the ring, ``latest()`` is the newest chunk from any thread, ``stop()``
    joins the thread and ends every subscriber.

    Built unbound by the bridge (``bridge.mic`` exists from construction) and bound to
    the session's robot by the media session through :meth:`bind`, which starts it
    right after the recording; a test binds at construction. ``seq`` counts on across
    ``start()`` / ``stop()``.
    """

    def __init__(
        self,
        read_sample: SampleReader | None = None,
        channels: int = 1,
        sample_rate: int = 16000,
    ) -> None:
        self._read_sample = read_sample
        self._channels = channels
        self._sample_rate = sample_rate
        self._lock = threading.Lock()
        self._capacity = MIC_RING_CHUNKS
        self._ring: list[MicChunk | None] = []
        self._head = 0  # the next seq — counts on across sessions
        self._session = 0  # bumped at each start(): a subscriber ends with its session
        self._session_start = 0  # the first seq of the running session
        self._running = False
        self._waiters: set[_Waiter] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def bind(self, read_sample: SampleReader, channels: int, sample_rate: int) -> None:
        """Give the feed its reader and the capture format (before ``start()``)."""
        if self._thread is not None:
            raise BridgeError("the mic feed cannot be rebound while it runs")
        self._read_sample = read_sample
        self._channels = channels
        self._sample_rate = sample_rate

    @property
    def running(self) -> bool:
        """Whether the reader thread runs."""
        return self._thread is not None

    @property
    def published_count(self) -> int:
        """Chunks published, ever, on this feed — the next chunk's ``seq``."""
        with self._lock:
            return self._head

    def latest(self) -> MicChunk | None:
        """The newest chunk, or ``None`` before the session's first and after ``stop()``."""
        with self._lock:
            if not self._running or self._head == self._session_start:
                return None
            return self._ring[(self._head - 1) % self._capacity]

    async def start(self) -> None:
        """Start the reader thread. A no-op while it runs; ``BridgeError`` unbound."""
        if self._thread is not None:
            return
        if self._read_sample is None:
            raise BridgeError("the mic feed has no reader: bind it to a robot first")
        with self._lock:
            self._capacity = MIC_RING_CHUNKS
            self._ring = [None] * self._capacity
            self._session += 1
            self._session_start = self._head
            self._running = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="reachy-mini-mic", daemon=True
        )
        self._thread.start()

    async def stop(self) -> None:
        """Stop the reader thread (joined off the event loop), end every subscriber and
        reset ``latest()`` to ``None``. A no-op on a feed that is not running."""
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        await asyncio.to_thread(thread.join)
        self._thread = None
        with self._lock:
            self._running = False
            self._ring = [None] * self._capacity
            waiters = tuple(self._waiters)
        _wake(waiters)

    # --- subscribers ---

    def subscribe(
        self, *, mono: bool = True, preroll_s: float = 0.0
    ) -> AsyncIterator[bytes]:
        """A new subscriber over the ring (specs/audio/microphone.md "Subscribers"):
        int16 LE ``bytes`` per chunk, downmixed to mono unless ``mono=False``, from the
        call on — or from up to ``preroll_s`` seconds before it, as far as the ring
        reaches. ``ValueError`` on a negative ``preroll_s``; ``BridgeError`` when the
        feed is not running. The stream ends when the feed stops."""
        if preroll_s < 0:
            raise ValueError(f"preroll_s must be >= 0, got {preroll_s}")
        with self._lock:
            if not self._running:
                raise BridgeError("the mic feed is not running")
            cursor = self._head
            if preroll_s > 0:
                cutoff = time.monotonic() - preroll_s
                oldest = max(self._head - self._capacity, self._session_start)
                while cursor > oldest:
                    previous = self._ring[(cursor - 1) % self._capacity]
                    if previous is None or previous.ts < cutoff:
                        break
                    cursor -= 1
            session = self._session
        return self._iterate(cursor, session, mono, self._channels)

    async def _iterate(
        self, cursor: int, session: int, mono: bool, channels: int
    ) -> AsyncIterator[bytes]:
        event = asyncio.Event()
        waiter: _Waiter = (asyncio.get_running_loop(), event)
        with self._lock:
            self._waiters.add(waiter)
        try:
            while True:
                # Cleared before head is read: a chunk published after the read sets
                # it again, so the wait below never misses one.
                event.clear()
                lost = 0
                with self._lock:
                    if not self._running or self._session != session:
                        return
                    head = self._head
                    oldest = max(head - self._capacity, self._session_start)
                    if cursor < oldest:  # lapped: those chunks were overwritten
                        lost, cursor = oldest - cursor, oldest
                    chunk = (
                        self._ring[cursor % self._capacity] if cursor < head else None
                    )
                if lost:
                    self._report_gap(lost, chunk)
                if chunk is None:
                    await event.wait()
                    continue
                cursor += 1
                samples = chunk.samples
                frame = downmix_to_mono(samples, channels) if mono else samples
                yield float32_to_int16(frame).tobytes()
        finally:
            with self._lock:
                self._waiters.discard(waiter)

    def _report_gap(self, lost: int, chunk: MicChunk | None) -> None:
        frames = 0 if chunk is None else len(chunk.samples)
        ms = 1000.0 * lost * frames / self._sample_rate if self._sample_rate else 0.0
        _logger.warning(
            "audio_input: a subscriber fell %d chunks (%.0f ms) behind the mic; "
            "resuming from the oldest buffered chunk",
            lost,
            ms,
        )

    # --- the thread ---

    def _run(self) -> None:
        read_sample = self._read_sample
        assert read_sample is not None  # start() checked
        failing_since: float | None = None
        down = False
        while not self._stop.is_set():
            read_at = time.monotonic()
            try:
                sample = read_sample()
            except Exception as e:  # noqa: BLE001 - a failed read is retried, never fatal
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                    _logger.debug("mic feed: read failed: %s", e)
                if not down and now - failing_since >= MIC_DOWN_S:
                    down = True
                    _logger.warning(
                        "mic feed: reading the microphone has failed for %.0f s (%s); "
                        "subscribers wait until chunks return",
                        MIC_DOWN_S,
                        e,
                    )
                self._stop.wait(MIC_RETRY_S)
                continue
            failing_since = None
            if down:
                down = False
                _logger.info("mic feed: chunks are back")
            if sample is None:
                # Nothing queued: one more pass, paced unless the read itself waited.
                waited = time.monotonic() - read_at
                if waited < MIC_EMPTY_S:
                    self._stop.wait(MIC_EMPTY_S - waited)
                continue
            self._publish(np.asarray(sample, dtype=np.float32))

    def _publish(self, samples: npt.NDArray[np.float32]) -> None:
        with self._lock:
            chunk = MicChunk(self._head, time.monotonic(), samples)
            self._ring[self._head % self._capacity] = chunk
            self._head += 1
            waiters = tuple(self._waiters)
        _wake(waiters)


def _wake(waiters: tuple[_Waiter, ...]) -> None:
    for loop, event in waiters:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # the subscriber's loop is closed: nothing left to wake
            pass
