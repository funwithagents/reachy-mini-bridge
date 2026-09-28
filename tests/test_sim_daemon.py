"""Fast-tier tests for `reachy_mini_bridge.sim_daemon` (specs/sim_daemon.md).

Daemon-free, camera-free and offline. The correction is exercised on the real upstream
`MujocoBackend`, with the face tracker's detector replaced by a stand-in queueing one
observation — no render, no detector, no network. Tests that need `mujoco` skip where the
sim extra is absent. The head's convergence on a face is the bridge tracker's, pinned in
tests/test_head_tracking.py.
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

from reachy_mini_bridge import sim_daemon
from reachy_mini_bridge.face_detection import DAEMON_DETECT_WEIGHT
from reachy_mini_bridge.head_tracking import pinhole_intrinsics
from reachy_mini_bridge.sim_daemon import (
    SimDaemonExtension,
    ViewerOverlay,
    WebcamRelay,
    corrected_backend,
    overlay_rect,
    relay_pipeline_candidates,
    relay_pipeline_description,
    resample_nearest,
    webcam_source,
)

FRAME = (320, 180)  # the face tracker's downscaled frame


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


# --- the correction: tracking stepped each control tick ----------------------------------


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


def test_a_control_tick_steps_head_tracking(mujoco: Any, scene_name: str) -> None:
    """The correction: upstream's MuJoCo loop never steps daemon-side tracking; the
    corrected backend steps it right after the kinematics update of each control tick,
    so an observation the detector queued becomes the backend's face target — what the
    daemon publishes and the bridge's `daemon` detection source reads — within a tick,
    at the negligible weight the bridge arms it at. Upstream's own loop leaves it
    undetected."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend
    from reachy_mini.vision.face_tracking import FaceObservation

    def backend_with_a_queued_face(backend_class: type) -> Any:
        backend = backend_class(scene=scene_name, headless=True, use_audio=False)
        observations = [
            FaceObservation(
                center=(0.25, -0.1),
                roll=0.0,
                width=FRAME[0],
                height=FRAME[1],
                camera_matrix=pinhole_intrinsics(112.3, FRAME),
                distortion=np.zeros(5),
                timestamp=time.monotonic(),
            )
        ]
        backend._tracking_enabled = True
        backend._tracking_requested_weight = DAEMON_DETECT_WEIGHT
        backend._tracker = SimpleNamespace(
            latest=lambda: observations.pop() if observations else None
        )
        assert not backend.get_tracked_face().detected
        backend.update_head_kinematics_model(np.zeros(7), np.zeros(2))
        return backend

    face = backend_with_a_queued_face(
        corrected_backend(MujocoBackend)
    ).get_tracked_face()
    assert face.detected and (face.x, face.y) == pytest.approx((0.25, -0.1))
    assert not backend_with_a_queued_face(MujocoBackend).get_tracked_face().detected


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
    backend = corrected_backend(_StubBackend, camera=webcam, relay_factory=_Relay)()
    assert backend.rendering_loop("eye_camera", 5005) is None
    backend.run()
    assert backend.ran and events == ["relay 2", "start", "stop"]
    # the daemon's aim is upstream's: the head pose it reads is the real one
    backend.set_tracking_face((0.0, 0.0), 0.0, 320, 180, np.eye(3), np.zeros(5), 0.0)
    assert backend.seen_in_aim[0, 3] == 0.5


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
        def __init__(self, device: Any, *, overlay: Any = None) -> None:
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
    # the daemon's aim is left as upstream has it: the bridge's tracker aims the head
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
