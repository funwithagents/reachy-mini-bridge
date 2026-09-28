"""Face detection: the detection loop over a pluggable source and the observable face
report (specs/user_perception.md).

The loop polls its source at ``FACE_POLL_HZ``, turns each observation into a
``FaceReport``, ``update``s the api's ``Observable[FaceReport]`` on every poll and
``set``s it (wakes subscribers) only when the face count changes — a rise at once, a
drop once it has held for ``FACE_ABSENT_S`` — or when ``active`` flips. The ``daemon``
source reads the daemon's own detector over its HTTP API; the bridge ships no vision
code.
"""

from __future__ import annotations

import asyncio
import logging
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

    from .observable import Observable
    from .robot import AnyReachyMini

__all__ = [
    "DAEMON_DETECT_WEIGHT",
    "FACE_ABSENT_S",
    "FACE_POLL_HZ",
    "FACE_SOURCE_DOWN_S",
    "Face",
    "FaceDetection",
    "FaceDetector",
    "FaceReport",
    "PixelFace",
    "daemon_face_target",
    "report_from_daemon",
]

_logger = logging.getLogger(__name__)

# The detection loop's timing (specs/user_perception.md "The detection loop"). Module
# constants, not config; read at run time so tests can shorten them.
# Three polls per observation: upstream's detector sees the daemon's local camera feed,
# capped at 10 fps (media_server.IPC_FPS), and polling that at 10 Hz would add up to a
# frame's worth of delay and alias; at 30 Hz each observation arrives within ~33 ms, for
# a sub-millisecond HTTP read on loopback.
FACE_POLL_HZ = 30.0
FACE_ABSENT_S = 0.3  # a drop in the count is published once it has held this long
FACE_SOURCE_DOWN_S = 5.0  # a source failing this long reads as not looking
# The weight the `daemon` source arms the daemon's tracker at: the daemon runs its
# detector only above zero, and blends its own aim into the head by this much.
DAEMON_DETECT_WEIGHT = 0.001

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


@dataclass(frozen=True)
class PixelFace:
    """A face as a custom detector returns it, in pixels of the frame it was given."""

    bbox: tuple[float, float, float, float]  # x, y, width, height
    nose: tuple[float, float] | None = None  # the point the head aims at
    eyes: tuple[tuple[float, float], tuple[float, float]] | None = None  # right, left


class FaceDetector(Protocol):
    """A developer's face detector for the ``custom`` source (not wired up yet)."""

    def detect(
        self, frame_bgr: npt.NDArray[np.uint8], ts: float
    ) -> Sequence[PixelFace]: ...


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


class FaceDetection:
    """The detection loop (specs/user_perception.md "The detection loop"): one asyncio
    task polling the source and publishing on ``faces``, restartable.

    ``on_observation`` receives every poll's report, undebounced (the head tracker's
    feed). In ``daemon`` mode the loop arms the daemon's detector at
    ``DAEMON_DETECT_WEIGHT`` when it starts and disarms it when it stops. Only the
    ``daemon`` source exists yet; ``custom`` fails :meth:`start`.
    """

    def __init__(
        self,
        robot: AnyReachyMini,
        *,
        source: str,
        faces: Observable[FaceReport],
        on_observation: Callable[[FaceReport], None] | None = None,
    ) -> None:
        self._robot = robot
        self._source = source
        self._faces = faces
        self._on_observation = on_observation
        self._task: asyncio.Task[None] | None = None
        # Whether this loop sent the daemon its detect weight (and so owes the disarm).
        self._armed = False

    @property
    def running(self) -> bool:
        """Whether the loop's task is running."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Arm the daemon's detector and start polling.

        Raises ``ValueError`` for a source this loop cannot run.
        """
        if self.running:
            return
        if self._source != "daemon":
            raise ValueError(
                f"the {self._source} detection source is not available yet"
            )
        await self._arm()
        self._task = asyncio.create_task(self._run(), name="face-detection")

    async def stop(self) -> None:
        """Stop polling, disarm the daemon's detector if this loop armed it, and publish
        the inactive report. A no-op on a loop that never started."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._armed:
            self._armed = False
            await asyncio.to_thread(self._robot.stop_head_tracking)
        if task is not None or self._faces.value.active:
            self._faces.set(FaceReport.inactive(self._source))

    async def _arm(self) -> None:
        self._armed = True  # set first: a cancelled arm still completes in its thread
        await asyncio.to_thread(self._robot.start_head_tracking, DAEMON_DETECT_WEIGHT)

    async def _run(self) -> None:
        published: FaceReport | None = None  # the last value `set`
        lower_since: float | None = None  # when the count first read below `published`
        last: FaceReport | None = None  # the last good observation
        failing_since: float | None = None
        down = False
        while True:
            try:
                target = await asyncio.to_thread(daemon_face_target, self._robot)
                report = report_from_daemon(target, active=True)
            except Exception as e:  # noqa: BLE001 - a failed poll is skipped, never fatal
                _logger.debug("face detection: poll failed: %s", e)
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                if not down and now - failing_since >= FACE_SOURCE_DOWN_S:
                    down = True
                    _logger.warning(
                        "face detection: the %s source has failed for %.0f s; "
                        "reporting detection inactive until it answers",
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
