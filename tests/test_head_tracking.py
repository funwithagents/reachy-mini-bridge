"""Functional tests for the bridge's head tracker (specs/motion/head_tracking.md).

Offline and daemon-free: the geometry is upstream's pure helpers, and the closed loop is
the real ``MotionSession`` on a ``FakeReachyMini`` at real time, fed by a "detector" that
projects the test scene's portrait through the sim camera's pinhole from the head pose
the loop commanded — the head-mounted camera — so the head's own motion feeds back
into what the tracker sees, as on the viewer sim.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from reachy_mini.vision.look_at import default_head_to_camera_transform
from scipy.spatial.transform import Rotation

from reachy_mini_bridge import head_tracking
from reachy_mini_bridge.config import SimCameraSettings
from reachy_mini_bridge.face_detection import Face, FaceReport
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.head_tracking import (
    SIM_EYE_CAMERA_FOVY_DEG,
    CameraModel,
    HeadTracker,
    HeadTrackingReport,
    frame_head_pose,
    pinhole_intrinsics,
    sim_hfov_deg,
)
from reachy_mini_bridge.motion import MotionSession
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS

if TYPE_CHECKING:
    import numpy.typing as npt

DETECT_HZ = 10.0  # upstream's detector sees the camera feed at 10 fps
POLL_HZ = 30.0  # the detection loop's poll rate


@asynccontextmanager
async def _running(session: MotionSession) -> AsyncIterator[MotionSession]:
    """``start()`` / ``stop()`` around a block — what the bridge does with the session."""
    await session.start()
    try:
        yield session
    finally:
        await session.stop()


def _yaw_deg(head: npt.NDArray[np.float64]) -> float:
    return float(Rotation.from_matrix(head[:3, :3]).as_euler("ZYX", degrees=True)[0])


def _report(x: float, y: float, ts: float) -> FaceReport:
    return FaceReport(
        faces=(Face(x=x, y=y, roll=None, size=0.2),),
        ts=ts,
        source="yunet",
        active=True,
    )


def _nobody() -> FaceReport:
    return FaceReport(faces=(), ts=time.monotonic(), source="yunet", active=True)


def _project(
    face: tuple[float, float, float], head: npt.NDArray[np.float64], camera: CameraModel
) -> tuple[float, float]:
    """The face's normalised image position seen by a camera mounted on ``head`` — the
    inverse of the tracker's geometry, with an ideal pinhole."""
    cam = head @ default_head_to_camera_transform()
    p = np.linalg.inv(cam) @ np.array([*face, 1.0])
    width, height = camera.size
    u = camera.K[0, 0] * p[0] / p[2] + camera.K[0, 2]
    v = camera.K[1, 1] * p[1] / p[2] + camera.K[1, 2]
    return u / (width - 1) * 2 - 1, v / (height - 1) * 2 - 1


async def _closed_loop(
    face: tuple[float, float, float],
    seconds: float,
    *,
    frame_delay_s: float = 0.0,
) -> tuple[list[float], tuple[float, float], float]:
    """Track ``face`` for ``seconds``: every detection projects it from the head pose
    of ``frame_delay_s`` ago — when its frame was taken — and is stamped *now*, as the
    camera feed stamps a frame with its arrival time, so the tracker is never told the
    delay.
    The commanded yaw at every poll, where the face projects at the end, and the
    tracker's delay estimate."""
    robot = FakeReachyMini()
    camera = CameraModel.for_sim(SimCameraSettings(source="sim"))
    async with _running(MotionSession(robot, presence=True, idle="hold")) as session:
        tracker = HeadTracker(
            camera,
            history=session.head_pose_history,
            set_gaze=session.set_gaze,
        )
        session.resume()
        yaws: list[float] = []
        report = _nobody()
        detected_at = -math.inf
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - detected_at >= 1.0 / DETECT_HZ:
                detected_at = now
                frame_pose = session.head_pose_at(now - frame_delay_s)
                x, y = _project(face, frame_pose, camera)
                report = _report(x, y, ts=now)
            tracker.observe(report)
            yaws.append(_yaw_deg(robot.last_target[0]))
            await asyncio.sleep(1.0 / POLL_HZ)
        return yaws, _project(face, robot.last_target[0], camera), tracker.delay_s


def _face_at(lateral: float) -> tuple[float, float, float]:
    x, _y, z = DEFAULT_FACE_POS
    return (x, lateral, z)


def _expected_yaw(lateral: float) -> float:
    return math.degrees(math.atan2(lateral, DEFAULT_FACE_POS[0]))


def _assert_converged(
    yaws: list[float], centre: tuple[float, float], lateral: float
) -> None:
    expected = _expected_yaw(lateral)
    assert yaws[-1] == pytest.approx(expected, abs=3.0)
    assert abs(centre[0]) < 0.1 and abs(centre[1]) < 0.1
    if lateral:
        direction = math.copysign(1.0, expected)
        past = [(y - expected) * direction for y in yaws]
        peak = past.index(max(past))
        assert past[peak] <= 10.0  # past the face once, by a bounded amount…
        assert min(past[peak:]) >= -3.0  # …and never swinging back short of it


def test_the_head_converges_on_the_face_ahead_and_to_either_side() -> None:
    laterals = (0.0, 0.15, -0.15)

    async def run() -> list[tuple[list[float], tuple[float, float], float]]:
        return list(
            await asyncio.gather(*(_closed_loop(_face_at(la), 4.0) for la in laterals))
        )

    for lateral, (yaws, centre, _delay) in zip(
        laterals, asyncio.run(run()), strict=True
    ):
        _assert_converged(yaws, centre, lateral)


def test_an_unreported_frame_delay_is_learned_and_the_head_still_settles() -> None:
    """The frame is 0.3 s older than its timestamp says — a frame stamped with its
    arrival time, as the live backends' are: the tracker learns the delay from its first
    turn and the head settles on the face."""
    face = _face_at(0.15)
    yaws, centre, delay = asyncio.run(_closed_loop(face, 5.0, frame_delay_s=0.3))
    assert delay == pytest.approx(0.3, abs=0.1)
    _assert_converged(yaws, centre, 0.15)


def _jumping_face_while_turning() -> float:
    """The delay estimate after 2 s of a face jumping from side to side while the head
    turns 30 deg over the window — a scene no delay explains."""
    now = time.monotonic()
    times = now - 3.0 + np.arange(0.0, 3.001, 0.02)
    poses = np.stack([_turned(yaw) for yaw in np.linspace(0.0, 30.0, len(times))])
    tracker = HeadTracker(
        CameraModel.for_sim(SimCameraSettings()),
        history=lambda: (times, poses),
        set_gaze=lambda aim, *, focus=False: None,
    )
    for k in range(20):
        tracker.observe(_report(0.5 if k % 2 else -0.5, 0.0, ts=now - 2.0 + 0.1 * k))
    return tracker.delay_s


def test_a_face_moving_while_the_head_turns_leaves_the_estimate_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fit is taken only when distinct (specs/motion/head_tracking.md "The delay estimate"):
    a face that jumps about while the head turns fits no delay — every delay spreads the
    directions alike — so the estimate holds. Without the contrast rule (every fit taken)
    the same scene moves it: a flat score's minimum is noise."""
    assert _jumping_face_while_turning() == head_tracking.DELAY_PRIOR_S
    monkeypatch.setattr(head_tracking, "DELAY_MAX_CONTRAST", 1.0)
    assert _jumping_face_while_turning() != head_tracking.DELAY_PRIOR_S


def _turned(yaw_deg: float) -> npt.NDArray[np.float64]:
    pose = np.eye(4)
    pose[:3, :3] = Rotation.from_euler("z", yaw_deg, degrees=True).as_matrix()
    return pose


def test_the_estimate_holds_while_the_head_is_still() -> None:
    """A still head says nothing about the delay: every delay fits alike."""
    tracker = HeadTracker(
        CameraModel.for_sim(SimCameraSettings()),
        history=lambda: (np.array([time.monotonic()]), np.eye(4)[np.newaxis]),
        set_gaze=lambda aim, *, focus=False: None,
    )
    for k in range(20):
        tracker.observe(_report(0.1 * math.sin(k), 0.0, ts=time.monotonic()))
        time.sleep(0.01)
    assert tracker.delay_s == head_tracking.DELAY_PRIOR_S


def test_a_report_carrying_its_frame_pose_is_aimed_against_it() -> None:
    """A source that knows the pose its frame was captured from (a custom detector)
    needs no estimate: the face straight ahead of a head turned 20 deg is at 20 deg."""
    sent: list[Any] = []
    tracker = HeadTracker(
        CameraModel.for_sim(SimCameraSettings()),
        history=lambda: (np.array([time.monotonic()]), np.eye(4)[np.newaxis]),
        set_gaze=lambda aim, *, focus=False: sent.append(aim),
    )
    turned = Rotation.from_euler("z", 20.0, degrees=True).as_matrix()
    pose = np.eye(4)
    pose[:3, :3] = turned
    report = replace(_report(0.0, 0.0, ts=time.monotonic()), head_pose=pose)
    tracker.observe(report)
    assert _yaw_deg(sent[-1]) == pytest.approx(20.0, abs=0.5)


def test_frame_head_pose_is_the_pose_the_frame_was_taken_from() -> None:
    """The one rule the aim and the sim's face markers share: the neutral pose for a
    fixed camera, a stamped report's own pose, else the reported pose at the report's
    time minus the delay — a read that leaves the delay estimate alone."""
    times = np.array([10.0, 10.1, 10.2, 10.3])
    poses = np.stack([_turned(yaw) for yaw in (0.0, 10.0, 20.0, 30.0)])
    history = lambda: (times, poses)
    head_mounted = CameraModel.for_sim(SimCameraSettings())
    unstamped = _report(0.0, 0.0, ts=10.3)

    def yaw(report: FaceReport, camera: CameraModel, delay: float, now: float) -> float:
        return _yaw_deg(frame_head_pose(report, camera, history, delay, now))

    # Unstamped: the history at the report's time minus the delay.
    assert yaw(unstamped, head_mounted, 0.0, 99.0) == pytest.approx(30.0)
    assert yaw(unstamped, head_mounted, 0.2, 99.0) == pytest.approx(10.0)
    # A report without a time is placed at `now`.
    timeless = _report(0.0, 0.0, ts=0.0)
    assert yaw(timeless, head_mounted, 0.1, 10.2) == pytest.approx(10.0)
    # Stamped: its own pose, whatever the history and the delay say.
    stamped = replace(unstamped, head_pose=_turned(-15.0))
    assert yaw(stamped, head_mounted, 0.2, 99.0) == pytest.approx(-15.0)
    # A fixed camera: the neutral pose, even for a stamped report.
    fixed = CameraModel.for_sim(SimCameraSettings(source="webcam"))
    assert yaw(stamped, fixed, 0.2, 99.0) == pytest.approx(0.0)
    assert yaw(unstamped, fixed, 0.0, 99.0) == pytest.approx(0.0)

    # Reading it for a marker never feeds the tracker's estimate.
    tracker = HeadTracker(
        head_mounted, history=history, set_gaze=lambda aim, *, focus=False: None
    )
    for _ in range(20):
        frame_head_pose(unstamped, head_mounted, history, tracker.delay_s, 99.0)
    assert tracker.delay_s == head_tracking.DELAY_PRIOR_S
    assert not tracker._detections


async def _constant_observation(camera: CameraModel, seconds: float) -> list[float]:
    """A person standing still in front of a camera: the same pixel every poll."""
    robot = FakeReachyMini()
    async with _running(MotionSession(robot, presence=True, idle="hold")) as session:
        tracker = HeadTracker(
            camera,
            history=session.head_pose_history,
            set_gaze=session.set_gaze,
        )
        session.resume()
        yaws: list[float] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            tracker.observe(_report(0.3, 0.0, ts=time.monotonic()))
            yaws.append(_yaw_deg(robot.last_target[0]))
            await asyncio.sleep(1.0 / POLL_HZ)
        return yaws


def test_a_fixed_camera_settles_where_the_pixel_points_from_rest() -> None:
    webcam = CameraModel.for_sim(SimCameraSettings(source="webcam", hfov_deg=70.0))
    assert webcam.fixed
    # x = 0.3 of a 70 deg pinhole is atan(0.3 tan 35 deg) to the right — negative yaw
    expected = -math.degrees(math.atan(0.3 * math.tan(math.radians(35.0))))

    async def run() -> tuple[list[float], list[float]]:
        return await asyncio.gather(
            _constant_observation(webcam, 3.0),
            _constant_observation(replace(webcam, fixed=False), 3.0),
        )

    fixed, mounted = asyncio.run(run())
    held = fixed[-int(1.5 * POLL_HZ) :]  # the last 1.5 s
    assert all(y == pytest.approx(expected, abs=1.0) for y in held)
    assert max(held) - min(held) < 0.5  # no drift
    # head-mounted geometry adds the same offset again at every observation: the bug a
    # fixed camera guards against
    assert max(abs(y) for y in mounted) > 30.0


def test_the_aim_is_withdrawn_once_nobody_is_seen_and_returns_with_a_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(head_tracking, "TRACKING_LOST_S", 0.2)
    sent: list[Any] = []
    tracker = HeadTracker(
        CameraModel.for_sim(SimCameraSettings()),
        history=lambda: (np.array([time.monotonic()]), np.eye(4)[np.newaxis]),
        set_gaze=lambda aim, *, focus=False: sent.append(aim),
    )
    tracker.observe(_report(0.0, 0.0, ts=time.monotonic()))
    assert tracker.engaged and len(sent) == 1 and sent[0] is not None
    tracker.observe(_nobody())  # a missed frame is not a loss
    assert tracker.engaged and len(sent) == 1
    time.sleep(0.25)
    for _ in range(3):
        tracker.observe(_nobody())
    assert not tracker.engaged
    assert len(sent) == 2 and sent[1] is None  # withdrawn once
    tracker.observe(_report(0.2, 0.0, ts=time.monotonic()))
    assert tracker.engaged and sent[-1] is not None


def test_focus_goes_with_every_aim_and_stop_withdraws_it() -> None:
    sent: list[tuple[Any, bool]] = []
    tracker = HeadTracker(
        CameraModel.for_sim(SimCameraSettings()),
        history=lambda: (np.array([time.monotonic()]), np.eye(4)[np.newaxis]),
        set_gaze=lambda aim, *, focus=False: sent.append((aim, focus)),
    )
    tracker.observe(_report(0.0, 0.0, ts=time.monotonic()))
    tracker.focus = True
    tracker.observe(_report(0.0, 0.0, ts=time.monotonic()))
    tracker.stop()
    assert [f for _, f in sent] == [False, True, True]
    assert sent[-1][0] is None and not tracker.engaged


# --- whom the head follows (specs/motion/head_tracking.md "Whom the head follows") -------
#
# Driven directly with scripted reports on a clock the test moves: a fixed (webcam)
# camera, so an aim needs no pose history and one face always gives the same aim.


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _Published:
    """What the tracker publishes on (``value`` / ``set`` / ``update``), recorded; the
    bridge's ``Observable`` must be driven from the event loop, these tests are not."""

    def __init__(self) -> None:
        self.value = HeadTrackingReport.inactive()
        self.sets: list[HeadTrackingReport] = []

    def set(self, value: HeadTrackingReport) -> None:
        self.value = value
        self.sets.append(value)

    def update(self, value: HeadTrackingReport) -> None:
        self.value = value


class _Chooser:
    """A running tracker on a test clock, recording its aims and published reports."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = _Clock()
        monkeypatch.setattr(
            head_tracking, "time", SimpleNamespace(monotonic=self.clock.monotonic)
        )
        self.aims: list[Any] = []
        self.report = _Published()
        self.published = self.report.sets
        self.tracker = HeadTracker(
            CameraModel.for_sim(SimCameraSettings(source="webcam")),
            history=lambda: (np.zeros(0), np.zeros((0, 4, 4))),
            set_gaze=lambda aim, *, focus=False: self.aims.append(aim),
            report=self.report,  # type: ignore[arg-type]
        )
        self.tracker.start()

    def see(self, *faces: Face, after: float = 0.1) -> None:
        self.clock.now += after
        self.tracker.observe(
            FaceReport(
                faces=tuple(faces), ts=self.clock.now, source="custom", active=True
            )
        )

    def aim_at(self, face: Face) -> Any:
        """The aim a tracker gives this face alone."""
        aims: list[Any] = []
        HeadTracker(
            self.tracker.camera,
            history=lambda: (np.zeros(0), np.zeros((0, 4, 4))),
            set_gaze=lambda aim, *, focus=False: aims.append(aim),
        ).observe(
            # the aim depends on the position alone; sized so a fresh tracker acquires it
            FaceReport(
                faces=(replace(face, size=1.0),), ts=0.0, source="custom", active=True
            )
        )
        return aims[0]

    def aiming_at(self, face: Face) -> bool:
        return self.aims[-1] is not None and np.allclose(
            self.aims[-1], self.aim_at(face)
        )


def _person(track_id: int, x: float, size: float) -> Face:
    return Face(x=x, y=0.0, roll=None, size=size, track_id=track_id)


NEAR = 0.30  # sizes: a face at desk range...
FAR = 0.12  # ...one further back, still above TRACKING_MIN_SIZE (0.07)
SPECK = 0.05  # below it


def test_the_biggest_eligible_face_is_followed_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chooser = _Chooser(monkeypatch)
    far, near = _person(1, -0.4, FAR), _person(2, 0.4, NEAR)
    chooser.see(far, near)
    assert chooser.tracker.following == 2 and chooser.aiming_at(near)
    assert chooser.report.value.track_id == 2
    assert chooser.report.value.attention == "engaged"


def test_a_bigger_face_appearing_does_not_take_the_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chooser = _Chooser(monkeypatch)
    far = _person(1, -0.4, FAR)
    chooser.see(far)
    for _ in range(30):  # three seconds with a nearer face beside the followed one
        chooser.see(far, _person(2, 0.4, NEAR))
    assert chooser.tracker.following == 1 and chooser.aiming_at(far)
    assert [r.track_id for r in chooser.published] == [None, 1]  # started, engaged


def test_a_short_absence_holds_the_aim_and_the_face_is_followed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(head_tracking, "TRACKING_SWITCH_S", 1.0)
    chooser = _Chooser(monkeypatch)
    followed, other = _person(1, -0.4, NEAR), _person(2, 0.4, NEAR)
    chooser.see(followed, other)
    aims_before, published_before = len(chooser.aims), len(chooser.published)
    for _ in range(5):  # half a second without the followed face
        chooser.see(other)
    assert len(chooser.aims) == aims_before  # no new aim: the last one stands
    chooser.see(followed, other)
    assert chooser.tracker.following == 1 and chooser.aiming_at(followed)
    assert len(chooser.published) == published_before  # nothing to wake anyone for


def test_after_the_switch_time_the_biggest_other_face_is_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(head_tracking, "TRACKING_SWITCH_S", 1.0)
    chooser = _Chooser(monkeypatch)
    followed = _person(1, 0.0, NEAR)
    small, big = _person(2, -0.5, FAR), _person(3, 0.5, 0.2)
    chooser.see(followed, small, big)
    chooser.see(small, big)  # the followed face goes missing
    chooser.see(small, big, after=0.5)
    assert chooser.tracker.following == 1  # still holding at 0.5 s
    chooser.see(small, big, after=0.5)  # 1.0 s missing: switch
    assert chooser.tracker.following == 3 and chooser.aiming_at(big)
    assert [r.track_id for r in chooser.published][-1] == 3
    assert chooser.published[-1].attention == "engaged"


def test_only_specks_in_view_hold_until_the_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(head_tracking, "TRACKING_SWITCH_S", 1.0)
    monkeypatch.setattr(head_tracking, "TRACKING_LOST_S", 2.0)
    chooser = _Chooser(monkeypatch)
    followed, speck = _person(1, 0.0, NEAR), _person(2, 0.5, SPECK)
    chooser.see(followed)
    chooser.see(speck)
    chooser.see(speck, after=1.5)  # past the switch time, nobody large enough
    assert chooser.tracker.following == 1 and chooser.aims[-1] is not None
    chooser.see(speck, after=0.5)  # 2.0 s: the loss
    assert chooser.tracker.following is None and chooser.aims[-1] is None
    assert chooser.report.value == HeadTrackingReport(
        active=True,
        focus=False,
        attention="watching",
        track_id=None,
        ts=chooser.report.value.ts,
    )


def test_an_eligible_face_between_the_switch_and_the_loss_is_followed_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(head_tracking, "TRACKING_SWITCH_S", 1.0)
    chooser = _Chooser(monkeypatch)
    chooser.see(_person(1, 0.0, NEAR))
    chooser.see()
    chooser.see(after=1.2)  # nobody at all, past the switch time
    newcomer = _person(4, 0.3, FAR)
    chooser.see(newcomer, after=0.3)
    assert chooser.tracker.following == 4 and chooser.aiming_at(newcomer)


def test_a_followed_face_moving_away_stays_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chooser = _Chooser(monkeypatch)
    chooser.see(_person(1, 0.0, NEAR))
    receding = _person(1, 0.1, SPECK)  # now smaller than the acquisition gate
    for _ in range(20):
        chooser.see(receding, _person(2, 0.5, NEAR))
    assert chooser.tracker.following == 1 and chooser.aiming_at(receding)


def test_the_report_wakes_on_state_changes_and_updates_on_aims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chooser = _Chooser(monkeypatch)
    face = _person(1, 0.0, NEAR)
    chooser.see(face)
    ts_first = chooser.report.value.ts
    for i in range(5):  # the face moves: fresh ts, nothing published
        chooser.see(_person(1, 0.05 * i, NEAR))
    assert chooser.report.value.ts > ts_first
    chooser.tracker.start(focus=True)
    chooser.tracker.stop()
    assert [
        (r.active, r.focus, r.attention, r.track_id) for r in chooser.published
    ] == [
        (True, False, "watching", None),  # started
        (True, False, "engaged", 1),  # engaged on the face
        (True, True, "engaged", 1),  # focus switched
        (False, False, None, None),  # stopped
    ]
    assert chooser.report.value == HeadTrackingReport.inactive()


def test_the_robot_model_is_the_clients_calibration_at_the_streamed_frame() -> None:
    model = CameraModel.for_robot(FakeReachyMini())
    specs = FakeReachyMini().media.camera.camera_specs
    assert model.size == specs.default_resolution.value[:2]
    np.testing.assert_allclose(model.K, specs.K)
    assert not model.fixed


def test_the_sim_eye_camera_pinhole() -> None:
    # MuJoCo's fovy is vertical: 80 deg at 16:9 is ~112 deg horizontally, and both give
    # the same focal length in pixels (square pixels).
    hfov = sim_hfov_deg(80.0, 1280, 720)
    assert hfov == pytest.approx(112.3, abs=0.1)
    K = pinhole_intrinsics(hfov, (320, 180))
    f_vertical = 90 / math.tan(math.radians(40.0))
    np.testing.assert_allclose(
        K, [[f_vertical, 0, 160], [0, f_vertical, 90], [0, 0, 1]], rtol=1e-9
    )
    model = CameraModel.for_sim(SimCameraSettings(source="sim"))
    assert model.K[0, 0] == pytest.approx(429.0, abs=0.1) and not model.fixed


def test_the_sim_eye_camera_fovy_matches_the_scene() -> None:
    mujoco = pytest.importorskip("mujoco", reason="sim extra (mujoco) not installed")
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend

    backend = MujocoBackend(scene="empty", headless=True, use_audio=False)
    cam = mujoco.mj_name2id(backend.model, mujoco.mjtObj.mjOBJ_CAMERA, "eye_camera")
    assert float(backend.model.cam_fovy[cam]) == pytest.approx(SIM_EYE_CAMERA_FOVY_DEG)
