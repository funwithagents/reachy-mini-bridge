"""Face detection: the detection loop running one detector over the camera feed and the
observable face report (specs/vision/user_perception.md).

The loop samples the camera feed ([camera](camera.py)) at ``FACE_POLL_HZ``, hands each
new frame to its detector — the shipped ``yunet`` ([yunet](yunet.py), upstream's model)
or a developer's ``custom`` ``FaceDetector`` — carries every face's ``track_id`` from
frame to frame, turns the result into a ``FaceReport``, ``update``s the bridge's ``Observable[FaceReport]`` on every
observation and ``set``s it (wakes subscribers) only when the face count changes — a rise
at once, a drop once it has held for ``FACE_ABSENT_S`` — or when ``active`` flips.
Detection is opt-in: a config names the detector, and with none nothing runs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import numpy as np
    import numpy.typing as npt

    from .camera import CameraFeed, CameraFrame

from .concurrency import owned
from .observable import Observable

__all__ = [
    "DETECT_WIDTH",
    "FACE_ABSENT_S",
    "FACE_COST_LOG_S",
    "FACE_DETECTOR_NAMES",
    "FACE_POLL_HZ",
    "FACE_SOURCE_DOWN_S",
    "TRACK_MAX_JUMP_FACES",
    "TRACK_MAX_MISSES",
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
# The width the shipped detector works at by default (`face_detection.width`): frames are
# subsampled to about this width before detection — upstream's own tracker detects at
# 320 px wide — so the detector keeps up with the feed on one CPU thread whatever the
# camera's resolution (1280 wide → stride 4, the Lite's 1920 → stride 6).
DETECT_WIDTH = 320
# The detector's cost (mean detect time, observations per second) is logged once per run,
# this long after its first observation — the numbers `width` / `target_fps` are tuned by.
FACE_COST_LOG_S = 10.0
# The tracks' gates (specs/vision/user_perception.md "Tracks"), upstream's association values:
# a face continues a track whose last centre lies within this many face sizes of its
# own (the smaller of the two boxes' larger sides, in pixels); a track is dropped after
# this many consecutive observations without its face. Whom the head follows is the head tracker's choice
# (specs/motion/head_tracking.md "Whom the head follows").
TRACK_MAX_JUMP_FACES = 1.5  # a face continues a track within this many of its sizes
TRACK_MAX_MISSES = 20

# The detectors a config names (specs/vision/user_perception.md "Detectors"); `None` is none.
FACE_DETECTOR_NAMES = ("yunet", "custom")


@dataclass(frozen=True)
class Face:
    """One face in front of the robot (specs/vision/user_perception.md "The face report"): in
    the tracker's normalised image coordinates, under the track that follows it."""

    x: float  # [-1, 1], x right; the nose when known, else the bbox centre
    y: float  # [-1, 1], y down; (0, 0) is the image centre
    # head roll in radians: the detector's fitted orientation when it gives one, else from
    # the eye line; None when it gives neither
    roll: float | None
    size: float  # bbox height as a fraction of the frame height
    # the same positive integer for the same person from frame to frame, never reused
    # (0 only on a Face built outside the loop)
    track_id: int = 0
    # x, y, width, height in pixels of the report's frame
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    # head pitch (positive down) / yaw (positive toward the image's right) in radians,
    # from a detector that fits a head; None otherwise
    pitch: float | None = None
    yaw: float | None = None


@dataclass(frozen=True)
class FaceReport:
    """Who the detection loop sees: the value of ``bridge.faces``."""

    faces: tuple[
        Face, ...
    ]  # every face the detector reports, by track_id (oldest first)
    ts: float  # the frame's time (the bridge's monotonic clock, specs/vision/camera.md)
    source: str | None  # the detector's name: "yunet" | "custom"; None when none is set
    active: bool  # a detector is running; False means "unknown", not "nobody"
    # The head pose the frame was captured from, when the camera feed could stamp it.
    head_pose: npt.NDArray[np.float64] | None = field(default=None, compare=False)
    # The camera frame the faces were found in (shared read-only, specs/vision/camera.md);
    # None while inactive. Kept by reference until the next observation.
    frame: CameraFrame | None = field(default=None, compare=False)

    @property
    def frame_id(self) -> int:
        """The frame's ``frame_id`` (0 without a frame): what a vision graph node
        stale-skips on when it samples the bridge's faces."""
        return 0 if self.frame is None else self.frame.frame_id

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
    # roll, pitch, yaw in radians from a detector that fits a head, in the report's
    # convention (specs/vision/user_perception.md "The face report"); preferred over the eyes
    orientation: tuple[float, float, float] | None = None


class FaceDetector(Protocol):
    """A developer's face detector for the ``custom`` source.

    ``detect`` runs on a worker thread once per new camera frame — the frame is the
    feed's, shared and read-only (copy before drawing) — and returns the faces it sees,
    in any order, within a frame period (a slower call skips frames, never queues them).
    It never touches the robot or the bridge; its dependencies are its own. A detector
    may also have a ``close()``, called on a worker thread when the loop lets go of it.
    """

    def detect(
        self, frame_bgr: npt.NDArray[np.uint8], ts: float
    ) -> Sequence[PixelFace]: ...


type FaceDetectorFactory = Callable[[], FaceDetector]


def check_face_detector_factory(factory: object) -> None:
    """Registration-time check (specs/vision/user_perception.md "Custom detectors"):
    ``ValueError`` unless ``factory`` is callable. Nothing is built here — the loop
    builds the detector when it starts, on a worker thread, and validates it then
    (:meth:`FaceDetection.start`)."""
    if not callable(factory):
        # ValueError, not TypeError: the bridge's one error for bad input (specs/core/bridge.md)
        raise ValueError(  # noqa: TRY004
            "a face detector factory must be a zero-argument callable returning a "
            "FaceDetector (a class with a `detect(frame_bgr, ts)` method is one), got "
            f"{type(factory).__name__}"
        )


def _check_face_detector(detector: object) -> FaceDetector:
    """What a factory built, or ``ValueError`` when it has no callable ``detect``."""
    if not callable(getattr(detector, "detect", None)):
        raise ValueError(  # noqa: TRY004 - ValueError is the bridge's error for bad input
            f"invalid face detector: {type(detector).__name__} has no callable "
            "`detect(frame_bgr, ts)` method"
        )
    return cast("FaceDetector", detector)


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


def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


@dataclass
class _Track:
    track_id: int
    centre: tuple[float, float]  # in pixels, on its last observation
    size: float  # the larger side of its box, in pixels, on its last observation
    misses: int = 0


def _pixel_size(face: PixelFace) -> float:
    return max(face.bbox[2], face.bbox[3], 1.0)


class _FaceTracks:
    """Every face's ``track_id`` (specs/vision/user_perception.md "Tracks"): each observation's
    faces continue the tracks whose centres lie nearest, within ``TRACK_MAX_JUMP_FACES``
    face sizes (a size is a box's larger side, in pixels; the smaller of the track's and
    the face's, so a small face near a large track's last place is held to its own
    size) — pairs taken nearest first,
    each track and face used once; an unmatched face opens a
    track with ``new_id()``; an unmatched track counts a miss and is dropped after
    ``TRACK_MAX_MISSES``. Pure geometry, no smoothing, no face singled out — whom the
    head follows is the head tracker's choice."""

    def __init__(
        self,
        new_id: Callable[[], int],
        max_jump: float | None = None,
        max_misses: int | None = None,
    ) -> None:
        self._new_id = new_id
        self._max_jump = max_jump
        self._max_misses = max_misses
        self._tracks: list[_Track] = []

    def update(self, faces: Sequence[PixelFace], size: tuple[int, int]) -> list[int]:
        """The ``track_id`` of each of ``faces``, in their order."""
        max_jump = TRACK_MAX_JUMP_FACES if self._max_jump is None else self._max_jump
        max_misses = TRACK_MAX_MISSES if self._max_misses is None else self._max_misses
        del size  # the gate is in the face's own pixels, whatever the frame's size
        centres = [_pixel_centre(face) for face in faces]
        sizes = [_pixel_size(face) for face in faces]
        pairs = sorted(
            (d2, t, f)
            for t, track in enumerate(self._tracks)
            for f, centre in enumerate(centres)
            if (d2 := _dist2(track.centre, centre))
            <= (max_jump * min(track.size, sizes[f])) ** 2
        )
        ids: list[int | None] = [None] * len(faces)
        matched: set[int] = set()
        for _d2, t, f in pairs:
            if t in matched or ids[f] is not None:
                continue
            track = self._tracks[t]
            track.centre, track.size, track.misses = centres[f], sizes[f], 0
            ids[f] = track.track_id
            matched.add(t)
        survivors: list[_Track] = []
        for t, track in enumerate(self._tracks):
            if t not in matched:
                track.misses += 1
                if track.misses > max_misses:
                    continue
            survivors.append(track)
        for f, track_id in enumerate(ids):
            if track_id is None:
                track = _Track(self._new_id(), centres[f], sizes[f])
                survivors.append(track)
                ids[f] = track.track_id
        self._tracks = survivors
        return [track_id for track_id in ids if track_id is not None]


def report_from_pixels(
    faces: Sequence[PixelFace],
    size: tuple[int, int],
    frame: CameraFrame,
    track_ids: Sequence[int],
    *,
    source: str = "custom",
) -> FaceReport:
    """A detector's faces on ``frame`` (``size`` = its width, height) as a report:
    every face normalised into the tracker's coordinates under its ``track_id``, its
    pixel box kept, its roll / pitch / yaw from the detector's ``orientation`` when given
    (else its roll from the eyes), its size as the bbox height over the frame's; the
    faces in ``track_id`` order; the frame, its ``ts`` and ``head_pose`` carried over."""
    _width, height = size
    reported: list[Face] = []
    for face, track_id in sorted(
        zip(faces, track_ids, strict=True), key=lambda p: p[1]
    ):
        x, y = _normalised(*_pixel_centre(face), size)
        roll = pitch = yaw = None
        if face.orientation is not None:
            roll, pitch, yaw = (float(a) for a in face.orientation)
        elif face.eyes is not None:
            (right_x, right_y), (left_x, left_y) = face.eyes
            roll = math.atan2(left_y - right_y, left_x - right_x)
        reported.append(
            Face(
                x=x,
                y=y,
                roll=roll,
                size=face.bbox[3] / max(height, 1),
                track_id=track_id,
                bbox=(
                    float(face.bbox[0]),
                    float(face.bbox[1]),
                    float(face.bbox[2]),
                    float(face.bbox[3]),
                ),
                pitch=pitch,
                yaw=yaw,
            )
        )
    return FaceReport(
        faces=tuple(reported),
        ts=frame.ts,
        source=source,
        active=True,
        head_pose=frame.head_pose,
        frame=frame,
    )


def _yunet_factory(width: int | None = DETECT_WIDTH) -> FaceDetector:
    """The shipped detector's factory (specs/vision/user_perception.md "The shipped detector"),
    at the config's ``width`` (``None``: the full frame); imported here, not at module load,
    since `yunet.py` imports this module."""
    from .yunet import YuNetDetector

    return YuNetDetector(width=width)


# --- the loop -------------------------------------------------------------------------------


class FaceDetection:
    """The detection loop (specs/vision/user_perception.md "The detection loop"): one asyncio
    task sampling the camera feed, running one detector once per new frame and
    publishing on ``faces``, restartable.

    ``detector`` names what runs — ``"yunet"`` (the shipped detector), ``"custom"`` (the
    factory registered through ``detector_factory``) or ``None`` (nothing: ``start``
    refuses). ``on_observation`` hears every poll, undebounced — the observation's
    report, or ``None`` for a poll that produced none (the head tracker's feed, and
    its clock while the camera is silent). The detector is built from its factory when
    the loop starts, on a worker thread (a build may load a model), and validated
    then; the daemon's own tracking is never touched.
    """

    def __init__(
        self,
        *,
        detector: str | None,
        faces: Observable[FaceReport],
        on_observation: Callable[[FaceReport | None], None] | None = None,
        feed: CameraFeed | None = None,
        detector_factory: FaceDetectorFactory | None = None,
        width: int | None = DETECT_WIDTH,
        target_fps: float | None = None,
        new_track_id: Callable[[], int] | None = None,
    ) -> None:
        self._name = detector
        self._width = width
        self._target_fps = target_fps
        self._faces = faces
        self._on_observation = on_observation
        self._feed = feed
        self._detector_factory = detector_factory
        self._task: asyncio.Task[None] | None = None
        # start() and stop() run one at a time (specs/vision/user_perception.md
        # "Lifecycle"): a second start during one in flight waits and finds the loop
        # running; a stop during a start waits for the build and stops what it built.
        self._transition = asyncio.Lock()
        # A cancelled caller cannot cancel a factory's worker. Retain its disposal
        # task until it finishes, and drain those tasks when the loop closes.
        self._acquisition_cleanups: set[asyncio.Task[None]] = set()
        self._factory_generation = 0
        # The runner state: the detector in use (built from the factory at start and
        # rebuilt after `restart`), the tracks, the last frame handed over. Track ids
        # come from a counter that outlives runs, so an id is never reused.
        self._detector: FaceDetector | None = None
        # A detector replaced by `restart`, released before its successor is built.
        self._retired: FaceDetector | None = None
        # detect and close never overlap: a stop that cancels the loop mid-detect releases
        # the detector only once that call has returned (the call runs on in its thread).
        self._detector_lock = threading.Lock()
        self._next_track_id = 1
        self._allocate_track_id = new_track_id or self._new_track_id
        self._tracks = _FaceTracks(self._allocate_track_id)
        self._last_frame_id = 0
        # target_fps: when the detector last started; the cost log's window.
        self._last_detect_at: float | None = None
        self._cost_since: float | None = None
        self._cost_calls = 0
        self._cost_total_s = 0.0
        self._cost_logged = False

    def _new_track_id(self) -> int:
        track_id = self._next_track_id
        self._next_track_id += 1
        return track_id

    @property
    def running(self) -> bool:
        """Whether the loop's task is running."""
        return self._task is not None and not self._task.done()

    def _factory(self) -> FaceDetectorFactory:
        """The factory of the configured detector, or ``ValueError`` when the loop
        cannot run: no detector named, ``custom`` with none registered, no feed."""
        if self._name is None:
            raise ValueError(
                "no face detector is configured (face_detection.detector is null): name one — "
                '"yunet", the shipped detector, or "custom" with a registered factory'
            )
        if self._name == "yunet":
            width = self._width

            def factory() -> FaceDetector:
                return _yunet_factory(width)

        elif self._name == "custom":
            if self._detector_factory is None:
                raise ValueError(
                    "face_detection.detector is 'custom' but no face detector is registered: "
                    "set FaceDetectionSettings.face_detector or call set_face_detector(...)"
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
        ``custom`` with none registered, no camera feed, a factory's result without a
        callable ``detect`` — released through its ``close()`` if it has one) and
        whatever the detector's factory raises — a model that cannot load — with the
        loop left not running.
        """
        async with self._transition:
            if self.running:
                return
            factory = self._factory()
            self._detector = await self._acquire(factory)
            self._tracks = _FaceTracks(self._allocate_track_id)
            self._last_detect_at = None
            self._cost_since, self._cost_calls, self._cost_total_s = None, 0, 0.0
            self._cost_logged = False
            self._task = asyncio.create_task(self._run(), name="face-detection")

    def restart(self, detector_factory: FaceDetectorFactory | None) -> None:
        """Register another detector factory (already checked). While the loop runs in
        ``custom`` mode the next poll builds the new detector (on a worker thread) and
        starts its tracks afresh — the swap happens between two polls, the replaced
        detector released (``close()``) before its successor is built."""
        self._detector_factory = detector_factory
        self._factory_generation += 1
        if self._detector is not None:
            self._retired = self._detector
        self._detector = None

    async def stop(self) -> None:
        """Stop sampling and publish the inactive report. A no-op on a loop that never
        started; a stop during a ``start()`` waits for it and stops the loop it built.
        Owned once called (specs/vision/user_perception.md "Lifecycle"): a cancel of the
        caller is absorbed until the loop has stopped and its detector is released, and
        propagates then — the switch that asked for the stop stands."""
        if await owned(self._stop()):
            raise asyncio.CancelledError()

    async def _stop(self) -> None:
        async with self._transition:
            task = self._task
            self._task = None
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task})  # its own cancel is its outcome, not ours
            if self._acquisition_cleanups:
                await asyncio.shield(asyncio.gather(*self._acquisition_cleanups))
            detector, retired = self._detector, self._retired
            self._detector = self._retired = None
            for released in (retired, detector):
                if released is not None:
                    await self._release(released)
            if task is not None or self._faces.value.active:
                self._faces.set(FaceReport.inactive(self._name))

    async def _acquire(self, factory: FaceDetectorFactory) -> FaceDetector:
        build = asyncio.create_task(asyncio.to_thread(factory))
        try:
            built = await asyncio.shield(build)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._discard_acquisition(build))
            self._acquisition_cleanups.add(cleanup)
            cleanup.add_done_callback(self._acquisition_cleanups.discard)
            raise
        try:
            return _check_face_detector(built)
        except ValueError:
            await self._release(built)
            raise

    async def _discard_acquisition(self, build: asyncio.Task[FaceDetector]) -> None:
        try:
            built = await build
        except Exception as e:  # noqa: BLE001 - an abandoned factory has no caller
            _logger.debug("face detection: abandoned factory failed: %s", e)
        else:
            await self._release(built)

    async def _release(self, detector: object) -> None:
        """Call the detector's ``close()``, when it has one, on a worker thread — once
        any ``detect`` in flight has returned. A raise is logged and ignored."""
        await asyncio.to_thread(self._close_detector, detector)

    def _close_detector(self, detector: object) -> None:
        close = getattr(detector, "close", None)
        if not callable(close):
            return
        try:
            with self._detector_lock:
                close()
        except Exception as e:  # noqa: BLE001 - releasing is best effort
            _logger.debug("face detection: the detector's close() failed: %s", e)

    def _locked_detect(
        self, detector: FaceDetector, image: npt.NDArray[np.uint8], ts: float
    ) -> tuple[Sequence[PixelFace], float]:
        """``detect`` under the lock ``close`` takes, timed."""
        with self._detector_lock:
            started = time.monotonic()
            faces = detector.detect(image, ts)
            return faces, time.monotonic() - started

    def _account(self, call_s: float) -> None:
        """The cost log (specs/vision/user_perception.md "The detection loop"): one ``INFO``
        line, ``FACE_COST_LOG_S`` after the run's first observation."""
        if self._cost_logged:
            return
        now = time.monotonic()
        if self._cost_since is None:
            self._cost_since = now
        self._cost_calls += 1
        self._cost_total_s += call_s
        elapsed = now - self._cost_since
        if elapsed >= FACE_COST_LOG_S:
            self._cost_logged = True
            _logger.info(
                "face detection: the %s detector (width %s, target_fps %s) takes %.1f ms "
                "a frame on average, %.1f observations/s over %.0f s",
                self._name,
                "full frame" if self._width is None else self._width,
                "none" if self._target_fps is None else f"{self._target_fps:g}",
                1000.0 * self._cost_total_s / self._cost_calls,
                (self._cost_calls - 1) / elapsed,
                elapsed,
            )

    async def _poll(self) -> FaceReport | None:
        """One observation, or ``None`` when the feed has nothing new (no frame yet, a
        frame already processed). Raises when the detector fails."""
        feed = self._feed
        if feed is None or self._name is None:
            return None
        detector = self._detector
        if detector is None:
            generation = self._factory_generation
            factory = self._detector_factory if self._name == "custom" else None
            if factory is None:
                return (
                    None  # cleared while running: the bridge stops the loop right after
                )
            retired, self._retired = self._retired, None
            if retired is not None:
                await self._release(retired)
            detector = await self._acquire(factory)
            if generation != self._factory_generation:
                await self._release(detector)
                return None
            self._detector = detector
            self._tracks = _FaceTracks(self._allocate_track_id)
        frame = feed.latest()
        if frame is None or frame.frame_id == self._last_frame_id:
            return None  # nothing new: the detector runs once per frame
        # Marked before the detector runs: a frame it raises on — or one skipped under
        # the rate ceiling — is not retried.
        self._last_frame_id = frame.frame_id
        now = time.monotonic()
        target_fps = self._target_fps
        if (
            target_fps is not None
            and self._last_detect_at is not None
            and now - self._last_detect_at < 1.0 / target_fps
        ):
            return None  # under the ceiling: the next frame after the period runs
        self._last_detect_at = now
        faces, call_s = await asyncio.to_thread(
            self._locked_detect, detector, frame.image, frame.ts
        )
        self._account(call_s)
        height, width = frame.image.shape[:2]
        size = (int(width), int(height))
        track_ids = self._tracks.update(faces, size)
        return report_from_pixels(faces, size, frame, track_ids, source=self._name)

    async def _run(self) -> None:
        published: FaceReport | None = None  # the last value `set`
        lower_since: float | None = None  # when the count first read below `published`
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
                down_after = FACE_SOURCE_DOWN_S
                if self._target_fps is not None:  # a slow ceiling is not a failure
                    down_after = max(down_after, 2.0 / self._target_fps)
                if not down and now - failing_since >= down_after:
                    down = True
                    _logger.warning(
                        "face detection: the %s detector has produced no observation "
                        "for %.0f s (no camera frame, or it keeps failing); reporting "
                        "detection inactive until it does",
                        self._name,
                        down_after,
                    )
                    published = FaceReport.inactive(self._name)
                    self._faces.set(published)
                    lower_since = None
                if self._on_observation is not None:
                    self._on_observation(None)  # the tracker keeps time
                await asyncio.sleep(1.0 / FACE_POLL_HZ)
                continue
            failing_since = None
            down = False
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
