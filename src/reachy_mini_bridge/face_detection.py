"""Face detection: the detection loop over a pluggable source and the observable face
report (specs/user_perception.md).

The loop polls its source at ``FACE_POLL_HZ``, turns each observation into a
``FaceReport``, ``update``s the api's ``Observable[FaceReport]`` on every poll and
``set``s it (wakes subscribers) only when the face count changes — a rise at once, a
drop once it has held for ``FACE_ABSENT_S`` — or when ``active`` flips. The ``daemon``
source reads the daemon's own detector over its HTTP API; the ``custom`` source samples
the camera feed ([camera](camera.py)) and hands each new frame to a developer's
``FaceDetector``, then selects the target face itself. The bridge ships no vision code.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from . import robot as _robot
from .fake_reachy_mini import FakeReachyMini

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import numpy as np
    import numpy.typing as npt

    from .camera import CameraFeed, CameraFrame
    from .observable import Observable
    from .robot import AnyReachyMini

__all__ = [
    "DAEMON_DETECT_WEIGHT",
    "FACE_ABSENT_S",
    "FACE_POLL_HZ",
    "FACE_SOURCE_DOWN_S",
    "SELECT_MAX_JUMP",
    "SELECT_MAX_MISSES",
    "SELECT_MIN_AREA_FRAC",
    "Face",
    "FaceDetection",
    "FaceDetector",
    "FaceDetectorFactory",
    "FaceReport",
    "PixelFace",
    "check_face_detector_factory",
    "daemon_face_target",
    "report_from_daemon",
    "report_from_pixels",
]

_logger = logging.getLogger(__name__)

# The detection loop's timing (specs/user_perception.md "The detection loop"). Module
# constants, not config; read at run time so tests can shorten them.
# Three polls per observation: upstream's detector sees the daemon's local camera feed,
# capped at 10 fps (media_server.IPC_FPS), and polling that at 10 Hz would add up to a
# frame's worth of delay and alias; at 30 Hz each observation arrives within ~33 ms, for
# a sub-millisecond HTTP read on loopback. The `custom` source samples the camera feed
# at the same rate and runs its detector once per new frame.
FACE_POLL_HZ = 30.0
FACE_ABSENT_S = 0.3  # a drop in the count is published once it has held this long
FACE_SOURCE_DOWN_S = 5.0  # a source failing this long reads as not looking
# The weight the `daemon` source arms the daemon's tracker at: the daemon runs its
# detector only above zero, and blends its own aim into the head by this much.
DAEMON_DETECT_WEIGHT = 0.001
# The `custom` source's selection gates (specs/user_perception.md "The pipeline"),
# upstream's own values so the source behaves like the daemon's: acquire the largest
# face above this fraction of the frame's area, keep the nearest face within this jump
# (normalised image units, [-1, 1] across the frame), drop the association after this
# many consecutive misses.
SELECT_MIN_AREA_FRAC = 0.003
SELECT_MAX_JUMP = 0.5
SELECT_MAX_MISSES = 20

_DAEMON_FACE_PATH = "/api/media/tracking/face"


@dataclass(frozen=True)
class Face:
    """One face in front of the robot, in the tracker's normalised image coordinates."""

    x: float  # [-1, 1], x right; the nose when known, else the bbox centre
    y: float  # [-1, 1], y down; (0, 0) is the image centre
    roll: float | None  # head roll in radians from the eye line; None when unknown
    size: (
        float | None
    )  # bbox height as a fraction of the frame height; None from the daemon


@dataclass(frozen=True)
class FaceReport:
    """Who the detection loop sees: the value of ``api.faces``."""

    faces: tuple[Face, ...]  # every face the source reports; the target face first
    ts: float  # when the observation was made (the source's monotonic clock)
    source: str  # "daemon" | "custom"
    active: bool  # a detector is running; False means "unknown", not "nobody"
    # The head pose the frame was captured from, when the source knows it (a custom
    # detector's frame); None from the daemon, whose report says only when it detected.
    head_pose: npt.NDArray[np.float64] | None = field(default=None, compare=False)

    @classmethod
    def inactive(cls, source: str) -> FaceReport:
        """The report while no detector is looking (before entry, after exit)."""
        return cls(faces=(), ts=0.0, source=source, active=False)


# --- custom detectors (specs/user_perception.md "Custom detectors") ---------------------


@dataclass(frozen=True)
class PixelFace:
    """A face as a custom detector returns it, in pixels of the frame it was given."""

    bbox: tuple[float, float, float, float]  # x, y, width, height
    nose: tuple[float, float] | None = None  # the point the head aims at
    eyes: tuple[tuple[float, float], tuple[float, float]] | None = None  # right, left


class FaceDetector(Protocol):
    """A developer's face detector for the ``custom`` source.

    ``detect`` runs on a worker thread once per new camera frame — the frame is the
    feed's, shared and read-only (copy before drawing) — and returns the faces it sees,
    in any order, within a frame period (a slower call skips frames, never queues them).
    It never touches the robot or the api; its dependencies are its own.
    """

    def detect(
        self, frame_bgr: npt.NDArray[np.uint8], ts: float
    ) -> Sequence[PixelFace]: ...


type FaceDetectorFactory = Callable[[], FaceDetector]


def check_face_detector_factory(factory: object) -> None:
    """Registration-time check (specs/user_perception.md "Custom detectors"):
    ``ValueError`` unless ``factory`` is a callable whose result has a callable
    ``detect``. Calls the factory once, on the caller's thread."""
    if not callable(factory):
        # ValueError, not TypeError: the api's one error for bad input (specs/api.md)
        raise ValueError(  # noqa: TRY004
            "a face detector factory must be a zero-argument callable returning a "
            "FaceDetector (a class with a `detect(frame_bgr, ts)` method is one), got "
            f"{type(factory).__name__}"
        )
    try:
        detector = factory()
    except Exception as e:
        raise ValueError(
            f"invalid face detector: the factory raised {type(e).__name__}: {e}"
        ) from e
    if not callable(getattr(detector, "detect", None)):
        raise ValueError(  # noqa: TRY004 - ValueError is the api's error for bad input
            f"invalid face detector: {type(detector).__name__} has no callable "
            "`detect(frame_bgr, ts)` method"
        )


def _normalised(u: float, v: float, size: tuple[int, int]) -> tuple[float, float]:
    """A pixel to the tracker's normalised image coordinates, [-1, 1] across the frame
    (the inverse of the tracker's ``u = (x + 1) / 2 * (width - 1)``)."""
    width, height = size
    return (
        u / max(width - 1, 1) * 2.0 - 1.0,
        v / max(height - 1, 1) * 2.0 - 1.0,
    )


def _pixel_centre(face: PixelFace) -> tuple[float, float]:
    """The point the head aims at: the nose when known, else the bbox centre."""
    if face.nose is not None:
        return face.nose
    x, y, w, h = face.bbox
    return (x + w / 2.0, y + h / 2.0)


def _area(face: PixelFace) -> float:
    return face.bbox[2] * face.bbox[3]


def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


class _FaceSelector:
    """Which of a detector's faces is the target (specs/user_perception.md "The
    pipeline"): acquire the largest face above the minimum size, then keep the nearest
    to the previous target while it stays within the jump gate, dropping the association
    after a run of misses. Pure geometry, upstream's rule re-implemented; no smoothing —
    the tracker smooths the aim in the world frame."""

    def __init__(
        self,
        min_area_frac: float = SELECT_MIN_AREA_FRAC,
        max_jump: float = SELECT_MAX_JUMP,
        max_misses: int = SELECT_MAX_MISSES,
    ) -> None:
        self._min_area_frac = min_area_frac
        self._max_jump = max_jump
        self._max_misses = max_misses
        self._centre: tuple[float, float] | None = None  # normalised
        self._misses = 0

    def select(self, faces: Sequence[PixelFace], size: tuple[int, int]) -> int | None:
        """The index of the target face in ``faces``, or ``None`` for no target."""
        width, height = size
        candidates = [
            i
            for i, face in enumerate(faces)
            if _area(face) >= self._min_area_frac * width * height
        ]
        if not candidates:
            self._miss()
            return None
        if self._centre is not None:
            centre = self._centre
            nearest = min(
                candidates,
                key=lambda i: _dist2(
                    _normalised(*_pixel_centre(faces[i]), size), centre
                ),
            )
            if (
                _dist2(_normalised(*_pixel_centre(faces[nearest]), size), centre)
                <= self._max_jump**2
            ):
                return self._keep(nearest, faces, size)
            self._miss()
            if self._centre is not None:
                return None  # the association holds through the miss
        return self._keep(max(candidates, key=lambda i: _area(faces[i])), faces, size)

    def _keep(
        self, index: int, faces: Sequence[PixelFace], size: tuple[int, int]
    ) -> int:
        self._centre = _normalised(*_pixel_centre(faces[index]), size)
        self._misses = 0
        return index

    def _miss(self) -> None:
        self._misses += 1
        if self._misses > self._max_misses:
            self._centre = None


def report_from_pixels(
    faces: Sequence[PixelFace],
    size: tuple[int, int],
    frame: CameraFrame,
    target_index: int | None,
) -> FaceReport:
    """A custom detector's faces on ``frame`` (``size`` = its width, height) as a report:
    every face normalised into the tracker's coordinates, its roll from the eyes when
    given, its size as the bbox height over the frame's; the target face first; the
    frame's ``ts`` and ``head_pose`` carried over."""
    _width, height = size
    order = list(range(len(faces)))
    if target_index is not None:
        order.remove(target_index)
        order.insert(0, target_index)
    reported: list[Face] = []
    for i in order:
        face = faces[i]
        x, y = _normalised(*_pixel_centre(face), size)
        roll = None
        if face.eyes is not None:
            (right_x, right_y), (left_x, left_y) = face.eyes
            roll = math.atan2(left_y - right_y, left_x - right_x)
        reported.append(Face(x=x, y=y, roll=roll, size=face.bbox[3] / max(height, 1)))
    return FaceReport(
        faces=tuple(reported),
        ts=frame.ts,
        source="custom",
        active=True,
        head_pose=frame.head_pose,
    )


# --- the daemon source --------------------------------------------------------------------


def daemon_face_target(robot: AnyReachyMini) -> dict[str, Any]:
    """The daemon's current face target — the ``face_target`` dict of its REST payload
    (``detected``, ``x``, ``y``, ``roll``, ``ts``). Blocking; run it under
    ``asyncio.to_thread``. The fake serves it from its daemon client stand-in."""
    if isinstance(robot, FakeReachyMini):
        return dict(robot.client.face_target)
    payload = _robot.fetch_daemon_json(robot, _DAEMON_FACE_PATH)
    return dict(payload["face_target"])


def report_from_daemon(target: dict[str, Any], *, active: bool) -> FaceReport:
    """A daemon face target as a report: one face when detected, none otherwise."""
    faces: tuple[Face, ...] = ()
    if target.get("detected"):
        roll = target.get("roll")
        faces = (
            Face(
                x=float(target["x"]),
                y=float(target["y"]),
                roll=None if roll is None else float(roll),
                size=None,
            ),
        )
    ts = target.get("ts")
    return FaceReport(
        faces=faces,
        ts=0.0 if ts is None else float(ts),
        source="daemon",
        active=active,
    )


# --- the loop -------------------------------------------------------------------------------


class FaceDetection:
    """The detection loop (specs/user_perception.md "The detection loop"): one asyncio
    task polling the source and publishing on ``faces``, restartable.

    ``on_observation`` receives every poll's report, undebounced (the head tracker's
    feed). In ``daemon`` mode the loop arms the daemon's detector at
    ``DAEMON_DETECT_WEIGHT`` when it starts and disarms it when it stops. In ``custom``
    mode it samples ``feed`` — the camera feed — and runs the detector built by
    ``detector_factory`` once per new frame, off the event loop; the daemon's tracking
    is left alone.
    """

    def __init__(
        self,
        robot: AnyReachyMini,
        *,
        source: str,
        faces: Observable[FaceReport],
        on_observation: Callable[[FaceReport], None] | None = None,
        feed: CameraFeed | None = None,
        detector_factory: FaceDetectorFactory | None = None,
    ) -> None:
        self._robot = robot
        self._source = source
        self._faces = faces
        self._on_observation = on_observation
        self._feed = feed
        self._detector_factory = detector_factory
        self._task: asyncio.Task[None] | None = None
        # Whether this loop sent the daemon its detect weight (and so owes the disarm).
        self._armed = False
        # The custom source's runner state: the detector in use (rebuilt from the factory
        # at start and after `restart`), the selector, the last frame handed over.
        self._detector: FaceDetector | None = None
        self._selector = _FaceSelector()
        self._last_frame_id = 0

    @property
    def running(self) -> bool:
        """Whether the loop's task is running."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start polling: arm the daemon's detector (``daemon``) or build the registered
        detector (``custom``).

        Raises ``ValueError`` for a source this loop cannot run — ``custom`` without a
        camera feed or a registered detector.
        """
        if self.running:
            return
        if self._source == "daemon":
            await self._arm()
        elif self._source == "custom":
            if self._feed is None:
                raise ValueError("the custom detection source needs the camera feed")
            if self._detector_factory is None:
                raise ValueError(
                    "faces.detector is 'custom' but no face detector is registered: "
                    "set FaceSettings.face_detector or call set_face_detector(...)"
                )
            self._detector = None  # built by the first poll, from the factory
        else:
            raise ValueError(f"unknown detection source {self._source!r}")
        self._task = asyncio.create_task(self._run(), name="face-detection")

    def restart(self, detector_factory: FaceDetectorFactory | None) -> None:
        """Register another detector factory (already checked). While the loop runs in
        ``custom`` mode the next poll builds the new detector and starts selecting
        afresh — the swap happens between two polls."""
        self._detector_factory = detector_factory
        self._detector = None

    async def stop(self) -> None:
        """Stop polling, disarm the daemon's detector if this loop armed it, and publish
        the inactive report. A no-op on a loop that never started."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._detector = None
        if self._armed:
            self._armed = False
            await asyncio.to_thread(self._robot.stop_head_tracking)
        if task is not None or self._faces.value.active:
            self._faces.set(FaceReport.inactive(self._source))

    async def _arm(self) -> None:
        self._armed = True  # set first: a cancelled arm still completes in its thread
        await asyncio.to_thread(self._robot.start_head_tracking, DAEMON_DETECT_WEIGHT)

    async def _poll(self) -> FaceReport | None:
        """One observation of the source, or ``None`` when it has nothing new (no frame
        yet, a frame already processed). Raises when the poll fails."""
        if self._source == "daemon":
            target = await asyncio.to_thread(daemon_face_target, self._robot)
            return report_from_daemon(target, active=True)
        return await self._poll_custom()

    async def _poll_custom(self) -> FaceReport | None:
        feed = self._feed
        factory = self._detector_factory
        if feed is None or factory is None:
            return None  # cleared while running: the api stops the loop right after
        detector = self._detector
        if detector is None:
            detector = self._detector = factory()  # checked at registration
            self._selector = _FaceSelector()
        frame = feed.latest()
        if frame is None or frame.frame_id == self._last_frame_id:
            return None  # nothing new: the detector runs once per frame
        # Marked before the detector runs: a frame it raises on is not retried.
        self._last_frame_id = frame.frame_id
        faces = await asyncio.to_thread(detector.detect, frame.image, frame.ts)
        height, width = frame.image.shape[:2]
        size = (int(width), int(height))
        target = self._selector.select(faces, size)
        return report_from_pixels(faces, size, frame, target)

    async def _run(self) -> None:
        published: FaceReport | None = None  # the last value `set`
        lower_since: float | None = None  # when the count first read below `published`
        last: FaceReport | None = None  # the last good observation
        failing_since: float | None = None
        down = False
        while True:
            try:
                report = await self._poll()
            except Exception as e:  # noqa: BLE001 - a failed poll is skipped, never fatal
                _logger.debug("face detection: poll failed: %s", e)
                report = None
            if report is None:
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                if not down and now - failing_since >= FACE_SOURCE_DOWN_S:
                    down = True
                    _logger.warning(
                        "face detection: the %s source has produced no observation "
                        "for %.0f s; reporting detection inactive until it does",
                        self._source,
                        FACE_SOURCE_DOWN_S,
                    )
                    base = last or FaceReport.inactive(self._source)
                    published = replace(base, active=False)
                    self._faces.set(published)
                    lower_since = None
                await asyncio.sleep(1.0 / FACE_POLL_HZ)
                continue
            failing_since = None
            down = False
            last = report
            if self._on_observation is not None:
                self._on_observation(report)
            published, lower_since = self._publish(report, published, lower_since)
            await asyncio.sleep(1.0 / FACE_POLL_HZ)

    def _publish(
        self,
        report: FaceReport,
        published: FaceReport | None,
        lower_since: float | None,
    ) -> tuple[FaceReport | None, float | None]:
        """The debounce: ``set`` on an ``active`` flip or a rise in the count, on a drop
        once it has held ``FACE_ABSENT_S``; ``update`` otherwise. Returns the new
        ``(published, lower_since)``."""
        if (
            published is None
            or published.active != report.active
            or len(report.faces) > len(published.faces)
        ):
            self._faces.set(report)
            return report, None
        if len(report.faces) < len(published.faces):
            now = time.monotonic()
            if lower_since is None:
                lower_since = now
            if now - lower_since >= FACE_ABSENT_S:
                self._faces.set(report)
                return report, None
            self._faces.update(report)
            return published, lower_since
        self._faces.update(report)
        return published, None
