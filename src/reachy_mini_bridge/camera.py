"""Camera feed: ``CameraFeed`` / ``CameraFrame``, the one reader of the robot's camera
(specs/vision/camera.md).

Upstream hands frames out one at a time — ``media.get_frame()`` returns each frame once,
then ``None`` until the next arrives — so two readers in one process steal frames from
each other, silently. The feed owns the read: one thread loops the robot's ``get_frame``
(the only call site of it in the bridge, through :func:`frame_reader`), stamps every
frame with its time and, when it can stand behind it, the head pose at that time, and
publishes the newest frame for any number of consumers to sample at their own rate —
the ``custom`` detection source, a display, a vision graph plugged on by shape.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import BridgeError
from .fake_reachy_mini import FakeReachyMini

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    import numpy.typing as npt

    from .robot import AnyReachyMini

__all__ = [
    "CAMERA_DOWN_S",
    "CAMERA_RETRY_S",
    "CameraFeed",
    "CameraFrame",
    "FrameReader",
    "PoseAt",
    "frame_reader",
]

_logger = logging.getLogger(__name__)

# The reader's failure rules (specs/vision/camera.md "The feed"): a read that raises is retried
# after CAMERA_RETRY_S; one that keeps raising for CAMERA_DOWN_S logs one WARNING (and
# one INFO when frames return). Module constants, read at run time so tests can shorten.
CAMERA_RETRY_S = 0.1
CAMERA_DOWN_S = 5.0

# What the feed reads: the next frame as ``(image, capture_time)`` — ``capture_time`` the
# frame's time on ``time.monotonic()``'s clock when the backend knows it, ``None`` when
# only the arrival is known — or ``None`` for no frame yet (specs/vision/camera.md "The frame's
# time and the head pose": a head pose is attached only to a capture time).
type FrameReader = Callable[[], tuple[npt.NDArray[np.uint8], float | None] | None]
# The head pose the robot reported at a monotonic time (the motion loop's head_pose_at).
type PoseAt = Callable[[float], npt.NDArray[np.float64]]


@dataclass(frozen=True)
class CameraFrame:
    """One published frame (specs/vision/camera.md "The frame")."""

    frame_id: int  # 1, 2, 3, … per feed — the key a result is matched on
    ts: float  # the frame's time on the monotonic clock: capture when known, else arrival
    # (H, W, 3) BGR, the array upstream returned — shared by reference with every
    # consumer, so read-only by convention: whoever draws on it copies first.
    image: npt.NDArray[np.uint8] = field(compare=False)
    # The head's 4x4 pose at ``ts``, when the feed knows it (a capture time and a motion
    # session); None otherwise — never a pose looked up at an arrival time.
    head_pose: npt.NDArray[np.float64] | None = field(default=None, compare=False)


def frame_reader(robot: AnyReachyMini) -> FrameReader:
    """The feed's reader for ``robot`` — the bridge's one call site of ``media.get_frame``.

    The fake synthesises its frame inside the call, so the instant it returns is the
    frame's capture time and a pose can be attached. Upstream's client pipeline knows no
    capture time (specs/vision/camera.md "The frame's time and the head pose": the appsink's
    buffers reach the client with their ``pts`` zeroed), so a robot's or the sim's frames
    carry their arrival time only, and the head tracker estimates the delay.
    """
    get_frame = robot.media.get_frame
    if isinstance(robot, FakeReachyMini):

        def read_fake() -> tuple[npt.NDArray[np.uint8], float | None] | None:
            image = get_frame()
            return None if image is None else (image, time.monotonic())

        return read_fake

    def read() -> tuple[npt.NDArray[np.uint8], float | None] | None:
        image = get_frame()
        return None if image is None else (image, None)

    return read


class CameraFeed:
    """The one reader of the robot's camera (specs/vision/camera.md "The feed"): ``start()``
    spawns the reader thread, ``latest()`` is the newest frame from any thread,
    ``stop()`` joins the thread and resets ``latest()`` to ``None``.

    Built unbound by the api (``api.camera`` exists from construction) and bound to the
    session's robot and motion session at entry through :meth:`bind`; a test binds at
    construction. ``frame_id`` counts on across ``start()`` / ``stop()``.
    """

    def __init__(
        self, read_frame: FrameReader | None = None, pose_at: PoseAt | None = None
    ) -> None:
        self._read_frame = read_frame
        self._pose_at = pose_at
        self._lock = threading.Lock()
        self._latest: CameraFrame | None = None
        self._count = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def bind(self, read_frame: FrameReader, pose_at: PoseAt | None) -> None:
        """Give the feed its reader and pose source (before ``start()``)."""
        if self._thread is not None:
            raise BridgeError("the camera feed cannot be rebound while it runs")
        self._read_frame = read_frame
        self._pose_at = pose_at

    @property
    def running(self) -> bool:
        """Whether the reader thread runs."""
        return self._thread is not None

    @property
    def published_count(self) -> int:
        """Frames published, ever, on this feed — the last frame's ``frame_id``."""
        with self._lock:
            return self._count

    def latest(self) -> CameraFrame | None:
        """The newest frame, or ``None`` before the first and after ``stop()``."""
        with self._lock:
            return self._latest

    def start(self) -> None:
        """Start the reader thread. A no-op while it runs; ``BridgeError`` unbound."""
        if self._thread is not None:
            return
        if self._read_frame is None:
            raise BridgeError("the camera feed has no reader: bind it to a robot first")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="reachy-mini-camera", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the reader thread (blocking: joins it) and reset ``latest()`` to
        ``None``, so nobody keeps reading a frame from a camera that is gone."""
        thread = self._thread
        if thread is not None:
            self._stop.set()
            thread.join()
            self._thread = None
        with self._lock:
            self._latest = None

    # --- the thread ---

    def _run(self) -> None:
        read_frame = self._read_frame
        assert read_frame is not None  # start() checked
        failing_since: float | None = None
        down = False
        while not self._stop.is_set():
            try:
                got = read_frame()
            except Exception as e:  # noqa: BLE001 - a failed read is retried, never fatal
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                    _logger.debug("camera feed: read failed: %s", e)
                if not down and now - failing_since >= CAMERA_DOWN_S:
                    down = True
                    _logger.warning(
                        "camera feed: reading the camera has failed for %.0f s (%s); "
                        "the last frame stays published until frames return",
                        CAMERA_DOWN_S,
                        e,
                    )
                self._stop.wait(CAMERA_RETRY_S)
                continue
            failing_since = None
            if down:
                down = False
                _logger.info("camera feed: frames are back")
            if got is None:
                continue  # no frame yet: one more pass (the read itself waited)
            image, capture_ts = got
            ts = time.monotonic() if capture_ts is None else capture_ts
            head_pose: npt.NDArray[np.float64] | None = None
            if capture_ts is not None and self._pose_at is not None:
                try:
                    head_pose = self._pose_at(ts)
                except Exception as e:  # noqa: BLE001 - a frame without a pose beats no frame
                    _logger.debug("camera feed: no head pose for the frame: %s", e)
            with self._lock:
                self._count += 1
                self._latest = CameraFrame(self._count, ts, image, head_pose)
