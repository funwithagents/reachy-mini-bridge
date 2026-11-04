"""The convergence kit of the head-tracking live tests — importable by consumers of the
bridge (specs/testing/testing_support.md "Public surface").

A test that puts a face in front of the robot (the test scene's portraits,
specs/testing/sim_scene.md, or a person) and wants to assert that the head *converged* on
it — not merely that it moved — needs the same few pieces every time: the yaw a face at
a known position implies, a sampler that follows the head until it holds still, the
measures of how it got there (overshoot, oscillation, where it settled) and the
assertions with thresholds measured on the viewer sim. They live here, shared by the
bridge's own ``tests-e2e/`` and a consumer's tests; the bridge's tracker is the same for
both, so the same numbers hold.

The thresholds are module constants, each with its measurement next to it. The angles are
in degrees, read from the robot's reported head pose (``get_current_head_pose``), so they
measure what the robot does, not what the bridge commanded.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from reachy_mini_bridge.head_tracking import TRACKING_LOST_S
from reachy_mini_bridge.motion import unframed
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS

if TYPE_CHECKING:
    from reachy_mini_bridge.bridge import ReachyMiniBridge
    from reachy_mini_bridge.face_detection import Face

# A face this far to the robot's side, at the scene's default distance, implies 18.4° of
# yaw — the one lateral the bridge's own tests move the portrait by.
LATERAL_M = 0.15
# Where the head settles: its yaw averaged over the last SETTLE_WINDOW_S of the track.
# The head keeps breathing around the aim (its roaming toned down to a quarter, about
# ±2° of yaw on a slow random walk that a 2 s mean does not cancel), so the yaw is a
# coarse check that the head is on the face; the precise one is the face at the image
# centre (CENTRED, below), which is what tracking guarantees.
YAW_TOLERANCE_DEG = 5.0
SETTLE_WINDOW_S = 2.0
# Turning onto a face, the head may swing once past it and creep back. Measured on the
# viewer sim: 3–9.5° with upstream's daemon-side tracking; 0–3° with the bridge's
# tracker, which aims against the reported head pose of the frame's time with the delay
# estimated online (specs/motion/head_tracking.md "The aim"). The head never swings back
# past the face (no oscillation).
OVERSHOOT_MAX_DEG = 12.0
# The settled pitch for a face that only moves sideways (it stays at the same height).
PITCH_TOLERANCE_DEG = 3.0
# The tracked face's normalised image position once centred (|x|, |y| in [-1, 1]): within
# about 3° of the image centre on the sim's 112° camera. Measured at settle on the viewer
# sim: |x|, |y| under 0.025 — the head breathing around the aim moves it a little.
CENTRED = 0.05
# The head has settled on the face when, over the last SETTLE_WINDOW_S, the followed face
# was reported on every sample within CENTRED of the image centre, its mean within
# SETTLED_CENTRED — the measured settle — and the yaw held within SETTLE_YAW_RANGE_DEG.
# The head alone holding still is not enough: a head creeping back onto a face after an
# overshoot moves less than a degree over two seconds, and read as settled it fails the
# yaw check at the wrong moment (measured on the viewer sim, 2026-11-04: settled at
# +28.6 deg against +23.2, the face at x +0.047, still closing).
# A face past base following's 20 deg deadband settles last: the base turns 0.8 s
# after the head is on it, and while it comes round under the head (two stages of
# 0.6 s) the face drifts up to 0.10 off the centre and creeps back over 2-3 s —
# measured on the viewer sim (2026-11-04): 6.4-6.8 s to settle at 23-34 deg, 2.8-5 s
# inside the deadband. `track_onto`'s timeout (15 s) leaves a slower runner room.
SETTLED_CENTRED = 0.025
SETTLE_YAW_RANGE_DEG = 1.0
# How far off neutral the head may sit once it has been handed back. The idle move roams
# in roll/pitch/yaw (specs/motion/motion.md "The moves"): it averages ~6° from neutral and
# reaches 10.3° at the corner of its envelope, so this is measured as a mean over a
# window rather than one sample. A head still locked on the face sits at the face's
# 18.4°, well clear of the threshold.
NEUTRAL_THRESHOLD_DEG = 12.0
# An emotion's choreography under tracking, well above breathing's own sway.
MOVE_THRESHOLD_DEG = 5.0


def face_at(lateral: float) -> tuple[float, float, float]:
    """The scene position of a face ``lateral`` metres to the robot's left (negative:
    right) at the default distance and height."""
    x, _y, z = DEFAULT_FACE_POS
    return (x, lateral, z)


def expected_yaw_deg(lateral: float, distance: float = DEFAULT_FACE_POS[0]) -> float:
    """The yaw at which the head looks at a face ``lateral`` metres to the side at
    ``distance`` metres ahead: the eye camera is on the head's forward axis, so it looks
    at the face when the head's heading from its pivot (the world origin) does — 18.4° at
    ±0.15 m and 0.45 m."""
    return math.degrees(math.atan2(lateral, distance))


_yaw_for_lateral = expected_yaw_deg  # `track_onto` has a parameter of that name


def yaw_pitch_deg(pose: Any) -> tuple[float, float]:
    """The yaw (positive left) and pitch (positive down) of a 4x4 head pose, in degrees."""
    r = np.asarray(pose)
    yaw = math.degrees(math.atan2(r[1, 0], r[0, 0]))
    pitch = math.degrees(math.asin(-float(np.clip(r[2, 0], -1.0, 1.0))))
    return yaw, pitch


def angle_from_neutral_deg(pose: Any) -> float:
    """The head's rotation angle from the identity pose, in degrees."""
    r = np.asarray(pose)[:3, :3]
    return float(np.degrees(np.arccos(np.clip((np.trace(r) - 1) / 2, -1, 1))))


def head_on_base(robot: Any) -> tuple[Any, float]:
    """The head pose the robot reports, brought into the frame of its base — the pose
    relative to the body yaw it reports — and that body yaw in degrees. "At rest on the
    idle move" is a head square on the base, wherever the base points
    (specs/motion/motion.md "The body yaw"): the base follows the person and holds
    after they leave, so neutral is judged here, not in the world."""
    pose = np.asarray(robot.get_current_head_pose(), dtype=np.float64)
    joints, _antennas = robot.get_current_joint_positions()
    body_yaw = float(joints[0])
    return unframed(pose, body_yaw), math.degrees(body_yaw)


async def wait_for(
    predicate: Callable[[], bool], timeout: float, interval: float = 0.1
) -> bool:
    """Poll a blocking predicate off the loop until it holds or ``timeout`` elapses."""
    deadline = time.perf_counter() + timeout
    while True:
        if await asyncio.to_thread(predicate):
            return True
        if time.perf_counter() >= deadline:
            return False
        await asyncio.sleep(interval)


class Track:
    """The head's path onto a face: every (yaw, pitch) sampled until it held still.

    Built by :func:`track_onto`; read through :func:`assert_tracked` or directly —
    ``yaw`` / ``pitch`` where the head settled (the mean over the last
    ``SETTLE_WINDOW_S``), ``overshoot_deg`` how far it swung past the face on its way,
    ``swing_back_deg`` how far it came back short of the face after that (an
    oscillation), ``settle_s`` how long it took, ``face`` the face the head followed at
    settle (from ``bridge.faces``, by ``bridge.head_tracking``'s ``track_id``).
    """

    def __init__(
        self, where: str, start_yaw: float, expected_yaw: float, face: Face | None
    ) -> None:
        self.where = where
        self.start_yaw = start_yaw
        self.expected_yaw = expected_yaw
        self.samples: list[tuple[float, float]] = []
        self.times: list[float] = []
        self.face = face  # the target face of `bridge.faces` once settled, or None
        self.delay_s: float | None = None  # the tracker's delay estimate once settled
        self.frame_note = ""  # the camera frame's age and whether it carried a pose
        # Each `track_id` the tracker followed along the way, in order (None: no aim).
        # One id is a clean track; more means the face's track was re-created under
        # a new id while the head turned (a detector flicker, a jump past the gate).
        self.ids: list[int | None] = []
        # The followed face's (x, y) on each sample, None while none was reported.
        self.centres: list[tuple[float, float] | None] = []
        # Why the head never settled when the timeout ended the track, else None.
        self.unsettled: str | None = None

    def _settled(self) -> list[tuple[float, float]]:
        """The samples of the track's last SETTLE_WINDOW_S."""
        end = self.times[-1]
        return [
            s
            for s, t in zip(self.samples, self.times, strict=True)
            if t >= end - SETTLE_WINDOW_S
        ]

    @property
    def yaw(self) -> float:
        """Where the head settled: the mean yaw over the last SETTLE_WINDOW_S."""
        settled = self._settled()
        return sum(y for y, _ in settled) / len(settled)

    @property
    def pitch(self) -> float:
        """Where the head settled: the mean pitch over the last SETTLE_WINDOW_S."""
        settled = self._settled()
        return sum(p for _, p in settled) / len(settled)

    @property
    def settle_s(self) -> float:
        """How long the head was sampled before it held still (or the timeout)."""
        return self.times[-1] - self.times[0]

    def _past_face(self) -> list[float]:
        """Each sample's yaw beyond the expected one, positive in the direction the head
        had to turn (all zero when the face did not move sideways)."""
        if abs(self.expected_yaw - self.start_yaw) < YAW_TOLERANCE_DEG:
            return [0.0 for _ in self.samples]
        direction = math.copysign(1.0, self.expected_yaw - self.start_yaw)
        return [(y - self.expected_yaw) * direction for y, _ in self.samples]

    @property
    def overshoot_deg(self) -> float:
        """How far the head swung past the face on its way there."""
        return max(0.0, *self._past_face())

    @property
    def swing_back_deg(self) -> float:
        """After its furthest point, how far the head came back short of the face — an
        oscillation, where a settle creeps back onto it."""
        past = self._past_face()
        peak = past.index(max(past))
        return max(0.0, *(-p for p in past[peak:]))


def _unsettled(track: Track, now: float) -> str | None:
    """Why the head has not settled on the face as of ``now`` — ``None`` once it has:
    the last SETTLE_WINDOW_S sampled, the followed face reported on every sample of it
    within CENTRED, their mean within SETTLED_CENTRED, the yaw within
    SETTLE_YAW_RANGE_DEG."""
    if track.times[0] > now - SETTLE_WINDOW_S:
        return "a full settle window"
    window = [
        (yaw, centre)
        for (yaw, _), centre, t in zip(
            track.samples, track.centres, track.times, strict=True
        )
        if t >= now - SETTLE_WINDOW_S
    ]
    centres = [c for _, c in window if c is not None]
    if len(centres) < len(window):
        return "the followed face reported on every sample"
    worst = max(max(abs(x), abs(y)) for x, y in centres)
    if worst >= CENTRED:
        return f"the face within {CENTRED} of the centre (it reached {worst:.3f})"
    mean_x = sum(x for x, _ in centres) / len(centres)
    mean_y = sum(y for _, y in centres) / len(centres)
    if max(abs(mean_x), abs(mean_y)) >= SETTLED_CENTRED:
        return (
            f"the face's mean within {SETTLED_CENTRED} of the centre "
            f"(({mean_x:+.3f}, {mean_y:+.3f}))"
        )
    yaws = [yaw for yaw, _ in window]
    if max(yaws) - min(yaws) >= SETTLE_YAW_RANGE_DEG:
        return (
            f"the yaw within {SETTLE_YAW_RANGE_DEG} deg ({max(yaws) - min(yaws):.1f})"
        )
    return None


def followed_face(bridge: ReachyMiniBridge) -> Face | None:
    """The face of ``bridge.faces`` the head follows, by ``bridge.head_tracking``'s
    ``track_id``."""
    followed = bridge.head_tracking.value.track_id
    return next((f for f in bridge.faces.value.faces if f.track_id == followed), None)


async def track_onto(
    bridge: ReachyMiniBridge,
    where: str,
    lateral: float,
    *,
    settle_timeout: float = 15.0,
    min_seconds: float = 2.5,
    expected_yaw_deg: float | None = None,
) -> Track:
    """Sample the head until it has settled on the face — over the last
    ``SETTLE_WINDOW_S``, the followed face reported on every sample within ``CENTRED`` of
    the image centre and its mean within ``SETTLED_CENTRED``, the yaw within
    ``SETTLE_YAW_RANGE_DEG``; and at least ``min_seconds`` in, since detection and the
    gaze layer's fade take a moment to start the head moving — or ``settle_timeout``,
    which :func:`assert_tracked` fails naming the condition that never held; then read
    the followed face from ``bridge.faces``. The window outlasts the tracker's hold (``TRACKING_SWITCH_S``, 1 s
    toward a face gone missing), so a head pausing while the tracker waits for a
    re-created track never reads as settled. The expected yaw is a face at ``lateral`` at
    the default distance, unless given. ``where`` names the step in the assertion
    messages and the printout.
    """
    robot: Any = bridge.robot
    start_yaw, _ = yaw_pitch_deg(await asyncio.to_thread(robot.get_current_head_pose))
    expected = (
        _yaw_for_lateral(lateral) if expected_yaw_deg is None else expected_yaw_deg
    )
    track = Track(where, start_yaw, expected, None)
    started = time.perf_counter()
    deadline = started + settle_timeout
    while True:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        now = time.perf_counter()
        track.samples.append(yaw_pitch_deg(pose))
        track.times.append(now)
        followed = bridge.head_tracking.value.track_id
        if not track.ids or track.ids[-1] != followed:
            track.ids.append(followed)
        face = followed_face(bridge)
        track.centres.append(None if face is None else (face.x, face.y))
        reason = _unsettled(track, now)
        if reason is None and now - started >= min_seconds:
            break
        if now >= deadline:
            track.unsettled = reason or "the minimum time"
            break
        await asyncio.sleep(0.05)
    track.face = followed_face(bridge)
    # The tracker's delay estimate, printed for the spec's numbers: the one private read.
    tracker = bridge._tracker
    track.delay_s = None if tracker is None else tracker.delay_s
    frame = bridge.camera.latest()
    track.frame_note = (
        "no frame"
        if frame is None
        else f"frame age {time.perf_counter() - frame.ts:.3f} s, "
        f"pose stamped: {frame.head_pose is not None}, "
        f"report pose: {bridge.faces.value.head_pose is not None}"
    )
    return track


def assert_tracked(track: Track, *, pitch_ahead: float | None = None) -> None:
    """The head moved onto the face: toward it, past it at most once and by a bounded
    amount, never swinging back past it, settled at the expected yaw on average (and, for
    a face that only moved sideways, the same pitch as ``pitch_ahead``), with the tracked
    face at the image centre. Prints the track's numbers first, for the run's log."""
    face = track.face
    trace = " ".join(
        "-" if c is None else f"{c[0]:+.2f}"
        for c in track.centres[:: max(1, len(track.centres) // 40)]
    )
    print(
        f"\n[e2e] {track.where}: yaw {track.start_yaw:+.1f} -> {track.yaw:+.1f} deg "
        f"(expected {track.expected_yaw:+.1f}, overshoot {track.overshoot_deg:.1f}, "
        f"swing back {track.swing_back_deg:.1f}), "
        f"pitch {track.pitch:+.1f}, settled in {track.settle_s:.1f} s, ids followed "
        f"{track.ids}, face {face}, delay estimate {track.delay_s}, {track.frame_note}"
        f"\n[e2e]   the face's x along the way: {trace}"
    )
    assert track.unsettled is None, (
        f"{track.where}: the head did not settle on the face within the timeout — "
        f"never {track.unsettled}"
    )
    assert track.overshoot_deg <= OVERSHOOT_MAX_DEG, (
        f"{track.where}: the head swung {track.overshoot_deg:.1f} deg past the face"
    )
    assert track.swing_back_deg <= YAW_TOLERANCE_DEG, (
        f"{track.where}: the head oscillated, swinging back {track.swing_back_deg:.1f} "
        "deg short of the face after overshooting"
    )
    assert track.yaw == pytest.approx(track.expected_yaw, abs=YAW_TOLERANCE_DEG), (
        f"{track.where}: the head settled at yaw {track.yaw:+.1f} deg, not on the face "
        f"({track.expected_yaw:+.1f})"
    )
    if pitch_ahead is not None:
        assert track.pitch == pytest.approx(pitch_ahead, abs=PITCH_TOLERANCE_DEG), (
            f"{track.where}: pitch {track.pitch:+.1f} deg, {pitch_ahead:+.1f} with the "
            "face ahead at the same height"
        )
    assert face is not None, f"{track.where}: the bridge reports no tracked face"
    assert abs(face.x) < CENTRED and abs(face.y) < CENTRED, (
        f"{track.where}: the tracked face is not at the image centre "
        f"({face.x:+.2f}, {face.y:+.2f})"
    )


async def arm_tracking(bridge: ReachyMiniBridge, *, focus: bool = False) -> None:
    """Tracking on (``live_bridge`` starts with it off) and no aim held — ``attention``
    at ``watching`` within ``TRACKING_LOST_S`` of nobody in view, which the tracker only
    reaches if the detection loop ticks it: the check that the loop is alive."""
    await bridge.start_head_tracking(focus=focus)
    assert await wait_for(
        lambda: bridge.attention == "watching", TRACKING_LOST_S + 2.0
    ), "an aim held with nobody in view: is the detection loop alive?"


async def sample_idle(robot: Any, seconds: float) -> tuple[float, float]:
    """The head's z range and its mean angle from neutral over ``seconds`` — the two
    things the idle move shows: it breathes on z, and it roams a few degrees about
    neutral rather than holding one direction (specs/motion/motion.md "The moves").
    Neutral is judged relative to the base (:func:`head_on_base`): the base may be
    turned toward where a person was."""
    zs: list[float] = []
    angles: list[float] = []
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        pose, _body_yaw = await asyncio.to_thread(head_on_base, robot)
        zs.append(float(pose[2, 3]))
        angles.append(angle_from_neutral_deg(pose))
        await asyncio.sleep(0.1)
    return max(zs) - min(zs), sum(angles) / len(angles)
