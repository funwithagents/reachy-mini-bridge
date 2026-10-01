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
Its state is published as a ``HeadTrackingReport`` (``bridge.head_tracking``).
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import numpy as np
from reachy_mini.media.camera_utils import intrinsics_for_size, undistort_points
from reachy_mini.reachy_mini import INIT_HEAD_POSE
from reachy_mini.vision.look_at import (
    default_head_to_camera_transform,
    look_at_image_pose,
)

from .motion import nearest_index

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
    "SIM_CAMERA_SIZE",
    "SIM_EYE_CAMERA_FOVY_DEG",
    "TRACKING_LOST_S",
    "TRACKING_MIN_SIZE",
    "TRACKING_SWITCH_S",
    "CameraModel",
    "HeadTracker",
    "HeadTrackingReport",
    "frame_head_pose",
    "pinhole_intrinsics",
    "sim_hfov_deg",
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


type PoseHistory = Callable[[], tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]]


def frame_head_pose(
    report: FaceReport,
    camera: CameraModel,
    history: PoseHistory,
    delay: float,
    now: float,
) -> npt.NDArray[np.float64]:
    """The head pose ``report``'s frame was taken from (specs/motion/head_tracking.md "The
    aim", "One rule, shared"): the neutral pose for a fixed camera, the report's own
    ``head_pose`` when its frame was stamped, else the reported pose at the report's time
    (``ts``, or ``now`` when it is unset) minus ``delay``. It reads the history and
    changes nothing: the tracker's aim and the sim's face markers both place a face with
    it."""
    if camera.fixed:
        return np.asarray(INIT_HEAD_POSE, dtype=np.float64)
    if report.head_pose is not None:
        return np.asarray(report.head_pose, dtype=np.float64)
    times, poses = history()
    t_obs = report.ts if report.ts > 0.0 else now
    return poses[nearest_index(times, np.array([t_obs - delay]))[0]]


class SetGaze(Protocol):
    """The motion loop's gaze command (``MotionSession.set_gaze``)."""

    def __call__(
        self, aim: npt.NDArray[np.float64] | None, *, focus: bool = False
    ) -> None: ...


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

    @classmethod
    def inactive(cls) -> HeadTrackingReport:
        """The report while tracking is off or outside a session."""
        return cls(active=False, focus=False, attention=None, track_id=None, ts=0.0)

    def same_state(self, other: HeadTrackingReport) -> bool:
        """Equal but for ``ts``: what a published change compares."""
        return (self.active, self.focus, self.attention, self.track_id) == (
            other.active,
            other.focus,
            other.attention,
            other.track_id,
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
    focus: bool = False
    report: Observable[HeadTrackingReport] | None = None
    _active: bool = field(default=False, init=False)
    # whom the head follows, by track_id, and when an observation last showed that face
    _following: int | None = field(default=None, init=False)
    _seen_at: float | None = field(default=None, init=False)
    _aimed_ts: float = field(default=0.0, init=False)
    _delay: float = field(default=DELAY_PRIOR_S, init=False)
    _detections: deque[_Detection] = field(default_factory=deque, init=False)
    _last_ts: float | None = field(default=None, init=False)
    _last_t_obs: float = field(default=0.0, init=False)

    @property
    def engaged(self) -> bool:
        """Whether the tracker follows a face (its aim held)."""
        return self._following is not None

    @property
    def following(self) -> int | None:
        """The ``track_id`` of the face the head follows, or ``None``."""
        return self._following

    @property
    def delay_s(self) -> float:
        """The current estimate of the delay between an observation's time and the head
        pose its frame was taken from."""
        return self._delay

    def start(self, *, focus: bool = False) -> None:
        """Run (or keep running with a new ``focus``): reports are observed from now on,
        and the state published."""
        self.focus = focus
        self._active = True
        self._publish()

    def observe(self, report: FaceReport) -> None:
        """One report of the detection loop: choose whom to follow
        (specs/motion/head_tracking.md "Whom the head follows") and aim that face — or hold
        the previous aim while the followed face is missing, switch after
        ``TRACKING_SWITCH_S``, withdraw after ``TRACKING_LOST_S`` with nobody to switch
        to. A face is missing from the last observation that showed it."""
        now = time.monotonic()
        face: Face | None = None
        if self._following is not None:
            face = next(
                (f for f in report.faces if f.track_id == self._following), None
            )
            if face is None:
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
        self._following = face.track_id
        self._seen_at = now
        aim = self._aim(face, report, now)
        if aim is not None:
            self._aimed_ts = report.ts
            self.set_gaze(aim, focus=self.focus)
        self._publish()

    def tick(self, now: float | None = None) -> None:
        """A poll of the detection loop that produced no observation (no new frame, a
        failed ``detect``): the followed face has been missing since the last
        observation that showed it, and ``TRACKING_LOST_S`` of that withdraws the aim
        as on an observation (specs/motion/head_tracking.md "Easing, loss, focus")."""
        if self._following is None:
            return
        if now is None:
            now = time.monotonic()
        if self._missing_for(now) >= TRACKING_LOST_S:
            self._lose()

    def _missing_for(self, now: float) -> float:
        """How long the followed face has been missing: since the observation that last
        showed it."""
        return 0.0 if self._seen_at is None else now - self._seen_at

    def _lose(self) -> None:
        """Follow nobody and withdraw the aim (the gaze layer fades out); published."""
        self._following = None
        self._seen_at = None
        self.set_gaze(None, focus=self.focus)
        self._publish()

    def stop(self) -> None:
        """Withdraw the aim (the gaze layer fades out), follow nobody, and publish the
        inactive report."""
        self._active = False
        self._following = None
        self._seen_at = None
        self.set_gaze(None, focus=self.focus)
        self._publish()

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

    def _aim(
        self, face: Face, report: FaceReport, now: float
    ) -> npt.NDArray[np.float64] | None:
        camera = self.camera
        width, height = camera.size
        u = (face.x + 1.0) / 2.0 * (width - 1)
        v = (face.y + 1.0) / 2.0 * (height - 1)
        try:
            if not camera.fixed and report.head_pose is None:
                self._note_detection(report, u, v, now)
            head = frame_head_pose(
                report, camera, self.history, self._delay, self._last_t_obs
            )
            return look_at_image_pose(
                u, v, camera.K, camera.D, head, default_head_to_camera_transform()
            )
        except Exception as e:  # noqa: BLE001 - one bad pixel keeps the previous aim
            _logger.debug("head tracking: no aim for the face at (%s, %s): %s", u, v, e)
            return None

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
        t_obs = np.array([d.t for d in self._detections])
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


def _span_deg(forwards: npt.NDArray[np.float64]) -> float:
    """How far apart the head's forward axes range, in degrees: the angle between the
    one farthest from their mean and the one farthest from it."""
    mean = forwards.mean(axis=0)
    far = forwards[int(np.argmin(forwards @ mean))]
    other = forwards[int(np.argmin(forwards @ far))]
    return math.degrees(math.acos(float(np.clip(far @ other, -1.0, 1.0))))
