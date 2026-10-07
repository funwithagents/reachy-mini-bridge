"""Functional tests for the camera feed (specs/vision/camera.md) on a scripted reader.

The feed runs its real thread over a stub `read_frame` that returns scripted frames,
`None`s, or raises, and a stub `pose_at`; tests observe what `latest()` publishes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import camera as camera_module
from reachy_mini_bridge.camera import CameraFeed
from reachy_mini_bridge.errors import BridgeError

Read = tuple[npt.NDArray[np.uint8], float | None] | None


def _image(value: int) -> npt.NDArray[np.uint8]:
    return np.full((4, 6, 3), value, dtype=np.uint8)


def _pose(yaw: float) -> npt.NDArray[np.float64]:
    pose = np.eye(4)
    pose[0, 3] = yaw
    return pose


class _Reader:
    """A scripted `read_frame`: each entry is a frame tuple, `None`, or an exception to
    raise; once the script is spent it blocks (returning `None` every 5 ms) until told
    to stop, like a camera with nothing new."""

    def __init__(self, script: list[Read | Exception], pace_s: float = 0.0) -> None:
        self._script = list(script)
        self._pace_s = pace_s  # a wait before each frame, like a camera's period
        self._lock = threading.Lock()
        self.reads = 0

    def extend(self, more: list[Read | Exception]) -> None:
        with self._lock:
            self._script.extend(more)

    def __call__(self) -> Read:
        with self._lock:
            self.reads += 1
            item = self._script.pop(0) if self._script else "idle"
        if item == "idle":
            time.sleep(0.005)
            return None
        if isinstance(item, Exception):
            raise item
        if item is not None and self._pace_s:
            time.sleep(self._pace_s)
        return item


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


@pytest.fixture
def running() -> Iterator[
    Callable[[_Reader, Callable[[float], npt.NDArray[np.float64]] | None], CameraFeed]
]:
    feeds: list[CameraFeed] = []

    def start(
        reader: _Reader, pose_at: Callable[[float], npt.NDArray[np.float64]] | None
    ) -> CameraFeed:
        feed = CameraFeed(reader, pose_at)
        asyncio.run(feed.start())
        feeds.append(feed)
        return feed

    yield start
    for feed in feeds:
        asyncio.run(feed.stop())


def test_frames_are_published_in_order_and_nones_publish_nothing(
    running: Callable[..., CameraFeed],
) -> None:
    reader = _Reader([(_image(1), 10.0), None, None, (_image(2), 11.0), None])
    feed = running(reader, None)
    _wait_until(lambda: feed.published_count == 2)
    latest = feed.latest()
    assert latest is not None
    assert (latest.frame_id, latest.ts) == (2, 11.0)
    assert latest.image[0, 0, 0] == 2
    # While the reader keeps returning None the last frame stays published.
    _wait_until(lambda: reader.reads >= 8)
    assert feed.latest() is latest
    assert feed.published_count == 2


def test_latest_is_none_unless_running_and_frame_ids_count_across_restarts() -> None:
    reader = _Reader([(_image(1), 1.0)])
    feed = CameraFeed(reader, None)
    asyncio.run(feed.stop())  # never started: a no-op
    assert feed.latest() is None and not feed.running
    asyncio.run(feed.start())
    asyncio.run(feed.start())  # already running: the same one thread keeps reading
    _wait_until(lambda: feed.latest() is not None)
    assert feed.running
    asyncio.run(feed.stop())
    assert feed.latest() is None and not feed.running
    assert feed.published_count == 1
    # frame_id counts on across start / stop, so an old result is never mistaken for new.
    reader.extend([(_image(2), 2.0)])
    asyncio.run(feed.start())
    _wait_until(lambda: feed.published_count == 2)
    latest = feed.latest()
    assert latest is not None and latest.frame_id == 2
    asyncio.run(feed.stop())


def test_a_raising_reader_keeps_the_last_frame_and_recovers(
    monkeypatch: pytest.MonkeyPatch, running: Callable[..., CameraFeed]
) -> None:
    monkeypatch.setattr(camera_module, "CAMERA_RETRY_S", 0.01)
    reader = _Reader([(_image(1), 1.0), OSError("no camera"), OSError("still none")])
    feed = running(reader, None)
    _wait_until(lambda: reader.reads >= 4)
    latest = feed.latest()
    assert latest is not None and latest.frame_id == 1
    reader.extend([(_image(2), 2.0)])
    _wait_until(lambda: feed.published_count == 2)
    latest = feed.latest()
    assert latest is not None and latest.image[0, 0, 0] == 2


def test_a_reader_failing_for_long_warns_once(
    monkeypatch: pytest.MonkeyPatch,
    running: Callable[..., CameraFeed],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(camera_module, "CAMERA_RETRY_S", 0.005)
    monkeypatch.setattr(camera_module, "CAMERA_DOWN_S", 0.05)
    reader = _Reader([OSError(f"down {i}") for i in range(200)])
    with caplog.at_level("INFO", logger="reachy_mini_bridge.camera"):
        feed = running(reader, None)
        _wait_until(lambda: reader.reads >= 40)
        reader.extend([(_image(1), 1.0)])
        _wait_until(lambda: feed.published_count == 1)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "has failed for" in warnings[0].getMessage()
    assert [r.getMessage() for r in caplog.records if r.levelname == "INFO"] == [
        "camera feed: frames are back"
    ]


def test_head_pose_is_the_pose_at_the_capture_time_or_none(
    running: Callable[..., CameraFeed],
) -> None:
    asked: list[float] = []

    def pose_at(t: float) -> npt.NDArray[np.float64]:
        asked.append(t)
        return _pose(t * 2)

    # A capture time known: the pose at that time.
    reader = _Reader([(_image(1), 3.0)])
    feed = running(reader, pose_at)
    _wait_until(lambda: feed.published_count == 1)
    latest = feed.latest()
    assert latest is not None and latest.ts == 3.0
    assert latest.head_pose is not None and latest.head_pose[0, 3] == 6.0
    # Unknown (arrival time): no pose, even with a pose source at hand — a pose is
    # attached only to a capture time.
    reader.extend([(_image(2), None)])
    _wait_until(lambda: feed.published_count == 2)
    latest = feed.latest()
    assert latest is not None
    assert latest.head_pose is None
    assert asked == [3.0]
    assert latest.ts >= 3.0  # the arrival on the monotonic clock
    without = CameraFeed(_Reader([(_image(3), 7.0)]), None)
    asyncio.run(without.start())
    _wait_until(lambda: without.published_count == 1)
    frame = without.latest()
    asyncio.run(without.stop())
    assert frame is not None and frame.ts == 7.0 and frame.head_pose is None


def test_two_consumers_both_see_every_frame(
    running: Callable[..., CameraFeed],
) -> None:
    """The single-reader property: sampling the feed takes nothing from another
    consumer, unlike two callers of upstream's one-shot `get_frame()`."""
    reader = _Reader([(_image(i), float(i)) for i in range(1, 21)], pace_s=0.03)
    feed = running(reader, None)
    seen: dict[str, set[int]] = {"a": set(), "b": set()}

    def consume(name: str) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(seen[name]) < 20:
            frame = feed.latest()
            if frame is not None:
                seen[name].add(frame.frame_id)
            time.sleep(0.002)  # a display's pace; also hands the GIL over

    threads = [threading.Thread(target=consume, args=(n,)) for n in seen]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["a"] == seen["b"] == set(range(1, 21))


def test_start_needs_a_reader_and_bind_gives_one() -> None:
    feed = CameraFeed()
    with pytest.raises(BridgeError, match="no reader"):
        asyncio.run(feed.start())
    feed.bind(_Reader([(_image(1), 1.0)]), None)
    asyncio.run(feed.start())
    _wait_until(lambda: feed.published_count == 1)
    with pytest.raises(BridgeError, match="while it runs"):
        feed.bind(_Reader([]), None)
    asyncio.run(feed.stop())


class _EmptyReader:
    """A `read_frame` with no camera behind it: `None` at once, or after `delay_s`."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self._delay_s = delay_s
        self.reads = 0

    def __call__(self) -> Read:
        self.reads += 1
        if self._delay_s:
            time.sleep(self._delay_s)
        return None


def test_an_empty_read_that_returns_at_once_is_paced() -> None:
    """A reader answering None immediately is called at most 1 / CAMERA_EMPTY_S times a
    second (specs/vision/camera.md "The feed") — not millions."""
    reader = _EmptyReader()
    feed = CameraFeed(reader, None)
    asyncio.run(feed.start())
    time.sleep(0.2)
    reads = reader.reads
    asyncio.run(feed.stop())
    assert 3 <= reads <= 0.2 / camera_module.CAMERA_EMPTY_S + 5
    assert feed.latest() is None


def test_an_empty_read_that_waited_itself_is_not_slowed() -> None:
    """A reader that blocks longer than the pace before answering None runs at its own
    rate: the pace adds nothing on top of the read's wait."""
    reader = _EmptyReader(delay_s=0.025)
    feed = CameraFeed(reader, None)
    asyncio.run(feed.start())
    time.sleep(0.2)
    reads = reader.reads
    asyncio.run(feed.stop())
    assert 5 <= reads <= 9


def test_stop_returns_promptly_during_the_empty_pace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera_module, "CAMERA_EMPTY_S", 0.5)
    reader = _EmptyReader()
    feed = CameraFeed(reader, None)
    asyncio.run(feed.start())
    _wait_until(lambda: reader.reads >= 1)
    started = time.monotonic()
    asyncio.run(feed.stop())
    assert time.monotonic() - started < 0.3
