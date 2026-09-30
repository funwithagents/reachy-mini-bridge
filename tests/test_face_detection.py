"""Functional tests for the detection loop (specs/vision/user_perception.md) on the fake.

The loop runs unchanged at real time, with its timing shortened: polls at 20 Hz, a
drop published after 0.15 s. A test drives a stub detector's scene over the fake's camera
feed — showing, moving and hiding faces — and observes the report (`value`) and what a
`changes()` subscriber wakes on. The fake has no detector of its own; the shipped one is
resolved by name and exercised on the stub.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import face_detection as fd
from reachy_mini_bridge import fake_reachy_mini as fake_module
from reachy_mini_bridge.camera import CameraFeed, CameraFrame, frame_reader
from reachy_mini_bridge.face_detection import (
    FaceDetection,
    FaceReport,
    PixelFace,
    check_face_detector_factory,
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


def _counts(reports: list[FaceReport]) -> list[int]:
    return [len(r.faces) for r in reports]


def _run[T](coro: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        return await coro()

    return asyncio.run(main())


# --- the report and the debounce, driven through a stub detector's scene -----------------
#
# The detector runs once per frame, so the scene's changes reach the loop at the fake's
# frame rate. These tests raise it to 40 fps (a frame every 25 ms) so the 0.15 s absence
# window spans several frames and a shorter gap still shows in `value`.

FAST_FRAME_HZ = 40.0


@pytest.fixture
def fast_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake_module, "FAKE_FRAME_HZ", FAST_FRAME_HZ)


def test_the_loop_publishes_active_then_a_face_appearing_once(
    fast_frames: None,
) -> None:
    scene = _Scene()

    async def run() -> tuple[list[FaceReport], FaceReport]:
        async with _running_custom(scene) as loop:
            await _wait_for(lambda: loop.faces.value.active)
            scene.show(0.2, 0.1)
            await asyncio.sleep(0.2)
            return list(loop.woken), loop.faces.value

    woken, value = _run(run)
    assert [(r.active, len(r.faces)) for r in woken] == [(True, 0), (True, 1)]
    assert value.faces[0].x == pytest.approx(0.2, abs=0.02)
    assert value.faces[0].y == pytest.approx(0.1, abs=0.03)
    assert value.source == "custom"


def test_a_gap_shorter_than_the_absence_window_wakes_nobody(fast_frames: None) -> None:
    scene = _Scene()

    async def run() -> tuple[list[int], list[int]]:
        async with _running_custom(scene) as loop:
            scene.show()
            await _wait_for(lambda: bool(loop.faces.value.faces))
            loop.woken.clear()
            scene.hide()
            await asyncio.sleep(0.08)  # < 0.15 s, > two frames at 40 fps
            read_meanwhile = len(loop.faces.value.faces)
            scene.show()
            await asyncio.sleep(0.3)
            return [read_meanwhile], _counts(loop.woken)

    read_meanwhile, woken = _run(run)
    assert read_meanwhile == [0]  # `value` did read empty during the gap
    assert woken == []


def test_a_face_gone_past_the_window_wakes_once_with_an_empty_report(
    fast_frames: None,
) -> None:
    scene = _Scene()

    async def run() -> list[FaceReport]:
        async with _running_custom(scene) as loop:
            scene.show()
            await _wait_for(lambda: bool(loop.faces.value.faces))
            loop.woken.clear()
            scene.hide()
            await asyncio.sleep(0.5)
            return list(loop.woken)

    woken = _run(run)
    assert [(r.active, r.faces) for r in woken] == [(True, ())]


def test_a_moving_face_updates_the_value_without_waking(fast_frames: None) -> None:
    scene = _Scene()

    async def run() -> tuple[float, list[int]]:
        async with _running_custom(scene) as loop:
            scene.show(0.0, 0.0)
            await _wait_for(lambda: bool(loop.faces.value.faces))
            loop.woken.clear()
            scene.show(0.5, 0.0)
            await _wait_for(lambda: loop.faces.value.faces[0].x > 0.4)
            return loop.faces.value.faces[0].x, _counts(loop.woken)

    x, woken = _run(run)
    assert x == pytest.approx(0.5, abs=0.02)
    assert woken == []


def test_stop_publishes_the_inactive_report(fast_frames: None) -> None:
    scene = _Scene()

    async def run() -> tuple[FaceReport, list[FaceReport]]:
        async with _running_custom(scene) as loop:
            scene.show()
            await _wait_for(lambda: bool(loop.faces.value.faces))
        await asyncio.sleep(0)
        return loop.faces.value, loop.woken

    value, woken = _run(run)
    assert value == FaceReport.inactive("custom")
    assert woken[-1] == FaceReport.inactive("custom")


def test_on_observation_sees_every_observation(fast_frames: None) -> None:
    scene = _Scene()

    async def run() -> tuple[int, int]:
        async with _running_custom(scene) as loop:
            await _wait_for(lambda: loop.faces.value.active)
            scene.show()
            await asyncio.sleep(0.05)
            loop.observed.clear()
            await asyncio.sleep(0.5)
            return len(loop.observed), sum(1 for r in loop.observed if r.faces)

    observations, with_face = _run(run)
    assert observations >= 6  # ~10 a second: one per poll at 20 Hz, once per new frame
    assert with_face == observations


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
        self.built_on: list[int] = []  # the thread each detector was built on

    def factory(self) -> _StubDetector:
        self.built_on.append(threading.get_ident())
        return _StubDetector(self)

    def show(self, x: float = 0.0, y: float = 0.0) -> None:
        """One face whose nose sits at the normalised (x, y) of the fake's frame."""
        u = (x + 1.0) / 2.0 * (WIDTH - 1)
        v = (y + 1.0) / 2.0 * (HEIGHT - 1)
        self.faces[:] = [_face(u, v, 10.0, 16.0)]

    def hide(self) -> None:
        self.faces.clear()


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
        [_face((WIDTH - 1) / 2, (HEIGHT - 1) / 2, h=24.0)], size, _frame(), [1]
    )
    assert centre.faces[0].x == pytest.approx(0.0)
    assert centre.faces[0].y == pytest.approx(0.0)
    assert centre.faces[0].size == pytest.approx(0.5)  # 24 of 48 rows
    assert centre.faces[0].roll is None
    corner = report_from_pixels([_face(WIDTH - 1, HEIGHT - 1)], size, _frame(), [1])
    assert (corner.faces[0].x, corner.faces[0].y) == (1.0, 1.0)
    # Without a nose the bbox centre is the point; the eyes give the roll.
    eyed = PixelFace(bbox=(0.0, 0.0, WIDTH - 1, HEIGHT - 1), eyes=((10, 10), (20, 20)))
    report = report_from_pixels([eyed], size, _frame(), [1])
    assert (report.faces[0].x, report.faces[0].y) == (0.0, 0.0)
    assert report.faces[0].roll == pytest.approx(math.pi / 4)
    assert (report.faces[0].pitch, report.faces[0].yaw) == (None, None)


def test_report_from_pixels_orders_by_track_id_and_carries_the_frame() -> None:
    """specs/vision/user_perception.md "The face report": the faces in track_id order,
    whatever order the detector returned them in, each with its id and pixel box; the
    report holds the frame itself (its id delegating to it), its time and pose."""
    pose = np.eye(4)
    pose[0, 3] = 0.42
    frame = _frame(ts=4.5, pose=pose)
    faces = [_face(10, 10), _face(50, 40), _face(30, 20)]
    report = report_from_pixels(faces, (WIDTH, HEIGHT), frame, [7, 3, 5])
    assert [f.track_id for f in report.faces] == [3, 5, 7]
    assert [f.bbox for f in report.faces] == [
        faces[1].bbox,
        faces[2].bbox,
        faces[0].bbox,
    ]
    assert report.faces[0].x == pytest.approx(50 / 63 * 2 - 1)
    assert (report.ts, report.source, report.active) == (4.5, "custom", True)
    assert report.head_pose is not None and report.head_pose[0, 3] == 0.42
    assert report.frame is frame and report.frame_id == frame.frame_id == 7
    nobody = report_from_pixels([], (WIDTH, HEIGHT), _frame(), [])
    assert nobody.faces == () and nobody.active is True
    # equality is on what was seen, not on the frame the report references
    again = report_from_pixels(faces, (WIDTH, HEIGHT), _frame(ts=4.5), [7, 3, 5])
    assert again == report
    assert FaceReport.inactive("custom").frame_id == 0


def test_a_fitted_orientation_is_preferred_over_the_eye_line() -> None:
    fitted = PixelFace(
        bbox=(0.0, 0.0, 20.0, 20.0),
        eyes=((5, 5), (15, 15)),  # an eye line of 45 degrees...
        orientation=(0.1, -0.2, 0.3),  # ...but the detector fitted the head
    )
    face = report_from_pixels([fitted], (WIDTH, HEIGHT), _frame(), [1]).faces[0]
    assert (face.roll, face.pitch, face.yaw) == (0.1, -0.2, 0.3)


# --- tracks (specs/vision/user_perception.md "Tracks": no smoothing, no face singled out) ------


def _counter() -> Callable[[], int]:
    return itertools.count(1).__next__


def test_a_moving_face_keeps_its_track_id() -> None:
    tracks = fd._FaceTracks(_counter())
    size = (WIDTH, HEIGHT)
    ids = [tracks.update([_face(10 + 3 * i, 24)], size) for i in range(10)]
    assert ids == [[1]] * 10


def test_two_faces_crossing_in_small_steps_keep_their_ids() -> None:
    """Two faces swapping sides a few pixels a frame never trade ids: each continues the
    track nearest to it, pairs taken nearest first."""
    tracks = fd._FaceTracks(_counter())
    size = (WIDTH, HEIGHT)
    left, right = 16.0, 48.0
    first = tracks.update([_face(left, 20), _face(right, 28)], size)
    assert first == [1, 2]
    for step in range(1, 9):
        a = left + 4 * step  # the first face walks right...
        b = right - 4 * step  # ...the second left, a little lower
        # the detector returns them in an arbitrary order
        ids = tracks.update([_face(b, 28), _face(a, 20)], size)
        assert ids == [2, 1], (step, ids)


def test_a_gap_within_the_miss_window_keeps_the_id_and_a_longer_one_does_not() -> None:
    size = (WIDTH, HEIGHT)
    tracks = fd._FaceTracks(_counter(), max_misses=3)
    assert tracks.update([_face(30, 24)], size) == [1]
    for _ in range(3):
        assert tracks.update([], size) == []
    assert tracks.update([_face(32, 22)], size) == [1]  # back within the window
    for _ in range(4):  # one miss more than the window
        tracks.update([], size)
    assert tracks.update([_face(32, 22)], size) == [2]  # a new id, 1 never reused


def test_a_jump_beyond_the_gate_opens_a_new_track() -> None:
    tracks = fd._FaceTracks(_counter())
    size = (WIDTH, HEIGHT)
    assert tracks.update([_face(8, 24)], size) == [1]  # x = -0.75
    # x = +0.9: a jump of 1.65 > TRACK_MAX_JUMP — someone else, not the same person
    assert tracks.update([_face(60, 24)], size) == [2]
    # the first track is still alive within its miss window
    assert tracks.update([_face(8, 24), _face(60, 24)], size) == [1, 2]


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
    scene: _Scene,
    *,
    start: bool = True,
    target_fps: float | None = None,
    factory: Callable[[], fd.FaceDetector] | None = None,
) -> AsyncIterator[_CustomLoop]:
    robot = FakeReachyMini()
    feed = CameraFeed(frame_reader(robot), lambda _t: POSE.copy())
    faces: Observable[FaceReport] = Observable(FaceReport.inactive("custom"))
    detection = FaceDetection(
        detector="custom",
        faces=faces,
        on_observation=None,
        feed=feed,
        detector_factory=scene.factory if factory is None else factory,
        target_fps=target_fps,
    )
    loop = _CustomLoop(robot, faces, feed, scene, detection)
    detection._on_observation = loop.observed.append

    async def subscribe() -> None:
        async for report in faces.changes():
            loop.woken.append(report)

    subscriber = asyncio.create_task(subscribe())
    await asyncio.sleep(0)
    await feed.start()
    if start:
        await detection.start()
    try:
        yield loop
    finally:
        await detection.stop()
        await feed.stop()
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
    assert [f.track_id for f in report.faces] == [1, 2]  # new tracks, in detector order
    big = report.faces[1]
    assert big.x == pytest.approx(0.5) and big.y == pytest.approx(0.0)
    assert big.size == pytest.approx(0.5)
    assert big.bbox == scene.faces[1].bbox
    # the report carries the frame its faces were found in
    assert report.frame is not None and report.frame_id == report.frame.frame_id
    # The report's time and pose are its frame's — the pose the frame was taken from.
    assert frame is not None and report.ts <= frame.ts
    assert report.head_pose is not None
    assert report.head_pose[1, 3] == 0.25
    assert scene.calls[0][1] == (HEIGHT, WIDTH, 3)
    assert (
        commands == []
    )  # nothing is sent to the robot: the daemon's tracking is untouched


def test_track_ids_keep_counting_across_a_restart_of_the_loop() -> None:
    """An id is never reused: the counter outlives a stop / start of the loop (the
    tracks themselves start afresh), as the camera feed's frame_id does."""
    scene = _Scene([_face(30, 24)])

    async def run() -> tuple[int, int]:
        async with _running_custom(scene) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            first = loop.faces.value.faces[0].track_id
            await loop.detection.stop()
            await loop.detection.start()
            await _wait_for(lambda: bool(loop.faces.value.faces))
            return first, loop.faces.value.faces[0].track_id

    first, second = _run(run)
    assert first == 1 and second == 2


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


def test_start_refuses_what_it_cannot_run() -> None:
    """No detector named, `custom` with none registered, no feed, an unknown name: each
    a ValueError, and nothing started."""
    scene = _Scene()

    async def run() -> None:
        robot = FakeReachyMini()
        faces: Observable[FaceReport] = Observable(FaceReport.inactive(None))
        feed = CameraFeed(frame_reader(robot), None)
        with pytest.raises(ValueError, match="no face detector is configured"):
            await FaceDetection(detector=None, faces=faces, feed=feed).start()
        with pytest.raises(ValueError, match="no face detector is registered"):
            await FaceDetection(detector="custom", faces=faces, feed=feed).start()
        with pytest.raises(ValueError, match="needs the camera feed"):
            await FaceDetection(
                detector="custom", faces=faces, detector_factory=scene.factory
            ).start()
        with pytest.raises(ValueError, match="unknown face detector 'daemon'"):
            await FaceDetection(detector="daemon", faces=faces, feed=feed).start()
        assert robot.commands == [] and scene.built_on == []
        assert faces.value == FaceReport.inactive(None)

    _run(run)


def test_the_detector_is_built_on_a_worker_thread_before_the_loop_runs() -> None:
    scene = _Scene([_face(30, 24)])
    running_when_built: list[bool] = []

    async def run() -> tuple[list[int], int]:
        async with _running_custom(scene, start=False) as loop:
            detection = loop.detection
            factory = scene.factory

            def watching_factory() -> _StubDetector:
                running_when_built.append(detection.running)
                return factory()

            detection.restart(watching_factory)
            await detection.start()
            assert detection.running
            await _wait_for(lambda: bool(loop.faces.value.faces))
            return list(scene.built_on), threading.get_ident()

    built_on, main_thread = _run(run)
    assert len(built_on) == 1 and built_on[0] != main_thread
    assert running_when_built == [False]


def test_a_factory_that_raises_fails_the_start_and_starts_nothing() -> None:
    def broken() -> _StubDetector:
        raise OSError("no network: the model could not be downloaded")

    async def run() -> tuple[bool, FaceReport, int]:
        robot = FakeReachyMini()
        faces: Observable[FaceReport] = Observable(FaceReport.inactive("custom"))
        feed = CameraFeed(frame_reader(robot), None)
        detection = FaceDetection(
            detector="custom", faces=faces, feed=feed, detector_factory=broken
        )
        with pytest.raises(OSError, match="no network"):
            await detection.start()
        await asyncio.sleep(0.05)
        running = detection.running
        await detection.stop()  # a no-op on a loop that never started
        return running, faces.value, len(robot.commands)

    running, value, commands = _run(run)
    assert running is False
    assert value == FaceReport.inactive("custom")
    assert commands == 0


def test_the_shipped_detector_is_resolved_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`yunet` builds the shipped detector through its factory (substituted here: the
    real one loads upstream's model) and labels its reports."""
    scene = _Scene([_face(30, 24)])
    widths: list[int | None] = []

    def shipped(width: int | None = None) -> _StubDetector:
        widths.append(width)
        return scene.factory()

    monkeypatch.setattr(fd, "_yunet_factory", shipped)

    async def run() -> FaceReport:
        robot = FakeReachyMini()
        feed = CameraFeed(frame_reader(robot), None)
        faces: Observable[FaceReport] = Observable(FaceReport.inactive("yunet"))
        detection = FaceDetection(detector="yunet", faces=faces, feed=feed, width=640)
        await feed.start()
        await detection.start()
        try:
            await _wait_for(lambda: bool(faces.value.faces))
            return faces.value
        finally:
            await detection.stop()
            await feed.stop()

    report = _run(run)
    assert report.source == "yunet" and report.active
    assert len(report.faces) == 1 and scene.built_on
    assert report.faces[0].size == pytest.approx(12 / 48)
    assert widths == [640]  # the config's width reaches the shipped detector


# --- the cost knobs and the detector's release (specs/vision/user_perception.md "The detection
# loop") -------------------------------------------------------------------------------------


def test_target_fps_caps_the_detector_below_the_frame_rate(fast_frames: None) -> None:
    """At 40 fps from the fake, a 2.0 ceiling runs the detector about twice a second —
    the loop skips the frames, so it holds for any detector."""
    scene = _Scene([_face(30, 24)])

    async def run(target_fps: float | None) -> int:
        async with _running_custom(scene, target_fps=target_fps) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            scene.calls.clear()
            await asyncio.sleep(2.0)
            return len(scene.calls)

    capped = _run(lambda: run(2.0))
    uncapped = _run(lambda: run(None))
    assert 3 <= capped <= 5, capped
    assert uncapped >= 20, uncapped  # once per new frame without a ceiling


def test_a_slow_ceiling_is_not_read_as_a_detector_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fd, "FACE_SOURCE_DOWN_S", 0.3)
    scene = _Scene([_face(30, 24)])

    async def run() -> list[bool]:
        async with _running_custom(scene, target_fps=0.5) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            await asyncio.sleep(1.0)  # past FACE_SOURCE_DOWN_S, frames arriving
            return [r.active for r in loop.woken]

    assert False not in _run(run)


class _ClosingDetector(_StubDetector):
    """A stub with a ``close()``; ``hold`` blocks ``detect`` until set."""

    def __init__(self, scene: _Scene, closed: list[str], name: str) -> None:
        super().__init__(scene)
        self._closed = closed
        self._name = name
        self.hold: threading.Event | None = None
        self.raises_on_close = False
        self.in_detect = threading.Event()

    def detect(
        self, frame_bgr: npt.NDArray[np.uint8], ts: float
    ) -> Sequence[PixelFace]:
        self.in_detect.set()
        if self.hold is not None:
            self.hold.wait(5.0)
        return super().detect(frame_bgr, ts)

    def close(self) -> None:
        self._closed.append(self._name)
        if self.raises_on_close:
            raise RuntimeError("the session was already gone")


def test_close_is_called_at_stop_and_when_a_factory_swap_replaces_the_detector() -> (
    None
):
    scene = _Scene([_face(30, 24)])
    closed: list[str] = []
    names = itertools.count(1)

    def factory() -> _ClosingDetector:
        return _ClosingDetector(scene, closed, f"detector {next(names)}")

    async def run() -> list[str]:
        async with _running_custom(scene, factory=factory) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            loop.detection.restart(factory)
            await _wait_for(lambda: closed == ["detector 1"])
            scene.calls.clear()
            await _wait_for(lambda: bool(scene.calls))  # the new one runs
        return closed

    assert _run(run) == ["detector 1", "detector 2"]


def test_a_stop_during_detect_closes_once_the_call_has_returned() -> None:
    """detect runs on in its thread when the loop is cancelled; close waits for it, so a
    stateful detector is never closed under a running call — and the loop restarts."""
    scene = _Scene([_face(30, 24)])
    closed: list[str] = []
    detectors: list[_ClosingDetector] = []

    def factory() -> _ClosingDetector:
        detector = _ClosingDetector(scene, closed, "held")
        detectors.append(detector)
        return detector

    async def run() -> tuple[list[str], list[str], bool]:
        async with _running_custom(scene, factory=factory) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            held = threading.Event()
            detectors[0].hold = held
            detectors[0].in_detect.clear()
            await asyncio.to_thread(detectors[0].in_detect.wait, 2.0)
            stopping = asyncio.create_task(loop.detection.stop())
            await asyncio.sleep(0.2)
            while_running = list(closed)
            held.set()
            await stopping
            after = list(closed)
            await loop.detection.start()  # restartable
            await _wait_for(lambda: loop.faces.value.active)
            return while_running, after, loop.detection.running

    while_running, after, running = _run(run)
    assert while_running == []
    assert after == ["held"]
    assert running


def test_a_close_that_raises_does_not_break_stop() -> None:
    scene = _Scene([_face(30, 24)])
    closed: list[str] = []

    def factory() -> _ClosingDetector:
        detector = _ClosingDetector(scene, closed, "broken")
        detector.raises_on_close = True
        return detector

    async def run() -> FaceReport:
        async with _running_custom(scene, factory=factory) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            await loop.detection.stop()
            return loop.faces.value

    assert _run(run) == FaceReport.inactive("custom")
    assert closed == ["broken"]


def test_the_detectors_cost_is_logged_once_per_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(fd, "FACE_COST_LOG_S", 0.3)
    scene = _Scene([_face(30, 24)])

    async def run() -> None:
        async with _running_custom(scene) as loop:
            await _wait_for(lambda: bool(loop.faces.value.faces))
            await asyncio.sleep(0.8)

    with caplog.at_level("INFO", logger=fd.__name__):
        _run(run)
    lines = [r.getMessage() for r in caplog.records if "ms a frame" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "custom detector" in lines[0] and "width 320" in lines[0]
    rate = float(lines[0].split("average, ")[1].split(" observations")[0])
    assert rate > 0


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
