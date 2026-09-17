"""Fast-tier tests for `reachy_mini_bridge.sim_daemon` (specs/sim_daemon.md).

Daemon-free, camera-free and offline. The tracking corrections are exercised in closed
loop on the real upstream `MujocoBackend` (physics, IK/FK, `step_head_tracking`), with the
face tracker replaced by a stand-in whose observations are the true pixel of the test
scene's face, projected through the MuJoCo eye camera — no render, no detector, no
network. Tests that need `mujoco` skip where the sim extra is absent.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from reachy_mini_bridge import sim_daemon
from reachy_mini_bridge.sim_daemon import (
    SimDaemonExtension,
    WebcamRelay,
    corrected_backend,
    pinhole_intrinsics,
    relay_pipeline_description,
    sim_hfov_deg,
    webcam_source,
)

FRAME = (320, 180)  # the face tracker's downscaled frame


@pytest.fixture(autouse=True)
def restore_upstream_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The launcher patches process globals (in the daemon, that is the point); undo
    them after each test."""
    monkeypatch.setattr(sim_daemon._TrackerCamera, "hfov_deg", None)
    try:
        from reachy_mini.daemon import daemon as upstream_daemon
        from reachy_mini.daemon.app import main as upstream_main
        from reachy_mini.vision import face_tracking
    except ImportError:
        yield
        return
    monkeypatch.setattr(upstream_daemon, "MujocoBackend", upstream_daemon.MujocoBackend)
    monkeypatch.setattr(upstream_main, "create_app", upstream_main.create_app)
    monkeypatch.setattr(
        face_tracking, "intrinsics_for_size", face_tracking.intrinsics_for_size
    )
    yield


# --- intrinsics -------------------------------------------------------------------------


def test_pinhole_intrinsics_and_the_sim_eye_camera_field_of_view() -> None:
    # MuJoCo's fovy is vertical: 80° at 16:9 is ~112° horizontally, and both give the
    # same focal length in pixels (square pixels).
    hfov = sim_hfov_deg(80.0, 1280, 720)
    assert hfov == pytest.approx(112.3, abs=0.1)
    K = pinhole_intrinsics(hfov, FRAME)
    f_vertical = (FRAME[1] / 2) / math.tan(math.radians(40.0))
    np.testing.assert_allclose(
        K, [[f_vertical, 0, 160], [0, f_vertical, 90], [0, 0, 1]], rtol=1e-9
    )
    assert pinhole_intrinsics(hfov, (1280, 720))[0, 0] == pytest.approx(429.0, abs=0.1)


def test_the_tracker_gets_the_eye_camera_intrinsics_and_nothing_else_changes(
    scene_name: str,
) -> None:
    """Installed, the face tracker's `intrinsics_for_size` is the pinhole of the scene's
    eye camera at any frame size; upstream's function is left alone for everyone else
    (and is the mis-scaled matrix this corrects)."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend
    from reachy_mini.media import camera_utils
    from reachy_mini.media.camera_constants import MujocoCameraSpecs
    from reachy_mini.vision import face_tracking

    sim_daemon.install_tracker_intrinsics()
    corrected_backend(MujocoBackend)(scene=scene_name, headless=True, use_audio=False)
    K = MujocoCameraSpecs.K
    hfov = sim_hfov_deg(80.0, 1280, 720)
    for size in (FRAME, (1280, 720)):
        np.testing.assert_allclose(
            face_tracking.intrinsics_for_size(K, 1.0, size),
            pinhole_intrinsics(hfov, size),
        )
    upstream = camera_utils.intrinsics_for_size(K, 1.0, FRAME)
    assert upstream[0, 2] == pytest.approx(53.33, abs=0.01)  # the bug: cx far off 160


# --- closed-loop tracking in the sim ------------------------------------------------------


@pytest.fixture
def mujoco() -> Any:
    return pytest.importorskip("mujoco", reason="sim extra (mujoco) not installed")


@pytest.fixture
def scene_name(tmp_path: Path, mujoco: Any) -> str:
    from reachy_mini_bridge.testing.sim_scene import (
        FacePlane,
        upstream_scene_name,
        write_test_scene,
    )

    path = write_test_scene(tmp_path, faces=(FacePlane(visible=True),))
    return upstream_scene_name(path)


class _Loop:
    """The MuJoCo daemon's control tick, driven by hand: 10 physics steps, the kinematics
    update (which the corrected backend follows with a tracking step), then IK."""

    def __init__(self, mujoco: Any, backend: Any, observe: Callable[[], Any]) -> None:
        self.mujoco = mujoco
        self.b = backend
        m, d = backend.model, backend.data
        # settle at neutral with collisions on, as upstream's run() does before its loop
        for i in backend.col_inds:
            m.geom_contype[i] = 1
            m.geom_conaffinity[i] = 1
        joints = backend.head_kinematics.ik(np.eye(4), no_iterations=20)
        d.qpos[backend.joint_qpos_addr[:7]] = np.asarray(joints).reshape(-1, 1)
        d.ctrl[:7] = joints
        mujoco.mj_forward(m, d)
        for _ in range(300):
            mujoco.mj_step(m, d)
        backend.head_kinematics.fk(
            backend.get_present_head_joint_positions(), no_iterations=20
        )
        backend.target_head_pose = np.eye(4)
        backend.target_body_yaw = 0.0
        backend.ik_required = True
        backend._tracking_enabled = True
        backend._tracking_requested_weight = 1.0
        ticks = iter(range(10**9))
        # a detector delivers an observation every other 50 Hz tick (25 fps)
        backend._tracker = SimpleNamespace(
            latest=lambda: observe() if next(ticks) % 2 == 0 else None
        )
        self.cam = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_CAMERA, "eye_camera")
        self.face = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "face")

    def run(self, seconds: float) -> list[float]:
        """Tick for ``seconds`` of sim time; the head yaw (degrees) after each tick."""
        b, d = self.b, self.b.data
        yaws: list[float] = []
        for _ in range(int(seconds * 50)):
            for _ in range(10):
                self.mujoco.mj_step(b.model, d)
            b.update_head_kinematics_model(
                b.get_present_head_joint_positions(),
                b.get_present_antenna_joint_positions(),
            )
            b.current_head_pose = b.get_mj_present_head_pose()
            if b.ik_required:
                try:
                    b.update_target_head_joints_from_ik(
                        b.target_head_pose, b.target_body_yaw
                    )
                except ValueError:
                    pass  # upstream logs and keeps the last targets
            if b.target_head_joint_positions is not None:
                d.ctrl[:7] = b.target_head_joint_positions
            pose = b.get_mj_present_head_pose()
            yaws.append(math.degrees(math.atan2(pose[1, 0], pose[0, 0])))
        return yaws

    def face_pixel(self) -> tuple[float, float]:
        """The face's centre as the rendered eye camera sees it, normalised to [-1, 1]
        the way upstream's tracker reports it (MuJoCo cameras look along -z, y up)."""
        d = self.b.data
        R = d.cam_xmat[self.cam].reshape(3, 3)
        p = R.T @ (d.xpos[self.face] - d.cam_xpos[self.cam])
        w, h = FRAME
        f = (h / 2) / math.tan(math.radians(40.0))
        u = w / 2 + f * p[0] / -p[2]
        v = h / 2 - f * p[1] / -p[2]
        return (u / (w - 1) * 2 - 1, v / (h - 1) * 2 - 1)

    def error_to_face_deg(self) -> float:
        d = self.b.data
        axis = -d.cam_xmat[self.cam].reshape(3, 3)[:, 2]
        to_face = d.xpos[self.face] - d.cam_xpos[self.cam]
        to_face /= np.linalg.norm(to_face)
        return math.degrees(math.acos(float(np.clip(axis @ to_face, -1.0, 1.0))))


def _observation(center: tuple[float, float], K: np.ndarray) -> Any:
    return SimpleNamespace(
        center=center,
        roll=0.0,
        width=FRAME[0],
        height=FRAME[1],
        camera_matrix=K,
        distortion=np.zeros(5),
        timestamp=time.monotonic(),
    )


def _place_face(backend: Any, mujoco: Any, pos: tuple[float, float, float]) -> None:
    backend.data.mocap_pos[0] = pos
    mujoco.mj_forward(backend.model, backend.data)


@pytest.mark.parametrize("lateral", [0.0, 0.15, -0.15])
def test_sim_tracking_converges_on_the_face(
    mujoco: Any, scene_name: str, lateral: float
) -> None:
    """Correction 2: with the eye camera's true intrinsics the head turns until the camera
    looks straight at the face — ahead, and 0.15 m to either side (~20° of yaw)."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend
    from reachy_mini.media.camera_constants import MujocoCameraSpecs
    from reachy_mini.vision import face_tracking

    sim_daemon.install_tracker_intrinsics()
    backend = corrected_backend(MujocoBackend)(
        scene=scene_name, headless=True, use_audio=False
    )
    _place_face(backend, mujoco, (0.45, lateral, 0.20))
    K = face_tracking.intrinsics_for_size(MujocoCameraSpecs.K, 1.0, FRAME)
    holder: dict[str, _Loop] = {}
    loop = _Loop(mujoco, backend, lambda: _observation(holder["loop"].face_pixel(), K))
    holder["loop"] = loop
    yaws = loop.run(4.0)

    assert loop.error_to_face_deg() < 3.0
    # the eye camera is on the head's forward axis: it looks at the face when the
    # head's heading from its pivot (the world origin) does
    expected_yaw = math.degrees(math.atan2(lateral, 0.45))
    assert yaws[-1] == pytest.approx(expected_yaw, abs=1.5)
    assert max(yaws[-50:]) - min(yaws[-50:]) < 1.0, "the head did not settle"


def test_sim_tracking_with_upstream_intrinsics_ends_far_off_the_face(
    mujoco: Any, scene_name: str
) -> None:
    """The bug correction 2 fixes, reproduced by the same loop: upstream's mis-scaled
    matrix settles the head tens of degrees away from a face dead ahead."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend
    from reachy_mini.media import camera_utils
    from reachy_mini.media.camera_constants import MujocoCameraSpecs

    backend = corrected_backend(MujocoBackend)(
        scene=scene_name, headless=True, use_audio=False
    )
    K = camera_utils.intrinsics_for_size(MujocoCameraSpecs.K, 1.0, FRAME)
    holder: dict[str, _Loop] = {}
    loop = _Loop(mujoco, backend, lambda: _observation(holder["loop"].face_pixel(), K))
    holder["loop"] = loop
    loop.run(4.0)
    assert loop.error_to_face_deg() > 30.0


def test_a_fixed_webcam_neither_drifts_nor_runs_away(
    mujoco: Any, scene_name: str
) -> None:
    """Correction 3: a webcam does not turn with the head, so a still person stays at the
    same pixel. Aimed from the rest pose, the head settles at the angle that pixel implies
    and holds; aimed through the present head pose (head-mounted geometry), each
    observation adds the same offset again and the head runs past it."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend
    from reachy_mini.vision.look_at import look_at_image_pose

    center = (0.5, 0.0)
    K = pinhole_intrinsics(70.0, FRAME)
    u = (center[0] + 1) / 2 * (FRAME[0] - 1)
    v = (center[1] + 1) / 2 * (FRAME[1] - 1)
    aim = look_at_image_pose(u, v, K, np.zeros(5), np.eye(4))
    expected_yaw = math.degrees(math.atan2(aim[1, 0], aim[0, 0]))
    assert abs(expected_yaw) > 10.0

    webcam = sim_daemon._Camera(source="webcam", hfov_deg=70.0)
    fixed = corrected_backend(MujocoBackend, camera=webcam)(
        scene=scene_name, headless=True, use_audio=False
    )
    yaws = _Loop(mujoco, fixed, lambda: _observation(center, K)).run(4.0)
    assert yaws[-1] == pytest.approx(expected_yaw, abs=2.0)
    assert max(yaws[-50:]) - min(yaws[-50:]) < 1.0, "the head drifts"

    mounted = corrected_backend(MujocoBackend)(
        scene=scene_name, headless=True, use_audio=False
    )
    yaws = _Loop(mujoco, mounted, lambda: _observation(center, K)).run(4.0)
    assert abs(yaws[-1]) > abs(expected_yaw) + 15.0


def test_a_control_tick_steps_head_tracking(mujoco: Any, scene_name: str) -> None:
    """Correction 1: upstream's MuJoCo loop never steps daemon-side tracking; the corrected
    backend steps it right after the kinematics update of each control tick. Observable:
    the tracker's observation lands in `get_tracked_face()` after one such update."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend

    backend = corrected_backend(MujocoBackend)(
        scene=scene_name, headless=True, use_audio=False
    )
    observations = [_observation((0.0, 0.0), pinhole_intrinsics(112.3, FRAME))]
    backend._tracking_enabled = True
    backend._tracking_requested_weight = 1.0
    backend._tracker = SimpleNamespace(
        latest=lambda: observations.pop() if observations else None
    )
    assert not backend.get_tracked_face().detected
    backend.update_head_kinematics_model(np.zeros(7), np.zeros(2))
    face = backend.get_tracked_face()
    assert face.detected and face.x == 0.0 and face.y == 0.0
    assert backend._tracking_target_pose is not None, "no aim latched from the face"


def test_on_backend_runs_once_the_model_exists(mujoco: Any, scene_name: str) -> None:
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend

    seen: list[int] = []
    extension = SimDaemonExtension(on_backend=lambda b: seen.append(int(b.model.nbody)))
    corrected_backend(MujocoBackend, extensions=[extension])(
        scene=scene_name, headless=True, use_audio=False
    )
    assert len(seen) == 1 and seen[0] > 0


# --- webcam mode wiring (no MuJoCo) --------------------------------------------------------


class _StubBackend:
    """Stands in for upstream's backend where a test needs no physics."""

    INIT_HEAD_POSE = np.eye(4)

    def __init__(self) -> None:
        self.ran = False
        self.pose = np.diag([1.0, 1.0, 1.0, 1.0])
        self.pose[0, 3] = 0.5
        self.seen_in_aim: Any = None

    def run(self) -> None:
        self.ran = True

    def rendering_loop(self, *args: Any) -> None:
        raise AssertionError("the eye camera must not be rendered in webcam mode")

    def get_current_head_pose(self) -> Any:
        return self.pose

    def set_tracking_face(self, *args: Any) -> None:
        self.seen_in_aim = self.get_current_head_pose()

    def update_head_kinematics_model(self, *args: Any) -> None:
        pass

    def step_head_tracking(self) -> None:
        pass


def test_webcam_mode_relays_instead_of_rendering_and_aims_from_rest() -> None:
    events: list[str] = []

    class _Relay:
        def __init__(self, device: Any) -> None:
            events.append(f"relay {device!r}")

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    webcam = sim_daemon._Camera(source="webcam", device=2, hfov_deg=65.0)
    backend = corrected_backend(_StubBackend, camera=webcam, relay_factory=_Relay)()
    assert sim_daemon._TrackerCamera.hfov_deg == 65.0
    assert backend.rendering_loop("eye_camera", 5005) is None
    backend.run()
    assert backend.ran and events == ["relay 2", "start", "stop"]
    backend.set_tracking_face((0.0, 0.0), 0.0, 320, 180, np.eye(3), np.zeros(5), 0.0)
    np.testing.assert_array_equal(backend.seen_in_aim, np.eye(4))
    assert backend.get_current_head_pose()[0, 3] == 0.5  # every other reader: real pose


def test_webcam_source_per_platform() -> None:
    assert webcam_source(None, "Darwin") == "autovideosrc"
    assert webcam_source(1, "Darwin") == "avfvideosrc device-index=1"
    assert webcam_source(0, "Linux") == "v4l2src device=/dev/video0"
    assert webcam_source("/dev/video3", "Linux") == "v4l2src device=/dev/video3"
    with pytest.raises(ValueError, match="index"):
        webcam_source("FaceTime HD Camera", "Darwin")
    with pytest.raises(ValueError, match="Windows"):
        webcam_source(0, "Windows")


def test_the_relay_sends_what_the_sim_media_server_reads() -> None:
    """The caps upstream's render thread sends (GStreamerUDPCamera) and the MuJoCo media
    server's UDP source expects: RGB 1280x720, RTP raw video, payload 96, port 5005."""
    description = relay_pipeline_description("autovideosrc")
    assert description.startswith("autovideosrc ! ")
    for part in (
        "format=RGB,width=1280,height=720",
        "rtpvrawpay",
        "payload=96",
        "port=5005",
    ):
        assert part in description


class _FakePipeline:
    def __init__(self, on_frame: Callable[[], None], frames: bool) -> None:
        self._on_frame = on_frame
        self._frames = frames
        self.stopped = False

    def start(self) -> bool:
        return True

    def error(self) -> str | None:
        if self._frames:
            self._on_frame()
        return None

    def stop(self) -> None:
        self.stopped = True


def _wait(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_relay_logs_a_silent_camera_once_retries_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No frame (a refused camera permission looks like this) is one ERROR naming the
    macOS permission, then quiet retries; frames arriving again are logged once."""
    pipelines: list[_FakePipeline] = []
    frames = threading.Event()

    def open_pipeline(description: str, on_frame: Callable[[], None]) -> _FakePipeline:
        pipeline = _FakePipeline(on_frame, frames.is_set())
        pipelines.append(pipeline)
        return pipeline

    relay = WebcamRelay(
        None,
        frame_timeout=0.3,
        retry=0.05,
        open_pipeline=open_pipeline,
        system="Darwin",
    )
    with caplog.at_level(logging.INFO, logger="reachy_mini_bridge.sim_daemon"):
        relay.start()
        try:
            assert _wait(lambda: len(pipelines) >= 3)
            errors = [r for r in caplog.records if r.levelno == logging.ERROR]
            assert len(errors) == 1
            assert "no frame" in errors[0].getMessage()
            assert "Camera access" in errors[0].getMessage()
            frames.set()
            assert _wait(
                lambda: any("flowing again" in r.getMessage() for r in caplog.records)
            )
        finally:
            relay.stop()
    assert all(p.stopped for p in pipelines)


# --- the launcher -----------------------------------------------------------------------


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> list[str]:
    """Run the launcher with upstream's `main()` recording the argv it would parse."""
    from reachy_mini.daemon.app import main as upstream_main

    seen: list[list[str]] = []
    monkeypatch.setattr(upstream_main, "main", lambda: seen.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["untouched"])
    sim_daemon.run_sim_daemon(argv, **kwargs)
    assert len(seen) == 1
    return seen[0]


def test_run_sim_daemon_rewrites_argv_and_installs_the_corrections(
    monkeypatch: pytest.MonkeyPatch, mujoco: Any
) -> None:
    fastapi = pytest.importorskip("fastapi")
    from reachy_mini.daemon import daemon as upstream_daemon
    from reachy_mini.daemon.app import main as upstream_main
    from reachy_mini.vision import face_tracking

    original_backend = upstream_daemon.MujocoBackend
    monkeypatch.setattr(
        upstream_main, "create_app", lambda *a, **kw: fastapi.FastAPI(title="upstream")
    )
    apps: list[Any] = []
    argv = _run(
        [
            "--scene",
            "minimal",
            "--headless",
            "--no-preload-datasets",
            "--log-level",
            "DEBUG",
        ],
        monkeypatch,
        extensions=[SimDaemonExtension(on_app=apps.append)],
    )
    assert argv[1:] == [
        "--sim",
        "--scene",
        "minimal",
        "--headless",
        "--no-preload-datasets",
        "--log-level",
        "DEBUG",
    ]
    assert issubclass(upstream_daemon.MujocoBackend, original_backend)
    assert upstream_daemon.MujocoBackend is not original_backend
    assert face_tracking.intrinsics_for_size is sim_daemon._tracker_intrinsics
    upstream_args: Any = SimpleNamespace()
    app = upstream_main.create_app(upstream_args, None)
    assert apps == [app] and app.title == "upstream"


def test_run_sim_daemon_viewer_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("reachy_mini.daemon.app.main")
    argv = _run([], monkeypatch)
    assert argv[1:] == ["--sim", "--preload-datasets"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--webcam-device", "1"],
        ["--webcam-hfov", "60"],
        ["--camera", "webcam", "--webcam-hfov", "180"],
        ["--camera", "usb"],
    ],
)
def test_run_sim_daemon_refuses_bad_camera_flags(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        sim_daemon.run_sim_daemon(argv)
