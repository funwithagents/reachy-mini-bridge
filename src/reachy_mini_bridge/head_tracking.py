"""Head tracking: the bridge's own tracker, choosing whom to follow and turning that face
into the aim the motion loop composes (specs/motion/head_tracking.md).

The tracker takes the detection loop's every report and chooses whom to follow by
``track_id`` — the biggest face, held while it is reported; a face gone missing held
toward its last position for ``TRACKING_SWITCH_S`` before switching to the biggest other
one. It turns the followed face into a look-at head pose with upstream's geometry — the face's pixel through a camera model,
rotated into the world by the head pose its frame was taken from (or the rest pose, for
a camera that does not turn with the head) — and hands that aim to the motion loop's
gaze layer, which eases, fades and composes it. That pose is the report's own when the
source knows it; otherwise the robot's reported pose at the observation's time minus a
delay the tracker estimates online, from how the face's world direction holds still
while the head turns. It never touches the robot; after ``TRACKING_LOST_S`` without the
followed face and nobody to switch to, it withdraws the aim and the robot idles in full.
While it follows a face the aim also carries a posture toward the person — the head's
roll mirroring theirs, a lean toward them, a retreat from someone too close — filtered
on the tracker's clock so it reads as a stance, not a motion. Its state is published as
a ``HeadTrackingReport`` (``bridge.head_tracking``).
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

import numpy as np
from reachy_mini.media.camera_utils import intrinsics_for_size, undistort_points
from reachy_mini.reachy_mini import INIT_HEAD_POSE
from reachy_mini.vision.look_at import (
    default_head_to_camera_transform,
    look_at_image_pose,
)

from .config import PERSONAL_SPACE_EXIT_SHARE, TrackingSettings
from .motion import GazeAim, nearest_index

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy.typing as npt

    from .config import SimCameraSettings
    from .face_detection import Face, FaceReport
    from .observable import Observable
    from .robot import AnyReachyMini

__all__ = [
    "DELAY_MAX_CONTRAST",
    "DELAY_MAX_S",
    "DELAY_MIN_MOTION_DEG",
    "DELAY_PRIOR_S",
    "DELAY_SMOOTHING",
    "DELAY_STEP_S",
    "DELAY_WINDOW_S",
    "OFFSET_SMOOTHING_S",
    "ROLL_DEADBAND_DEG",
    "ROLL_SMOOTHING_S",
    "SIM_CAMERA_SIZE",
    "SIM_EYE_CAMERA_FOVY_DEG",
    "TRACKING_LOST_S",
    "TRACKING_MIN_SIZE",
    "TRACKING_SWITCH_S",
    "UNOBSERVABLE_MAX_S",
    "CameraModel",
    "HeadTracker",
    "HeadTrackingReport",
    "TrackGeometry",
    "distort_points",
    "expected_pixel",
    "face_camera_point",
    "frame_head_pose",
    "in_frame",
    "pinhole_intrinsics",
    "sim_hfov_deg",
    "world_direction",
]

_logger = logging.getLogger(__name__)

# The tracker's timing (specs/motion/head_tracking.md "Easing, loss, focus"). Module constants,
# read at run time so tests can shorten them.
# Whom the head follows (specs/motion/head_tracking.md "Whom the head follows"): a followed
# face missing this long is replaced by the biggest face in view at least this tall (a
# fraction of the frame's height — about upstream's acquisition gate, 0.3 % of a 16:9
# frame's area for a square face); missing TRACKING_LOST_S with nobody that large, the
# aim is withdrawn (upstream's own timeout).
TRACKING_SWITCH_S = 1.0
TRACKING_MIN_SIZE = 0.07
TRACKING_LOST_S = 2.0
# A face the camera is not framing — the head turned away by an emotion, the base swung
# under it — is out of view, not missing: the polls meanwhile count toward neither the
# switch nor the loss, for at most this long without a sighting (specs/motion/head_tracking.md
# "Whom the head follows"); the detection loop drops a track unseen this long whatever
# the view (specs/vision/user_perception.md "Tracks"). Longer than the longest recording
# of the emotions library (16 s).
UNOBSERVABLE_MAX_S = 20.0
# The online delay estimate (specs/motion/head_tracking.md "The aim"): the delay L between an
# observation's time — the frame's arrival at the bridge — and the head pose its frame
# was taken from, fitted over the new detections of the last DELAY_WINDOW_S by the L in
# [0, DELAY_MAX_S] (DELAY_STEP_S apart) that keeps the face's world direction most
# constant; refitted on each new detection while the head has turned at least
# DELAY_MIN_MOTION_DEG over the window and the fit is distinct — the best delay's spread
# at most DELAY_MAX_CONTRAST of the worst's, since a person moving while the head turns
# flattens the score and a flat score says nothing — the estimate moving DELAY_SMOOTHING
# of the way. DELAY_PRIOR_S is where it starts, corrected by the first turn; on the
# viewer sim the estimate settles between 0.05 and 0.45 s, mostly 0.1–0.25.
DELAY_WINDOW_S = 3.0
DELAY_MAX_S = 0.5
DELAY_STEP_S = 0.02
DELAY_MIN_MOTION_DEG = 4.0
DELAY_MAX_CONTRAST = 0.5
DELAY_SMOOTHING = 0.3
DELAY_PRIOR_S = 0.2
_DELAY_MIN_DETECTIONS = 8  # a fit needs a few detections across the turn

# The posture (specs/motion/head_tracking.md "The posture"): the person's roll under this
# soft deadband reads as upright (landmark jitter, not a tilt); the mirrored roll and the
# offset each follow their target through a first-order filter of this time constant,
# stepped on every aimed observation by the time since the previous one, at most
# _POSTURE_MAX_STEP_S so a face back from a gap never steps the posture.
ROLL_DEADBAND_DEG = 3.0
ROLL_SMOOTHING_S = 0.3
OFFSET_SMOOTHING_S = 0.6
_POSTURE_MAX_STEP_S = 0.2

# The sim's eye camera (the MJCF's `eye_camera`): MuJoCo's fovy is vertical, rendered at
# the stream's 1280x720 — about 112° horizontally. Pinned against the MJCF by a test.
SIM_EYE_CAMERA_FOVY_DEG = 80.0
SIM_CAMERA_SIZE = (1280, 720)


def pinhole_intrinsics(hfov_deg: float, size: tuple[int, int]) -> np.ndarray:
    """The camera matrix of an ideal pinhole with horizontal field of view ``hfov_deg``
    at ``size`` = (width, height): square pixels, principal point at the frame centre."""
    width, height = size
    f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0.0, width / 2.0], [0.0, f, height / 2.0], [0.0, 0.0, 1.0]])


def sim_hfov_deg(fovy_deg: float, width: int, height: int) -> float:
    """The horizontal field of view of a MuJoCo camera (``fovy`` is vertical) rendered at
    ``width`` x ``height`` with square pixels."""
    half = math.atan(math.tan(math.radians(fovy_deg) / 2.0) * width / height)
    return math.degrees(2.0 * half)


@dataclass(frozen=True)
class CameraModel:
    """The active camera as the tracker needs it (specs/motion/head_tracking.md "The aim"):
    intrinsics ``K`` and distortion ``D`` at a frame of ``size`` (width, height), and
    whether it is ``fixed`` — a camera that does not turn with the head."""

    K: npt.NDArray[np.float64]
    D: npt.NDArray[np.float64]
    size: tuple[int, int]
    fixed: bool = False

    @classmethod
    def for_robot(cls, robot: AnyReachyMini) -> CameraModel:
        """The SDK client's calibration of the robot's camera (the Lite's when the
        client runs without media), scaled to the frame its daemon streams and detects
        on — the default resolution's crop of the sensor, as upstream's tracker does."""
        camera = getattr(robot.media, "camera", None)
        specs = getattr(camera, "camera_specs", None)
        if specs is None:
            from reachy_mini.media.camera_constants import ReachyMiniLiteCamSpecs

            specs = ReachyMiniLiteCamSpecs()
        width, height, _fps, crop_scale = specs.default_resolution.value
        size = (int(width), int(height))
        K = intrinsics_for_size(np.asarray(specs.K, dtype=np.float64), crop_scale, size)
        return cls(K=K, D=np.asarray(specs.D, dtype=np.float64), size=size)

    @classmethod
    def for_sim(cls, camera: SimCameraSettings) -> CameraModel:
        """The bridge's pinhole of the sim's camera: the scene's eye camera for a ``sim``
        source (upstream's matrix is wrong for it), the configured field of view of a
        ``webcam`` — a fixed camera."""
        if camera.source == "webcam":
            hfov, fixed = camera.hfov_deg, True
        else:
            hfov, fixed = sim_hfov_deg(SIM_EYE_CAMERA_FOVY_DEG, *SIM_CAMERA_SIZE), False
        return cls(
            K=pinhole_intrinsics(hfov, SIM_CAMERA_SIZE),
            D=np.zeros(5),
            size=SIM_CAMERA_SIZE,
            fixed=fixed,
        )


def face_camera_point(
    face: Face, camera: CameraModel, box_height_m: float
) -> npt.NDArray[np.float64] | None:
    """Where ``face`` is in the camera's frame (x right, y down, z forward), in metres
    (specs/vision/user_perception.md "The face report"): its pixel undistorted into a ray
    of ``camera``, at the depth the pinhole's similar triangles give a box
    ``box_height_m`` tall that spans the face's ``size`` of the model's frame height.
    ``None`` for a face without a size. The face's ``distance_m`` is this point's norm;
    the sim's face markers place a face at it."""
    width, height = camera.size
    if face.size <= 0.0:
        return None
    u = (face.x + 1.0) / 2.0 * (width - 1)
    v = (face.y + 1.0) / 2.0 * (height - 1)
    x_n, y_n = undistort_points(u, v, camera.K, camera.D)
    depth = float(camera.K[1, 1]) * box_height_m / (face.size * height)
    return depth * np.array([x_n, y_n, 1.0])


type PoseHistory = Callable[[], tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]]


def frame_head_pose(
    ts: float,
    head_pose: npt.NDArray[np.float64] | None,
    camera: CameraModel,
    history: PoseHistory,
    delay: float,
    now: float,
) -> npt.NDArray[np.float64]:
    """The head pose a frame was taken from, given its time ``ts`` and the ``head_pose``
    it was stamped with, if any (specs/motion/head_tracking.md "The aim", "One rule,
    shared"): the neutral pose for a fixed camera, the frame's own ``head_pose`` when
    stamped, else the reported pose at the frame's time (``ts``, or ``now`` when it is
    unset) minus ``delay``. It reads the history and changes nothing: the tracker's
    aim, the detection loop's tracks and the sim's face markers all place a face with
    it."""
    if camera.fixed:
        return np.asarray(INIT_HEAD_POSE, dtype=np.float64)
    if head_pose is not None:
        return np.asarray(head_pose, dtype=np.float64)
    times, poses = history()
    t_obs = ts if ts > 0.0 else now
    return poses[nearest_index(times, np.array([t_obs - delay]))[0]]


def distort_points(
    x_n: float, y_n: float, K: npt.NDArray[np.float64], D: npt.NDArray[np.float64]
) -> tuple[float, float]:
    """A point on the normalised image plane to its pixel through ``K`` and ``D``: the
    forward, closed-form half of upstream's ``undistort_points`` — OpenCV's model, the
    radial terms ``k1..k6`` as the rational model, the tangential ``p1``, ``p2``, the
    thin prism ``s1..s4``; up to 14 coefficients, the unused ones zero."""
    d = np.zeros(14)
    n = min(len(D), 14)
    d[:n] = D[:n]
    r2 = x_n * x_n + y_n * y_n
    r4 = r2 * r2
    r6 = r4 * r2
    cdist = 1.0 + d[0] * r2 + d[1] * r4 + d[4] * r6
    icdist2_den = 1.0 + d[5] * r2 + d[6] * r4 + d[7] * r6
    icdist2 = 1.0 / icdist2_den if icdist2_den != 0.0 else 1.0
    a1 = 2.0 * x_n * y_n
    a2 = r2 + 2.0 * x_n * x_n
    a3 = r2 + 2.0 * y_n * y_n
    xd = x_n * cdist * icdist2 + d[2] * a1 + d[3] * a2 + d[8] * r2 + d[9] * r4
    yd = y_n * cdist * icdist2 + d[2] * a3 + d[3] * a1 + d[10] * r2 + d[11] * r4
    return (
        float(xd * K[0, 0] + K[0, 2]),
        float(yd * K[1, 1] + K[1, 2]),
    )


def world_direction(
    u: float, v: float, head_pose: npt.NDArray[np.float64], camera: CameraModel
) -> npt.NDArray[np.float64]:
    """The direction in the world of the pixel ``(u, v)`` of ``camera``'s frame, seen from
    ``head_pose`` — the pose the frame was taken from — as a unit vector: the pixel
    undistorted into a ray of the camera and rotated by the head-to-camera transform and
    the head's rotation, the ray upstream's ``look_at_image_pose`` aims along
    (specs/vision/user_perception.md "Tracks": a track's anchor in the world)."""
    x_n, y_n = undistort_points(u, v, camera.K, camera.D)
    rotation = (np.asarray(head_pose) @ default_head_to_camera_transform())[:3, :3]
    ray = rotation @ np.array([x_n, y_n, 1.0])
    return ray / np.linalg.norm(ray)


def expected_pixel(
    direction: npt.NDArray[np.float64],
    head_pose: npt.NDArray[np.float64],
    camera: CameraModel,
) -> tuple[float, float] | None:
    """Where the world ``direction`` is seen in ``camera``'s frame from ``head_pose``: the
    inverse of :func:`world_direction` — the direction into the camera's frame,
    projected through ``K`` and ``D``. ``None`` for a direction behind the camera."""
    rotation = (np.asarray(head_pose) @ default_head_to_camera_transform())[:3, :3]
    ray = rotation.T @ np.asarray(direction, dtype=np.float64)
    if ray[2] <= 1e-9:
        return None
    return distort_points(ray[0] / ray[2], ray[1] / ray[2], camera.K, camera.D)


def in_frame(pixel: tuple[float, float], size: tuple[int, int]) -> bool:
    """Whether ``pixel`` lies on a frame of ``size`` (width, height): the in-view rule of
    specs/vision/user_perception.md "Tracks". A face is detected until about its centre
    reaches the edge, so a departing person's last sighting lies inside and counts; a
    face the head turned away from projects far outside and does not."""
    u, v = pixel
    width, height = size
    return 0.0 <= u <= width - 1 and 0.0 <= v <= height - 1


type FramePose = Callable[
    [float, "npt.NDArray[np.float64] | None"], "npt.NDArray[np.float64]"
]


@dataclass(frozen=True)
class TrackGeometry:
    """The detection loop's anchor of its tracks in the world
    (specs/vision/user_perception.md "Tracks"): the head tracker's camera model and its
    rule for the head pose a frame was taken from — ``frame_pose(ts, head_pose)``,
    :func:`frame_head_pose` with the tracker's history and delay — handed to the loop
    by the bridge (:meth:`HeadTracker.track_geometry`). Pixels are the frame's own, of
    a frame of ``size``: mapped onto the model's frame the way the report normalises
    them, so the loop never sees the model's resolution."""

    camera: CameraModel
    frame_pose: FramePose

    def direction(
        self,
        u: float,
        v: float,
        size: tuple[int, int],
        ts: float,
        head_pose: npt.NDArray[np.float64] | None,
    ) -> npt.NDArray[np.float64]:
        """The world direction of the frame pixel ``(u, v)`` seen at ``ts``."""
        mu, mv = self._to_model(u, v, size)
        return world_direction(mu, mv, self.frame_pose(ts, head_pose), self.camera)

    def pixel(
        self,
        direction: npt.NDArray[np.float64],
        size: tuple[int, int],
        ts: float,
        head_pose: npt.NDArray[np.float64] | None,
    ) -> tuple[float, float] | None:
        """The frame pixel where ``direction`` is expected at ``ts``, ``None`` behind
        the camera."""
        pixel = expected_pixel(direction, self.frame_pose(ts, head_pose), self.camera)
        return None if pixel is None else self._to_frame(*pixel, size)

    def _to_model(
        self, u: float, v: float, size: tuple[int, int]
    ) -> tuple[float, float]:
        width, height = size
        model_width, model_height = self.camera.size
        return (
            u * (model_width - 1) / max(width - 1, 1),
            v * (model_height - 1) / max(height - 1, 1),
        )

    def _to_frame(
        self, u: float, v: float, size: tuple[int, int]
    ) -> tuple[float, float]:
        width, height = size
        model_width, model_height = self.camera.size
        return (
            u * (width - 1) / max(model_width - 1, 1),
            v * (height - 1) / max(model_height - 1, 1),
        )


class SetGaze(Protocol):
    """The motion loop's gaze command (``MotionSession.set_gaze``)."""

    def __call__(self, aim: GazeAim | None, *, focus: bool = False) -> None: ...


@dataclass(frozen=True)
class _Detection:
    t: float  # the observation's time, monotonic
    ray: npt.NDArray[np.float64]  # the face's unit ray in the head frame


@dataclass(frozen=True)
class HeadTrackingReport:
    """The head tracker's state: the value of ``bridge.head_tracking``
    (specs/motion/head_tracking.md "The head tracking report")."""

    active: bool  # the tracker is running (tracking on, inside a session)
    focus: bool  # it holds the head exactly on the face (start_head_tracking's focus)
    attention: str | None  # "engaged" | "watching" | None
    track_id: int | None  # the track of the face the head follows; None unless engaged
    ts: float  # the time of the face report behind the last aim; 0.0 before any
    # the posture in the last aim (specs/motion/head_tracking.md "The posture"), filtered:
    roll_deg: float = 0.0  # the mirrored roll
    offset_mm: float = 0.0  # along the look direction: + toward the person, - back
    # the followed person is inside the personal space ("The personal space event")
    in_personal_space: bool = False

    @classmethod
    def inactive(cls) -> HeadTrackingReport:
        """The report while tracking is off or outside a session."""
        return cls(active=False, focus=False, attention=None, track_id=None, ts=0.0)

    def same_state(self, other: HeadTrackingReport) -> bool:
        """Equal but for ``ts``: what a published change compares."""
        return (
            self.active,
            self.focus,
            self.attention,
            self.track_id,
            self.in_personal_space,
        ) == (
            other.active,
            other.focus,
            other.attention,
            other.track_id,
            other.in_personal_space,
        )


@dataclass
class HeadTracker:
    """Chooses whom to follow among the reported faces and turns that face into the gaze
    layer's aim (specs/motion/head_tracking.md).

    ``history()`` is the motion loop's record of the head poses the robot reported
    (``head_pose_history``: monotonic times and 4x4 poses), ``set_gaze(aim, focus=)``
    its gaze command. A report's ``ts`` is its frame's time on this process's monotonic
    clock (specs/vision/camera.md). ``focus`` is the caller's, handed over with every aim.
    :meth:`observe` is fed every report of the detection loop while :meth:`start` has
    it running, :meth:`tick` every poll of it that produced none, so the tracker keeps
    time while the camera is silent; its state is published on ``report`` when one is
    given.
    """

    camera: CameraModel
    history: PoseHistory
    set_gaze: SetGaze
    settings: TrackingSettings = field(default_factory=TrackingSettings)
    report: Observable[HeadTrackingReport] | None = None
    _active: bool = field(default=False, init=False)
    # whom the head follows, by track_id, and when an observation last showed that face
    _following: int | None = field(default=None, init=False)
    _seen_at: float | None = field(default=None, init=False)
    # the followed face's direction in the world on its last sighting, and the time
    # since then the camera did not frame it (specs/motion/head_tracking.md "Whom the
    # head follows": out of view, not missing), measured poll to poll
    _direction: npt.NDArray[np.float64] | None = field(default=None, init=False)
    _unobservable_s: float = field(default=0.0, init=False)
    _polled_at: float | None = field(default=None, init=False)
    _aimed_ts: float = field(default=0.0, init=False)
    _delay: float = field(default=DELAY_PRIOR_S, init=False)
    _detections: deque[_Detection] = field(default_factory=deque, init=False)
    _last_ts: float | None = field(default=None, init=False)
    _last_t_obs: float = field(default=0.0, init=False)
    # the posture's filtered values, and when they were last stepped (None: grow in
    # afresh from the look-at alone)
    _roll: float = field(default=0.0, init=False)  # radians
    _offset: float = field(default=0.0, init=False)  # metres
    _posture_at: float | None = field(default=None, init=False)
    # the followed person inside the personal space, with its hysteresis
    _in_space: bool = field(default=False, init=False)

    @property
    def focus(self) -> bool:
        """Whether the head holds exactly on the aim (the settings' ``focus``)."""
        return self.settings.focus

    @focus.setter
    def focus(self, value: bool) -> None:
        self.settings = replace(self.settings, focus=value)

    @property
    def engaged(self) -> bool:
        """Whether the tracker follows a face (its aim held)."""
        return self._following is not None

    @property
    def following(self) -> int | None:
        """The ``track_id`` of the face the head follows, or ``None``."""
        return self._following

    @property
    def fixed_delay_s(self) -> float | None:
        """The fixed delay in force (specs/motion/head_tracking.md "The frame delay"): the
        settings' number, ``None`` while it is ``"estimate"``."""
        setting = self.settings.frame_delay_s
        return None if isinstance(setting, str) else float(setting)

    @property
    def delay_s(self) -> float:
        """The delay between an observation's time and the head pose its frame was taken
        from: the fixed one in force, else the current estimate."""
        fixed = self.fixed_delay_s
        return self._delay if fixed is None else fixed

    def start(
        self, settings: TrackingSettings | None = None, *, focus: bool | None = None
    ) -> None:
        """Run (or keep running with new settings, or a new ``focus``): reports are
        observed from now on, and the state published."""
        if settings is not None:
            self.settings = settings
        if focus is not None:
            self.focus = focus
        self._active = True
        self._publish()

    def configure(self, settings: TrackingSettings) -> None:
        """Take new settings while running (specs/motion/head_tracking.md "The posture"):
        the posture's filters move toward the new targets from where they stand, so a
        behaviour switched on or off eases in or out, never steps; a change of focus is
        published and handed over with the next aim."""
        self.settings = settings
        self._publish()

    def observe(self, report: FaceReport) -> None:
        """One report of the detection loop: choose whom to follow
        (specs/motion/head_tracking.md "Whom the head follows") and aim that face — or hold
        the previous aim while the followed face is missing, switch after
        ``TRACKING_SWITCH_S``, withdraw after ``TRACKING_LOST_S`` with nobody to switch
        to. A face is missing from the last observation that showed it."""
        now = time.perf_counter()
        face: Face | None = None
        if self._following is not None:
            face = next(
                (f for f in report.faces if f.track_id == self._following), None
            )
            if face is None:
                self._note_poll(now, self._in_view(report.ts, report.head_pose, now))
                missing = self._missing_for(now)
                if missing >= TRACKING_SWITCH_S:
                    face = self._biggest(report)
                if face is None:
                    if missing >= TRACKING_LOST_S:
                        self._lose()
                    return  # the previous aim stands: the head holds toward the face
        else:
            face = self._biggest(report)
            if face is None:
                return
        if face.track_id != self._following:
            self._in_space = False  # a new person: their own entry, not the last one's
        self._following = face.track_id
        self._seen_at = self._polled_at = now
        self._unobservable_s = 0.0
        self._note_personal_space(face)
        aim = self._aim(face, report, now)
        if aim is not None:
            self._aimed_ts = report.ts
            self.set_gaze(aim, focus=self.focus)
        self._publish()

    def tick(self, now: float | None = None) -> None:
        """A poll of the detection loop that produced no observation (no new frame, a
        failed ``detect``): the followed face has been missing since the last
        observation that showed it — the time the camera did not frame it, judged on
        the latest reported head pose, not counted — and ``TRACKING_LOST_S`` of that
        withdraws the aim as on an observation (specs/motion/head_tracking.md "Easing,
        loss, focus")."""
        if self._following is None:
            return
        if now is None:
            now = time.perf_counter()
        self._note_poll(now, self._in_view(now, None, now))
        if self._missing_for(now) >= TRACKING_LOST_S:
            self._lose()

    def _missing_for(self, now: float) -> float:
        """How long the followed face has been missing: since the observation that last
        showed it, less the time the camera did not frame its direction."""
        if self._seen_at is None:
            return 0.0
        return now - self._seen_at - self._unobservable_s

    def _note_poll(self, now: float, in_view: bool) -> None:
        """A poll without the followed face: the time since the previous poll joins the
        unobservable time when the camera was not framing the face, up to
        ``UNOBSERVABLE_MAX_S`` — after which the face counts as missing whatever the
        view (specs/motion/head_tracking.md "Whom the head follows")."""
        elapsed = 0.0 if self._polled_at is None else max(0.0, now - self._polled_at)
        self._polled_at = now
        if not in_view:
            self._unobservable_s = min(
                self._unobservable_s + elapsed, UNOBSERVABLE_MAX_S
            )

    def _in_view(
        self, ts: float, head_pose: npt.NDArray[np.float64] | None, now: float
    ) -> bool:
        """Whether the camera, from the head pose a frame at ``ts`` (stamped
        ``head_pose``) was taken from, frames the followed face's direction — its
        expected pixel on the model's frame (specs/vision/user_perception.md "Tracks");
        a face without a direction yet is taken as in view."""
        if self._direction is None:
            return True
        head = frame_head_pose(
            ts, head_pose, self.camera, self.history, self.delay_s, now
        )
        pixel = expected_pixel(self._direction, head, self.camera)
        return pixel is not None and in_frame(pixel, self.camera.size)

    def track_geometry(self) -> TrackGeometry:
        """The detection loop's anchor of its tracks in the world: this tracker's camera
        model and frame-pose rule, reading its delay as it stands at each call."""
        return TrackGeometry(
            self.camera,
            lambda ts, head_pose: frame_head_pose(
                ts,
                head_pose,
                self.camera,
                self.history,
                self.delay_s,
                time.perf_counter(),
            ),
        )

    def _lose(self) -> None:
        """Follow nobody and withdraw the aim (the gaze layer fades out); published."""
        self._following = None
        self._forget_sighting()
        self._reset_posture()
        self.set_gaze(None, focus=self.focus)
        self._publish()

    def stop(self) -> None:
        """Withdraw the aim (the gaze layer fades out), follow nobody, and publish the
        inactive report."""
        self._active = False
        self._following = None
        self._forget_sighting()
        self._reset_posture()
        self.set_gaze(None, focus=self.focus)
        self._publish()

    def _forget_sighting(self) -> None:
        self._seen_at = self._polled_at = None
        self._direction = None
        self._unobservable_s = 0.0

    def _note_personal_space(self, face: Face) -> None:
        """Whether the followed person is inside the personal space
        (specs/motion/head_tracking.md "The personal space event"): entered nearer than
        ``personal_space.distance_m``, left beyond it by ``exit_margin_m`` (by default
        ``PERSONAL_SPACE_EXIT_SHARE`` of the distance), so a person standing at the
        boundary never flickers it; unknown distance, outside."""
        distance = face.distance_m
        space = self.settings.personal_space
        boundary = space.distance_m
        margin = (
            space.exit_margin_m
            if space.exit_margin_m is not None
            else PERSONAL_SPACE_EXIT_SHARE * boundary
        )
        if distance is None:
            self._in_space = False
        elif self._in_space:
            self._in_space = distance <= boundary + margin
        else:
            self._in_space = distance < boundary

    def _reset_posture(self) -> None:
        self._in_space = False
        self._roll = self._offset = 0.0
        self._posture_at = None

    @staticmethod
    def _biggest(report: FaceReport) -> Face | None:
        """The largest face at least ``TRACKING_MIN_SIZE`` tall, or ``None``."""
        eligible = [f for f in report.faces if f.size >= TRACKING_MIN_SIZE]
        return max(eligible, key=lambda f: f.size) if eligible else None

    def _state(self) -> HeadTrackingReport:
        if not self._active:
            return HeadTrackingReport.inactive()
        return HeadTrackingReport(
            active=True,
            focus=self.focus,
            attention="engaged" if self._following is not None else "watching",
            track_id=self._following,
            ts=self._aimed_ts,
            roll_deg=math.degrees(self._roll) if self._following is not None else 0.0,
            offset_mm=self._offset * 1000.0 if self._following is not None else 0.0,
            in_personal_space=self._in_space and self._following is not None,
        )

    def _publish(self) -> None:
        """``set`` the report when the state changed, ``update`` it otherwise (a fresh
        ``ts``): a subscriber wakes on a change, never on a face moving."""
        observable = self.report
        if observable is None:
            return
        state = self._state()
        if state.same_state(observable.value):
            observable.update(state)
        else:
            observable.set(state)

    def _aim(self, face: Face, report: FaceReport, now: float) -> GazeAim | None:
        camera = self.camera
        width, height = camera.size
        u = (face.x + 1.0) / 2.0 * (width - 1)
        v = (face.y + 1.0) / 2.0 * (height - 1)
        try:
            if not camera.fixed and report.head_pose is None:
                self._note_detection(report, u, v, now)
            head = frame_head_pose(
                report.ts,
                report.head_pose,
                camera,
                self.history,
                self.delay_s,
                self._last_t_obs,
            )
            look = look_at_image_pose(
                u, v, camera.K, camera.D, head, default_head_to_camera_transform()
            )
            self._direction = world_direction(u, v, head, camera)
            return self._posture(face, look, head, now)
        except Exception as e:  # noqa: BLE001 - one bad pixel keeps the previous aim
            _logger.debug("head tracking: no aim for the face at (%s, %s): %s", u, v, e)
            return None

    def _posture(
        self,
        face: Face,
        look: npt.NDArray[np.float64],
        head: npt.NDArray[np.float64],
        now: float,
    ) -> GazeAim:
        """The aim's parts (specs/motion/head_tracking.md "The posture"): the look-at pose,
        the mirrored roll about its viewing axis and the offset along its horizontal
        viewing direction — both stepped through their filters toward this
        observation's targets first. The motion loop composes them, within reach."""
        settings = self.settings
        roll_target = 0.0
        if settings.mirroring.enabled:
            person = _person_roll(face, look, head)
            if person is not None:
                deadband = math.radians(ROLL_DEADBAND_DEG)
                excess = math.copysign(max(0.0, abs(person) - deadband), person)
                limit = math.radians(settings.mirroring.max_deg)
                roll_target = max(-limit, min(limit, settings.mirroring.gain * excess))
        offset_target = 0.0
        if settings.approach.enabled:
            offset_target += settings.approach.lean_mm / 1000.0
        distance = face.distance_m
        if settings.personal_space.enabled and distance is not None:
            pd = settings.personal_space
            intrusion = max(0.0, pd.distance_m - distance) / pd.span_m
            offset_target -= pd.max_back_mm / 1000.0 * min(1.0, intrusion)
        if self._posture_at is not None:
            dt = min(max(0.0, now - self._posture_at), _POSTURE_MAX_STEP_S)
            self._roll += (roll_target - self._roll) * _filter_gain(
                dt, ROLL_SMOOTHING_S
            )
            self._offset += (offset_target - self._offset) * _filter_gain(
                dt, OFFSET_SMOOTHING_S
            )
        self._posture_at = now
        return GazeAim(look, self._roll, self._offset)

    def _note_detection(
        self, report: FaceReport, u: float, v: float, now: float
    ) -> None:
        """A new detection joins the delay estimate's window and refits it, before the
        report is aimed against the pose that estimate gives."""
        # Only a new detection counts: the loop reports once per new frame, so a new
        # frame time is a new detection.
        if report.ts != self._last_ts:
            self._last_ts = report.ts
            self._last_t_obs = report.ts if report.ts > 0.0 else now
            if self.fixed_delay_s is None:  # a fixed delay is not estimated
                self._add_detection(self._last_t_obs, u, v, now)

    def _add_detection(self, t_obs: float, u: float, v: float, now: float) -> None:
        x_n, y_n = undistort_points(u, v, self.camera.K, self.camera.D)
        ray = default_head_to_camera_transform()[:3, :3] @ np.array([x_n, y_n, 1.0])
        self._detections.append(_Detection(t_obs, ray / np.linalg.norm(ray)))
        while self._detections and self._detections[0].t < now - DELAY_WINDOW_S:
            self._detections.popleft()
        self._refit_delay()

    def _refit_delay(self) -> None:
        """Move the estimate toward the delay that keeps the face's world direction
        most constant over the window — informative only while the head turns."""
        if len(self._detections) < _DELAY_MIN_DETECTIONS:
            return
        times, poses = self.history()
        t_obs = np.array([d.t for d in self._detections], dtype=np.float64)
        rays = np.stack([d.ray for d in self._detections])
        delays = np.arange(0.0, DELAY_MAX_S + DELAY_STEP_S / 2, DELAY_STEP_S)
        queries = t_obs[:, np.newaxis] - delays[np.newaxis, :]
        rotations = poses[nearest_index(times, queries)][..., :3, :3]  # (M, K, 3, 3)
        if _span_deg(rotations[..., 0].reshape(-1, 3)) < DELAY_MIN_MOTION_DEG:
            return  # the head held still: every delay fits alike
        directions = np.einsum("mkij,mj->mki", rotations, rays)
        spread = 1.0 - np.linalg.norm(directions.mean(axis=0), axis=-1)
        if spread.min() > DELAY_MAX_CONTRAST * spread.max():
            return  # no delay explains the directions: the face itself moved
        best = float(delays[int(np.argmin(spread))])
        self._delay += DELAY_SMOOTHING * (best - self._delay)


def _filter_gain(dt: float, tau: float) -> float:
    """How far a first-order filter of time constant ``tau`` moves toward its target
    over ``dt``."""
    return 1.0 - math.exp(-dt / tau)


def _person_roll(
    face: Face, look: npt.NDArray[np.float64], head: npt.NDArray[np.float64]
) -> float | None:
    """The person's head roll as seen from where ``look`` points, in radians, positive
    when their head leans toward the robot's right (specs/motion/head_tracking.md "The
    mirrored roll"): the face's eye line — the image direction at ``face.roll``, y down —
    lifted into the camera frame, carried into the world by ``head`` (the pose the frame
    was taken from, so the robot's own roll is not read as the person's), projected
    across ``look``'s viewing axis and measured from ``look``'s level eye line about it.
    Rolling ``look`` by that angle about its own x axis sets the robot's eye line
    parallel to the person's: the mirror. ``None`` for a face without a roll."""
    if face.roll is None:
        return None
    to_world = (np.asarray(head) @ default_head_to_camera_transform())[:3, :3]
    eye_line = to_world @ np.array([math.cos(face.roll), math.sin(face.roll), 0.0])
    axis = look[:3, 0]
    eye_line = eye_line - float(eye_line @ axis) * axis
    norm = float(np.linalg.norm(eye_line))
    if norm < 1e-6:
        return None
    eye_line /= norm
    level = -look[:3, 1]  # toward the robot's right: where image right looks
    return math.atan2(float(np.cross(level, eye_line) @ axis), float(level @ eye_line))


def _span_deg(forwards: npt.NDArray[np.float64]) -> float:
    """How far apart the head's forward axes range, in degrees: the angle between the
    one farthest from their mean and the one farthest from it."""
    mean = forwards.mean(axis=0)
    far = forwards[int(np.argmin(forwards @ mean))]
    other = forwards[int(np.argmin(forwards @ far))]
    return math.degrees(math.acos(float(np.clip(far @ other, -1.0, 1.0))))
