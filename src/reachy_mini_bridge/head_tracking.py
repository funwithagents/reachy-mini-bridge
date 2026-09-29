"""Head tracking: the bridge's own tracker, turning the target face into the aim the
motion loop composes (specs/head_tracking.md).

The tracker takes the detection loop's every report, turns the target face into a
look-at head pose with upstream's geometry — the face's pixel through a camera model,
rotated into the world by the head pose its frame was taken from (or the rest pose, for
a camera that does not turn with the head) — and hands that aim to the motion loop's
gaze layer, which eases, fades and composes it. That pose is the report's own when the
source knows it; otherwise the robot's reported pose at the observation's time minus a
delay the tracker estimates online, from how the face's world direction holds still
while the head turns. It never touches the robot; after ``TRACKING_LOST_S`` without a
face it withdraws the aim and the robot idles in full.
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
    from .face_detection import FaceReport
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
    "CameraModel",
    "HeadTracker",
    "pinhole_intrinsics",
    "sim_hfov_deg",
]

_logger = logging.getLogger(__name__)

# The tracker's timing (specs/head_tracking.md "Easing, loss, focus"). Module constants,
# read at run time so tests can shorten them.
TRACKING_LOST_S = (
    2.0  # no face for this long withdraws the aim (upstream's own timeout)
)
# The online delay estimate (specs/head_tracking.md "The aim"): the delay L between an
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
    """The active camera as the tracker needs it (specs/head_tracking.md "The aim"):
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


class SetGaze(Protocol):
    """The motion loop's gaze command (``MotionSession.set_gaze``)."""

    def __call__(
        self, aim: npt.NDArray[np.float64] | None, *, focus: bool = False
    ) -> None: ...


@dataclass(frozen=True)
class _Detection:
    t: float  # the observation's time, monotonic
    ray: npt.NDArray[np.float64]  # the face's unit ray in the head frame


@dataclass
class HeadTracker:
    """Turns the reported target face into the gaze layer's aim (specs/head_tracking.md).

    ``history()`` is the motion loop's record of the head poses the robot reported
    (``head_pose_history``: monotonic times and 4x4 poses), ``set_gaze(aim, focus=)``
    its gaze command. A report's ``ts`` is its frame's time on this process's monotonic
    clock (specs/camera.md). ``focus`` is the caller's, handed over with every aim.
    :meth:`observe` is fed every report of the detection loop.
    """

    camera: CameraModel
    history: PoseHistory
    set_gaze: SetGaze
    focus: bool = False
    _engaged: bool = field(default=False, init=False)
    _seen_at: float = field(default=0.0, init=False)
    _delay: float = field(default=DELAY_PRIOR_S, init=False)
    _detections: deque[_Detection] = field(default_factory=deque, init=False)
    _last_ts: float | None = field(default=None, init=False)
    _last_t_obs: float = field(default=0.0, init=False)

    @property
    def engaged(self) -> bool:
        """Whether an aim is held: a face was seen within ``TRACKING_LOST_S``."""
        return self._engaged

    @property
    def delay_s(self) -> float:
        """The current estimate of the delay between an observation's time and the head
        pose its frame was taken from."""
        return self._delay

    def observe(self, report: FaceReport) -> None:
        """One report of the detection loop: aim the target face, or withdraw the aim
        once nobody has been seen for ``TRACKING_LOST_S``."""
        now = time.monotonic()
        if not report.faces:
            if self._engaged and now - self._seen_at >= TRACKING_LOST_S:
                self._engaged = False
                self.set_gaze(None, focus=self.focus)
            return
        self._seen_at = now
        aim = self._aim(report, now)
        if aim is None:
            return  # the previous aim stands
        self._engaged = True
        self.set_gaze(aim, focus=self.focus)

    def stop(self) -> None:
        """Withdraw the aim: the gaze layer fades out."""
        self._engaged = False
        self.set_gaze(None, focus=self.focus)

    def _aim(self, report: FaceReport, now: float) -> npt.NDArray[np.float64] | None:
        face = report.faces[0]
        camera = self.camera
        width, height = camera.size
        u = (face.x + 1.0) / 2.0 * (width - 1)
        v = (face.y + 1.0) / 2.0 * (height - 1)
        try:
            if camera.fixed:
                head = np.asarray(INIT_HEAD_POSE, dtype=np.float64)
            elif report.head_pose is not None:
                head = np.asarray(report.head_pose, dtype=np.float64)
            else:
                head = self._head_at_frame(report, u, v, now)
            return look_at_image_pose(
                u, v, camera.K, camera.D, head, default_head_to_camera_transform()
            )
        except Exception as e:  # noqa: BLE001 - one bad pixel keeps the previous aim
            _logger.debug("head tracking: no aim for the face at (%s, %s): %s", u, v, e)
            return None

    def _head_at_frame(
        self, report: FaceReport, u: float, v: float, now: float
    ) -> npt.NDArray[np.float64]:
        """The reported head pose at the observation's time minus the estimated delay;
        a new detection joins the estimate's window and refits it first."""
        # Only a new detection counts: the loop reports once per new frame, so a new
        # frame time is a new detection.
        if report.ts != self._last_ts:
            self._last_ts = report.ts
            self._last_t_obs = report.ts if report.ts > 0.0 else now
            self._add_detection(self._last_t_obs, u, v, now)
        times, poses = self.history()
        index = nearest_index(times, np.array([self._last_t_obs - self._delay]))[0]
        return poses[index]

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
