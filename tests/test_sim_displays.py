"""Fast-tier tests for `reachy_mini_bridge.sim_displays` (specs/daemon/sim_displays.md).

Daemon-free and offline: the displays are exercised on stand-ins for the viewer handle;
tests that need `mujoco` skip where the sim extra is absent. How the launcher wires the
displays in is tests/test_sim_daemon.py's.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from reachy_mini.vision.look_at import default_head_to_camera_transform
from scipy.spatial.transform import Rotation

from reachy_mini_bridge import sim_displays
from reachy_mini_bridge.config import SimCameraSettings
from reachy_mini_bridge.face_detection import Face, FaceReport
from reachy_mini_bridge.head_tracking import (
    CameraModel,
    HeadTrackingReport,
    frame_head_pose,
)
from reachy_mini_bridge.observable import Observable
from reachy_mini_bridge.sim_displays import (
    FACE_BOX_HEIGHT_M,
    FACE_MARKER_ASPECT,
    FACE_MARKER_THICKNESS_M,
    ROBOT_GAZE_LENGTH_M,
    FaceMarker,
    FaceMarkerPublisher,
    FaceMarkersView,
    RobotGazeView,
    SceneGeom,
    SceneLayer,
    ViewerOverlay,
    build_router,
    capture_viewer,
    face_marker,
    overlay_rect,
    resample_nearest,
)

if TYPE_CHECKING:
    import numpy.typing as npt


def _wait(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- the camera overlay -----------------------------------------------------------------


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
    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.sim_displays"):
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
    with caplog.at_level(logging.INFO, logger="reachy_mini_bridge.sim_displays"):
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


# --- the viewer handle: every display that is on --------------------------------------


class _RecordingDisplay:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.handle: Any = None

    def attach(self, handle: Any) -> None:
        self.handle = handle
        self.events.append(f"attach {self.name}")

    def stop(self) -> None:
        self.events.append(f"stop {self.name}")


def test_capture_viewer_hands_the_handle_to_every_display_and_stops_them_first() -> (
    None
):
    """Upstream's run() launches the viewer as a local and closes it itself: while the
    capture is active the launch hands the handle to every display, the handle's close
    stops them all before closing, and the launch function is restored — also when the
    block raises."""
    events: list[str] = []
    handle = _FakeHandle(events=events)

    def launch_passive(*args: Any, **kwargs: Any) -> _FakeHandle:
        events.append("launch")
        return handle

    viewer = SimpleNamespace(launch_passive=launch_passive)
    overlay = _RecordingDisplay("overlay", events)
    layer = _RecordingDisplay("layer", events)
    with capture_viewer([overlay, layer], viewer):
        launched = viewer.launch_passive(None, None)
        launched.close()
    assert overlay.handle is handle and layer.handle is handle
    assert events == [
        "launch",
        "attach overlay",
        "attach layer",
        "stop overlay",
        "stop layer",
        "close",
    ]
    assert viewer.launch_passive is launch_passive

    with (
        pytest.raises(RuntimeError, match="boom"),
        capture_viewer([overlay], viewer),
    ):
        raise RuntimeError("boom")
    assert viewer.launch_passive is launch_passive


# --- the face markers: from a face to a marker ----------------------------------------

SIM_CAMERA = CameraModel.for_sim(SimCameraSettings())


def _turned(yaw_deg: float) -> npt.NDArray[np.float64]:
    pose = np.eye(4)
    pose[:3, :3] = Rotation.from_euler("z", yaw_deg, degrees=True).as_matrix()
    return pose


def _camera_pose(head_pose: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """The eye camera's pose in the head-pose frame, for a head at ``head_pose``."""
    return head_pose @ default_head_to_camera_transform()


def _pixel(
    point: npt.NDArray[np.float64], head_pose: npt.NDArray[np.float64]
) -> tuple[float, float, float]:
    """A point of the head-pose frame through the sim camera's pinhole, from a head at
    ``head_pose``: its pixel and its depth along the optical axis."""
    camera = _camera_pose(head_pose)
    x, y, z = camera[:3, :3].T @ (point - camera[:3, 3])
    K = SIM_CAMERA.K
    return (K[0, 0] * x / z + K[0, 2], K[1, 1] * y / z + K[1, 2], float(z))


def _seen(
    point: tuple[float, float, float],
    head_pose: npt.NDArray[np.float64],
    *,
    box_height_m: float = FACE_BOX_HEIGHT_M,
    **angles: float | None,
) -> Face:
    """The ``Face`` a detector would report for a face ``box_height_m`` tall at ``point``,
    seen from a head at ``head_pose``."""
    width, height = SIM_CAMERA.size
    u, v, depth = _pixel(np.array(point), head_pose)
    box_px = SIM_CAMERA.K[1, 1] * box_height_m / depth
    return Face(
        x=u / (width - 1) * 2.0 - 1.0,
        y=v / (height - 1) * 2.0 - 1.0,
        roll=angles.get("roll"),
        size=box_px / height,
        track_id=7,
        bbox=(u - 0.4 * box_px, v - 0.5 * box_px, 0.8 * box_px, box_px),
        pitch=angles.get("pitch"),
        yaw=angles.get("yaw"),
    )


def _axes_in_camera(
    marker: FaceMarker, head_pose: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64]:
    """The marker's right, up and normal (columns) in the camera's frame."""
    w, x, y, z = marker.quat
    world = Rotation.from_quat([x, y, z, w]).as_matrix()
    return _camera_pose(head_pose)[:3, :3].T @ world


@pytest.mark.parametrize(
    "point", [(0.45, 0.0, 0.05), (0.35, 0.15, 0.10), (0.60, -0.15, -0.02)]
)
def test_a_marker_lands_where_the_face_is(point: tuple[float, float, float]) -> None:
    """A face of the assumed height comes back at its own position: on the ray of its
    pixel, at the depth its apparent size implies."""
    head = np.eye(4)
    marker = face_marker(_seen(point, head), SIM_CAMERA, head)
    assert marker is not None
    assert marker.pos == pytest.approx(point, abs=1e-6)
    assert marker.size == (FACE_MARKER_ASPECT * FACE_BOX_HEIGHT_M, FACE_BOX_HEIGHT_M)
    assert marker.label == "7" and not marker.followed
    # It reprojects onto the face's pixel.
    face = _seen(point, head)
    u, v, _ = _pixel(np.array(marker.pos), head)
    width, height = SIM_CAMERA.size
    assert u / (width - 1) * 2.0 - 1.0 == pytest.approx(face.x, abs=1e-6)
    assert v / (height - 1) * 2.0 - 1.0 == pytest.approx(face.y, abs=1e-6)


def test_the_distance_is_as_good_as_the_assumed_height() -> None:
    """The direction is the pixel's; the distance comes from the apparent size, so a
    face half the assumed height is placed twice as far along the same ray."""
    head, point = np.eye(4), np.array([0.45, 0.10, 0.05])
    small = _seen(tuple(point), head, box_height_m=FACE_BOX_HEIGHT_M / 2.0)
    marker = face_marker(small, SIM_CAMERA, head)
    assert marker is not None
    origin = _camera_pose(head)[:3, 3]
    assert np.array(marker.pos) - origin == pytest.approx(2.0 * (point - origin))
    # The caller's own height puts it back.
    exact = face_marker(small, SIM_CAMERA, head, box_height_m=FACE_BOX_HEIGHT_M / 2.0)
    assert exact is not None and exact.pos == pytest.approx(tuple(point), abs=1e-6)


def test_a_marker_faces_the_camera_and_follows_the_reported_angles() -> None:
    """With no angle a marker's normal points back at the camera, its up along the
    camera's. The report's angles turn it with the report's own signs: roll clockwise in
    the image, yaw toward the image's right, pitch down."""
    head, point = np.eye(4), (0.45, 0.10, 0.05)
    plain = face_marker(_seen(point, head), SIM_CAMERA, head)
    assert plain is not None
    axes = _axes_in_camera(plain, head)
    camera = _camera_pose(head)
    to_camera = camera[:3, :3].T @ (camera[:3, 3] - np.array(plain.pos))
    assert axes[:, 2] == pytest.approx(to_camera / np.linalg.norm(to_camera))
    assert axes[1, 1] < -0.9  # up is the image's up (the camera's y points down)

    rolled = face_marker(_seen(point, head, roll=0.3), SIM_CAMERA, head)
    assert rolled is not None
    # A turn is only a turn: the marker stays the same object, at the same place.
    assert rolled.size == plain.size and rolled.pos == pytest.approx(plain.pos)
    # The eye line — the marker's width axis — as the image shows it: two points of it
    # projected through the pinhole. Its tilt is the reported roll (y down: clockwise).
    world = Rotation.from_quat([*rolled.quat[1:], rolled.quat[0]]).as_matrix()
    centre = np.array(rolled.pos)
    u0, v0, _ = _pixel(centre - 0.05 * world[:, 0], head)
    u1, v1, _ = _pixel(centre + 0.05 * world[:, 0], head)
    assert np.arctan2(v1 - v0, u1 - u0) == pytest.approx(0.3, abs=0.03)

    yawed = face_marker(_seen(point, head, yaw=0.4), SIM_CAMERA, head)
    assert yawed is not None
    assert (
        _axes_in_camera(yawed, head)[0, 2] - axes[0, 2]
    ) > 0.3  # toward the image's right
    pitched = face_marker(_seen(point, head, pitch=0.4), SIM_CAMERA, head)
    assert pitched is not None
    assert (_axes_in_camera(pitched, head)[1, 2] - axes[1, 2]) > 0.3  # down


def test_a_face_seen_from_a_turned_head_lands_where_it_stands() -> None:
    """The head pose of the frame carries the face into the world: the same face seen
    from a head turned 25 degrees is placed where it is seen from the neutral head."""
    point = (0.45, 0.10, 0.05)
    turned = _turned(25.0)
    marker = face_marker(_seen(point, turned), SIM_CAMERA, turned)
    assert marker is not None and marker.pos == pytest.approx(point, abs=1e-6)
    # Placed with the wrong pose — the neutral one — it swings with the head instead.
    wrong = face_marker(_seen(point, turned), SIM_CAMERA, np.eye(4))
    assert wrong is not None
    assert np.linalg.norm(np.array(wrong.pos) - np.array(point)) > 0.1


def test_a_fixed_camera_places_a_face_from_the_neutral_pose() -> None:
    """A webcam does not turn with the head: whatever the head does, the frame's pose
    is the neutral one, and the face is placed in front of the robot at rest."""
    fixed = CameraModel.for_sim(SimCameraSettings(source="webcam", hfov_deg=70.0))
    face = Face(x=0.2, y=0.0, roll=None, size=0.3)
    report = FaceReport(faces=(face,), ts=5.0, source="yunet", active=True)
    history = lambda: (np.array([5.0]), _turned(40.0)[np.newaxis])
    pose = frame_head_pose(report, fixed, history, 0.2, 5.0)
    marker = face_marker(face, fixed, pose)
    at_rest = face_marker(face, fixed, np.eye(4))
    assert marker is not None and at_rest is not None
    assert marker.pos == pytest.approx(at_rest.pos)
    assert marker.pos[0] > 0.2 and marker.pos[1] < 0.0  # ahead, to the image's right


def test_a_marker_is_always_the_same_object() -> None:
    """The marker's size never follows the detector's box — an upright box grows and
    squares up around a tilted face — only its distance follows the face's size."""
    face = _seen((0.45, 0.0, 0.05), np.eye(4))
    assert face_marker(replace(face, size=0.0), SIM_CAMERA, np.eye(4)) is None
    usual = face_marker(face, SIM_CAMERA, np.eye(4))
    square = face_marker(
        replace(face, bbox=(600.0, 300.0, 120.0, 120.0)), SIM_CAMERA, np.eye(4)
    )
    boxless = face_marker(
        replace(face, bbox=(0.0, 0.0, 0.0, 0.0), track_id=0),
        SIM_CAMERA,
        np.eye(4),
        followed=True,
    )
    assert usual is not None and square is not None and boxless is not None
    assert usual.size == square.size == boxless.size
    assert boxless.followed and boxless.label is None
    # A face twice as big in the image is the same marker, half as far.
    near = face_marker(replace(face, size=2.0 * face.size), SIM_CAMERA, np.eye(4))
    assert near is not None and near.size == usual.size
    origin = _camera_pose(np.eye(4))[:3, 3]
    assert np.array(near.pos) - origin == pytest.approx(
        (np.array(usual.pos) - origin) / 2.0
    )


def test_a_marker_round_trips_through_json_and_refuses_bad_values() -> None:
    marker = FaceMarker(
        pos=(0.4, 0.1, 0.0), quat=(1.0, 0.0, 0.0, 0.0), size=(0.16, 0.2), label="3"
    )
    assert FaceMarker.from_json(marker.to_json()) == marker
    doubled = {**marker.to_json(), "quat": [2.0, 0.0, 0.0, 0.0]}
    assert FaceMarker.from_json(doubled).quat == (1.0, 0.0, 0.0, 0.0)
    for bad in (
        {**marker.to_json(), "colour": "red"},
        {**marker.to_json(), "pos": [0.0, 0.0]},
        {**marker.to_json(), "pos": [0.0, 0.0, float("nan")]},
        {**marker.to_json(), "quat": [0.0, 0.0, 0.0, 0.0]},
        {**marker.to_json(), "size": [0.2, 0.0]},
        {**marker.to_json(), "followed": "yes"},
        {**marker.to_json(), "label": 3},
        ["not", "an", "object"],
    ):
        with pytest.raises((TypeError, ValueError)):
            FaceMarker.from_json(bad)


# --- the scene layer and its views (on a real MuJoCo scene) -----------------------------


@pytest.fixture
def mujoco() -> Any:
    return pytest.importorskip("mujoco", reason="sim extra (mujoco) not installed")


@pytest.fixture
def robot(mujoco: Any) -> tuple[Any, Any]:
    """Upstream's robot in its empty scene: the model and its data, forwarded."""
    import reachy_mini

    scene = (
        Path(reachy_mini.__file__).parent
        / "descriptions/reachy_mini/mjcf/scenes/empty.xml"
    )
    model = mujoco.MjModel.from_xml_path(str(scene))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    return model, data


class _SceneHandle:
    """Stands in for `mujoco.viewer.Handle` as the scene layer uses it: a real
    `MjvScene` as its user scene, and a lock that records it was held."""

    def __init__(self, mujoco: Any, model: Any, maxgeom: int = 20) -> None:
        self.user_scn = mujoco.MjvScene(model, maxgeom=maxgeom)
        self.running = True
        self.locked = 0
        self._lock = threading.RLock()

    def is_running(self) -> bool:
        return self.running

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            self.locked += 1
            yield


class _Geoms:
    def __init__(self, geoms: list[SceneGeom]) -> None:
        self.geoms_now = geoms

    def geoms(self) -> list[SceneGeom]:
        return list(self.geoms_now)


_LINE = SceneGeom(
    kind="line", rgba=(0.0, 0.0, 1.0, 1.0), a=(0.0, 0.0, 0.2), b=(1.0, 0.0, 0.2)
)
_BLOB = SceneGeom(
    kind="ellipsoid",
    rgba=(0.0, 1.0, 0.0, 0.6),
    a=(0.45, 0.1, 0.25),
    size=(0.08, 0.1, 0.005),
    label="3",
)


def test_the_layer_writes_its_views_geoms_into_the_user_scene(
    mujoco: Any, robot: tuple[Any, Any]
) -> None:
    """Each tick the layer writes every view's geoms into the viewer's user scene under
    the handle's lock, follows what the views return, and clears the scene on stop."""
    model, _ = robot
    handle = _SceneHandle(mujoco, model)
    first, second = _Geoms([_LINE]), _Geoms([_BLOB])
    layer = SceneLayer([first, second], hz=100.0)
    layer.attach(handle)
    try:
        assert _wait(lambda: handle.user_scn.ngeom == 2)
        assert handle.locked > 0
        with handle.lock():  # the layer rewrites the scene every tick
            line, blob = handle.user_scn.geoms[0], handle.user_scn.geoms[1]
            assert line.type == mujoco.mjtGeom.mjGEOM_LINE
            assert blob.type == mujoco.mjtGeom.mjGEOM_ELLIPSOID
            assert tuple(blob.pos) == pytest.approx(_BLOB.a)
            assert tuple(blob.size) == pytest.approx(_BLOB.size)
            assert tuple(blob.rgba) == pytest.approx(_BLOB.rgba)
            assert blob.label == "3" and line.label == ""
            # The line runs from a to b: a connector starts at a, its z axis along the
            # segment, its length the segment's.
            assert tuple(line.pos) == pytest.approx(_LINE.a, abs=1e-6)
            along = np.array(line.mat).reshape(3, 3)[:, 2] * line.size[2]
            assert along == pytest.approx((1.0, 0.0, 0.0), abs=1e-6)
        second.geoms_now = []
        assert _wait(lambda: handle.user_scn.ngeom == 1)
    finally:
        layer.stop()
    assert handle.user_scn.ngeom == 0 and not layer.drawing
    layer.stop()  # idempotent


def test_the_layer_drops_what_the_user_scene_cannot_hold(
    mujoco: Any, robot: tuple[Any, Any], caplog: pytest.LogCaptureFixture
) -> None:
    model, _ = robot
    handle = _SceneHandle(mujoco, model, maxgeom=2)
    layer = SceneLayer([_Geoms([_LINE, _BLOB, _BLOB])], hz=100.0)
    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.sim_displays"):
        layer.attach(handle)
        try:
            assert _wait(lambda: handle.user_scn.ngeom == 2)
            time.sleep(0.1)  # more ticks: still one warning
        finally:
            layer.stop()
    warnings = [r for r in caplog.records if "user scene of 2" in r.getMessage()]
    assert len(warnings) == 1


def test_the_layer_warns_once_on_a_viewer_without_a_user_scene(
    caplog: pytest.LogCaptureFixture,
) -> None:
    layer = SceneLayer([_Geoms([_LINE])], mujoco_module=SimpleNamespace())
    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.sim_displays"):
        layer.attach(SimpleNamespace(user_scn=None, is_running=lambda: True))
    assert not layer.drawing
    assert len([r for r in caplog.records if "no user scene" in r.getMessage()]) == 1
    layer.stop()  # harmless


def test_the_robots_gaze_is_the_eye_cameras_axis_and_turns_with_the_head(
    mujoco: Any, robot: tuple[Any, Any]
) -> None:
    """The line starts at the eye camera and runs along its optical axis — world +x at
    the neutral head — and it is read from the simulated head, so it turns when the
    head does."""
    model, data = robot
    view = RobotGazeView(model, data)
    (line,) = view.geoms()
    camera = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, "eye_camera")
    assert line.kind == "line"
    assert line.a == pytest.approx(tuple(data.cam_xpos[camera]))
    assert np.array(line.b) - np.array(line.a) == pytest.approx(
        (ROBOT_GAZE_LENGTH_M, 0.0, 0.0), abs=1e-3
    )
    # Turn the robot under the camera: the line turns with it.
    yaw = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, "yaw_body")
    data.qpos[model.jnt_qposadr[yaw]] = np.radians(30.0)
    mujoco.mj_forward(model, data)
    (turned,) = view.geoms()
    direction = np.array(turned.b) - np.array(turned.a)
    assert np.degrees(np.arctan2(direction[1], direction[0])) == pytest.approx(
        30.0, abs=0.5
    )
    assert turned.a == pytest.approx(tuple(data.cam_xpos[camera]))


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _markers_view(clock: _Clock) -> FaceMarkersView:
    return FaceMarkersView(lambda: np.array([0.0, 0.0, 0.177]), clock=clock)


def test_face_markers_are_drawn_in_the_world_until_they_go_stale() -> None:
    """A marker arrives in the head-pose frame and is drawn in MuJoCo's world — the
    frame offset added — as an ellipsoid of its size, thin along its normal, in the
    followed colour or the other; a set the bridge stopped refreshing is not drawn."""
    clock = _Clock()
    view = _markers_view(clock)
    assert view.geoms() == [] and view.state() == {"age_s": None, "markers": []}
    quarter = (np.sqrt(0.5), 0.0, 0.0, np.sqrt(0.5))  # 90 degrees about z
    followed = FaceMarker(
        pos=(0.45, 0.1, 0.05), quat=quarter, size=(0.16, 0.2), followed=True, label="3"
    )
    other = FaceMarker(pos=(0.6, -0.1, 0.0), quat=(1.0, 0.0, 0.0, 0.0), size=(0.1, 0.2))
    view.set_markers([followed, other])
    first, second = view.geoms()
    assert first.kind == "ellipsoid" and first.label == "3" and second.label == ""
    assert first.a == pytest.approx((0.45, 0.1, 0.227))
    assert first.size == pytest.approx((0.08, 0.1, FACE_MARKER_THICKNESS_M / 2.0))
    assert first.rgba == sim_displays.FOLLOWED_RGBA
    assert second.rgba == sim_displays.FACE_RGBA
    # The marker's right axis (the first column) is the quaternion's: +y after a
    # quarter turn about z.
    assert np.array(first.mat).reshape(3, 3)[:, 0] == pytest.approx((0.0, 1.0, 0.0))
    clock.now += 0.3
    state = view.state()
    assert state["age_s"] == pytest.approx(0.3)
    assert state["markers"][0]["world_pos"] == pytest.approx([0.45, 0.1, 0.227])
    assert state["markers"][0]["pos"] == pytest.approx([0.45, 0.1, 0.05])
    assert len(view.geoms()) == 2
    clock.now += 0.3  # 0.6 s old: stale
    assert view.geoms() == []
    assert len(view.state()["markers"]) == 2  # still readable, with its age


def test_the_frame_offset_is_measured_on_the_backend(
    mujoco: Any, robot: tuple[Any, Any]
) -> None:
    """The MuJoCo world and the head-pose frame differ by the shift upstream's backend
    applies to the pose it reports; the view measures it on the backend."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend

    model, data = robot
    site = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "head")
    backend = SimpleNamespace(model=model, data=data, head_site_id=site)
    backend.get_mj_present_head_pose = lambda: MujocoBackend.get_mj_present_head_pose(
        backend  # type: ignore[arg-type]
    )
    view = FaceMarkersView.for_backend(backend)
    marker = FaceMarker(pos=(0.4, 0.1, 0.0), quat=(1.0, 0.0, 0.0, 0.0), size=(0.1, 0.2))
    assert view.world_pos(marker) == pytest.approx((0.4, 0.1, 0.177))


# --- the displays router ----------------------------------------------------------------


def test_the_router_stores_validates_and_returns_the_face_markers() -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    clock = _Clock()
    views: list[FaceMarkersView] = []
    app = fastapi.FastAPI()
    app.include_router(
        build_router(lambda: views[-1] if views else None),
        prefix=sim_displays.DISPLAYS_ROUTE_PREFIX,
    )
    http = testclient.TestClient(app)
    marker = FaceMarker(
        pos=(0.45, 0.1, 0.05), quat=(1.0, 0.0, 0.0, 0.0), size=(0.16, 0.2), label="3"
    )
    body = {"markers": [marker.to_json()]}
    # Before the backend is built there is nowhere to put them.
    assert http.put("/api/sim/displays/face_markers", json=body).status_code == 503
    views.append(_markers_view(clock))
    assert http.get("/api/sim/displays/face_markers").json() == {
        "age_s": None,
        "markers": [],
    }
    response = http.put("/api/sim/displays/face_markers", json=body)
    assert response.status_code == 200 and response.json() == {"markers": 1}
    clock.now += 0.25
    stored = http.get("/api/sim/displays/face_markers").json()
    assert stored["age_s"] == pytest.approx(0.25)
    assert stored["markers"][0]["label"] == "3"
    assert stored["markers"][0]["world_pos"] == pytest.approx([0.45, 0.1, 0.227])
    assert len(views[-1].geoms()) == 1
    for bad in (
        {"markers": [{**marker.to_json(), "colour": "red"}]},
        {"markers": [{**marker.to_json(), "quat": [0, 0, 0, 0]}]},
        {"markers": "none"},
        {"faces": []},
    ):
        assert http.put("/api/sim/displays/face_markers", json=bad).status_code == 400
    assert len(views[-1].geoms()) == 1  # a refused set leaves the stored one
    assert http.put("/api/sim/displays/face_markers", json={"markers": []}).json() == {
        "markers": 0
    }
    assert views[-1].geoms() == []


# --- the face markers: the bridge's publisher -------------------------------------------


class _Sent:
    """Stands in for the daemon's route: records each set the publisher puts."""

    def __init__(self) -> None:
        self.sets: list[list[FaceMarker]] = []
        self.answer: Callable[[], bool] = lambda: True

    def __call__(self, url: str, markers: Any) -> bool:
        assert url == "http://daemon/api/sim/displays/face_markers"
        self.sets.append(list(markers))
        return self.answer()


def _publisher(
    sent: _Sent,
    camera: CameraModel = SIM_CAMERA,
) -> tuple[FaceMarkerPublisher, Observable[FaceReport], Observable[HeadTrackingReport]]:
    faces: Observable[FaceReport] = Observable(FaceReport.inactive("custom"))
    tracking: Observable[HeadTrackingReport] = Observable(HeadTrackingReport.inactive())
    publisher = FaceMarkerPublisher(
        faces,
        tracking,
        camera=camera,
        history=lambda: (np.array([0.0]), np.eye(4)[np.newaxis]),
        delay=lambda: 0.2,
        url="http://daemon/api/sim/displays/face_markers",
        send=sent,
        poll_hz=200.0,
    )
    return publisher, faces, tracking


def _two_people(ts: float) -> FaceReport:
    head = np.eye(4)
    near = replace(_seen((0.35, 0.10, 0.05), head), track_id=1)
    far = replace(_seen((0.60, -0.15, 0.05), head), track_id=2)
    return FaceReport(faces=(near, far), ts=ts, source="custom", active=True)


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


def test_the_publisher_sends_one_set_per_new_report() -> None:
    """The publisher polls the report — a moving face updates it without publishing —
    and sends each new one once: every face placed, the followed one marked; an
    inactive report is one empty set."""

    async def run() -> list[list[FaceMarker]]:
        sent = _Sent()
        publisher, faces, tracking = _publisher(sent)
        await publisher.start()
        try:
            await _until(lambda: len(sent.sets) == 1)  # the inactive report: empty
            tracking.update(
                HeadTrackingReport(
                    active=True, focus=False, attention="engaged", track_id=2, ts=1.0
                )
            )
            faces.update(_two_people(1.0))  # an update: no subscriber is woken
            await _until(lambda: len(sent.sets) == 2)
            await asyncio.sleep(0.05)  # the same report again and again: nothing more
            assert len(sent.sets) == 2
            faces.update(_two_people(1.1))
            await _until(lambda: len(sent.sets) == 3)
            faces.set(FaceReport.inactive("custom"))
            await _until(lambda: len(sent.sets) == 4)
        finally:
            await publisher.stop()
        assert not publisher.running
        return sent.sets

    empty, people, again, inactive = asyncio.run(run())
    assert empty == [] and inactive == []
    near, far = people
    assert near.pos == pytest.approx((0.35, 0.10, 0.05), abs=1e-6)
    assert far.pos == pytest.approx((0.60, -0.15, 0.05), abs=1e-6)
    assert (near.label, near.followed) == ("1", False)
    assert (far.label, far.followed) == ("2", True)
    assert again == people


def test_the_publisher_places_faces_the_same_way_for_every_camera() -> None:
    """One ratio turns a face's size into a distance, whatever the camera: the same
    face is the same marker, at the same depth, in front of a webcam as in front of
    the rendered eye camera with the same field of view."""

    async def run(camera: CameraModel) -> FaceMarker:
        sent = _Sent()
        publisher, faces, _ = _publisher(sent, camera)
        await publisher.start()
        try:
            face = Face(x=0.0, y=0.0, roll=None, size=0.3, track_id=1)
            faces.update(FaceReport(faces=(face,), ts=1.0, source="x", active=True))
            await _until(lambda: bool(sent.sets and sent.sets[-1]))
        finally:
            await publisher.stop()
        return sent.sets[-1][0]

    eye = asyncio.run(run(SIM_CAMERA))
    hfov = np.degrees(2.0 * np.arctan(SIM_CAMERA.size[0] / 2.0 / SIM_CAMERA.K[0, 0]))
    webcam = CameraModel.for_sim(SimCameraSettings(source="webcam", hfov_deg=hfov))
    person = asyncio.run(run(webcam))
    assert (
        eye.size
        == person.size
        == (
            FACE_MARKER_ASPECT * FACE_BOX_HEIGHT_M,
            FACE_BOX_HEIGHT_M,
        )
    )
    assert person.pos == pytest.approx(eye.pos)


def test_the_publisher_stops_on_a_daemon_without_the_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A daemon started without `--sim-display face_markers` answers 404: one warning
    naming the flag, and nothing more is sent."""

    async def run() -> int:
        sent = _Sent()
        sent.answer = lambda: False
        publisher, faces, _ = _publisher(sent)
        await publisher.start()
        await _until(lambda: not publisher.running)
        faces.update(_two_people(2.0))
        await asyncio.sleep(0.05)
        await publisher.stop()
        return len(sent.sets)

    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.sim_displays"):
        assert asyncio.run(run()) == 1
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "--sim-display face_markers" in warnings[0]


def test_the_publisher_keeps_going_through_a_failing_daemon(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A daemon that cannot be reached is one warning, and the next report is sent
    again: a daemon restarting under the session recovers."""

    async def run() -> list[list[FaceMarker]]:
        sent = _Sent()

        def unreachable() -> bool:
            raise ConnectionRefusedError("no daemon")

        sent.answer = unreachable
        publisher, faces, _ = _publisher(sent)
        await publisher.start()
        try:
            await _until(lambda: len(sent.sets) == 1)
            faces.update(_two_people(3.0))
            await _until(lambda: len(sent.sets) == 2)
            sent.answer = lambda: True
            faces.update(_two_people(3.1))
            await _until(lambda: len(sent.sets) == 3)
            assert publisher.running
        finally:
            await publisher.stop()
        return sent.sets

    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.sim_displays"):
        sets = asyncio.run(run())
    assert len(sets[2]) == 2
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_the_publisher_stops_mid_request() -> None:
    """Stopping while a request is in flight ends the publisher at once — the request
    is left to its thread — and it can be started again."""

    async def run() -> tuple[float, int]:
        sent = _Sent()
        entered, release = threading.Event(), threading.Event()

        def slow() -> bool:
            entered.set()
            release.wait(5.0)
            return True

        sent.answer = slow
        publisher, faces, _ = _publisher(sent)
        await publisher.start()
        await _until(entered.is_set)
        started = time.monotonic()
        await publisher.stop()
        took = time.monotonic() - started
        release.set()
        assert not publisher.running
        # A fresh start sends again.
        sent.answer = lambda: True
        await publisher.start()
        faces.update(_two_people(4.0))
        await _until(lambda: len(sent.sets[-1]) == 2)
        await publisher.stop()
        return took, len(sent.sets[-1])

    took, people = asyncio.run(run())
    assert took < 0.5
    assert people == 2
