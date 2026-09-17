"""Motion loop, presence & breathing: ``MotionSession`` (specs/motion.md).

The one writer of the robot's target pose: a dedicated 60 Hz thread that arbitrates
one exclusive primary move at a time (an emotion, later a gesture) over an idle move
— breathing, a still neutral hold, or nothing — every transition a short blend so the
robot never snaps and never goes dead between verbs. Nothing else in the bridge calls
``set_target``, and the loop never calls upstream's own move helpers (``async_play_move``
/ ``goto_target`` / ``wake_up`` / ``goto_sleep``): each is a second writer, and a
daemon-side move makes the daemon drop every ``set_target`` for its duration.
"""

from __future__ import annotations

import asyncio
import bisect
import concurrent.futures
import logging
import math
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Self

import numpy as np
from reachy_mini.motion.goto import GotoMove
from reachy_mini.motion.move import Move
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS, INIT_HEAD_POSE
from reachy_mini.utils.interpolation import InterpolationTechnique, time_trajectory

from .errors import BridgeError

if TYPE_CHECKING:
    from pathlib import Path

    import numpy.typing as npt

    from .robot import AnyReachyMini

__all__ = ["BreathingMove", "HoldMove", "MotionSession"]

_logger = logging.getLogger(__name__)

# Not 100 Hz: the conversation app's breathing at ~100 Hz shivers the Stewart platform
# (specs/motion.md open question 1). 50 Hz was tried on hardware and made it worse
# (whole-body shake instead of just the head) — reverted; see the open question.
CONTROL_HZ = 60.0
# Every entry into a move is a minjerk blend of this length (specs/motion.md "The loop").
BLEND_S = 0.5
# BreathingMove parameters (specs/motion.md "The moves"). The peaks are the conversation
# app's, seen on hardware; the rests and the independent antennas are what make the idle
# read as organic rather than mechanical.
BREATH_Z_M = 0.005  # a breath peaks this far above neutral, then returns to it
BREATH_S = 5.0  # one breath: a raised-cosine rise and fall
BREATH_REST_S = (1.0, 5.0)  # uniform rest at neutral between two breaths
ANTENNA_HOLD_S = (0.5, 4.0)  # uniform hold between two antenna moves
ANTENNA_MOVE_S = (0.8, 2.5)  # uniform duration of one minjerk antenna move

NEUTRAL_HEAD: npt.NDArray[np.float64] = np.array(INIT_HEAD_POSE, dtype=np.float64)
NEUTRAL_ANTENNAS: npt.NDArray[np.float64] = np.array(
    INIT_ANTENNAS_JOINT_POSITIONS, dtype=np.float64
)
NEUTRAL_BODY_YAW = 0.0
# The antennas roam between the neutral lean (upstream's ~10° anti-shake offset — the
# floor is exactly NEUTRAL_ANTENNAS, not the rounded radians(10)) and that lean plus the
# previous animation's 15° sway, i.e. ~25° outward.
ANTENNA_MIN_RAD = float(abs(NEUTRAL_ANTENNAS[0]))
ANTENNA_MAX_RAD = ANTENNA_MIN_RAD + math.radians(15)
# Joint sign of "outward from vertical" per antenna [right, left]: the sign of upstream's
# SLEEP_ANTENNAS_JOINT_POSITIONS ([-3.05, 3.05], the antennas folded fully out).
ANTENNA_OUTWARD: npt.NDArray[np.float64] = np.array([-1.0, 1.0])

# (head 4x4, antennas [right, left] rad, body yaw rad) — a fully specified pose.
type Pose = tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], float]

NEUTRAL: Pose = (NEUTRAL_HEAD, NEUTRAL_ANTENNAS, NEUTRAL_BODY_YAW)


class HoldMove(Move):
    """The idle move with breathing off: a still head at the neutral pose, forever."""

    @property
    def duration(self) -> float:
        return math.inf

    def evaluate(
        self, t: float
    ) -> tuple[
        npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
    ]:
        return NEUTRAL_HEAD.copy(), NEUTRAL_ANTENNAS.copy(), NEUTRAL_BODY_YAW


def _fade_in(t: float, duration: float = BLEND_S) -> float:
    """The minjerk 0 -> 1 ramp (the entry blend's shape) that ``_BreathingFadeOut``
    inverts to fade a plan's offsets out to neutral at rest."""
    if t >= duration:
        return 1.0
    return time_trajectory(t / duration, InterpolationTechnique.MIN_JERK)


@dataclass(frozen=True)
class _Segment:
    """One rest-to-rest piece of a scalar track: a hold, a breath or a minjerk move.

    Every shape has zero slope at both ends, so consecutive segments hand off with
    continuous velocity whatever their order (specs/motion.md "The moves").
    """

    start: float  # value at the segment's start
    end: float  # value at its end (== start for a hold or a breath)
    duration: float
    shape: Literal["hold", "breath", "minjerk"]
    peak: float = 0.0  # breath only: the value at mid-segment

    def value(self, t: float) -> float:
        u = min(max(t / self.duration, 0.0), 1.0)
        if self.shape == "hold":
            return self.start
        if self.shape == "breath":
            return (
                self.start
                + (self.peak - self.start) * (1.0 - math.cos(2.0 * math.pi * u)) / 2.0
            )
        return self.start + (self.end - self.start) * float(
            time_trajectory(u, InterpolationTechnique.MIN_JERK)
        )


class _Track:
    """A lazily generated sequence of segments. ``value(t)`` is a pure function of
    ``t``: segments are drawn only when ``t`` runs past the last one, and never
    re-drawn, so any ``t`` evaluates the same whenever it is asked."""

    def __init__(
        self, first: _Segment, draw_next: Callable[[_Segment], _Segment]
    ) -> None:
        self._segments = [first]
        self._ends = [first.duration]  # cumulative end time of each segment
        self._draw_next = draw_next

    def value(self, t: float) -> float:
        while t >= self._ends[-1]:
            nxt = self._draw_next(self._segments[-1])
            self._segments.append(nxt)
            self._ends.append(self._ends[-1] + nxt.duration)
        i = bisect.bisect_right(self._ends, t)
        seg_start = self._ends[i - 1] if i > 0 else 0.0
        return self._segments[i].value(t - seg_start)


def _breath() -> _Segment:
    return _Segment(0.0, 0.0, BREATH_S, "breath", peak=BREATH_Z_M)


class BreathingMove(Move):
    """The idle move with breathing on (specs/motion.md "The moves"): a randomised plan
    of rest-to-rest segments — raised-cosine breaths separated by random rests on the
    head's z axis, and two independent antenna tracks roaming outward from vertical.

    ``evaluate(t)`` is a pure function of ``t`` for a given ``rng``: the plan extends
    lazily as ``t`` grows and is never re-drawn. The loop builds an unseeded move at
    each idle entry; tests pass ``random.Random(seed)``.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        rng = rng if rng is not None else random.Random()
        # One independent stream per track, so one antenna's draws never shift the
        # other's (or the head's).
        head_rng = random.Random(rng.random())
        antenna_rngs = [random.Random(rng.random()), random.Random(rng.random())]

        def next_head(prev: _Segment) -> _Segment:
            if prev.shape == "breath":
                return _Segment(0.0, 0.0, head_rng.uniform(*BREATH_REST_S), "hold")
            return _breath()

        # The plan begins with a breath, so a fresh idle shows life at once.
        self._head = _Track(_breath(), next_head)

        def antenna_drawer(r: random.Random) -> Callable[[_Segment], _Segment]:
            def next_antenna(prev: _Segment) -> _Segment:
                if prev.shape == "minjerk":
                    return _Segment(
                        prev.end, prev.end, r.uniform(*ANTENNA_HOLD_S), "hold"
                    )
                target = r.uniform(ANTENNA_MIN_RAD, ANTENNA_MAX_RAD)
                return _Segment(prev.end, target, r.uniform(*ANTENNA_MOVE_S), "minjerk")

            return next_antenna

        # Each antenna begins with a hold at the floor (== its neutral value).
        self._antennas = [
            _Track(
                _Segment(
                    ANTENNA_MIN_RAD, ANTENNA_MIN_RAD, r.uniform(*ANTENNA_HOLD_S), "hold"
                ),
                antenna_drawer(r),
            )
            for r in antenna_rngs
        ]

    @property
    def duration(self) -> float:
        return math.inf

    def evaluate(
        self, t: float
    ) -> tuple[
        npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
    ]:
        head = NEUTRAL_HEAD.copy()
        head[2, 3] += self._head.value(t)
        angles = np.array([track.value(t) for track in self._antennas])
        return head, ANTENNA_OUTWARD * angles, NEUTRAL_BODY_YAW


class _BreathingFadeOut(Move):
    """Leaving breathing mid-plan (specs/motion.md "The moves"): keep playing ``move``
    from ``t_offset`` while a minjerk envelope scales every track's offset from neutral
    down to zero over ``duration`` — landing at neutral at rest, so whatever follows
    (a blend, or nothing) starts from a source that is actually at rest. A plain blend
    assumes that, and a track caught mid-segment is not at rest.
    """

    def __init__(
        self, move: BreathingMove, t_offset: float, duration: float = BLEND_S
    ) -> None:
        self._move = move
        self._t_offset = t_offset
        self._duration = duration

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(
        self, t: float
    ) -> tuple[
        npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
    ]:
        envelope = 1.0 - _fade_in(t, self._duration)
        head, antennas, body_yaw = self._move.evaluate(self._t_offset + t)
        assert head is not None and antennas is not None  # BreathingMove sets both
        head = head.copy()
        head[2, 3] = NEUTRAL_HEAD[2, 3] + envelope * (head[2, 3] - NEUTRAL_HEAD[2, 3])
        antennas = NEUTRAL_ANTENNAS + envelope * (antennas - NEUTRAL_ANTENNAS)
        return head, antennas, body_yaw


def blend_into(source: Pose, move: Move, seconds: float = BLEND_S) -> GotoMove:
    """A minjerk ``GotoMove`` from ``source`` to ``move.evaluate(0)`` (specs/motion.md:
    never snap).

    A component the move leaves ``None`` keeps the source value (``GotoMove`` does that).
    """
    head, antennas, body_yaw = move.evaluate(0.0)
    src_head, src_antennas, src_yaw = source
    return GotoMove(
        start_head_pose=src_head,
        target_head_pose=None if head is None else np.asarray(head, dtype=np.float64),
        start_antennas=src_antennas,
        target_antennas=None
        if antennas is None
        else np.asarray(antennas, dtype=np.float64),
        start_body_yaw=src_yaw,
        target_body_yaw=body_yaw,
        duration=seconds,
        method=InterpolationTechnique.MIN_JERK,
    )


@dataclass
class _Primary:
    """A queued primary move and the future the api awaits for it."""

    move: Move
    sound_path: Path | None
    done: concurrent.futures.Future[None] = field(
        default_factory=concurrent.futures.Future
    )


@dataclass
class _Playing:
    """What the loop is currently evaluating: an entry blend, then the move itself."""

    stages: list[Move]  # [blend, move]  (or [blend] alone for the exit blend)
    primary: _Primary | None  # None for an idle move / the exit blend
    stage: int = 0
    stage_start: float = 0.0  # monotonic time the current stage began
    exit_blend: bool = False  # True for close()'s final blend to neutral
    sound_started: bool = False


class MotionSession:
    """The one writer of the robot's target: a 60 Hz thread (specs/motion.md).

    Started paused; the api resumes it once the motors read ``enabled``. Every public
    method is safe to call from the event loop and returns at once; the thread applies
    it at the top of its next tick.
    """

    def __init__(
        self, robot: AnyReachyMini, *, presence: bool, breathing: bool
    ) -> None:
        self._robot = robot
        self._commands: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._run, name="reachy-mini-motion", daemon=True
        )
        # --- thread-owned state (touch only from closures run by the thread, or _run) ---
        self._presence = presence
        self._breathing = breathing
        self._paused = True
        self._commanding = False  # sent a target on the previous tick
        self._last_target: Pose = NEUTRAL
        self._queue: list[_Primary] = []  # pending primaries, FIFO
        self._playing: _Playing | None = None
        self._stop = False

    # --- api-facing commands (event-loop thread; enqueue and return at once) ---

    def submit(
        self, move: Move, sound_path: Path | None
    ) -> concurrent.futures.Future[None]:
        """Queue ``move`` as the next primary; returns a future resolved when it ends
        (or fails, or is cancelled). Resumes the loop if it was idle-paused by presence."""
        primary = _Primary(move=move, sound_path=sound_path)
        self._commands.put(lambda: self._on_submit(primary))
        return primary.done

    def _on_submit(self, primary: _Primary) -> None:
        self._queue.append(primary)
        self._paused = False  # specs/motion.md "Motors": play_emotion resumes the loop

    def set_presence(self, enabled: bool) -> None:
        self._commands.put(lambda: self._on_set_presence(enabled))

    def _on_set_presence(self, enabled: bool) -> None:
        if enabled == self._presence:
            return
        self._presence = enabled
        if self._playing is not None and self._playing.primary is not None:
            return  # applies once the queue drains
        self._playing = None
        if not enabled:
            self._commanding = False  # quiet at once, no easing

    def set_breathing(self, enabled: bool) -> None:
        self._commands.put(lambda: self._on_set_breathing(enabled))

    def _on_set_breathing(self, enabled: bool) -> None:
        if enabled == self._breathing:
            return
        self._breathing = enabled
        if self._playing is not None and self._playing.primary is not None:
            return  # applies once the queue drains
        breathing = self._playing_breathing()
        if not enabled and breathing is not None:
            # Fade the plan's offsets out rather than handing a track caught
            # mid-segment (a nonzero velocity) straight to a fresh blend, which assumes
            # rest (specs/motion.md "Leaving breathing mid-plan").
            move, elapsed = breathing
            self._playing = _Playing(
                stages=[_BreathingFadeOut(move, t_offset=elapsed)],
                primary=None,
                stage=0,
                stage_start=time.monotonic(),
            )
            return
        self._playing = None  # re-blend into the other idle move next tick

    def _playing_breathing(self) -> tuple[BreathingMove, float] | None:
        """The ``BreathingMove`` currently playing past its entry blend (no primary),
        with the seconds elapsed into it — else ``None``."""
        playing = self._playing
        if playing is None or playing.primary is not None or playing.stage != 1:
            return None
        move = playing.stages[1]
        if not isinstance(move, BreathingMove):
            return None
        return move, time.monotonic() - playing.stage_start

    def pause(self) -> None:
        self._commands.put(self._on_pause)

    def _on_pause(self) -> None:
        self._paused = True
        self._commanding = False
        pending = list(self._queue)
        self._queue.clear()
        if self._playing is not None and self._playing.primary is not None:
            pending.append(self._playing.primary)
        self._playing = None
        for primary in pending:
            if not primary.done.done():
                primary.done.set_exception(
                    BridgeError("the motors left 'enabled': the motion loop paused")
                )

    def resume(self) -> None:
        self._commands.put(self._on_resume)

    def _on_resume(self) -> None:
        self._paused = False  # the next tick re-anchors: _commanding is False

    def close(self) -> None:
        """Blocking: stop the thread, easing to neutral first if it is commanding and
        presence is on. Call under ``asyncio.to_thread`` — never on the event loop."""
        self._commands.put(self._on_close)
        self._thread.join(timeout=BLEND_S + 2.0)

    def _on_close(self) -> None:
        for primary in self._queue:
            primary.done.cancel()
        self._queue.clear()
        if self._playing is not None and self._playing.primary is not None:
            self._playing.primary.done.cancel()
        if self._presence and self._commanding and not self._paused:
            breathing = self._playing_breathing()
            exit_stage: Move = (
                _BreathingFadeOut(breathing[0], t_offset=breathing[1])
                if breathing is not None
                else blend_into(self._last_target, HoldMove())
            )
            self._playing = _Playing(
                stages=[exit_stage],
                primary=None,
                stage=0,
                stage_start=time.monotonic(),
                exit_blend=True,
            )
        else:
            self._stop = True

    @property
    def presence(self) -> bool:
        return self._presence

    @property
    def breathing(self) -> bool:
        return self._breathing

    # --- lifecycle ---

    async def __aenter__(self) -> Self:
        self._thread.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(self.close)

    # --- the thread ---

    def _run(self) -> None:
        period = 1.0 / CONTROL_HZ
        next_tick = time.monotonic()
        while not self._stop:
            self._drain_commands()
            if self._stop:
                break
            if not self._paused:
                try:
                    self._tick(time.monotonic())
                except Exception as e:  # noqa: BLE001 - the loop must survive a bad tick
                    _logger.warning("motion tick failed: %s", e)
                    self._fail_current(e)
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind: don't burst to catch up

    def _drain_commands(self) -> None:
        while True:
            try:
                self._commands.get_nowait()()
            except queue.Empty:
                return

    def _select_next(self) -> tuple[Move, _Primary | None] | None:
        while self._queue:
            primary = self._queue.pop(0)
            if primary.done.cancelled():
                continue
            return primary.move, primary
        if not self._presence:
            return None
        return (BreathingMove() if self._breathing else HoldMove()), None

    def _tick(self, now: float) -> None:
        playing = self._playing
        t = 0.0  # reassigned below; a fresh `playing` always starts its blend at t=0

        # Drop a primary the api cancelled, or an idle move a queued primary preempts
        # (idle has no duration of its own — it plays only while the queue is empty).
        if playing is not None:
            cancelled = playing.primary is not None and playing.primary.done.cancelled()
            preempted = playing.primary is None and bool(self._queue)
            if cancelled or preempted:
                playing = None
                self._playing = None

        if playing is not None:
            stage = playing.stages[playing.stage]
            t = now - playing.stage_start
            if t >= stage.duration:
                playing.stage += 1
                playing.stage_start = now
                t = 0.0
                if playing.stage >= len(playing.stages):
                    primary = playing.primary
                    if primary is not None and not primary.done.done():
                        primary.done.set_result(None)
                    exit_blend = playing.exit_blend
                    playing = None
                    self._playing = None
                    if exit_blend:
                        self._stop = True
                        return

        if playing is None:
            selected = self._select_next()
            if selected is None:
                self._commanding = False
                return
            move, primary = selected
            source = (
                self._last_target if self._commanding else self._read_present_pose()
            )
            playing = _Playing(
                stages=[blend_into(source, move), move],
                primary=primary,
                stage=0,
                stage_start=now,
            )
            self._playing = playing
            t = 0.0

        stage = playing.stages[playing.stage]
        eval_t = (
            t if math.isinf(stage.duration) else min(t, max(stage.duration - 1e-3, 0.0))
        )
        head, antennas, body_yaw = stage.evaluate(eval_t)
        if (
            playing.stage == 1
            and playing.primary is not None
            and not playing.sound_started
            and playing.primary.sound_path is not None
        ):
            self._robot.media.play_sound(str(playing.primary.sound_path))
            playing.sound_started = True

        last_head, last_antennas, last_yaw = self._last_target
        final_head = last_head if head is None else np.asarray(head, dtype=np.float64)
        final_antennas = (
            last_antennas
            if antennas is None
            else np.asarray(antennas, dtype=np.float64)
        )
        final_yaw = last_yaw if body_yaw is None else float(body_yaw)
        self._robot.set_target(
            head=final_head, antennas=final_antennas, body_yaw=final_yaw
        )
        self._last_target = (final_head, final_antennas, final_yaw)
        self._commanding = True

    def _read_present_pose(self) -> Pose:
        head = self._robot.get_current_head_pose()
        joints, antennas = self._robot.get_current_joint_positions()
        return (
            np.asarray(head, dtype=np.float64),
            np.asarray(antennas, dtype=np.float64),
            float(joints[0]),
        )

    def _fail_current(self, error: Exception) -> None:
        if self._playing is not None and self._playing.primary is not None:
            primary = self._playing.primary
            if not primary.done.done():
                primary.done.set_exception(error)
        self._playing = None
