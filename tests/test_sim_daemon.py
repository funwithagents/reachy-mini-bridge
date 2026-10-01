"""Fast-tier tests for `reachy_mini_bridge.sim_daemon` (specs/daemon/sim_daemon.md).

Daemon-free, camera-free and offline: the backend subclass's hooks are exercised on the
real upstream `MujocoBackend` (no render, no network), the camera-source wiring and the
overlay on stubs. Tests that need `mujoco` skip where the sim extra is absent. The sim's
faces are the bridge's detector's to find and the head's convergence the bridge tracker's,
pinned in tests/test_head_tracking.py.
"""

from __future__ import annotations

import logging
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

from reachy_mini_bridge import sim_daemon, sim_displays
from reachy_mini_bridge.sim_daemon import (
    SimDaemonExtension,
    WebcamRelay,
    bridge_backend,
    relay_pipeline_candidates,
    relay_pipeline_description,
    webcam_source,
)


@pytest.fixture(autouse=True)
def restore_upstream_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The launcher patches process globals (in the daemon, that is the point); undo
    them after each test."""
    try:
        from reachy_mini.daemon import daemon as upstream_daemon
        from reachy_mini.daemon.app import main as upstream_main
    except ImportError:
        yield
        return
    monkeypatch.setattr(upstream_daemon, "MujocoBackend", upstream_daemon.MujocoBackend)
    monkeypatch.setattr(upstream_main, "create_app", upstream_main.create_app)
    yield


# --- the backend subclass: hooks, and the control loop left as upstream's ----------


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


def test_on_backend_runs_once_the_model_exists(mujoco: Any, scene_name: str) -> None:
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend

    seen: list[int] = []
    extension = SimDaemonExtension(on_backend=lambda b: seen.append(int(b.model.nbody)))
    bridge_backend(MujocoBackend, extensions=[extension])(
        scene=scene_name, headless=True, use_audio=False
    )
    assert len(seen) == 1 and seen[0] > 0


def test_the_subclass_leaves_the_control_loop_and_tracking_to_upstream() -> None:
    """The launcher changes what the camera stream carries and what the viewer shows,
    nothing of the daemon's control loop, kinematics or face tracking
    (specs/daemon/sim_daemon.md "The backend subclass")."""
    subclass = bridge_backend(_StubBackend)
    own = set(vars(subclass))  # `run` is wrapped for the overlay's sake; nothing else
    assert not own & {
        "update_head_kinematics_model",
        "step_head_tracking",
        "set_tracking_face",
        "update_target_head_joints_from_ik",
    }, own


# --- webcam mode wiring (no MuJoCo) --------------------------------------------------------


class _StubBackend:
    """Stands in for upstream's backend where a test needs no physics."""

    INIT_HEAD_POSE = np.eye(4)

    def __init__(self) -> None:
        self.ran = False
        self.pose = np.diag([1.0, 1.0, 1.0, 1.0])
        self.pose[0, 3] = 0.5

    def run(self) -> None:
        self.ran = True

    def rendering_loop(self, *args: Any) -> None:
        raise AssertionError("the eye camera must not be rendered in webcam mode")

    def get_current_head_pose(self) -> Any:
        return self.pose


def test_webcam_mode_relays_instead_of_rendering() -> None:
    events: list[str] = []

    class _Relay:
        def __init__(self, device: Any, *, overlay: Any = None) -> None:
            events.append(f"relay {device!r}")

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    webcam = sim_daemon._Camera(source="webcam", device=2, hfov_deg=65.0)
    backend = bridge_backend(_StubBackend, camera=webcam, relay_factory=_Relay)()
    assert backend.rendering_loop("eye_camera", 5005) is None
    backend.run()
    assert backend.ran and events == ["relay 2", "start", "stop"]


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


def test_the_relay_names_its_camera_and_resolution_once_frames_flow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The camera that is actually feeding the stream is named in the log with the
    resolution it negotiated, once per run — which one the platform default turned out
    to be is not otherwise visible — and a pipeline that cannot report a size is still
    named."""

    def relay_for(size: tuple[int, int] | None) -> WebcamRelay:
        return WebcamRelay(
            None,
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
            assert _wait(lambda: "Fake Camera open at 3840x2592" in caplog.text)
            time.sleep(0.3)  # more frames flow
        finally:
            relay.stop()
        assert caplog.text.count("Fake Camera open") == 1  # once for the run

        silent = relay_for(None)
        silent.start()
        try:
            assert _wait(lambda: caplog.text.count("Fake Camera open") >= 2)
        finally:
            silent.stop()


def test_the_relay_falls_back_when_the_camera_lacks_the_streams_size(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The camera that cannot give 1280x720 (the Reachy Mini's own sensor is one) fails
    the preferred pipeline — that is what the fallback is for, so it is not an error the
    person running the sim should see, and the fallback's frames flow."""
    tried: list[str] = []

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
        frame_timeout=5.0,
        retry=0.05,
        open_pipeline=open_pipeline,
        system="Linux",
    )
    with (
        caplog.at_level(logging.INFO, logger="reachy_mini_bridge.sim_daemon"),
        caplog_errors() as errors,
    ):
        relay.start()
        try:
            assert _wait(lambda: "open at 3840x2592" in caplog.text)
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
    """Stands in for `mujoco.viewer.Handle` as far as the run wiring goes: it closes."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("close")


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
    backend = bridge_backend(_RenderingStubBackend, displays=on, overlay_factory=make)()
    renderer = backend._get_renderer("eye_camera")
    frame = renderer.render()
    assert frame.shape == (720, 1280, 3) and int(frame[0, 0, 0]) == 7
    assert len(overlays) == 1 and len(overlays[0].frames) == 1
    shown = overlays[0].frames[0]
    assert shown.shape == (360, 640, 3) and int(shown[0, 0, 0]) == 7
    assert overlays[0].labels == ["eye camera 1280x720"]
    assert renderer.scene == "scene"  # everything else is the renderer's
    off = bridge_backend(_RenderingStubBackend)()
    assert off._get_renderer("eye_camera") is off.renderer


def test_webcam_mode_with_the_overlay_hands_it_to_the_relay() -> None:
    relays: list[Any] = []
    events: list[str] = []

    class _Relay:
        def __init__(self, device: Any, *, overlay: Any = None) -> None:
            relays.append(overlay)

        def start(self) -> None:
            events.append("relay start")

        def stop(self) -> None:
            events.append("relay stop")

    webcam = sim_daemon._Camera(source="webcam")
    on = sim_daemon._Displays(camera_overlay=True)
    overlay = _FakeOverlay(events)
    backend = bridge_backend(
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
    handle = _FakeHandle(events)

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
    bridge_backend(
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
        bridge_backend(
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


def test_run_sim_daemon_rewrites_argv_and_installs_the_backend(
    monkeypatch: pytest.MonkeyPatch, mujoco: Any
) -> None:
    fastapi = pytest.importorskip("fastapi")
    from reachy_mini.daemon import daemon as upstream_daemon
    from reachy_mini.daemon.app import main as upstream_main
    from reachy_mini.media import camera_utils
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
    # the daemon's tracking is left as upstream has it: the bridge's tracker aims the head
    assert face_tracking.intrinsics_for_size is camera_utils.intrinsics_for_size
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

    monkeypatch.setattr(sim_daemon, "bridge_backend", corrected)
    argv = _run(["--sim-display", "camera_overlay"], monkeypatch)
    assert argv[1:] == ["--sim", "--preload-datasets"]
    assert seen["displays"] == sim_daemon._Displays(camera_overlay=True)
    _run([], monkeypatch)
    assert seen["displays"] == sim_daemon._Displays()
    # Every display has its flag, and several go together.
    _run(["--sim-display", "face_markers", "--sim-display", "robot_gaze"], monkeypatch)
    assert seen["displays"] == sim_daemon._Displays(robot_gaze=True, face_markers=True)


def test_the_scene_displays_are_built_on_the_model_and_handed_the_viewer(
    mujoco: Any, scene_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `robot_gaze` / `face_markers` on, the backend builds one scene layer over
    the views that are on, once its model exists; the face markers view is handed out
    for the router; and run() gives the layer the viewer like the overlay, stopping
    both before the close."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend

    events: list[str] = []
    layers: list[Any] = []

    class _Layer:
        def __init__(self, views: Any) -> None:
            self.views = list(views)
            layers.append(self)

        def attach(self, handle: Any) -> None:
            events.append("attach layer")

        def stop(self) -> None:
            events.append("stop layer")

    handle = _FakeHandle(events)
    viewer_module = SimpleNamespace(launch_passive=lambda *a, **kw: handle)
    handed: list[Any] = []
    on = sim_daemon._Displays(camera_overlay=True, robot_gaze=True, face_markers=True)
    backend_class = bridge_backend(
        MujocoBackend,
        displays=on,
        overlay_factory=lambda: _FakeOverlay(events),
        layer_factory=_Layer,
        on_face_markers=handed.append,
        viewer_module=viewer_module,
    )
    backend = backend_class(scene=scene_name, headless=True, use_audio=False)
    (layer,) = layers
    gaze, markers = layer.views
    assert isinstance(gaze, sim_displays.RobotGazeView)
    assert isinstance(markers, sim_displays.FaceMarkersView) and handed == [markers]
    # The views read the backend's own model: the gaze starts at its eye camera, and
    # the markers' frame offset is the shift upstream applies to the pose it reports.
    camera = mujoco.mj_name2id(backend.model, mujoco.mjtObj.mjOBJ_CAMERA, "eye_camera")
    assert gaze.geoms()[0].a == pytest.approx(tuple(backend.data.cam_xpos[camera]))
    marker = sim_displays.FaceMarker(
        pos=(0.4, 0.0, 0.0), quat=(1.0, 0.0, 0.0, 0.0), size=(0.1, 0.2)
    )
    assert markers.world_pos(marker) == pytest.approx((0.4, 0.0, 0.177))

    def run(self: Any) -> None:  # upstream's run(), as far as the viewer goes
        viewer_module.launch_passive(None, None).close()

    monkeypatch.setattr(MujocoBackend, "run", run)
    backend.run()
    assert events == [
        "attach",
        "attach layer",
        "stop",
        "stop layer",
        "close",
        "stop",
        "stop layer",  # the run's own finally: idempotent on the displays
    ]

    # Only the gaze: no markers view, nothing handed out.
    layers.clear()
    bridge_backend(
        MujocoBackend,
        displays=sim_daemon._Displays(robot_gaze=True),
        layer_factory=_Layer,
        on_face_markers=handed.append,
    )(scene=scene_name, headless=True, use_audio=False)
    assert [type(v) for v in layers[0].views] == [sim_displays.RobotGazeView]
    assert handed == [markers]


def test_the_displays_router_is_mounted_with_face_markers_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--sim-display face_markers` mounts the displays router on upstream's app; it
    answers 503 until a backend is built, and without the display it is not there."""
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from reachy_mini.daemon.app import main as upstream_main

    monkeypatch.setattr(
        upstream_main, "create_app", lambda *a, **kw: fastapi.FastAPI(title="upstream")
    )
    seen: dict[str, Any] = {}

    def corrected(backend_class: type, **kwargs: Any) -> type:
        seen.update(kwargs)
        return backend_class

    monkeypatch.setattr(sim_daemon, "bridge_backend", corrected)
    upstream_args: Any = SimpleNamespace()
    _run(["--sim-display", "face_markers"], monkeypatch)
    http = testclient.TestClient(upstream_main.create_app(upstream_args, None))
    assert http.get("/api/sim/displays/face_markers").status_code == 503
    # The backend, once built, hands its view over: the route serves it.
    seen["on_face_markers"](
        sim_displays.FaceMarkersView(lambda: np.array([0.0, 0.0, 0.177]))
    )
    assert http.get("/api/sim/displays/face_markers").json() == {
        "age_s": None,
        "markers": [],
    }
    # A launch without the display (a fresh process: upstream's own create_app again).
    monkeypatch.setattr(
        upstream_main, "create_app", lambda *a, **kw: fastapi.FastAPI(title="upstream")
    )
    _run(["--sim-display", "robot_gaze"], monkeypatch)
    http = testclient.TestClient(upstream_main.create_app(upstream_args, None))
    assert http.get("/api/sim/displays/face_markers").status_code == 404


@pytest.mark.parametrize(
    "argv",
    [
        ["--webcam-device", "1"],
        ["--webcam-hfov", "60"],
        ["--camera", "webcam", "--webcam-hfov", "180"],
        ["--camera", "usb"],
        ["--headless", "--sim-display", "camera_overlay"],
        ["--headless", "--sim-display", "robot_gaze"],
        ["--headless", "--sim-display", "face_markers"],
        ["--sim-display", "hud"],
    ],
)
def test_run_sim_daemon_refuses_bad_camera_flags(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        sim_daemon.run_sim_daemon(argv)
