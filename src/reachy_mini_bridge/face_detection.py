"""Face detection: the detection loop running one detector over the camera feed and the
observable face report (specs/vision/user_perception.md).

The loop samples the camera feed ([camera](camera.py)) at ``FACE_POLL_HZ``, hands each
new frame to its detector — the shipped ``yunet`` ([yunet](yunet.py), upstream's model)
or a developer's ``custom`` ``FaceDetector`` — selects the target face itself, turns the
result into a ``FaceReport``, ``update``s the api's ``Observable[FaceReport]`` on every
observation and ``set``s it (wakes subscribers) only when the face count changes — a rise
at once, a drop once it has held for ``FACE_ABSENT_S`` — or when ``active`` flips.
Detection is opt-in: a config names the detector, and with none nothing runs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import numpy as np
    import numpy.typing as npt

    from .camera import CameraFeed, CameraFrame
    from .observable import Observable

__all__ = [
    "FACE_ABSENT_S",
    "FACE_DETECTOR_NAMES",
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
    "report_from_pixels",
]

_logger = logging.getLogger(__name__)

# The detection loop's timing (specs/vision/user_perception.md "The detection loop"). Module
# constants, not config; read at run time so tests can shorten them.
# Three polls per frame: a local daemon's camera feed is capped at 10 fps
# (media_server.IPC_FPS), and sampling it at 10 Hz would add up to a frame's worth of
# delay and alias; at 30 Hz a new frame is picked up within ~33 ms, and the detector
# runs once per new frame whatever the poll rate.
FACE_POLL_HZ = 30.0
FACE_ABSENT_S = 0.3  # a drop in the count is published once it has held this long
FACE_SOURCE_DOWN_S = 5.0  # a detector producing nothing this long reads as not looking
# The selection gates (specs/vision/user_perception.md "The pipeline"), upstream's own values
# so the bridge selects as the daemon's tracker does: acquire the largest face above this
# fraction of the frame's area, keep the nearest face within this jump (normalised image
# units, [-1, 1] across the frame), drop the association after this many consecutive
# misses.
SELECT_MIN_AREA_FRAC = 0.003
SELECT_MAX_JUMP = 0.5
SELECT_MAX_MISSES = 20

# The detectors a config names (specs/vision/user_perception.md "Detectors"); `None` is none.
FACE_DETECTOR_NAMES = ("yunet", "custom")


@dataclass(frozen=True)
class Face:
    """One face in front of the robot, in the tracker's normalised image coordinates."""

    x: float  # [-1, 1], x right; the nose when known, else the bbox centre
    y: float  # [-1, 1], y down; (0, 0) is the image centre
    roll: float | None  # head roll in radians from the eye line; None when unknown
    size: float  # bbox height as a fraction of the frame height


@dataclass(frozen=True)
class FaceReport:
    """Who the detection loop sees: the value of ``api.faces``."""

    faces: tuple[Face, ...]  # every face the detector reports; the target face first
    ts: float  # the frame's time (the bridge's monotonic clock, specs/vision/camera.md)
    source: str | None  # the detector's name: "yunet" | "custom"; None when none is set
    active: bool  # a detector is running; False means "unknown", not "nobody"
    # The head pose the frame was captured from, when the camera feed could stamp it.
    head_pose: npt.NDArray[np.float64] | None = field(default=None, compare=False)

    @classmethod
    def inactive(cls, source: str | None) -> FaceReport:
        """The report while no detector is looking (before entry, after exit)."""
        return cls(faces=(), ts=0.0, source=source, active=False)


# --- custom detectors (specs/vision/user_perception.md "Custom detectors") ---------------------


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
    """Registration-time check (specs/vision/user_perception.md "Custom detectors"):
    ``ValueError`` unless ``factory`` is a callable whose result has a callable
    ``detect``. Calls the factory once, on the caller's thread."""
    if not callable(factory):
        # ValueError, not TypeError: the api's one error for bad input (specs/core/api.md)
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
    """Which of a detector's faces is the target (specs/vision/user_perception.md "The
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
    *,
    source: str = "custom",
) -> FaceReport:
    """A detector's faces on ``frame`` (``size`` = its width, height) as a report:
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
        source=source,
        active=True,
        head_pose=frame.head_pose,
    )


def _yunet_factory() -> FaceDetector:
    """The shipped detector's factory (specs/vision/user_perception.md "The shipped detector");
    imported here, not at module load, since `yunet.py` imports this module."""
    from .yunet import YuNetDetector

    return YuNetDetector()


# --- the loop -------------------------------------------------------------------------------


class FaceDetection:
    """The detection loop (specs/vision/user_perception.md "The detection loop"): one asyncio
    task sampling the camera feed, running one detector once per new frame and
    publishing on ``faces``, restartable.

    ``detector`` names what runs — ``"yunet"`` (the shipped detector), ``"custom"`` (the
    factory registered through ``detector_factory``) or ``None`` (nothing: ``start``
    refuses). ``on_observation`` receives every observation's report, undebounced (the
    head tracker's feed). The detector is built from its factory when the loop starts,
    on a worker thread (a build may load a model); the daemon's own tracking is never
    touched.
    """

    def __init__(
        self,
        *,
        detector: str | None,
        faces: Observable[FaceReport],
        on_observation: Callable[[FaceReport], None] | None = None,
        feed: CameraFeed | None = None,
        detector_factory: FaceDetectorFactory | None = None,
    ) -> None:
        self._name = detector
        self._faces = faces
        self._on_observation = on_observation
        self._feed = feed
        self._detector_factory = detector_factory
        self._task: asyncio.Task[None] | None = None
        # The runner state: the detector in use (built from the factory at start and
        # rebuilt after `restart`), the selector, the last frame handed over.
        self._detector: FaceDetector | None = None
        self._selector = _FaceSelector()
        self._last_frame_id = 0

    @property
    def running(self) -> bool:
        """Whether the loop's task is running."""
        return self._task is not None and not self._task.done()

    def _factory(self) -> FaceDetectorFactory:
        """The factory of the configured detector, or ``ValueError`` when the loop
        cannot run: no detector named, ``custom`` with none registered, no feed."""
        if self._name is None:
            raise ValueError(
                "no face detector is configured (faces.detector is null): name one — "
                '"yunet", the shipped detector, or "custom" with a registered factory'
            )
        if self._name == "yunet":
            factory: FaceDetectorFactory = _yunet_factory
        elif self._name == "custom":
            if self._detector_factory is None:
                raise ValueError(
                    "faces.detector is 'custom' but no face detector is registered: "
                    "set FaceSettings.face_detector or call set_face_detector(...)"
                )
            factory = self._detector_factory
        else:
            raise ValueError(f"unknown face detector {self._name!r}")
        if self._feed is None:
            raise ValueError("the detection loop needs the camera feed")
        return factory

    async def start(self) -> None:
        """Build the detector (on a worker thread) and start sampling the feed.

        Raises ``ValueError`` for a detector this loop cannot run (no detector named,
        ``custom`` with none registered, no camera feed) and whatever the detector's
        factory raises — a model that cannot load — with the loop left not running.
        """
        if self.running:
            return
        factory = self._factory()
        self._detector = await asyncio.to_thread(factory)
        self._selector = _FaceSelector()
        self._task = asyncio.create_task(self._run(), name="face-detection")

    def restart(self, detector_factory: FaceDetectorFactory | None) -> None:
        """Register another detector factory (already checked). While the loop runs in
        ``custom`` mode the next poll builds the new detector (on a worker thread) and
        starts selecting afresh — the swap happens between two polls."""
        self._detector_factory = detector_factory
        self._detector = None

    async def stop(self) -> None:
        """Stop sampling and publish the inactive report. A no-op on a loop that never
        started."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._detector = None
        if task is not None or self._faces.value.active:
            self._faces.set(FaceReport.inactive(self._name))

    async def _poll(self) -> FaceReport | None:
        """One observation, or ``None`` when the feed has nothing new (no frame yet, a
        frame already processed). Raises when the detector fails."""
        feed = self._feed
        if feed is None or self._name is None:
            return None
        detector = self._detector
        if detector is None:
            factory = self._detector_factory if self._name == "custom" else None
            if factory is None:
                return None  # cleared while running: the api stops the loop right after
            detector = self._detector = await asyncio.to_thread(factory)
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
        return report_from_pixels(faces, size, frame, target, source=self._name)

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
                        "face detection: the %s detector has produced no observation "
                        "for %.0f s (no camera frame, or it keeps failing); reporting "
                        "detection inactive until it does",
                        self._name,
                        FACE_SOURCE_DOWN_S,
                    )
                    base = last or FaceReport.inactive(self._name)
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
