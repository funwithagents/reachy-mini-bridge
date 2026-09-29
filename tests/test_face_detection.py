"""Functional tests for the detection loop (specs/user_perception.md) on the fake.

The loop runs unchanged at real time, with its timing shortened: polls at 20 Hz, a
drop published after 0.15 s. A test drives the scene through the fake's show_face /
hide_face (the `daemon` source) or a stub detector over the camera feed (the `custom`
source) and observes the report (`value`) and what a `changes()` subscriber wakes on.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import face_detection as fd
from reachy_mini_bridge.camera import CameraFeed, CameraFrame, frame_reader
from reachy_mini_bridge.face_detection import (
    DAEMON_DETECT_WEIGHT,
    Face,
    FaceDetection,
    FaceReport,
    PixelFace,
    check_face_detector_factory,
    report_from_daemon,
    report_from_pixels,
)
from reachy_mini_bridge.fake_reachy_mini import FAKE_FRAME_HZ, FakeReachyMini
from reachy_mini_bridge.observable import Observable

POLL_S = 0.05
FRAME_S = 1.0 / FAKE_FRAME_HZ
# The fake's frame: 64 x 48.
WIDTH, HEIGHT = 64, 48


@pytest.fixture(autouse=True)
def fast_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fd, "FACE_POLL_HZ", 1.0 / POLL_S)
    monkeypatch.setattr(fd, "FACE_ABSENT_S", 0.15)


class _Loop:
    """A running detection loop on a fake, with a subscriber recording what it wakes on."""

    def __init__(self, robot: FakeReachyMini, faces: Observable[FaceReport]) -> None:
        self.robot = robot
        self.faces = faces
        self.woken: list[FaceReport] = []
        self.observed: list[FaceReport] = []


@asynccontextmanager
async def _running() -> AsyncIterator[_Loop]:
    robot = FakeReachyMini()
    faces: Observable[FaceReport] = Observable(FaceReport.inactive("daemon"))
    loop = _Loop(robot, faces)
    detection = FaceDetection(
        robot,
        source="daemon",
        faces=faces,
        on_observation=loop.observed.append,
    )

    async def subscribe() -> None:
        async for report in faces.changes():
            loop.woken.append(report)

    subscriber = asyncio.create_task(subscribe())
    await asyncio.sleep(0)
    await detection.start()
    await asyncio.sleep(2 * POLL_S)  # the first poll has published `active`
    try:
        yield loop
    finally:
        await detection.stop()
        await asyncio.sleep(0)
        subscriber.cancel()


def _counts(reports: list[FaceReport]) -> list[int]:
    return [len(r.faces) for r in reports]


def _run[T](coro: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        return await coro()

    return asyncio.run(main())


def test_report_from_daemon_maps_the_payload() -> None:
    detected = {"detected": True, "x": 0.25, "y": -0.5, "roll": 0.1, "ts": 12.5}
    assert report_from_daemon(detected, active=True) == FaceReport(
        faces=(Face(x=0.25, y=-0.5, roll=0.1, size=None),),
        ts=12.5,
        source="daemon",
        active=True,
    )
    nobody = {"detected": False, "x": None, "y": None, "roll": None, "ts": None}
    assert report_from_daemon(nobody, active=True) == FaceReport(
        (), 0.0, "daemon", True
    )


def test_the_loop_publishes_active_then_a_face_appearing_once() -> None:
    async def run() -> tuple[list[FaceReport], FaceReport]:
        async with _running() as loop:
            loop.robot.show_face(0.2, 0.1)
            await asyncio.sleep(4 * POLL_S)
            return list(loop.woken), loop.faces.value

    woken, value = _run(run)
    assert [(r.active, len(r.faces)) for r in woken] == [(True, 0), (True, 1)]
    assert value.faces == (Face(x=0.2, y=0.1, roll=None, size=None),)


def test_a_gap_shorter_than_the_absence_window_wakes_nobody() -> None:
    async def run() -> tuple[list[int], list[int]]:
        async with _running() as loop:
            loop.robot.show_face()
            await asyncio.sleep(3 * POLL_S)
            loop.woken.clear()
            loop.robot.hide_face()
            await asyncio.sleep(0.08)  # < 0.15 s
            read_meanwhile = len(loop.faces.value.faces)
            loop.robot.show_face()
            await asyncio.sleep(0.3)
            return [read_meanwhile], _counts(loop.woken)

    read_meanwhile, woken = _run(run)
    assert read_meanwhile == [0]  # `value` did read empty during the gap
    assert woken == []


def test_a_face_gone_past_the_window_wakes_once_with_an_empty_report() -> None:
    async def run() -> list[FaceReport]:
        async with _running() as loop:
            loop.robot.show_face()
            await asyncio.sleep(3 * POLL_S)
            loop.woken.clear()
            loop.robot.hide_face()
            await asyncio.sleep(0.5)
            return list(loop.woken)

    woken = _run(run)
    assert [(r.active, r.faces) for r in woken] == [(True, ())]


def test_a_moving_face_updates_the_value_without_waking() -> None:
    async def run() -> tuple[float, list[int]]:
        async with _running() as loop:
            loop.robot.show_face(0.0, 0.0)
            await asyncio.sleep(3 * POLL_S)
            loop.woken.clear()
            loop.robot.show_face(0.5, 0.0)
            await asyncio.sleep(3 * POLL_S)
            return loop.faces.value.faces[0].x, _counts(loop.woken)

    x, woken = _run(run)
    assert x == 0.5
    assert woken == []


def test_stop_publishes_the_inactive_report() -> None:
    async def run() -> tuple[FaceReport, list[FaceReport]]:
        async with _running() as loop:
            loop.robot.show_face()
            await asyncio.sleep(3 * POLL_S)
        await asyncio.sleep(0)
        return loop.faces.value, loop.woken

    value, woken = _run(run)
    assert value == FaceReport.inactive("daemon")
    assert woken[-1] == FaceReport.inactive("daemon")


def test_the_loop_arms_the_daemons_detector_and_disarms_it() -> None:
    async def run() -> list[tuple[str, dict[str, Any]]]:
        async with _running() as loop:
            pass
        return [c for c in loop.robot.commands if "head_tracking" in c[0]]

    assert _run(run) == [
        ("start_head_tracking", {"weight": DAEMON_DETECT_WEIGHT}),
        ("stop_head_tracking", {}),
    ]


def test_a_failing_source_reads_inactive_then_active_on_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fd, "FACE_SOURCE_DOWN_S", 0.2)
    real = fd.daemon_face_target
    failing = {"on": False}

    def flaky(robot: Any) -> dict[str, Any]:
        if failing["on"]:
            raise OSError("503 Service Unavailable")
        return real(robot)

    monkeypatch.setattr(fd, "daemon_face_target", flaky)

    async def run() -> tuple[list[bool], bool]:
        async with _running() as loop:
            loop.robot.show_face()
            await asyncio.sleep(3 * POLL_S)
            loop.woken.clear()
            failing["on"] = True
            await asyncio.sleep(0.4)
            down = loop.faces.value.active
            failing["on"] = False
            await asyncio.sleep(3 * POLL_S)
            return [r.active for r in loop.woken], down

    woken, down = _run(run)
    assert down is False
    assert woken == [False, True]


def test_on_observation_sees_every_poll() -> None:
    async def run() -> tuple[int, int]:
        async with _running() as loop:
            loop.observed.clear()
            loop.robot.show_face()
            await asyncio.sleep(0.5)
            return len(loop.observed), sum(1 for r in loop.observed if r.faces)

    polls, with_face = _run(run)
    assert polls >= 6  # ~10 polls in 0.5 s at 20 Hz
    assert with_face >= polls - 1


# --- custom detectors: the contract and its check ----------------------------------------


class _StubDetector:
    """A detector returning whatever its scene holds; counts and times its calls."""

    def __init__(self, scene: _Scene) -> None:
        self._scene = scene

    def detect(
        self, frame_bgr: npt.NDArray[np.uint8], ts: float
    ) -> Sequence[PixelFace]:
        scene = self._scene
        scene.calls.append((ts, frame_bgr.shape))
        if scene.raises:
            raise RuntimeError("model crashed")
        if scene.delay_s:
            time.sleep(scene.delay_s)
        return list(scene.faces)


class _Scene:
    def __init__(self, faces: Sequence[PixelFace] = ()) -> None:
        self.faces = list(faces)
        self.calls: list[tuple[float, tuple[int, ...]]] = []
        self.raises = False
        self.delay_s = 0.0

    def factory(self) -> _StubDetector:
        return _StubDetector(self)


def _face(
    cx: float, cy: float, w: float = 8.0, h: float = 12.0, *, nose: bool = True
) -> PixelFace:
    """A pixel face centred at (cx, cy), its nose there too unless ``nose`` is False."""
    return PixelFace(
        bbox=(cx - w / 2, cy - h / 2, w, h), nose=(cx, cy) if nose else None
    )


def test_the_factory_check_accepts_a_class_and_a_lambda() -> None:
    scene = _Scene()
    check_face_detector_factory(scene.factory)
    check_face_detector_factory(lambda: _StubDetector(scene))

    class Bare:
        def detect(self, frame_bgr: object, ts: float) -> list[PixelFace]:
            return []

    check_face_detector_factory(Bare)


def test_the_factory_check_names_the_problem() -> None:
    with pytest.raises(ValueError, match="zero-argument callable.*got int"):
        check_face_detector_factory(42)

    def boom() -> _StubDetector:
        raise RuntimeError("no model file")

    with pytest.raises(ValueError, match="factory raised RuntimeError: no model file"):
        check_face_detector_factory(boom)
    with pytest.raises(ValueError, match="object has no callable `detect"):
        check_face_detector_factory(object)


def _frame(ts: float = 4.5, pose: npt.NDArray[np.float64] | None = None) -> CameraFrame:
    return CameraFrame(
        7, ts, np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8), head_pose=pose
    )


def test_report_from_pixels_normalises_into_the_trackers_coordinates() -> None:
    size = (WIDTH, HEIGHT)
    centre = report_from_pixels(
        [_face((WIDTH - 1) / 2, (HEIGHT - 1) / 2, h=24.0)], size, _frame(), 0
    )
    assert centre.faces[0].x == pytest.approx(0.0)
    assert centre.faces[0].y == pytest.approx(0.0)
    assert centre.faces[0].size == pytest.approx(0.5)  # 24 of 48 rows
    assert centre.faces[0].roll is None
    corner = report_from_pixels([_face(WIDTH - 1, HEIGHT - 1)], size, _frame(), 0)
    assert (corner.faces[0].x, corner.faces[0].y) == (1.0, 1.0)
    # Without a nose the bbox centre is the point; the eyes give the roll.
    eyed = PixelFace(bbox=(0.0, 0.0, WIDTH - 1, HEIGHT - 1), eyes=((10, 10), (20, 20)))
    report = report_from_pixels([eyed], size, _frame(), 0)
    assert (report.faces[0].x, report.faces[0].y) == (0.0, 0.0)
    assert report.faces[0].roll == pytest.approx(math.pi / 4)


def test_report_from_pixels_puts_the_target_first_and_carries_the_frame() -> None:
    pose = np.eye(4)
    pose[0, 3] = 0.42
    faces = [_face(10, 10), _face(50, 40), _face(30, 20)]
    report = report_from_pixels(faces, (WIDTH, HEIGHT), _frame(ts=4.5, pose=pose), 1)
    assert [round(f.x, 3) for f in report.faces] == [
        round(50 / 63 * 2 - 1, 3),
        round(10 / 63 * 2 - 1, 3),
        round(30 / 63 * 2 - 1, 3),
    ]
    assert (report.ts, report.source, report.active) == (4.5, "custom", True)
    assert report.head_pose is not None and report.head_pose[0, 3] == 0.42
    nobody = report_from_pixels([], (WIDTH, HEIGHT), _frame(), None)
    assert nobody.faces == () and nobody.active is True


# --- selection (specs/user_perception.md "The pipeline": no smoothing) ------------------


def test_the_largest_face_above_the_minimum_size_is_acquired() -> None:
    selector = fd._FaceSelector()
    size = (WIDTH, HEIGHT)
    small, big = _face(10, 10, 4, 4), _face(50, 30, 10, 16)
    assert selector.select([small, big], size) == 1
    # A speck below 0.3 % of the frame (64 x 48 = 3072 px: under ~9 px) is nobody.
    assert fd._FaceSelector().select([_face(10, 10, 2, 3)], size) is None


def test_the_nearest_face_is_kept_over_a_larger_newcomer() -> None:
    selector = fd._FaceSelector()
    size = (WIDTH, HEIGHT)
    assert selector.select([_face(20, 24)], size) == 0
    newcomer = _face(58, 24, 12, 18)  # larger, far to the right
    assert selector.select([newcomer, _face(22, 24)], size) == 1
    assert selector.select([_face(24, 25), newcomer], size) == 0


def test_a_jump_beyond_the_gate_is_a_miss_and_misses_drop_the_association() -> None:
    selector = fd._FaceSelector(max_misses=2)
    size = (WIDTH, HEIGHT)
    assert selector.select([_face(8, 24)], size) == 0  # x ≈ -0.75
    far = _face(60, 24)  # x ≈ +0.9: a jump of 1.65 > 0.5
    assert selector.select([far], size) is None  # miss 1: the association holds
    assert selector.select([far], size) is None  # miss 2
    assert (
        selector.select([far], size) == 0
    )  # miss 3 drops it: the far face is acquired
    # Once acquired, the association follows the new face.
    assert selector.select([_face(8, 24), _face(58, 26)], size) == 1


def test_missing_frames_then_a_return_nearby_keeps_the_target() -> None:
    selector = fd._FaceSelector(max_misses=5)
    size = (WIDTH, HEIGHT)
    assert selector.select([_face(30, 24)], size) == 0
    for _ in range(3):
        assert selector.select([], size) is None
    assert selector.select([_face(50, 24), _face(32, 22)], size) == 1


# --- the runner over the camera feed ------------------------------------------------------


class _CustomLoop(_Loop):
    def __init__(
        self,
        robot: FakeReachyMini,
        faces: Observable[FaceReport],
        feed: CameraFeed,
        scene: _Scene,
        detection: FaceDetection,
    ) -> None:
        super().__init__(robot, faces)
        self.feed = feed
        self.scene = scene
        self.detection = detection


POSE = np.eye(4)
POSE[1, 3] = 0.25


@asynccontextmanager
async def _running_custom(
    scene: _Scene, *, start: bool = True
) -> AsyncIterator[_CustomLoop]:
    robot = FakeReachyMini()
    feed = CameraFeed(frame_reader(robot), lambda _t: POSE.copy())
    faces: Observable[FaceReport] = Observable(FaceReport.inactive("custom"))
    detection = FaceDetection(
        robot,
        source="custom",
        faces=faces,
        on_observation=None,
        feed=feed,
        detector_factory=scene.factory,
    )
    loop = _CustomLoop(robot, faces, feed, scene, detection)
    detection._on_observation = loop.observed.append

    async def subscribe() -> None:
        async for report in faces.changes():
            loop.woken.append(report)

    subscriber = asyncio.create_task(subscribe())
    await asyncio.sleep(0)
    feed.start()
    if start:
        await detection.start()
    try:
        yield loop
    finally:
        await detection.stop()
        feed.stop()
        await asyncio.sleep(0)
        subscriber.cancel()


async def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_the_custom_source_reports_the_detectors_faces_on_the_frame() -> None:
    scene = _Scene([_face(10, 10, 6, 6), _face(47.25, 23.5, 10, 24)])

    async def run() -> tuple[FaceReport, CameraFrame | None, list[str]]:
        async with _running_custom(scene) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            report = loop.faces.value
            frame = loop.feed.latest()
            return report, frame, [c[0] for c in loop.robot.commands]

    report, frame, commands = _run(run)
    assert report.source == "custom" and report.active is True
    assert len(report.faces) == 2
    target = report.faces[0]  # the largest, first
    assert target.x == pytest.approx(0.5) and target.y == pytest.approx(0.0)
    assert target.size == pytest.approx(0.5)
    # The report's time and pose are its frame's — the pose the frame was taken from.
    assert frame is not None and report.ts <= frame.ts
    assert report.head_pose is not None
    assert report.head_pose[1, 3] == 0.25
    assert scene.calls[0][1] == (HEIGHT, WIDTH, 3)
    assert "start_head_tracking" not in commands  # the daemon's tracking is left alone


def test_the_detector_runs_once_per_frame_not_once_per_poll() -> None:
    scene = _Scene([_face(30, 24)])

    async def run() -> tuple[int, int, int]:
        async with _running_custom(scene) as loop:
            await asyncio.sleep(0.3)
            scene.calls.clear()
            polls_before = len(loop.observed)
            frames_before = loop.feed.published_count
            await asyncio.sleep(1.0)
            return (
                len(scene.calls),
                loop.feed.published_count - frames_before,
                len(loop.observed) - polls_before,
            )

    calls, frames, observations = _run(run)
    assert 7 <= calls <= 13, calls  # ≈ FAKE_FRAME_HZ, not the 20 Hz poll rate
    assert calls == observations  # one report per detector call
    assert abs(calls - frames) <= 1


def test_a_slow_detector_skips_frames_and_never_queues_them() -> None:
    scene = _Scene([_face(30, 24)])
    scene.delay_s = 0.25  # past two frame periods

    async def run() -> tuple[int, int, float]:
        async with _running_custom(scene) as loop:
            await asyncio.sleep(1.2)
            calls = len(scene.calls)
            frames = loop.feed.published_count
            frame_ts = [ts for ts, _ in scene.calls]
            gaps = [b - a for a, b in itertools.pairwise(frame_ts)]
            return calls, frames, min(gaps)

    calls, frames, min_gap = _run(run)
    assert calls <= 6 and frames >= 10  # more frames than calls: frames were dropped
    assert min_gap >= 0.2  # each call saw a frame at least two periods newer


def test_a_detector_raising_on_every_frame_reads_inactive_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fd, "FACE_SOURCE_DOWN_S", 0.3)
    scene = _Scene([_face(30, 24)])

    async def run() -> tuple[bool, bool, list[bool]]:
        async with _running_custom(scene) as loop:
            await _wait_for(lambda: loop.faces.value.active)
            loop.woken.clear()
            scene.raises = True
            await asyncio.sleep(0.6)
            down = loop.faces.value.active
            scene.raises = False
            await _wait_for(lambda: loop.faces.value.active)
            return down, loop.faces.value.active, [r.active for r in loop.woken]

    down, up, woken = _run(run)
    assert down is False and up is True
    assert woken == [False, True]


def test_restart_swaps_the_detector_between_polls() -> None:
    first = _Scene([_face(8, 24)])
    second = _Scene([_face(56, 24)])

    async def run() -> tuple[float, float, int]:
        async with _running_custom(first) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            before = loop.faces.value.faces[0].x
            loop.detection.restart(second.factory)
            await _wait_for(lambda: loop.faces.value.faces[0].x > 0)
            calls_after = len(first.calls)
            await asyncio.sleep(3 * FRAME_S)
            return before, loop.faces.value.faces[0].x, len(first.calls) - calls_after

    before, after, stale_calls = _run(run)
    assert before < -0.7 and after > 0.7
    assert stale_calls == 0  # the old detector is never called again


def test_the_custom_source_needs_a_feed_and_a_detector() -> None:
    async def run() -> None:
        robot = FakeReachyMini()
        faces: Observable[FaceReport] = Observable(FaceReport.inactive("custom"))
        with pytest.raises(ValueError, match="needs the camera feed"):
            await FaceDetection(robot, source="custom", faces=faces).start()
        feed = CameraFeed(frame_reader(robot), None)
        with pytest.raises(ValueError, match="no face detector is registered"):
            await FaceDetection(robot, source="custom", faces=faces, feed=feed).start()
        assert robot.commands == []

    _run(run)


def test_a_display_sampling_the_feed_costs_the_detector_no_frames() -> None:
    """The single-reader property end to end: a second consumer polling the feed at
    50 Hz (the control panel's display) while the detector runs; the detector still
    sees every frame."""
    scene = _Scene([_face(30, 24)])
    stop = threading.Event()
    seen: set[int] = set()

    def display(feed: CameraFeed) -> None:
        while not stop.is_set():
            frame = feed.latest()
            if frame is not None:
                seen.add(frame.frame_id)
            time.sleep(0.02)

    async def run() -> tuple[int, int]:
        async with _running_custom(scene) as loop:
            thread = threading.Thread(target=display, args=(loop.feed,))
            thread.start()
            try:
                await asyncio.sleep(0.3)
                scene.calls.clear()
                frames_before = loop.feed.published_count
                await asyncio.sleep(1.0)
                return len(scene.calls), loop.feed.published_count - frames_before
            finally:
                stop.set()
                thread.join()

    calls, frames = _run(run)
    assert abs(calls - frames) <= 1
    assert len(seen) >= 10
