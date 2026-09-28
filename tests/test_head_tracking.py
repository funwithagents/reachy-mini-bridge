"""Functional tests for the bridge's head tracker (specs/head_tracking.md).

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
from dataclasses import replace
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
    pinhole_intrinsics,
    sim_hfov_deg,
)
from reachy_mini_bridge.motion import MotionSession
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS

if TYPE_CHECKING:
    import numpy.typing as npt

DETECT_HZ = 10.0  # upstream's detector sees the camera feed at 10 fps
POLL_HZ = 30.0  # the detection loop's poll rate


def _yaw_deg(head: npt.NDArray[np.float64]) -> float:
    return float(Rotation.from_matrix(head[:3, :3]).as_euler("ZYX", degrees=True)[0])


def _report(x: float, y: float, ts: float) -> FaceReport:
    return FaceReport(
        faces=(Face(x=x, y=y, roll=None, size=None),),
        ts=ts,
        source="daemon",
        active=True,
    )


def _nobody() -> FaceReport:
    return FaceReport(faces=(), ts=time.monotonic(), source="daemon", active=True)


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
    same_host: bool = True,
) -> tuple[list[float], tuple[float, float], float]:
    """Track ``face`` for ``seconds``: every detection projects it from the head pose
    of ``frame_delay_s`` ago — when its frame was taken — and is stamped *now*, as the
    daemon stamps a detection when it finishes, so the tracker is never told the delay.
    The commanded yaw at every poll, where the face projects at the end, and the
    tracker's delay estimate."""
    robot = FakeReachyMini()
    camera = CameraModel.for_sim(SimCameraSettings(source="sim"))
    async with MotionSession(robot, presence=True, idle="hold") as session:
        tracker = HeadTracker(
            camera,
            history=session.head_pose_history,
            set_gaze=session.set_gaze,
            same_host=same_host,
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
    """The frame is 0.3 s older than its timestamp says — the viewer sim's figure — on
    the same host and on another (where only the receipt time is known): the tracker
    learns the delay from its first turn and the head settles on the face."""
    face = _face_at(0.15)

    async def run() -> list[tuple[list[float], tuple[float, float], float]]:
        return list(
            await asyncio.gather(
                _closed_loop(face, 5.0, frame_delay_s=0.3),
                _closed_loop(face, 5.0, frame_delay_s=0.3, same_host=False),
            )
        )

    for yaws, centre, delay in asyncio.run(run()):
        assert delay == pytest.approx(0.3, abs=0.1)
        _assert_converged(yaws, centre, 0.15)


def test_the_estimate_holds_while_the_head_is_still() -> None:
    """A still head says nothing about the delay: every delay fits alike."""
    tracker = HeadTracker(
        CameraModel.for_sim(SimCameraSettings()),
        history=lambda: (np.array([time.monotonic()]), np.eye(4)[np.newaxis]),
        set_gaze=lambda aim, *, focus=False: None,
        same_host=True,
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
        same_host=True,
    )
    turned = Rotation.from_euler("z", 20.0, degrees=True).as_matrix()
    pose = np.eye(4)
    pose[:3, :3] = turned
    report = replace(_report(0.0, 0.0, ts=time.monotonic()), head_pose=pose)
    tracker.observe(report)
    assert _yaw_deg(sent[-1]) == pytest.approx(20.0, abs=0.5)


async def _constant_observation(camera: CameraModel, seconds: float) -> list[float]:
    """A person standing still in front of a camera: the same pixel every poll."""
    robot = FakeReachyMini()
    async with MotionSession(robot, presence=True, idle="hold") as session:
        tracker = HeadTracker(
            camera,
            history=session.head_pose_history,
            set_gaze=session.set_gaze,
            same_host=True,
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
        same_host=True,
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
        same_host=True,
    )
    tracker.observe(_report(0.0, 0.0, ts=time.monotonic()))
    tracker.focus = True
    tracker.observe(_report(0.0, 0.0, ts=time.monotonic()))
    tracker.stop()
    assert [f for _, f in sent] == [False, True, True]
    assert sent[-1][0] is None and not tracker.engaged


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
