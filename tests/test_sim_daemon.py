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
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from reachy_mini_bridge import sim_daemon
from reachy_mini_bridge.sim_daemon import (
    SimDaemonExtension,
    ViewerOverlay,
    WebcamRelay,
    corrected_backend,
    cropped_hfov_deg,
    overlay_rect,
    pinhole_intrinsics,
    relay_pipeline_candidates,
    relay_pipeline_description,
    resample_nearest,
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
        def __init__(
            self,
            device: Any,
            *,
            on_source_size: Callable[[tuple[int, int]], None],
            overlay: Any = None,
        ) -> None:
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


def test_the_tracker_follows_the_camera_the_relay_negotiated() -> None:
    """End to end: the relay reports the resolution its camera negotiated, and the matrix
    the tracker resolves becomes the pinhole of what the crop leaves of that camera."""
    from reachy_mini.vision import face_tracking

    reports: list[Callable[[tuple[int, int]], None]] = []

    class _Relay:
        def __init__(
            self,
            device: Any,
            *,
            on_source_size: Callable[[tuple[int, int]], None],
            overlay: Any = None,
        ) -> None:
            reports.append(on_source_size)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    sim_daemon.install_tracker_intrinsics()
    webcam = sim_daemon._Camera(source="webcam", device=None, hfov_deg=100.0)
    backend = corrected_backend(_StubBackend, camera=webcam, relay_factory=_Relay)()
    K = np.eye(3)  # upstream's specs matrix: unused, the pinhole replaces it
    # Before any frame, the configured field of view stands.
    np.testing.assert_allclose(
        face_tracking.intrinsics_for_size(K, 1.0, FRAME),
        pinhole_intrinsics(100.0, FRAME),
    )
    backend.run()
    assert len(reports) == 1
    reports[0]((2560, 1080))  # an ultrawide camera: the crop takes width off it
    np.testing.assert_allclose(
        face_tracking.intrinsics_for_size(K, 1.0, FRAME),
        pinhole_intrinsics(cropped_hfov_deg(100.0, (2560, 1080)), FRAME),
    )
    assert sim_daemon._TrackerCamera.hfov_deg == pytest.approx(
        cropped_hfov_deg(100.0, (2560, 1080))
    )


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
    assert description.startswith("autovideosrc name=")
    for part in (
        "format=RGB,width=1280,height=720",
        "rtpvrawpay",
        "payload=96",
        "port=5005",
    ):
        assert part in description


def test_the_relay_crops_and_scales_instead_of_constraining_the_camera() -> None:
    """A camera is asked for nothing but system memory — one asked for a mode it does not
    have never negotiates, so it never opens; a macOS camera offering GPU memory first
    will not even link — and whatever it offers is centre-cropped to the stream's aspect
    and scaled to its size, converted last, at the smallest frame."""
    stages = [
        stage.strip() for stage in relay_pipeline_description("v4l2src").split("!")
    ]
    assert stages[0].startswith("v4l2src name=")
    assert stages[1] == "video/x-raw"  # system memory, and nothing else asked of it
    assert stages[2] == "aspectratiocrop aspect-ratio=16/9"  # 1280x720 reduced
    assert stages[3:6] == ["videoscale", "videoconvert", "videorate"]
    # The only size in the pipeline is the stream's, downstream of the scale.
    sized = [i for i, stage in enumerate(stages) if "width=" in stage]
    assert sized == [6] and "width=1280,height=720" in stages[6]


def test_the_relay_asks_for_the_streams_size_before_settling_for_a_crop() -> None:
    """A camera that has the stream's own size is asked for exactly that, so it keeps its
    whole landscape view; only one that cannot takes the crop. Both pipelines are
    otherwise identical — the crop and the scale are no-ops at the stream's size."""
    preferred, fallback = relay_pipeline_candidates("v4l2src")
    assert "video/x-raw,width=1280,height=720 ! aspectratiocrop" in preferred
    assert "video/x-raw ! aspectratiocrop" in fallback
    assert (
        preferred.replace("video/x-raw,width=1280,height=720", "video/x-raw")
        == fallback
    )


def test_cropped_hfov_follows_the_width_the_crop_keeps() -> None:
    """The crop takes nothing off the width of a camera at or narrower than 16:9 — its
    field of view is the one configured — and narrows a wider one to what it keeps."""
    for source in ((1280, 720), (1920, 1080), (640, 480), (3840, 2592), (1080, 1920)):
        assert cropped_hfov_deg(70.0, source) == pytest.approx(70.0)

    ultrawide = cropped_hfov_deg(100.0, (2560, 1080))  # 21:9, wider than the frame
    kept = (16 / 9) / (2560 / 1080)
    expected = math.degrees(2 * math.atan(math.tan(math.radians(50.0)) * kept))
    assert ultrawide == pytest.approx(expected) and ultrawide < 100.0
    # and the pinhole that follows is longer-focus by exactly what the crop took
    K = pinhole_intrinsics(ultrawide, FRAME)
    assert K[0, 0] == pytest.approx(
        (FRAME[0] / 2) / (kept * math.tan(math.radians(50.0)))
    )
    assert K[0, 0] == pytest.approx(pinhole_intrinsics(100.0, FRAME)[0, 0] / kept)


class _FakePipeline:
    def __init__(
        self,
        on_frame: Callable[[], None],
        frames: bool,
        size: tuple[int, int] | None = (1920, 1080),
    ) -> None:
        self._on_frame = on_frame
        self._frames = frames
        self._size = size
        self.stopped = False

    def source_size(self) -> tuple[int, int] | None:
        return self._size

    def device_name(self) -> str | None:
        return "Fake Camera"

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


def test_the_relay_reports_its_camera_resolution_once_frames_flow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The size goes out with the first frame of a run, once — and a pipeline that cannot
    report one leaves the configured field of view standing rather than guessing. Either
    way the camera that is actually feeding the stream is named in the log: which one the
    platform default turned out to be is not otherwise visible."""
    sizes: list[tuple[int, int]] = []

    def relay_for(size: tuple[int, int] | None) -> WebcamRelay:
        return WebcamRelay(
            None,
            on_source_size=sizes.append,
            frame_timeout=5.0,
            retry=0.05,
            open_pipeline=lambda description, on_frame, on_overlay_frame=None: (
                _FakePipeline(on_frame, True, size)
            ),
            system="Linux",
        )

    with caplog.at_level(logging.INFO, logger="reachy_mini_bridge.sim_daemon"):
        relay = relay_for((3840, 2592))
        relay.start()
        try:
            assert _wait(lambda: sizes == [(3840, 2592)])
        finally:
            relay.stop()
        assert sizes == [(3840, 2592)]  # one report for the run, not one per frame
        assert "Fake Camera open at 3840x2592" in caplog.text

        silent = relay_for(None)
        silent.start()
        try:
            assert _wait(lambda: caplog.text.count("Fake Camera open") >= 2)
        finally:
            silent.stop()
    assert sizes == [(3840, 2592)]  # no size: the configured field of view stands


def test_the_relay_falls_back_when_the_camera_lacks_the_streams_size() -> None:
    """The camera that cannot give 1280x720 (the Reachy Mini's own sensor is one) fails
    the preferred pipeline — that is what the fallback is for, so it is not an error the
    person running the sim should see, and the fallback's frames flow."""
    tried: list[str] = []
    sizes: list[tuple[int, int]] = []

    def open_pipeline(
        description: str,
        on_frame: Callable[[], None],
        on_overlay_frame: Callable[[np.ndarray], None] | None = None,
    ) -> _FakePipeline:
        tried.append(description)
        if "width=1280,height=720 ! aspectratiocrop" in description:
            raise RuntimeError("could not negotiate")  # no such mode on this camera
        return _FakePipeline(on_frame, True, (3840, 2592))

    relay = WebcamRelay(
        None,
        on_source_size=sizes.append,
        frame_timeout=5.0,
        retry=0.05,
        open_pipeline=open_pipeline,
        system="Linux",
    )
    with caplog_errors() as errors:
        relay.start()
        try:
            assert _wait(lambda: sizes == [(3840, 2592)])
        finally:
            relay.stop()
    assert len(tried) == 2 and "width=1280,height=720" in tried[0]
    assert errors() == []  # falling back is the design, not a failure to report


@contextmanager
def caplog_errors() -> Iterator[Callable[[], list[str]]]:
    """The ERROR messages the module logs inside the block."""
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("reachy_mini_bridge.sim_daemon")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        yield lambda: [r.getMessage() for r in records if r.levelno >= logging.ERROR]
    finally:
        logger.removeHandler(handler)


def test_the_relay_logs_a_silent_camera_once_retries_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No frame (a refused camera permission looks like this) is one ERROR naming the
    macOS permission, then quiet retries; frames arriving again are logged once."""
    pipelines: list[_FakePipeline] = []
    frames = threading.Event()

    def open_pipeline(
        description: str,
        on_frame: Callable[[], None],
        on_overlay_frame: Callable[[np.ndarray], None] | None = None,
    ) -> _FakePipeline:
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


# --- the viewer overlay -----------------------------------------------------------------


def test_overlay_rect_sits_in_the_top_right_corner() -> None:
    """A 16:9 picture a quarter of the view's width, even-sided, inset by the margin from
    the top-right corner — MuJoCo rectangles have their origin at the bottom-left, so
    the corner is where left+width and bottom+height meet the view's."""
    for viewport in ((0, 0, 1920, 1080), (10, 20, 1280, 720)):
        rect = overlay_rect(viewport)
        assert rect is not None
        left, bottom, width, height = rect
        view_left, view_bottom, view_width, view_height = viewport
        assert width == int(view_width * 0.25) // 2 * 2
        assert width % 2 == 0 and height % 2 == 0
        assert abs(width / height - 16 / 9) < 0.02
        inset = int(view_width * 0.02)
        assert left + width + inset == view_left + view_width
        assert bottom + height + inset == view_bottom + view_height
        assert left > view_left and bottom > view_bottom
    assert overlay_rect((0, 0, 100, 60)) is None  # too small a picture to draw
    assert overlay_rect((0, 0, 1920, 40)) is None  # a view not tall enough for it


def test_resample_nearest_keeps_the_size_and_the_corners() -> None:
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[0, 0], frame[0, -1] = (1, 2, 3), (4, 5, 6)
    frame[-1, 0], frame[-1, -1] = (7, 8, 9), (10, 11, 12)
    for size in ((480, 270), (1280, 720), (33, 17)):
        out = resample_nearest(frame, size)
        assert out.shape == (size[1], size[0], 3) and out.flags.c_contiguous
        assert tuple(out[0, 0]) == (1, 2, 3) and tuple(out[0, -1]) == (4, 5, 6)
        assert tuple(out[-1, 0]) == (7, 8, 9) and tuple(out[-1, -1]) == (10, 11, 12)
    same = resample_nearest(frame, (640, 360))
    assert same is not frame and np.array_equal(same, frame)


def test_the_relay_pipeline_grows_an_overlay_branch_only_when_asked() -> None:
    """With the overlay, a tee after the RGB caps adds a leaky one-buffer branch scaled
    to the overlay's size and mirrored into a named appsink; the stream branch is
    unchanged, so the media server and the tracker see exactly what they see without
    it — unmirrored, as the camera does."""
    plain = relay_pipeline_description("autovideosrc")
    assert plain == (
        "autovideosrc name=camera ! video/x-raw ! aspectratiocrop aspect-ratio=16/9 ! "
        "videoscale ! videoconvert ! videorate ! "
        "video/x-raw,format=RGB,width=1280,height=720,framerate=25/1 ! "
        "queue leaky=downstream max-size-buffers=2 ! rtpvrawpay mtu=1400 name=pay ! "
        "application/x-rtp,payload=96 ! udpsink host=127.0.0.1 port=5005 sync=false"
    )
    head, marker, stream = plain.partition("framerate=25/1 ! ")
    with_overlay = relay_pipeline_description("autovideosrc", overlay=True)
    assert with_overlay.startswith(f"{head}{marker}tee name=t ! {stream} t. ! ")
    assert with_overlay.split(" t. ! ")[1] == (
        "queue leaky=downstream max-size-buffers=1 ! videoscale ! "
        "video/x-raw,width=640,height=360 ! videoflip method=horizontal-flip ! "
        "appsink name=overlay emit-signals=true max-buffers=1 drop=true sync=false"
    )
    assert all("appsink" in d for d in relay_pipeline_candidates("v", overlay=True))
    assert not any("tee" in d for d in relay_pipeline_candidates("v"))


class _FakeHandle:
    """Stands in for `mujoco.viewer.Handle`: records the overlay calls, and holds a
    `set_images` call until released, as the real one waits for the render thread."""

    def __init__(
        self,
        viewport: tuple[int, int, int, int] = (0, 0, 1920, 1080),
        events: list[str] | None = None,
    ) -> None:
        self.resize(viewport)
        self.events = [] if events is None else events
        self.images: list[tuple[Any, np.ndarray]] = []
        self.texts: list[Any] = []
        self.cleared: list[str] = []
        self.running = True
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def resize(self, viewport: tuple[int, int, int, int]) -> None:
        left, bottom, width, height = viewport
        self.viewport = SimpleNamespace(
            left=left, bottom=bottom, width=width, height=height
        )

    def is_running(self) -> bool:
        return self.running

    def set_images(self, pairs: list[tuple[Any, np.ndarray]]) -> None:
        self.entered.set()
        self.release.wait(5.0)
        self.images.extend(pairs)

    def set_texts(self, texts: Any) -> None:
        self.texts.append(texts)

    def clear_images(self) -> None:
        self.cleared.append("images")

    def clear_texts(self) -> None:
        self.cleared.append("texts")

    def close(self) -> None:
        self.running = False
        self.events.append("close")


def _rect(left: int, bottom: int, width: int, height: int) -> tuple[int, int, int, int]:
    return (left, bottom, width, height)


def _overlay() -> ViewerOverlay:
    return ViewerOverlay(rect_factory=_rect, text_style=("font", "grid"))


def test_the_overlay_draws_the_latest_frame_at_the_corner_rectangle() -> None:
    handle = _FakeHandle()
    overlay = _overlay()
    overlay.attach(handle)
    try:
        assert overlay.drawing
        overlay.show(np.full((360, 640, 3), 1, dtype=np.uint8))
        assert _wait(lambda: len(handle.images) == 1)
        rect, image = handle.images[0]
        assert rect == overlay_rect((0, 0, 1920, 1080))
        assert image.shape == (rect[3], rect[2], 3) and image.dtype == np.uint8
        assert int(image[rect[3] // 2, rect[2] // 2, 0]) == 1
        # a light frame around the picture, so it shows against a same-coloured scene
        assert int(image[0, 0, 0]) == 230 and int(image[-1, -1, 0]) == 230
        assert int(image[1, rect[2] // 2, 0]) == 230 and int(image[2, 2, 0]) == 1
        handle.resize((0, 0, 1280, 720))  # a resized window: the picture follows
        overlay.show(np.full((360, 640, 3), 2, dtype=np.uint8))
        assert _wait(lambda: len(handle.images) == 2)
        assert handle.images[1][0] == overlay_rect((0, 0, 1280, 720))
        overlay.label("webcam: Fake Camera 1920x1080")
        assert _wait(lambda: len(handle.texts) == 1)
        assert handle.texts == [("font", "grid", "webcam: Fake Camera 1920x1080", "")]
    finally:
        overlay.stop()
    assert not overlay.drawing and handle.cleared == ["images", "texts"]
    overlay.stop()  # idempotent
    assert handle.cleared == ["images", "texts"]


def test_the_overlay_skips_to_the_latest_frame_while_the_viewer_is_busy() -> None:
    """`set_images` waits for the viewer's render thread; frames shown meanwhile replace
    each other, and the next draw is the latest one — never a queue of stale frames."""
    handle = _FakeHandle()
    overlay = _overlay()
    overlay.attach(handle)
    try:
        handle.release.clear()
        overlay.show(np.full((36, 64, 3), 1, dtype=np.uint8))
        assert handle.entered.wait(5.0)  # the first draw is waiting on the viewer
        overlay.show(np.full((36, 64, 3), 2, dtype=np.uint8))
        overlay.show(np.full((36, 64, 3), 3, dtype=np.uint8))
        handle.release.set()
        assert _wait(lambda: len(handle.images) == 2)
        time.sleep(0.1)
        assert [int(image[10, 10, 0]) for _, image in handle.images] == [1, 3]
    finally:
        overlay.stop()


def test_the_overlay_warns_once_on_a_mujoco_without_set_images(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _OldHandle:
        viewport = SimpleNamespace(left=0, bottom=0, width=1920, height=1080)

        def is_running(self) -> bool:
            return True

    overlay = _overlay()
    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.sim_daemon"):
        overlay.attach(_OldHandle())
        overlay.show(np.zeros((36, 64, 3), dtype=np.uint8))
        overlay.label("webcam")
        overlay.stop()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "3.3.1" in warnings[0].getMessage()
    assert not overlay.drawing


def test_the_overlay_reports_a_failing_draw_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A draw that raises is a WARNING with the traceback the first time — the person
    running the sim sees why there is no picture — and quiet after that."""

    class _BrokenHandle(_FakeHandle):
        def set_images(self, pairs: list[tuple[Any, np.ndarray]]) -> None:
            raise ValueError("Image shape (1, 1) does not match target shape")

    handle = _BrokenHandle()
    overlay = _overlay()
    with caplog.at_level(logging.INFO, logger="reachy_mini_bridge.sim_daemon"):
        overlay.attach(handle)
        for value in (1, 2, 3):
            overlay.show(np.full((36, 64, 3), value, dtype=np.uint8))
            time.sleep(0.05)
        assert _wait(
            lambda: sum("draw failed" in r.getMessage() for r in caplog.records) >= 1
        )
        time.sleep(0.1)
        overlay.stop()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "draw failed" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None
    assert any("drawing on the viewer" in r.getMessage() for r in caplog.records)


class _FakeOverlay:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = [] if events is None else events
        self.frames: list[np.ndarray] = []
        self.labels: list[str] = []
        self.handle: Any = None

    def attach(self, handle: Any) -> None:
        self.handle = handle
        self.events.append("attach")

    def show(self, frame: np.ndarray) -> None:
        self.frames.append(frame)

    def label(self, text: str) -> None:
        self.labels.append(text)

    def stop(self) -> None:
        self.events.append("stop")


class _SimStubBackend(_StubBackend):
    """A stub for `sim` camera mode, where the corrected backend reads the scene's eye
    camera off the model: a minimal MuJoCo model with just that camera."""

    def __init__(self) -> None:
        super().__init__()
        mujoco = pytest.importorskip("mujoco")
        self.model = mujoco.MjModel.from_xml_string(
            '<mujoco><worldbody><camera name="eye_camera" fovy="80"/></worldbody>'
            "</mujoco>"
        )


class _RenderingStubBackend(_SimStubBackend):
    def __init__(self) -> None:
        super().__init__()
        self.renderer = SimpleNamespace(
            render=lambda: np.full((720, 1280, 3), 7, dtype=np.uint8),
            update_scene=lambda *args: None,
            scene="scene",
        )

    def _get_renderer(self, camera_name: str) -> Any:
        return self.renderer


def test_sim_mode_with_the_overlay_taps_the_eye_camera_renderer() -> None:
    """Upstream's render thread gets its renderer from `_get_renderer`; with the overlay
    on, every frame it renders is also shown, at the overlay's size, and the stream still
    gets the full frame. Off, the renderer is upstream's own."""
    overlays: list[_FakeOverlay] = []

    def make() -> _FakeOverlay:
        overlays.append(_FakeOverlay())
        return overlays[-1]

    on = sim_daemon._Displays(camera_overlay=True)
    backend = corrected_backend(
        _RenderingStubBackend, displays=on, overlay_factory=make
    )()
    renderer = backend._get_renderer("eye_camera")
    frame = renderer.render()
    assert frame.shape == (720, 1280, 3) and int(frame[0, 0, 0]) == 7
    assert len(overlays) == 1 and len(overlays[0].frames) == 1
    shown = overlays[0].frames[0]
    assert shown.shape == (360, 640, 3) and int(shown[0, 0, 0]) == 7
    assert overlays[0].labels == ["eye camera 1280x720"]
    assert renderer.scene == "scene"  # everything else is the renderer's
    off = corrected_backend(_RenderingStubBackend)()
    assert off._get_renderer("eye_camera") is off.renderer


def test_webcam_mode_with_the_overlay_hands_it_to_the_relay() -> None:
    relays: list[Any] = []
    events: list[str] = []

    class _Relay:
        def __init__(
            self,
            device: Any,
            *,
            on_source_size: Callable[[tuple[int, int]], None],
            overlay: Any = None,
        ) -> None:
            relays.append(overlay)

        def start(self) -> None:
            events.append("relay start")

        def stop(self) -> None:
            events.append("relay stop")

    webcam = sim_daemon._Camera(source="webcam")
    on = sim_daemon._Displays(camera_overlay=True)
    overlay = _FakeOverlay(events)
    backend = corrected_backend(
        _StubBackend,
        camera=webcam,
        displays=on,
        relay_factory=_Relay,
        overlay_factory=lambda: overlay,
        viewer_module=SimpleNamespace(launch_passive=lambda *a, **kw: None),
    )()
    backend.run()
    assert relays == [overlay]
    assert events == ["relay start", "relay stop", "stop"]


def test_the_run_hands_the_viewer_to_the_overlay_and_stops_it_before_the_close() -> (
    None
):
    """Upstream's run() launches the viewer as a local and closes it itself; the run
    wrapper hands that handle to the overlay and makes its close stop the overlay first,
    then restores the launch function — also when the run raises."""
    events: list[str] = []
    handle = _FakeHandle(events=events)

    def launch_passive(*args: Any, **kwargs: Any) -> _FakeHandle:
        events.append(f"launch {kwargs.get('show_left_ui')}")
        return handle

    viewer_module = SimpleNamespace(launch_passive=launch_passive)

    class _ViewerBackend(_SimStubBackend):
        def run(self) -> None:
            viewer = viewer_module.launch_passive(None, None, show_left_ui=False)
            events.append("running")
            viewer.close()
            events.append("after close")

    overlay = _FakeOverlay(events)
    on = sim_daemon._Displays(camera_overlay=True)
    corrected_backend(
        _ViewerBackend,
        displays=on,
        overlay_factory=lambda: overlay,
        viewer_module=viewer_module,
    )().run()
    assert overlay.handle is handle
    assert events == [
        "launch False",
        "attach",
        "running",
        "stop",
        "close",
        "after close",
        "stop",  # the run's own finally: idempotent on the overlay
    ]
    assert viewer_module.launch_passive is launch_passive

    class _FailingBackend(_SimStubBackend):
        def run(self) -> None:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        corrected_backend(
            _FailingBackend,
            displays=on,
            overlay_factory=lambda: overlay,
            viewer_module=viewer_module,
        )().run()
    assert viewer_module.launch_passive is launch_passive


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


def test_run_sim_daemon_turns_on_a_viewer_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--sim-display camera_overlay` reaches the corrected backend as its displays and
    nothing of it reaches upstream's argv."""
    pytest.importorskip("reachy_mini.daemon.app.main")
    seen: dict[str, Any] = {}

    def corrected(backend_class: type, **kwargs: Any) -> type:
        seen.update(kwargs)
        return backend_class

    monkeypatch.setattr(sim_daemon, "corrected_backend", corrected)
    argv = _run(["--sim-display", "camera_overlay"], monkeypatch)
    assert argv[1:] == ["--sim", "--preload-datasets"]
    assert seen["displays"] == sim_daemon._Displays(camera_overlay=True)
    _run([], monkeypatch)
    assert seen["displays"] == sim_daemon._Displays()


@pytest.mark.parametrize(
    "argv",
    [
        ["--webcam-device", "1"],
        ["--webcam-hfov", "60"],
        ["--camera", "webcam", "--webcam-hfov", "180"],
        ["--camera", "usb"],
        ["--headless", "--sim-display", "camera_overlay"],
        ["--sim-display", "hud"],
    ],
)
def test_run_sim_daemon_refuses_bad_camera_flags(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        sim_daemon.run_sim_daemon(argv)
