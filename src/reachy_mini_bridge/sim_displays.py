"""The sim displays: what the MuJoCo viewer shows besides the scene (specs/daemon/sim_displays.md).

One ``--sim-display`` / ``daemon.sim_displays`` switch each, all drawn for the person
watching the viewer and none of them a camera — the stream clients read is untouched:

- ``camera_overlay`` draws the camera stream — the webcam, or the rendered eye camera — as
  a picture in the top-right corner of the viewer window, through the passive viewer's
  ``set_images`` (MuJoCo 3.3.1+; an older MuJoCo gets one warning and no picture).
- ``robot_gaze`` draws the eye camera's optical axis — the axis the head tracker aligns
  with the followed face — as a line in the 3D scene.
- ``face_markers`` draws an ellipsoid per face the bridge detects, at the pose the bridge
  estimates (``face_marker``): the bridge's ``FaceMarkerPublisher`` sends them to the
  daemon's ``/api/sim/displays/face_markers`` route.

The last two are geoms in the viewer handle's ``user_scn``, written by one ``SceneLayer``:
the eye camera's offscreen renderer never sees them, so they are in the viewer window and
never in the camera stream.

The sim daemon launcher (``sim_daemon.py``) builds the displays that are on, feeds the
overlay from its camera source, hands them the viewer handle (``capture_viewer``) and
mounts the displays router.

Importing this module pulls in neither ``mujoco`` nor ``fastapi``; the daemon-side pieces
import them when they run.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

if TYPE_CHECKING:
    import numpy.typing as npt

    from .face_detection import Face, FaceReport
    from .head_tracking import CameraModel, HeadTrackingReport, PoseHistory
    from .observable import Observable

__all__ = [
    "DISPLAYS_ROUTE_PREFIX",
    "FACE_BOX_HEIGHT_M",
    "FACE_MARKERS_STALE_S",
    "FACE_MARKER_ASPECT",
    "FACE_MARKER_THICKNESS_M",
    "OVERLAY_FRACTION",
    "OVERLAY_MARGIN",
    "OVERLAY_SOURCE_SIZE",
    "ROBOT_GAZE_LENGTH_M",
    "SCENE_LAYER_HZ",
    "FaceMarker",
    "FaceMarkerPublisher",
    "FaceMarkersView",
    "RobotGazeView",
    "SceneGeom",
    "SceneLayer",
    "ViewerDisplay",
    "ViewerOverlay",
    "build_router",
    "capture_viewer",
    "face_marker",
    "face_markers_url",
    "fetch_face_markers",
    "overlay_rect",
    "resample_nearest",
]

_logger = logging.getLogger(__name__)

# The camera overlay (specs/daemon/sim_displays.md "Camera overlay"): the camera stream drawn in
# the top-right corner of the MuJoCo viewer, this fraction of the view's width, inset by
# this fraction of it; frames reach the overlay at OVERLAY_SOURCE_SIZE (the relay's second
# branch scales to it, the renderer tap resamples to it) and are resampled to the
# rectangle. A viewport whose rectangle would have a side under _OVERLAY_MIN_SIDE pixels
# draws nothing.
OVERLAY_FRACTION = 0.25
OVERLAY_MARGIN = 0.02
OVERLAY_SOURCE_SIZE = (640, 360)
_OVERLAY_MIN_SIDE = 32
# A light frame around the picture, so it stands out from a scene of the same colours
# (the eye camera's view of the empty scene is the viewer's own skybox and floor).
_OVERLAY_BORDER_PX = 2
_OVERLAY_BORDER_VALUE = 230

# The scene layer (specs/daemon/sim_displays.md "The scene layer"): how often the displays
# drawn in the 3D scene are rewritten into the viewer's user scene.
SCENE_LAYER_HZ = 30.0
# The robot's gaze: the eye camera's optical axis, this long (past the test scene's
# portraits, 0.30-0.70 m away), as a line this wide.
ROBOT_GAZE_LENGTH_M = 1.0
ROBOT_GAZE_WIDTH_PX = 3.0
ROBOT_GAZE_RGBA = (0.2, 0.5, 1.0, 1.0)
_EYE_CAMERA = "eye_camera"
_HEAD_SITE = "head"
# The face markers: an ellipsoid this thick, the followed face in one colour and the
# others in another; a set older than this on the daemon's clock is not drawn.
FACE_MARKER_THICKNESS_M = 0.01
FOLLOWED_RGBA = (0.2, 0.9, 0.3, 0.6)
FACE_RGBA = (1.0, 0.85, 0.2, 0.6)
FACE_MARKERS_STALE_S = 0.5
# The physical height a detected face's box is taken to have: the one ratio that turns a
# face's apparent size into a distance, whatever the camera (specs/daemon/sim_displays.md
# "From a face to a marker"). A heuristic for a debug view, calibrated on the test
# scene's portrait: its box measured 0.124, 0.124 and 0.129 m at 0.35, 0.45 and 0.60 m
# on the viewer sim (2026-10-01). A person's face is bigger, so a person in front of a
# webcam is drawn nearer than they stand.
FACE_BOX_HEIGHT_M = 0.125
# A marker is always the same object: this wide for its height, whatever the detector's
# box looks like (an upright box grows and squares up around a tilted face).
FACE_MARKER_ASPECT = 0.8
# Where a display that takes data from a client mounts its router.
DISPLAYS_ROUTE_PREFIX = "/api/sim/displays"
_FACE_MARKERS_PATH = "/face_markers"
# The bridge's publisher: how often it looks at the face report (the detection loop's own
# poll rate — a new report is sent within a third of a frame period), and how long one
# request to the daemon may take.
_PUBLISH_POLL_HZ = 30.0
_PUBLISH_TIMEOUT_S = 0.5
# How long stop() waits for the thread: long enough for an idle one to notice, well
# short of a request held by the daemon.
_PUBLISH_STOP_JOIN_S = 0.1


# --- the camera overlay -----------------------------------------------------------------


def overlay_rect(
    viewport: tuple[int, int, int, int],
    *,
    fraction: float = OVERLAY_FRACTION,
    margin: float = OVERLAY_MARGIN,
) -> tuple[int, int, int, int] | None:
    """Where the camera overlay goes in a viewer ``viewport`` (``left, bottom, width,
    height`` in framebuffer pixels, origin bottom-left like MuJoCo's ``MjrRect``): a 16:9
    rectangle ``fraction`` of the viewport's width, both sides even, in the top-right
    corner inset by ``margin`` of the viewport's width. ``None`` when the viewport is too
    small for it — nothing is drawn then."""
    left, bottom, view_width, view_height = viewport
    width = int(view_width * fraction) // 2 * 2
    height = int(width * OVERLAY_SOURCE_SIZE[1] / OVERLAY_SOURCE_SIZE[0]) // 2 * 2
    inset = int(view_width * margin)
    if (
        min(width, height) < _OVERLAY_MIN_SIDE
        or width + inset > view_width
        or height + inset > view_height
    ):
        return None
    return (
        left + view_width - width - inset,
        bottom + view_height - height - inset,
        width,
        height,
    )


def resample_nearest(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """``frame`` (``height x width x channels``) at ``size`` (``width, height``) by
    nearest-neighbour index arrays — the daemon has no OpenCV. A contiguous copy either
    way."""
    width, height = size
    source_height, source_width = frame.shape[:2]
    if (source_width, source_height) == (width, height):
        return np.array(frame, order="C")  # a copy: the caller may reuse its buffer
    # The first and last output pixels take the first and last source pixels, the rest
    # the nearest in between — so the corners are the corners whichever way the size
    # goes.
    rows = np.rint(np.arange(height) * (source_height - 1) / max(height - 1, 1))
    cols = np.rint(np.arange(width) * (source_width - 1) / max(width - 1, 1))
    return np.ascontiguousarray(frame[rows.astype(int)[:, None], cols.astype(int)])


def _mujoco_version() -> str:
    try:
        return str(importlib.import_module("mujoco").__version__)
    except Exception:  # noqa: BLE001 - no mujoco at all
        return "unknown"


class ViewerOverlay:
    """The camera stream drawn over the MuJoCo viewer (specs/daemon/sim_displays.md "Camera
    overlay").

    Frames arrive through ``show`` from whichever thread has them — the webcam relay's
    appsink, the eye-camera render thread — into a latest-frame slot, and a thread of
    the overlay's own draws them through the viewer handle's ``set_images``: a call that
    waits for the viewer's render thread, so it never runs on a feed thread or the
    physics loop, and a feed faster than the viewer only ever loses intermediate frames.
    The rectangle is recomputed from the handle's viewport for every frame, so a window
    resize keeps the picture in its corner. ``label`` puts one line of text at the top
    left of the view (the camera's name and size). A handle without ``set_images`` — a
    MuJoCo before 3.3.1 — is logged once at ``WARNING`` and everything else is a no-op.

    ``rect_factory`` and ``text_style`` default to MuJoCo's ``MjrRect`` and
    ``(mjFONTSCALE_100, mjGRID_TOPLEFT)``, imported when a handle is attached; the tests
    hand in their own and need no MuJoCo."""

    def __init__(
        self,
        *,
        rect_factory: Callable[[int, int, int, int], Any] | None = None,
        text_style: tuple[Any, Any] | None = None,
    ) -> None:
        self._rect_factory = rect_factory
        self._text_style = text_style
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._label: str | None = None
        self._new = threading.Event()
        self._stop = threading.Event()
        self._handle: Any = None
        self._thread: threading.Thread | None = None

    @property
    def drawing(self) -> bool:
        """Whether a viewer is attached and being drawn on."""
        return self._thread is not None

    def attach(self, handle: Any) -> None:
        """Start drawing on ``handle`` (a ``mujoco.viewer.Handle``)."""
        if not hasattr(handle, "set_images"):
            _logger.warning(
                "camera overlay: needs MuJoCo 3.3.1 or later (installed %s); not drawn",
                _mujoco_version(),
            )
            return
        rect_factory, text_style = self._rect_factory, self._text_style
        if rect_factory is None or text_style is None:
            mujoco: Any = importlib.import_module("mujoco")
            rect_factory = rect_factory or mujoco.MjrRect
            text_style = text_style or (
                mujoco.mjtFontScale.mjFONTSCALE_100,
                mujoco.mjtGridPos.mjGRID_TOPLEFT,
            )
        self._handle = handle
        self._stop.clear()
        _logger.info("camera overlay: drawing on the viewer")
        self._thread = threading.Thread(
            target=self._run,
            args=(handle, rect_factory, text_style),
            name="viewer-overlay",
            daemon=True,
        )
        self._thread.start()

    def show(self, frame: np.ndarray) -> None:
        """The latest frame (RGB ``uint8``, any size); replaces an undrawn one."""
        with self._lock:
            self._frame = frame
        self._new.set()

    def label(self, text: str) -> None:
        with self._lock:
            self._label = text
        self._new.set()

    def stop(self) -> None:
        """Stop drawing, and clear the overlay while the viewer is still up; idempotent,
        harmless before ``attach``."""
        self._stop.set()
        self._new.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        handle, self._handle = self._handle, None
        if handle is not None and handle.is_running():
            handle.clear_images()
            if hasattr(handle, "clear_texts"):
                handle.clear_texts()

    def _run(
        self,
        handle: Any,
        rect_factory: Callable[[int, int, int, int], Any],
        text_style: tuple[Any, Any],
    ) -> None:
        shown_label: str | None = None
        drawn = 0
        failed = 0
        while True:
            self._new.wait()
            if self._stop.is_set():
                return
            self._new.clear()
            with self._lock:
                frame, label = self._frame, self._label
            if not handle.is_running():
                continue
            try:
                if label is not None and label != shown_label:
                    if hasattr(handle, "set_texts"):
                        handle.set_texts((*text_style, label, ""))
                    shown_label = label
                if frame is None:
                    continue
                viewport = handle.viewport
                if viewport is None:
                    continue
                rect = overlay_rect(
                    (viewport.left, viewport.bottom, viewport.width, viewport.height)
                )
                if rect is None:
                    continue
                image = resample_nearest(frame, (rect[2], rect[3]))
                b = _OVERLAY_BORDER_PX
                image[:b], image[-b:] = _OVERLAY_BORDER_VALUE, _OVERLAY_BORDER_VALUE
                image[:, :b], image[:, -b:] = (
                    _OVERLAY_BORDER_VALUE,
                    _OVERLAY_BORDER_VALUE,
                )
                handle.set_images([(rect_factory(*rect), image)])
                drawn += 1
                if drawn == 1:
                    _logger.info(
                        "camera overlay: first frame drawn, %dx%d at (%d, %d) of a "
                        "%dx%d view",
                        rect[2],
                        rect[3],
                        rect[0],
                        rect[1],
                        viewport.width,
                        viewport.height,
                    )
            except Exception:  # the viewer going away under a draw — or a real fault
                failed += 1
                if failed == 1:
                    _logger.warning("camera overlay: a draw failed", exc_info=True)
                else:
                    _logger.debug("camera overlay: a draw failed", exc_info=True)


# --- the viewer handle --------------------------------------------------------------------


class ViewerDisplay(Protocol):
    """What ``capture_viewer`` needs of a display: it is handed the viewer handle, and
    stopped before the viewer closes."""

    def attach(self, handle: Any) -> None: ...
    def stop(self) -> None: ...


@contextmanager
def capture_viewer(
    displays: Sequence[ViewerDisplay], viewer_module: Any | None = None
) -> Iterator[None]:
    """While active, the viewer that upstream's ``run()`` launches is handed to every
    display in ``displays``, and its ``close`` stops them all first
    (specs/daemon/sim_displays.md "One viewer handle, every display"). Upstream keeps the
    handle as a local and closes it itself at the end of ``run()``, and a draw issued
    after the close could wait on a render thread that is gone — so the ordering lives on
    the handle. A process-local substitution of ``mujoco.viewer.launch_passive`` (looked
    up on the module at call time); restored on exit."""
    module: Any = (
        importlib.import_module("mujoco.viewer")
        if viewer_module is None
        else viewer_module
    )
    original = module.launch_passive

    def launch_passive(*args: Any, **kwargs: Any) -> Any:
        handle = original(*args, **kwargs)
        for display in displays:
            display.attach(handle)
        close = handle.close

        def close_after_displays() -> None:
            for display in displays:
                display.stop()
            close()

        handle.close = close_after_displays
        return handle

    module.launch_passive = launch_passive
    try:
        yield
    finally:
        module.launch_passive = original


# --- the face markers: from a face to a marker (pure) ---------------------------------


@dataclass(frozen=True)
class FaceMarker:
    """One face as the bridge places it (specs/daemon/sim_displays.md "What a marker is"):
    ``pos`` in metres and ``quat`` (``w, x, y, z``) in the head-pose frame — the frame of
    the poses the SDK reports — with the marker's local axes the face's right, up and
    normal; ``size`` its (width, height) in metres; ``followed`` whether the head
    follows it; ``label`` its track id as text."""

    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float]
    size: tuple[float, float]
    followed: bool = False
    label: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "pos": list(self.pos),
            "quat": list(self.quat),
            "size": list(self.size),
            "followed": self.followed,
            "label": self.label,
        }

    @classmethod
    def from_json(cls, data: Any) -> FaceMarker:
        """A marker from a route body; ``ValueError`` / ``TypeError`` on an unknown
        field or a malformed value. The quaternion is normalised (all-zero is refused)."""
        if not isinstance(data, dict):
            raise TypeError("a marker is a JSON object")
        unknown = set(data) - {"pos", "quat", "size", "followed", "label"}
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        pos = _numbers(data.get("pos"), 3, "pos")
        quat = _numbers(data.get("quat"), 4, "quat")
        size = _numbers(data.get("size"), 2, "size")
        norm = math.sqrt(sum(q * q for q in quat))
        if norm < 1e-9:
            raise ValueError("quat must not be all zeros")
        if min(size) <= 0.0:
            raise ValueError("size must be two positive numbers")
        followed = data.get("followed", False)
        if not isinstance(followed, bool):
            raise TypeError("followed must be a boolean")
        label = data.get("label")
        if label is not None and not isinstance(label, str):
            raise TypeError("label must be a string or null")
        return cls(
            pos=(pos[0], pos[1], pos[2]),
            quat=(quat[0] / norm, quat[1] / norm, quat[2] / norm, quat[3] / norm),
            size=(size[0], size[1]),
            followed=followed,
            label=label,
        )


def _numbers(value: Any, count: int, name: str) -> list[float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != count
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)
        or not all(math.isfinite(v) for v in value)
    ):
        raise ValueError(f"{name} must be {count} finite numbers")
    return [float(v) for v in value]


def _rotation(axis: npt.NDArray[np.float64], angle: float) -> npt.NDArray[np.float64]:
    """The rotation by ``angle`` about the unit ``axis`` (Rodrigues)."""
    x, y, z = axis
    cross = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + math.sin(angle) * cross + (1.0 - math.cos(angle)) * cross @ cross


def _quat_from_matrix(m: npt.NDArray[np.float64]) -> tuple[float, float, float, float]:
    """``(w, x, y, z)`` of a rotation matrix, with ``w >= 0``."""
    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0.0:
        k = math.sqrt(trace + 1.0) * 2.0
        w, x, y, z = (
            0.25 * k,
            (m[2, 1] - m[1, 2]) / k,
            (m[0, 2] - m[2, 0]) / k,
            (m[1, 0] - m[0, 1]) / k,
        )
    else:
        i = int(np.argmax([m[0, 0], m[1, 1], m[2, 2]]))
        j, k_ = (i + 1) % 3, (i + 2) % 3
        k = math.sqrt(max(m[i, i] - m[j, j] - m[k_, k_] + 1.0, 0.0)) * 2.0
        q = [0.0, 0.0, 0.0]
        q[i] = 0.25 * k
        q[j] = (m[j, i] + m[i, j]) / k
        q[k_] = (m[k_, i] + m[i, k_]) / k
        w = (m[k_, j] - m[j, k_]) / k
        x, y, z = q
    if w < 0.0:
        w, x, y, z = -w, -x, -y, -z
    return (float(w), float(x), float(y), float(z))


def face_marker(
    face: Face,
    camera: CameraModel,
    head_pose: npt.NDArray[np.float64],
    *,
    box_height_m: float = FACE_BOX_HEIGHT_M,
    followed: bool = False,
) -> FaceMarker | None:
    """Where one face of a report is, as the bridge places it (specs/daemon/sim_displays.md
    "From a face to a marker"): the face's pixel undistorted into a ray of the camera
    model, a depth from the face's apparent size (the pinhole's similar triangles on a
    box ``box_height_m`` tall), the orientation the report gives it — the marker itself
    is always the same size, only placed and turned — and ``head_pose``
    — the pose its frame was taken from — to carry it into the head-pose frame.
    ``None`` for a face without a size, which has no distance."""
    from reachy_mini.media.camera_utils import undistort_points
    from reachy_mini.vision.look_at import default_head_to_camera_transform

    width, height = camera.size
    if face.size <= 0.0:
        return None
    u = (face.x + 1.0) / 2.0 * (width - 1)
    v = (face.y + 1.0) / 2.0 * (height - 1)
    x_n, y_n = undistort_points(u, v, camera.K, camera.D)
    depth = float(camera.K[1, 1]) * box_height_m / (face.size * height)
    point = depth * np.array([x_n, y_n, 1.0])

    # The face's frame in the camera's (x right, y down, z forward): its normal back to
    # the camera centre, its up the camera's up made orthogonal to it, its right
    # completing a right-handed (right, up, normal).
    normal = -point / np.linalg.norm(point)
    up = np.array([0.0, -1.0, 0.0])
    up = up - float(up @ normal) * normal
    up /= np.linalg.norm(up)
    right = np.cross(up, normal)
    frame = np.column_stack([right, up, normal])
    # Turned in its own axes: yaw about its up (positive: the normal toward the image's
    # right), pitch about its right (positive: the normal down), roll about the line of
    # sight (positive: clockwise in the image).
    x_axis, y_axis, z_axis = np.eye(3)
    frame = (
        frame
        @ _rotation(y_axis, face.yaw or 0.0)
        @ _rotation(x_axis, face.pitch or 0.0)
        @ _rotation(z_axis, -(face.roll or 0.0))
    )

    to_world = (
        np.asarray(head_pose, dtype=np.float64) @ default_head_to_camera_transform()
    )
    position = to_world[:3, :3] @ point + to_world[:3, 3]
    return FaceMarker(
        pos=(float(position[0]), float(position[1]), float(position[2])),
        quat=_quat_from_matrix(to_world[:3, :3] @ frame),
        size=(box_height_m * FACE_MARKER_ASPECT, box_height_m),
        followed=followed,
        label=str(face.track_id) if face.track_id else None,
    )


# --- the scene layer: displays drawn in the 3D scene (daemon side) --------------------


@dataclass(frozen=True)
class SceneGeom:
    """One geom a display asks the scene layer to draw, in MuJoCo world coordinates: a
    ``line`` from ``a`` to ``b`` ``width`` pixels wide, or an ``ellipsoid`` at ``a`` with
    semi-axes ``size`` along the columns of ``mat``."""

    kind: str
    rgba: tuple[float, float, float, float]
    a: tuple[float, float, float]
    b: tuple[float, float, float] = (0.0, 0.0, 0.0)
    width: float = 1.0
    size: tuple[float, float, float] = (0.0, 0.0, 0.0)
    mat: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    label: str = ""


class SceneView(Protocol):
    """A display drawn in the 3D scene: the geoms it wants, each tick."""

    def geoms(self) -> list[SceneGeom]: ...


def _write_geom(mujoco: Any, target: Any, geom: SceneGeom) -> None:
    rgba = np.array(geom.rgba, dtype=np.float32)
    if geom.kind == "line":
        mujoco.mjv_initGeom(
            target,
            mujoco.mjtGeom.mjGEOM_LINE,
            np.zeros(3),
            np.zeros(3),
            np.eye(3).flatten(),
            rgba,
        )
        mujoco.mjv_connector(
            target,
            mujoco.mjtGeom.mjGEOM_LINE,
            geom.width,
            np.array(geom.a, dtype=np.float64),
            np.array(geom.b, dtype=np.float64),
        )
    else:
        mujoco.mjv_initGeom(
            target,
            mujoco.mjtGeom.mjGEOM_ELLIPSOID,
            np.array(geom.size, dtype=np.float64),
            np.array(geom.a, dtype=np.float64),
            np.array(geom.mat, dtype=np.float64),
            rgba,
        )
    target.label = geom.label


class SceneLayer:
    """The one owner of the viewer handle's ``user_scn`` (specs/daemon/sim_displays.md
    "The scene layer"): a thread that, ``hz`` times a second, asks each view for its
    geoms and writes them into the user scene under ``handle.lock()`` — which waits for
    the viewer, so it never runs on the physics loop or a request handler. ``stop``
    clears the scene while the viewer is still up. A handle without ``user_scn`` is
    logged once at ``WARNING`` and nothing is drawn.

    ``mujoco_module`` defaults to ``mujoco``, imported when a handle is attached."""

    def __init__(
        self,
        views: Sequence[SceneView],
        *,
        hz: float = SCENE_LAYER_HZ,
        mujoco_module: Any | None = None,
    ) -> None:
        self._views = list(views)
        self._period = 1.0 / hz
        self._mujoco = mujoco_module
        self._stop = threading.Event()
        self._handle: Any = None
        self._thread: threading.Thread | None = None

    @property
    def drawing(self) -> bool:
        """Whether a viewer is attached and being drawn on."""
        return self._thread is not None

    def attach(self, handle: Any) -> None:
        """Start drawing on ``handle`` (a ``mujoco.viewer.Handle``)."""
        if getattr(handle, "user_scn", None) is None:
            _logger.warning(
                "sim displays: this MuJoCo viewer (%s) has no user scene; the robot's "
                "gaze and the face markers are not drawn",
                _mujoco_version(),
            )
            return
        mujoco = self._mujoco or importlib.import_module("mujoco")
        self._handle = handle
        self._stop.clear()
        _logger.info("sim displays: drawing in the viewer's 3D scene")
        self._thread = threading.Thread(
            target=self._run, args=(handle, mujoco), name="scene-layer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop drawing, and clear the user scene while the viewer is still up;
        idempotent, harmless before ``attach``."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)
        handle, self._handle = self._handle, None
        if handle is not None and handle.is_running():
            with handle.lock():
                handle.user_scn.ngeom = 0

    def _run(self, handle: Any, mujoco: Any) -> None:
        failed = 0
        overflowed = False
        while not self._stop.wait(self._period):
            if not handle.is_running():
                continue
            try:
                geoms = [geom for view in self._views for geom in view.geoms()]
                with handle.lock():
                    scene = handle.user_scn
                    count = min(len(geoms), int(scene.maxgeom))
                    for index in range(count):
                        _write_geom(mujoco, scene.geoms[index], geoms[index])
                    scene.ngeom = count
                if len(geoms) > count and not overflowed:
                    overflowed = True
                    _logger.warning(
                        "sim displays: %d geoms for a user scene of %d; the rest "
                        "are not drawn",
                        len(geoms),
                        count,
                    )
            except Exception:  # the viewer going away under a draw — or a real fault
                failed += 1
                if failed == 1:
                    _logger.warning("sim displays: a draw failed", exc_info=True)
                else:
                    _logger.debug("sim displays: a draw failed", exc_info=True)


class RobotGazeView:
    """The robot's gaze (specs/daemon/sim_displays.md "The robot's gaze"): the eye
    camera's optical axis, read from the simulated head's ``eye_camera`` frame in
    ``data`` — so it turns with the head whatever the camera source."""

    def __init__(
        self, model: Any, data: Any, *, mujoco_module: Any | None = None
    ) -> None:
        mujoco = mujoco_module or importlib.import_module("mujoco")
        self._data = data
        self._camera = int(
            mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, _EYE_CAMERA)
        )
        if self._camera < 0:
            raise ValueError(f"the scene has no camera named {_EYE_CAMERA!r}")

    def geoms(self) -> list[SceneGeom]:
        origin = np.array(self._data.cam_xpos[self._camera], dtype=np.float64)
        # A MuJoCo camera looks along its frame's -z.
        forward = -np.array(self._data.cam_xmat[self._camera]).reshape(3, 3)[:, 2]
        end = origin + ROBOT_GAZE_LENGTH_M * forward
        return [
            SceneGeom(
                kind="line",
                rgba=ROBOT_GAZE_RGBA,
                a=(float(origin[0]), float(origin[1]), float(origin[2])),
                b=(float(end[0]), float(end[1]), float(end[2])),
                width=ROBOT_GAZE_WIDTH_PX,
            )
        ]


def _matrix_from_quat(
    quat: tuple[float, float, float, float],
) -> npt.NDArray[np.float64]:
    w, x, y, z = quat
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


class FaceMarkersView:
    """The face markers (specs/daemon/sim_displays.md "The face markers"), daemon side:
    the latest set the bridge sent, drawn until it goes stale.

    Markers arrive in the head-pose frame, the one every SDK client speaks. The MuJoCo
    world differs from it by a translation — upstream's MuJoCo backend reports the
    ``head`` site shifted down — which ``frame_offset`` measures on the backend itself
    (``site_xpos[head] - get_mj_present_head_pose()``) rather than hard-coding
    upstream's constant."""

    def __init__(
        self,
        frame_offset: Callable[[], npt.NDArray[np.float64]],
        *,
        clock: Callable[[], float] = time.monotonic,
        stale_s: float = FACE_MARKERS_STALE_S,
    ) -> None:
        self._frame_offset = frame_offset
        self._offset: npt.NDArray[np.float64] | None = None
        self._clock = clock
        self._stale_s = stale_s
        self._lock = threading.Lock()
        self._markers: tuple[FaceMarker, ...] = ()
        self._arrived: float | None = None

    @classmethod
    def for_backend(
        cls, backend: Any, *, mujoco_module: Any | None = None
    ) -> FaceMarkersView:
        """The view of a built ``MujocoBackend``: the offset between the pose it
        reports and its ``head`` site."""
        mujoco = mujoco_module or importlib.import_module("mujoco")
        site = int(
            mujoco.mj_name2id(backend.model, mujoco.mjtObj.mjOBJ_SITE, _HEAD_SITE)
        )
        if site < 0:
            raise ValueError(f"the scene has no site named {_HEAD_SITE!r}")

        def offset() -> npt.NDArray[np.float64]:
            reported = np.asarray(backend.get_mj_present_head_pose())[:3, 3]
            return np.array(backend.data.site_xpos[site], dtype=np.float64) - reported

        return cls(offset)

    def set_markers(self, markers: Sequence[FaceMarker]) -> None:
        with self._lock:
            self._markers = tuple(markers)
            self._arrived = self._clock()

    def world_pos(self, marker: FaceMarker) -> tuple[float, float, float]:
        """``marker.pos`` in MuJoCo world coordinates."""
        if self._offset is None:
            self._offset = np.asarray(self._frame_offset(), dtype=np.float64)
        x, y, z = np.array(marker.pos) + self._offset
        return (float(x), float(y), float(z))

    def state(self) -> dict[str, Any]:
        """What ``GET`` returns: the stored set, each marker with its ``world_pos``,
        and the time since it arrived (``None`` before the first)."""
        with self._lock:
            markers, arrived = self._markers, self._arrived
        return {
            "age_s": None if arrived is None else self._clock() - arrived,
            "markers": [
                {**m.to_json(), "world_pos": list(self.world_pos(m))} for m in markers
            ],
        }

    def geoms(self) -> list[SceneGeom]:
        with self._lock:
            markers, arrived = self._markers, self._arrived
        if arrived is None or self._clock() - arrived > self._stale_s:
            return []
        return [
            SceneGeom(
                kind="ellipsoid",
                rgba=FOLLOWED_RGBA if marker.followed else FACE_RGBA,
                a=self.world_pos(marker),
                size=(
                    marker.size[0] / 2.0,
                    marker.size[1] / 2.0,
                    FACE_MARKER_THICKNESS_M / 2.0,
                ),
                mat=tuple(float(v) for v in _matrix_from_quat(marker.quat).flatten()),
                label=marker.label or "",
            )
            for marker in markers
        ]


def build_router(view: Callable[[], FaceMarkersView | None]) -> Any:
    """A FastAPI ``APIRouter`` for the displays that take data from a client (mounted at
    ``/api/sim/displays``): ``PUT /face_markers`` replaces the set of face markers
    (``{"markers": [...]}``; 400 on an unknown field or a malformed value), ``GET
    /face_markers`` returns the stored set. ``view`` gives the backend's view, ``None``
    until the backend is built (503)."""
    from fastapi import APIRouter, Body, HTTPException

    router = APIRouter(tags=["sim-displays"])
    markers_body = Body(...)

    def current() -> FaceMarkersView:
        found = view()
        if found is None:
            raise HTTPException(503, "the sim backend is not built yet")
        return found

    @router.put("/face_markers")
    def put_face_markers(payload: dict[str, Any] = markers_body) -> dict[str, Any]:
        unknown = set(payload) - {"markers"}
        if unknown:
            raise HTTPException(400, f"unknown field(s): {', '.join(sorted(unknown))}")
        raw = payload.get("markers")
        if not isinstance(raw, list):
            raise HTTPException(400, "markers must be a list")
        try:
            markers = [FaceMarker.from_json(item) for item in raw]
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        current().set_markers(markers)
        return {"markers": len(markers)}

    @router.get("/face_markers")
    def get_face_markers() -> dict[str, Any]:
        return current().state()

    return router


# --- the face markers: the bridge's publisher (bridge side) ---------------------------


def face_markers_url(host: str, port: int) -> str:
    """Where the daemon at ``host:port`` takes the face markers."""
    return f"http://{host}:{port}{DISPLAYS_ROUTE_PREFIX}{_FACE_MARKERS_PATH}"


def _put_markers(url: str, markers: Sequence[FaceMarker]) -> bool:
    """``PUT`` one set of markers; ``False`` when the daemon has no such route (404),
    an ``OSError`` for anything else that went wrong."""
    body = json.dumps({"markers": [marker.to_json() for marker in markers]}).encode()
    request = urllib.request.Request(
        url, data=body, method="PUT", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_PUBLISH_TIMEOUT_S):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def fetch_face_markers(
    host: str, port: int, *, timeout: float = 2.0
) -> dict[str, Any] | None:
    """What the daemon at ``host:port`` holds as face markers — ``{"age_s", "markers":
    [...]}``, each marker with its ``world_pos`` — or ``None`` when it has no such
    display (not launched with ``--sim-display face_markers``, or unreachable)."""
    try:
        with urllib.request.urlopen(
            face_markers_url(host, port), timeout=timeout
        ) as response:
            state = json.load(response)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


class FaceMarkerPublisher:
    """Sends the faces the bridge detects to the sim's viewer (specs/daemon/sim_displays.md
    "Bridge -> daemon: the displays route"): a thread that polls ``faces`` — the report
    is updated, not published, when a face merely moves — and, for each new report,
    places every face (``face_marker``, with the head pose its frame was taken from)
    and ``PUT``s the set to ``url``, one request at a time. It is a thread, not a task:
    it only reads values and blocks on HTTP, and it runs for the whole session whatever
    event loop the host drives the bridge from.

    An inactive report sends one empty set. A daemon without the route (404) is one
    ``WARNING`` and the publisher stops; any other failure is one ``WARNING``, then
    ``DEBUG``, and it keeps going. Nothing it does raises into the session.

    ``delay`` is the delay the frame's pose is looked up with — the head tracker's
    estimate. ``send`` puts one set and says whether the route exists (tests
    substitute their own)."""

    def __init__(
        self,
        faces: Observable[FaceReport],
        head_tracking: Observable[HeadTrackingReport],
        *,
        camera: CameraModel,
        history: PoseHistory,
        delay: Callable[[], float],
        url: str,
        send: Callable[[str, Sequence[FaceMarker]], bool] = _put_markers,
        poll_hz: float = _PUBLISH_POLL_HZ,
    ) -> None:
        self._faces = faces
        self._head_tracking = head_tracking
        self._camera = camera
        self._history = history
        self._delay = delay
        self._url = url
        self._send = send
        self._period = 1.0 / poll_hz
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Whether the publisher is still sending (false once stopped, or once the
        daemon turned out to have no route)."""
        return self._thread is not None and self._thread.is_alive()

    async def start(self) -> None:
        if self._thread is not None:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(self._stop,),
            name="face-marker-publisher",
            daemon=True,
        )
        self._thread.start()

    async def stop(self) -> None:
        """Stop the thread. A request in flight is left to end on its own (within its
        timeout): ``stop`` does not wait for the daemon. Idempotent."""
        stop, self._stop = self._stop, None
        thread, self._thread = self._thread, None
        if stop is None or thread is None:
            return
        stop.set()
        await asyncio.to_thread(thread.join, _PUBLISH_STOP_JOIN_S)

    def _run(self, stop: threading.Event) -> None:
        sent: tuple[Any, ...] | None = None
        failed = False
        while not stop.is_set():
            report = self._faces.value
            key = (report.active, report.frame_id, report.ts)
            if key != sent:
                sent = key
                try:
                    found = self._send(self._url, self._markers(report))
                except Exception:
                    if stop.is_set():
                        return
                    if not failed:
                        failed = True
                        _logger.warning(
                            "face markers: could not send to %s; still trying",
                            self._url,
                            exc_info=True,
                        )
                    else:
                        _logger.debug("face markers: a send failed", exc_info=True)
                else:
                    if not found and not stop.is_set():
                        _logger.warning(
                            "face markers: the daemon has no %s route — it was not "
                            "started with --sim-display face_markers; nothing is sent",
                            self._url,
                        )
                        return
            stop.wait(self._period)

    def _markers(self, report: FaceReport) -> list[FaceMarker]:
        if not report.active or not report.faces:
            return []
        from .head_tracking import frame_head_pose

        head = frame_head_pose(
            report, self._camera, self._history, self._delay(), time.monotonic()
        )
        followed = self._head_tracking.value.track_id
        markers = [
            face_marker(
                face,
                self._camera,
                head,
                followed=followed is not None and face.track_id == followed,
            )
            for face in report.faces
        ]
        return [marker for marker in markers if marker is not None]
