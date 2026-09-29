"""Functional tests for the motion loop (specs/motion/motion.md).

Plain-function tests for the moves and the blend helper; ``MotionSession`` tests drive
it on a bare ``FakeReachyMini`` at real time (the loop runs the same way on both
backends), so timings stay tight but real.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import logging
import math
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest
import websockets.exceptions
from reachy_mini.motion.move import Move
from scipy.spatial.transform import Rotation

from reachy_mini_bridge.errors import BridgeError
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.motion import (
    ANTENNA_FLICK_MAX_RAD,
    ANTENNA_MAX_RAD,
    ANTENNA_MIN_RAD,
    ANTENNA_OUTWARD,
    BLEND_S,
    BREATH_REST_S,
    BREATH_S,
    BREATH_Z_M,
    BREATHING_GAZE_ROTATION_SCALE,
    CONTROL_HZ,
    GAZE_HISTORY_S,
    HEAD_PITCH_RAD,
    HEAD_ROLL_RAD,
    HEAD_YAW_RAD,
    NEUTRAL,
    NEUTRAL_ANTENNAS,
    NEUTRAL_BODY_YAW,
    NEUTRAL_HEAD,
    ROAM_MIN_TRAVEL_FRACTION,
    BreathingMove,
    HoldMove,
    IdleMove,
    IdleOffsets,
    MotionSession,
    _IdleFadeOut,
    _roam_target,
    blend_into,
)

if TYPE_CHECKING:
    import numpy.typing as npt

    from reachy_mini_bridge.motion import IdleMoveFactory


@asynccontextmanager
async def _running(session: MotionSession) -> AsyncIterator[MotionSession]:
    """``start()`` / ``stop()`` around a block — what the bridge does with the session."""
    await session.start()
    try:
        yield session
    finally:
        await session.stop()


def test_hold_is_neutral_at_any_time() -> None:
    move = HoldMove()
    assert move.duration == float("inf")
    for t in (0.0, 1.5, 100.0):
        head, antennas, body_yaw = move.evaluate(t)
        assert head is not None and antennas is not None
        assert np.array_equal(head, NEUTRAL[0])
        assert np.array_equal(antennas, NEUTRAL[1])
        assert body_yaw == NEUTRAL[2]


def _z(move: BreathingMove, t: float) -> float:
    head, _antennas, _yaw = move.evaluate(t)
    assert head is not None
    return float(head[2, 3])


def _outward(move: BreathingMove, t: float) -> npt.NDArray[np.float64]:
    """Each antenna's lean outward from vertical, in rad (sign folded away)."""
    _head, antennas, _yaw = move.evaluate(t)
    assert antennas is not None
    return ANTENNA_OUTWARD * antennas


# The rotation tracks' limits, in the order `_rpy_rad` returns them.
ROTATION_LIMITS = np.array([HEAD_ROLL_RAD, HEAD_PITCH_RAD, HEAD_YAW_RAD])
# The furthest the composed rotation can sit from neutral: the envelope's corner.
ROTATION_CORNER_DEG = float(np.degrees(np.linalg.norm(ROTATION_LIMITS)))


def _rpy_rad(move: BreathingMove, t: float) -> npt.NDArray[np.float64]:
    """The head's roll, pitch and yaw offsets from neutral, in rad."""
    o = move.offsets(t)
    return np.radians([o.roll_deg, o.pitch_deg, o.yaw_deg])


def _angle_from_neutral_deg(move: BreathingMove, t: float) -> float:
    """The head's rotation angle from the identity pose, in degrees."""
    head, _antennas, _yaw = move.evaluate(t)
    assert head is not None
    rot = np.asarray(head)[:3, :3]
    return float(np.degrees(np.arccos(np.clip((np.trace(rot) - 1) / 2, -1.0, 1.0))))


@pytest.mark.parametrize("seed", range(5))
def test_breathing_starts_at_neutral_at_rest(seed: int) -> None:
    move = BreathingMove(random.Random(seed))
    assert move.duration == float("inf")
    head0, antennas0, yaw0 = move.evaluate(0.0)
    assert head0 is not None and antennas0 is not None
    assert np.allclose(head0, NEUTRAL[0])
    assert np.allclose(antennas0, NEUTRAL[1])
    assert yaw0 == NEUTRAL[2]
    head1, antennas1, _yaw1 = move.evaluate(1e-3)  # zero initial velocity
    assert head1 is not None and antennas1 is not None
    assert np.allclose(head1, NEUTRAL[0], atol=1e-6)
    assert np.allclose(antennas1, NEUTRAL[1], atol=1e-6)


def test_breath_is_a_raised_cosine_then_a_rest() -> None:
    move = BreathingMove(random.Random(0))
    assert _z(move, BREATH_S / 2) == pytest.approx(BREATH_Z_M, abs=1e-9)
    assert _z(move, BREATH_S) == pytest.approx(0.0, abs=1e-9)
    # the shortest rest is 1 s: right after the first breath z reads exactly neutral
    assert all(_z(move, BREATH_S + k * 0.01) == 0.0 for k in range(100))
    zs = [_z(move, k * 0.01) for k in range(6000)]
    assert min(zs) >= 0.0
    assert max(zs) <= BREATH_Z_M + 1e-12
    # the head translates on z alone — x, y and body yaw stay neutral — whatever the
    # rotation tracks are doing meanwhile
    for k in range(0, 6000, 7):
        head, _antennas, yaw = move.evaluate(k * 0.01)
        assert head is not None
        assert head[0, 3] == 0.0 and head[1, 3] == 0.0
        assert yaw == NEUTRAL[2]


def test_breathing_rests_vary_in_length() -> None:
    move = BreathingMove(random.Random(0))
    step = 0.01
    zs = [_z(move, k * step) for k in range(30000)]  # 300 s
    rests: list[float] = []
    for at_rest, run in itertools.groupby(zs, key=lambda z: z == 0.0):
        if at_rest:
            rests.append(len(list(run)) * step)
    # t = 0 is a breath's first sample (z == 0 for one sample, not a rest) and the last
    # run may be cut by the scan window: keep only whole runs at least half the shortest
    # rest
    rests = [r for r in rests[:-1] if r >= BREATH_REST_S[0] / 2]
    assert len(rests) >= 20
    for rest in rests:
        assert BREATH_REST_S[0] - 2 * step <= rest <= BREATH_REST_S[1] + 2 * step
    assert max(rests) - min(rests) > 0.5


@pytest.mark.parametrize("seed", range(5))
def test_antennas_stay_outward_within_range(seed: int) -> None:
    assert np.array_equal(ANTENNA_OUTWARD * ANTENNA_MIN_RAD, NEUTRAL_ANTENNAS)
    move = BreathingMove(random.Random(seed))
    leans = np.array([_outward(move, k * 0.02) for k in range(6000)])  # 120 s at 20 ms
    assert np.all(leans >= ANTENNA_MIN_RAD - 1e-9), "an antenna leaned inside neutral"
    assert np.all(leans <= ANTENNA_FLICK_MAX_RAD + 1e-9)
    # only a flick passes the roaming ceiling, and a flick is punctuation: the antennas
    # spend the great majority of their time inside the roaming window (measured ~4%)
    above = float(np.mean(leans > ANTENNA_MAX_RAD))
    assert 0.0 < above < 0.15, f"{above:.3f} of samples above the roaming ceiling"


def test_antennas_move_independently() -> None:
    move = BreathingMove(random.Random(0))
    samples = np.array([_outward(move, k * 0.02) for k in range(6000)])
    right, left = samples[:, 0], samples[:, 1]
    assert not np.allclose(right, left)
    for series in (right, left):
        holds = {
            round(v, 3) for v, run in itertools.groupby(series) if len(list(run)) >= 25
        }
        assert len(holds) >= 3, (
            "each antenna should have reached several distinct holds"
        )


def test_breathing_is_continuous_and_pure() -> None:
    move = BreathingMove(random.Random(0))
    period = 1.0 / CONTROL_HZ
    ts = [k * period for k in range(int(120 * CONTROL_HZ))]
    poses = [move.evaluate(t) for t in ts]
    zs = [float(h[2, 3]) for h, _, _ in poses if h is not None]
    ants = [a for _, a, _ in poses if a is not None]
    rpys = [_rpy_rad(move, t) for t in ts]
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.0003
    # a flick is the fastest thing the plan does: ~5 deg (0.088 rad) per tick at its peak
    assert max(float(np.max(np.abs(b - a))) for a, b in itertools.pairwise(ants)) < 0.11
    # the rotations are slow by comparison: ~0.35 deg (0.006 rad) per tick at their peak
    assert max(float(np.max(np.abs(b - a))) for a, b in itertools.pairwise(rpys)) < 0.02
    twin = BreathingMove(random.Random(0))
    for t, (head, antennas, _yaw) in zip(ts[::7], poses[::7], strict=True):
        twin_head, twin_antennas, _ = twin.evaluate(t)
        assert head is not None and twin_head is not None
        assert antennas is not None and twin_antennas is not None
        assert np.array_equal(head, twin_head) and np.array_equal(
            antennas, twin_antennas
        )
    # out-of-order re-evaluation on the same move reads the same plan
    head_10, ant_10, _ = move.evaluate(10.0)
    head_10_ref, ant_10_ref, _ = poses[int(10.0 * CONTROL_HZ)]
    assert head_10 is not None and head_10_ref is not None
    assert ant_10 is not None and ant_10_ref is not None
    assert np.array_equal(head_10, head_10_ref) and np.array_equal(ant_10, ant_10_ref)


@pytest.mark.parametrize("seed", range(5))
def test_head_rotation_roams_within_its_envelope(seed: int) -> None:
    """specs/motion/motion.md "The moves": the idle head looks about on three independent
    rotation tracks, reaching each axis' limit without ever passing it."""
    move = BreathingMove(random.Random(seed))
    ts = [k / CONTROL_HZ for k in range(int(300 * CONTROL_HZ))]  # 300 s
    rpy = np.array([_rpy_rad(move, t) for t in ts])
    assert np.all(np.abs(rpy) <= ROTATION_LIMITS + 1e-9)
    # each axis uses its range in both directions (measured >= 0.95 of the limit)
    assert np.all(rpy.max(axis=0) >= 0.7 * ROTATION_LIMITS), "an axis never roamed +"
    assert np.all((-rpy).max(axis=0) >= 0.7 * ROTATION_LIMITS), "an axis never roamed -"
    angles = [_angle_from_neutral_deg(move, t) for t in ts[::7]]
    assert max(angles) <= ROTATION_CORNER_DEG + 1.0
    # the head lives a few degrees off neutral rather than hugging it or the corner
    assert 4.0 < float(np.median(angles)) < 8.0


@pytest.mark.parametrize("seed", range(5))
def test_head_rotation_keeps_at_least_one_axis_moving(seed: int) -> None:
    """specs/motion/motion.md "The moves": three independent tracks mean the head is almost
    always doing something — the idle head before them held one fixed heading."""
    move = BreathingMove(random.Random(seed))
    ts = [k / CONTROL_HZ for k in range(int(300 * CONTROL_HZ))]
    rpy = np.array([_rpy_rad(move, t) for t in ts])
    speeds = np.abs(np.diff(rpy, axis=0)) * CONTROL_HZ
    moving = float((speeds > math.radians(0.5)).any(axis=1).mean())
    assert moving > 0.5, f"the head rotated only {moving:.0%} of the time"


@pytest.mark.parametrize("seed", range(5))
def test_antennas_move_fast_enough_to_read_and_flick(seed: int) -> None:
    """specs/motion/motion.md "The moves": drawing a speed (not a duration) and punctuating the
    roaming with flicks puts the antennas in the band the emotions library commands.
    A fixed-duration plan over the same window reads 4 deg/s at p90 and 13 at p99."""
    move = BreathingMove(random.Random(seed))
    ts = [k / CONTROL_HZ for k in range(int(300 * CONTROL_HZ))]
    leans = np.degrees(np.array([_outward(move, t) for t in ts]))
    speeds = np.abs(np.diff(leans, axis=0)) * CONTROL_HZ
    p90, p99 = np.percentile(speeds, [90, 99])
    assert p90 > 20.0, f"antenna speed p90 only {p90:.1f} deg/s"
    assert p99 > 80.0, f"antenna speed p99 only {p99:.1f} deg/s"
    # each excursion past the roaming ceiling is one flick (measured ~110 over 300 s)
    flicks = sum(
        1
        for column in leans.T
        for above, _run in itertools.groupby(column > np.degrees(ANTENNA_MAX_RAD))
        if above
    )
    assert flicks >= 20, f"only {flicks} flicks in 300 s"


@pytest.mark.parametrize(
    ("lo", "hi"), [(-HEAD_YAW_RAD, HEAD_YAW_RAD), (ANTENNA_MIN_RAD, ANTENNA_MAX_RAD)]
)
def test_roam_target_always_travels(lo: float, hi: float) -> None:
    """specs/motion/motion.md "The moves": a roam target lands inside the range and far enough
    from where the track sits that the move is worth making."""
    rng = random.Random(0)
    span = hi - lo
    for k in range(41):
        prev = lo + span * k / 40
        for _ in range(50):
            target = _roam_target(rng, prev, lo, hi)
            assert lo - 1e-12 <= target <= hi + 1e-12
            assert abs(target - prev) >= ROAM_MIN_TRAVEL_FRACTION * span - 1e-12


def test_fade_out_lands_at_neutral_at_rest() -> None:
    move = BreathingMove(random.Random(0))
    offset = next(
        k * 0.01
        for k in range(6000)
        if _z(move, k * 0.01) > 0.0
        and np.any(_outward(move, k * 0.01) > ANTENNA_MIN_RAD + 1e-6)
        and np.any(np.abs(_rpy_rad(move, k * 0.01)) > math.radians(1.0))
    )
    fade = _IdleFadeOut(move, t_offset=offset)
    assert fade.duration == BLEND_S
    head_end, antennas_end, _ = fade.evaluate(BLEND_S)
    assert head_end is not None and antennas_end is not None
    assert np.allclose(head_end, NEUTRAL[0], atol=1e-6)
    assert np.allclose(antennas_end, NEUTRAL[1], atol=1e-6)
    head_near, antennas_near, _ = fade.evaluate(BLEND_S - 1e-3)
    assert head_near is not None and antennas_near is not None
    assert np.allclose(head_near, NEUTRAL[0], atol=1e-5)
    assert np.allclose(antennas_near, NEUTRAL[1], atol=1e-5)
    period = 1.0 / CONTROL_HZ
    poses = [fade.evaluate(k * period) for k in range(int(BLEND_S * CONTROL_HZ) + 1)]
    zs = [float(h[2, 3]) for h, _, _ in poses if h is not None]
    ants = [a for _, a, _ in poses if a is not None]
    # the envelope adds its own slope on top of the plan's, which keeps playing under it
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.0005
    assert max(float(np.max(np.abs(b - a))) for a, b in itertools.pairwise(ants)) < 0.12

    # The rotation fades out with everything else: the plan keeps playing underneath, so
    # a track caught mid-move can still be growing — but the envelope only ever scales it
    # down, and it lands at zero.
    def angle_deg(pose: npt.NDArray[np.float64]) -> float:
        rot = np.asarray(pose)[:3, :3]
        return float(np.degrees(np.arccos(np.clip((np.trace(rot) - 1) / 2, -1.0, 1.0))))

    angles = [angle_deg(h) for h, _, _ in poses if h is not None]
    assert angles[0] > 0.5, "the fade-out did not start from a rotated head"
    assert angles[-1] == pytest.approx(0.0, abs=1e-4)
    for k, angle in enumerate(angles):
        played = move.offsets(offset + k * period).pose()[0]
        assert angle <= angle_deg(played) + 1e-9, (
            "the fade-out rotated the head further than the plan itself did"
        )


def test_blend_into_goes_from_source_to_the_moves_start() -> None:
    head = np.eye(4)
    head[2, 3] = 0.02
    antennas = np.array([0.5, -0.5])
    source = (head, antennas, 0.3)

    b = blend_into(source, HoldMove())
    assert b.duration == BLEND_S

    start_head, start_antennas, start_yaw = b.evaluate(0.0)
    assert start_head is not None and start_antennas is not None
    assert np.allclose(start_head, head)
    assert np.allclose(start_antennas, antennas)
    assert start_yaw == pytest.approx(0.3)

    end_head, end_antennas, end_yaw = b.evaluate(BLEND_S)
    assert end_head is not None and end_antennas is not None
    assert np.allclose(end_head, NEUTRAL[0])
    assert np.allclose(end_antennas, NEUTRAL[1])
    assert end_yaw == pytest.approx(0.0)

    mid_head, _mid_antennas, _mid_yaw = b.evaluate(BLEND_S / 2)
    assert mid_head is not None
    assert NEUTRAL[0][2, 3] < mid_head[2, 3] < head[2, 3]


# --- MotionSession -------------------------------------------------------------------


class _TestPrimary(Move):
    """A finite move with a distinctive z displacement, for driving the loop directly."""

    def __init__(
        self,
        duration: float,
        z: float,
        *,
        sound_path: Path | None = None,
        fail_after: float | None = None,
    ) -> None:
        self._duration = duration
        self._z = z
        self._sound_path = sound_path
        self._fail_after = fail_after

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def sound_path(self) -> Path | None:
        return self._sound_path

    def evaluate(
        self, t: float
    ) -> tuple[
        npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
    ]:
        if self._fail_after is not None and t > self._fail_after:
            raise RuntimeError("boom")
        head = NEUTRAL_HEAD.copy()
        head[2, 3] = self._z
        return head, NEUTRAL_ANTENNAS.copy(), NEUTRAL_BODY_YAW


def _head_zs(robot: FakeReachyMini) -> list[float]:
    return [float(h[2, 3]) for h, _, _ in robot.targets if h is not None]


def test_paused_session_sends_nothing() -> None:
    async def run() -> FakeReachyMini:
        robot = FakeReachyMini()
        async with _running(MotionSession(robot, presence=True, idle="breathing")):
            await asyncio.sleep(0.2)
        return robot

    robot = asyncio.run(run())
    assert robot.targets == []


def test_resume_blends_from_the_present_pose_into_breathing() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        head = np.eye(4)
        head[2, 3] = 0.02
        robot.set_target(head=head)  # simulate another writer, before the loop starts
        robot.targets.clear()  # isolate the loop's own stream
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            # captured before exit, whose own easing blend would otherwise be counted
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert zs, "expected the loop to have sent targets"
    assert zs[0] == pytest.approx(0.02, abs=0.003)
    assert abs(zs[-1]) < BREATH_Z_M + 0.002
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.002
    rate = len(zs) / (BLEND_S + 0.2)
    assert 40 <= rate <= 75


def test_breathing_off_holds_neutral() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="hold")
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert zs
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in zs[-5:])


def test_presence_off_goes_quiet_when_idle() -> None:
    async def run() -> tuple[int, int, int]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=False, idle="breathing")
        ) as session:
            session.resume()
            await asyncio.sleep(0.3)
            idle_count = len(robot.targets)
            future = session.submit(_TestPrimary(0.15, 0.01), None)
            await asyncio.wrap_future(future)
            after_count = len(robot.targets)
            await asyncio.sleep(0.2)
            final_count = len(robot.targets)
        return idle_count, after_count, final_count

    idle_count, after_count, final_count = asyncio.run(run())
    assert idle_count == 0
    assert after_count > 0
    assert final_count == after_count


def test_primary_plays_after_a_blend_then_idle_resumes() -> None:
    async def run() -> tuple[float, list[float], list[float]]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            t0 = time.monotonic()
            future = session.submit(_TestPrimary(0.3, 0.03), None)
            await asyncio.wrap_future(future)
            elapsed = time.monotonic() - t0
            mid_zs = _head_zs(robot)
            await asyncio.sleep(BLEND_S + 0.15)
            final_zs = _head_zs(robot)
        return elapsed, mid_zs, final_zs

    elapsed, mid_zs, final_zs = asyncio.run(run())
    assert 0.7 <= elapsed < 1.2
    assert max(mid_zs) >= 0.025
    assert all(abs(z) < 0.001 for z in final_zs[-5:])


def test_sound_starts_with_the_trajectory_not_the_blend() -> None:
    async def run() -> float:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            t0 = time.monotonic()
            future = session.submit(_TestPrimary(0.2, 0.02), Path("x.ogg"))
            while not any(name == "media.play_sound" for name, _ in robot.commands):
                await asyncio.sleep(0.01)
            sound_elapsed = time.monotonic() - t0
            await asyncio.wrap_future(future)
        return sound_elapsed

    sound_elapsed = asyncio.run(run())
    assert sound_elapsed >= BLEND_S * 0.8


def test_primaries_are_fifo_and_exclusive() -> None:
    async def run() -> tuple[int, int]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            f1 = session.submit(_TestPrimary(0.15, 0.02), Path("one.ogg"))
            f2 = session.submit(_TestPrimary(0.15, 0.03), Path("two.ogg"))
            await asyncio.wrap_future(f1)
            commands_at_f1_done = len(robot.commands)
            await asyncio.wrap_future(f2)
            two_index = next(
                i
                for i, (name, args) in enumerate(robot.commands)
                if name == "media.play_sound" and args["sound_file"] == "two.ogg"
            )
        return commands_at_f1_done, two_index

    commands_at_f1_done, two_index = asyncio.run(run())
    assert two_index >= commands_at_f1_done


def test_cancelled_future_drops_the_primary_within_a_tick() -> None:
    async def run() -> tuple[list[float], int, int]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            future = session.submit(_TestPrimary(2.0, 0.05), None)
            await asyncio.sleep(BLEND_S + 0.15)
            future.cancel()
            await asyncio.sleep(0.08)
            before = len(robot.targets)
            await asyncio.sleep(0.08)
            after = len(robot.targets)
            last_zs = _head_zs(robot)[-3:]
        return last_zs, before, after

    last_zs, before, after = asyncio.run(run())
    assert all(abs(z - 0.05) > 0.001 for z in last_zs)
    assert after > before


def test_toggle_during_a_primary_is_deferred() -> None:
    async def run() -> tuple[list[float], list[float]]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            future = session.submit(_TestPrimary(0.35, 0.02), None)
            await asyncio.sleep(0.15)
            session.set_idle("hold")
            await asyncio.wrap_future(future)  # still completes at its own duration
            during_zs = _head_zs(robot)
            await asyncio.sleep(BLEND_S + 0.15)
            after_zs = _head_zs(robot)[-10:]
        return during_zs, after_zs

    during_zs, after_zs = asyncio.run(run())
    assert max(during_zs) >= 0.015  # the emotion played fully
    assert all(abs(z) < 1e-4 for z in after_zs)  # settled at hold, not breathing


def test_pause_fails_in_flight_primaries() -> None:
    async def run() -> tuple[concurrent.futures.Future[None], int, int]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            future = session.submit(_TestPrimary(2.0, 0.02), None)
            await asyncio.sleep(0.2)
            session.pause()
            await asyncio.sleep(0.15)
            before = len(robot.targets)
            await asyncio.sleep(0.15)
            after = len(robot.targets)
        return future, before, after

    future, before, after = asyncio.run(run())
    exc = future.exception(timeout=1)
    assert isinstance(exc, BridgeError)
    assert after == before


def test_close_eases_to_neutral_when_commanding() -> None:
    async def run() -> FakeReachyMini:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            await asyncio.sleep(0.6)
        return robot

    robot = asyncio.run(run())
    head, antennas, _yaw = robot.last_target
    assert abs(head[2, 3]) < 0.001
    assert antennas == pytest.approx(NEUTRAL_ANTENNAS, abs=1e-3)
    zs = _head_zs(robot)
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.003


def test_close_is_immediate_when_quiet() -> None:
    async def run() -> tuple[float, int]:
        robot = FakeReachyMini()
        t0 = time.monotonic()
        async with _running(MotionSession(robot, presence=False, idle="breathing")):
            pass
        return time.monotonic() - t0, len(robot.targets)

    elapsed, count = asyncio.run(run())
    assert elapsed < 0.2
    assert count == 0


def test_a_failing_tick_fails_the_primary_and_keeps_the_loop_alive() -> None:
    async def run() -> tuple[BaseException | None, int, int]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="breathing")
        ) as session:
            session.resume()
            future = session.submit(_TestPrimary(1.0, 0.02, fail_after=0.1), None)
            exc: BaseException | None = None
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), timeout=2)
            except Exception as e:  # noqa: BLE001 - captured for the assertion below
                exc = e
            before = len(robot.targets)
            await asyncio.sleep(0.2)
            after = len(robot.targets)
        return exc, before, after

    exc, before, after = asyncio.run(run())
    assert isinstance(exc, RuntimeError)
    assert after > before


# --- a lost connection (specs/motion/motion.md "Lifecycle") ---------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("Lost connection with the server."),
        websockets.exceptions.ConnectionClosedError(None, None),
    ],
    ids=["ConnectionError", "ConnectionClosedError"],
)
def test_lost_connection_logs_once_and_pauses_for_good(
    error: Exception, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="reachy_mini_bridge.motion")

    async def run() -> tuple[
        concurrent.futures.Future[None],
        concurrent.futures.Future[None],
        int,
        int,
        float,
    ]:
        robot = FakeReachyMini()
        real_set_target = robot.set_target

        def set_target(*args: object, **kwargs: object) -> None:
            if daemon_gone:
                raise error
            real_set_target(*args, **kwargs)  # type: ignore[arg-type]

        daemon_gone = False
        monkeypatch.setattr(robot, "set_target", set_target)
        session = MotionSession(robot, presence=True, idle="breathing")
        async with _running(session):
            session.resume()
            in_flight = session.submit(_TestPrimary(2.0, 0.02), None)
            await asyncio.sleep(BLEND_S + 0.15)  # the trajectory is playing
            daemon_gone = True
            await asyncio.sleep(0.1)
            count_after_loss = len(robot.targets)
            session.resume()  # a motors resume cannot restart a dead stream
            later = session.submit(_TestPrimary(0.2, 0.01), None)
            await asyncio.sleep(0.2)
            count_later = len(robot.targets)
            t0 = time.monotonic()
        return in_flight, later, count_after_loss, count_later, time.monotonic() - t0

    in_flight, later, count_after_loss, count_later, exit_s = asyncio.run(run())
    exc = in_flight.exception(timeout=1)
    assert isinstance(exc, BridgeError) and exc.__cause__ is error
    later_exc = later.exception(timeout=1)
    assert isinstance(later_exc, BridgeError)
    assert count_later == count_after_loss  # nothing sent after the loss
    assert exit_s < 0.2  # no exit blend into a dead socket
    warnings = [
        r
        for r in caplog.records
        if r.name == "reachy_mini_bridge.motion" and r.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "lost connection" in warnings[0].getMessage()


# --- idle offsets, custom idle moves (specs/motion/motion.md "Custom idle moves") -----------


def test_idle_offsets_pose_is_in_human_units_and_scales_to_neutral() -> None:
    offsets = IdleOffsets(
        z_mm=5.0, yaw_deg=10.0, antenna_right_deg=15.0, antenna_left_deg=5.0
    )
    head, antennas, body_yaw = offsets.pose()
    assert head[2, 3] == pytest.approx(0.005)
    assert math.degrees(math.atan2(head[1, 0], head[0, 0])) == pytest.approx(10.0)
    # outward is negative for the right antenna, positive for the left
    assert antennas[0] == pytest.approx(NEUTRAL_ANTENNAS[0] - math.radians(15.0))
    assert antennas[1] == pytest.approx(NEUTRAL_ANTENNAS[1] + math.radians(5.0))
    assert body_yaw == NEUTRAL_BODY_YAW

    half_head, half_antennas, _ = offsets.pose(0.5)
    assert half_head[2, 3] == pytest.approx(0.0025)
    assert half_antennas[1] == pytest.approx(NEUTRAL_ANTENNAS[1] + math.radians(2.5))

    for neutral in (offsets.pose(0.0), IdleOffsets().pose()):
        assert np.array_equal(neutral[0], NEUTRAL_HEAD)
        assert np.array_equal(neutral[1], NEUTRAL_ANTENNAS)


LIFT_MM = 8.0


class _Lift(IdleMove):
    """A custom idle move: the head held ``LIFT_MM`` above neutral. Constant, so it is
    at rest everywhere; records every ``t`` it is asked for."""

    def __init__(self) -> None:
        self.ts: list[float] = []

    def offsets(self, t: float) -> IdleOffsets:
        self.ts.append(t)
        return IdleOffsets(z_mm=LIFT_MM)


class _BreaksAfter(IdleMove):
    """A custom idle move whose ``offsets`` raises once ``t`` passes 0.1 s."""

    def offsets(self, t: float) -> IdleOffsets:
        if t > 0.1:
            raise RuntimeError("boom")
        return IdleOffsets()


def test_custom_idle_move_plays_in_custom_mode() -> None:
    async def run() -> tuple[list[float], FakeReachyMini]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=_Lift)
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.3)
            zs = _head_zs(robot)
        return zs, robot

    zs, robot = asyncio.run(run())
    assert zs[0] == pytest.approx(0.0, abs=0.001)  # blended in from neutral
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.001
    # exit faded the custom move out: the robot is left at neutral (the loop evaluates
    # a finite stage up to 1 ms short of its end, hence the tolerance)
    assert robot.last_target is not None
    assert np.allclose(robot.last_target[0], NEUTRAL_HEAD, atol=1e-4)


def test_custom_mode_without_a_move_holds_neutral() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom")
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert zs
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in zs)


def test_set_idle_move_in_custom_mode_takes_effect_at_once() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom")
        ) as session:
            session.resume()
            await asyncio.sleep(0.2)
            session.set_idle_move(_Lift)
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)


def test_idle_move_is_stored_in_another_mode_and_plays_once_custom() -> None:
    async def run() -> tuple[list[float], list[float]]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="hold")
        ) as session:
            session.resume()
            session.set_idle_move(_Lift)
            await asyncio.sleep(BLEND_S + 0.2)
            held = _head_zs(robot)
            session.set_idle("custom")
            await asyncio.sleep(BLEND_S + 0.3)
            return held, _head_zs(robot)

    held, zs = asyncio.run(run())
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in held)  # stored, not played
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)


def test_leaving_a_custom_idle_move_fades_it_out_to_neutral() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=_Lift)
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            before = len(robot.targets)
            session.set_idle("hold")
            await asyncio.sleep(
                2 * BLEND_S + 0.2
            )  # the fade-out, then the hold's blend
            return _head_zs(robot)[before:]

    zs = asyncio.run(run())
    assert zs[0] == pytest.approx(LIFT_MM / 1000.0, abs=0.001)
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in zs[-5:])
    assert all(b <= a + 1e-9 for a, b in itertools.pairwise(zs))  # only ever down
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.001


def test_each_idle_entry_builds_a_fresh_custom_move() -> None:
    built: list[_Lift] = []

    def factory() -> _Lift:
        built.append(_Lift())
        return built[-1]

    async def run() -> int:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=factory)
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            before = len(built)
            await asyncio.wrap_future(session.submit(_TestPrimary(0.15, 0.01), None))
            await asyncio.sleep(BLEND_S + 0.2)
            return before

    before = asyncio.run(run())
    assert len(built) > before  # the idle entry after the primary built a new move
    played = [move for move in built if len(move.ts) > 2]
    assert len(played) >= 2
    # each entry plays its own move from t = 0 (the first call is the blend's evaluate(0))
    assert all(move.ts[0] == 0.0 and move.ts[1] < 0.1 for move in played)


def test_failing_custom_idle_move_falls_back_to_the_hold_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> tuple[list[float], int, int, list[float]]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=_BreaksAfter)
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.1 + BLEND_S + 0.3)  # breaks, then holds
            held = _head_zs(robot)[-5:]
            before = len(robot.targets)
            await asyncio.sleep(0.2)
            after = len(robot.targets)
            session.set_idle_move(_Lift)  # registering another move recovers
            await asyncio.sleep(BLEND_S + 0.3)
            return held, before, after, _head_zs(robot)

    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.motion"):
        held, before, after, zs = asyncio.run(run())
    warnings = [r for r in caplog.records if "custom idle move failed" in r.message]
    assert len(warnings) == 1
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in held)
    assert after > before  # the loop is still commanding (the hold), not dead
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)


class _NanOffsets(IdleMove):
    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=math.nan)


class _WrongReturn(IdleMove):
    def offsets(self, t: float) -> IdleOffsets:
        return (0.0, 0.0)  # type: ignore[return-value]


def _raising_factory() -> IdleMove:
    raise RuntimeError("no move today")


@pytest.mark.parametrize(
    "bad",
    [
        "breathing",  # not callable
        HoldMove,  # builds a Move that is not an IdleMove
        _raising_factory,
        _NanOffsets,
        _WrongReturn,
    ],
)
def test_a_bad_idle_move_factory_is_rejected_and_changes_nothing(bad: object) -> None:
    with pytest.raises(ValueError, match="idle move"):
        MotionSession(
            FakeReachyMini(),
            presence=True,
            idle="custom",
            idle_move=bad,  # type: ignore[arg-type]
        )

    async def run() -> tuple[object, list[float]]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=_Lift)
        ) as session:
            session.resume()
            with pytest.raises(ValueError, match="idle move"):
                session.set_idle_move(bad)  # type: ignore[arg-type]
            await asyncio.sleep(BLEND_S + 0.3)
            return session.idle_move, _head_zs(robot)

    registered, zs = asyncio.run(run())
    assert registered is _Lift
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)


def test_clearing_the_idle_move_returns_custom_mode_to_the_hold() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=_Lift)
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            session.set_idle_move(None)
            await asyncio.sleep(2 * BLEND_S + 0.2)
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in zs[-5:])


# --- the gaze layer (specs/motion/motion.md "The gaze layer") ----------------------------------


AIM_YAW_DEG = 30.0
# The largest per-tick yaw step a gaze fade may take: the minjerk's peak is ~1.9 deg a
# tick onto a 30 deg aim, with room for a tick that runs late under load.
MAX_GAZE_STEP_DEG = 5.0


def _yaw_pose(yaw_deg: float) -> npt.NDArray[np.float64]:
    """A head pose turned ``yaw_deg`` about the vertical — an aim, as the tracker would
    hand it over."""
    return IdleOffsets(yaw_deg=yaw_deg).pose()[0]


def _yaw_pitch_deg(head: npt.NDArray[np.float64]) -> tuple[float, float]:
    yaw, pitch, _roll = Rotation.from_matrix(head[:3, :3]).as_euler("ZYX", degrees=True)
    return float(yaw), float(pitch)


def _yaws(robot: FakeReachyMini, since: int = 0) -> list[float]:
    return [_yaw_pitch_deg(h)[0] for h, _, _ in robot.targets[since:] if h is not None]


def test_an_aim_eases_the_head_onto_it_and_withdrawing_it_eases_it_back() -> None:
    async def run() -> tuple[list[float], list[float], list[float]]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="hold")
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.1)  # holding neutral
            before = len(robot.targets)
            session.set_gaze(_yaw_pose(AIM_YAW_DEG))
            await asyncio.sleep(1.5)
            onto = _yaws(robot, before)
            pitches = [
                _yaw_pitch_deg(h)[1]
                for h, _, _ in robot.targets[before:]
                if h is not None
            ]
            before = len(robot.targets)
            session.set_gaze(None)
            await asyncio.sleep(BLEND_S + 0.2)
            return onto, pitches, _yaws(robot, before)

    onto, pitches, back = asyncio.run(run())
    assert onto[0] == pytest.approx(0.0, abs=0.5)
    assert all(b >= a - 1e-6 for a, b in itertools.pairwise(onto))  # only ever toward
    assert onto[-1] == pytest.approx(AIM_YAW_DEG, abs=2.0)
    assert all(abs(p) < 0.5 for p in pitches)
    assert back[-1] == pytest.approx(0.0, abs=0.5)
    # a fade, not a step: the minjerk's peak speed over BLEND_S is ~1.9 deg a tick (more
    # on a tick that runs late under load), where a snap would be the whole 30
    assert max(abs(b - a) for a, b in itertools.pairwise(back)) < MAX_GAZE_STEP_DEG


def test_breathing_keeps_breathing_on_the_aim_with_its_roaming_toned_down() -> None:
    async def run() -> list[npt.NDArray[np.float64]]:
        robot = FakeReachyMini()
        seeded = lambda: BreathingMove(random.Random(3))
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=seeded)
        ) as session:
            session.set_gaze(_yaw_pose(AIM_YAW_DEG))  # before the loop runs
            session.resume()
            await asyncio.sleep(BLEND_S + 0.5)  # entered, faded in, eased on
            before = len(robot.targets)
            await asyncio.sleep(2.5)  # the first breath peaks 2.5 s in
            return [h for h, _, _ in robot.targets[before:] if h is not None]

    heads = asyncio.run(run())
    zs = [float(h[2, 3]) for h in heads]
    yaws = [_yaw_pitch_deg(h)[0] for h in heads]
    assert max(zs) - min(zs) >= 0.004  # still breathing
    assert sum(yaws) / len(yaws) == pytest.approx(AIM_YAW_DEG, abs=3.0)
    roam = math.degrees(HEAD_YAW_RAD) * BREATHING_GAZE_ROTATION_SCALE
    assert max(abs(y - AIM_YAW_DEG) for y in yaws) <= roam + 1.0


def test_focus_holds_the_head_on_the_aim_and_keeps_the_antennas_alive() -> None:
    async def run() -> tuple[
        list[npt.NDArray[np.float64]], list[npt.NDArray[np.float64]]
    ]:
        robot = FakeReachyMini()
        seeded = lambda: BreathingMove(random.Random(3))
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=seeded)
        ) as session:
            session.set_gaze(_yaw_pose(AIM_YAW_DEG), focus=True)
            session.resume()
            await asyncio.sleep(BLEND_S + 0.5)
            before = len(robot.targets)
            await asyncio.sleep(2.5)  # the breath would peak here
            heads = [h for h, _, _ in robot.targets[before:] if h is not None]
            antennas = [a for _, a, _ in robot.targets[before:] if a is not None]
            return heads, antennas

    heads, antennas = asyncio.run(run())
    assert all(
        _yaw_pitch_deg(h)[0] == pytest.approx(AIM_YAW_DEG, abs=0.3) for h in heads
    )
    assert max(float(h[2, 3]) for h in heads) < 1e-4  # no breath, no roaming
    spread = np.ptp(np.degrees(np.array(antennas)), axis=0)
    assert spread.max() > 2.0  # the antennas still roam


class _Nod(IdleMove):
    """A custom idle move lifting the head ``LIFT_MM`` while nobody is tracked, and
    nodding in pitch around the aim while someone is."""

    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=LIFT_MM)

    def gaze_offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(pitch_deg=4.0 * (1.0 - math.cos(2.0 * math.pi * t)) / 2.0)


def test_a_custom_idle_move_sits_still_on_the_aim_unless_it_says_otherwise() -> None:
    async def run(
        factory: IdleMoveFactory,
    ) -> tuple[list[tuple[float, float, float]], list[npt.NDArray[np.float64]], float]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="custom", idle_move=factory)
        ) as session:
            session.set_gaze(_yaw_pose(AIM_YAW_DEG))
            session.resume()
            await asyncio.sleep(BLEND_S + 0.8)
            before = len(robot.targets)
            await asyncio.sleep(1.0)
            held = [
                (*_yaw_pitch_deg(h), float(h[2, 3]))
                for h, _, _ in robot.targets[before:]
                if h is not None
            ]
            antennas = [a for _, a, _ in robot.targets[before:] if a is not None]
            session.set_gaze(None)
            await asyncio.sleep(BLEND_S + 0.3)
            return held, antennas, float(robot.last_target[0][2, 3])

    held, antennas, released_z = asyncio.run(run(_Lift))
    # no gaze_offsets: the head is the aim, the antennas rest, the lift is gone…
    assert all(y == pytest.approx(AIM_YAW_DEG, abs=0.5) for y, _, _ in held)
    assert all(abs(p) < 0.5 and abs(z) < 1e-4 for _, p, z in held)
    assert all(np.allclose(a, NEUTRAL_ANTENNAS) for a in antennas)
    # …and shows in full once nobody is tracked
    assert released_z == pytest.approx(LIFT_MM / 1000.0, abs=1e-5)

    held, _antennas, _z = asyncio.run(run(_Nod))
    pitches = [p for _, p, _ in held]
    assert all(y == pytest.approx(AIM_YAW_DEG, abs=0.5) for y, _, _ in held)
    assert max(pitches) - min(pitches) > 3.0  # the nod, around the aim


def test_idle_offsets_scaled_multiplies_each_group_by_its_factor() -> None:
    offsets = IdleOffsets(
        z_mm=4.0,
        roll_deg=1.0,
        pitch_deg=-2.0,
        yaw_deg=8.0,
        antenna_right_deg=10.0,
        antenna_left_deg=-6.0,
    )
    assert offsets.scaled(translation=0.5, rotation=0.25, antennas=2.0) == IdleOffsets(
        z_mm=2.0,
        roll_deg=0.25,
        pitch_deg=-0.5,
        yaw_deg=2.0,
        antenna_right_deg=20.0,
        antenna_left_deg=-12.0,
    )
    assert offsets.scaled() == offsets
    assert BreathingMove(random.Random(1)).gaze_offsets(7.0) == BreathingMove(
        random.Random(1)
    ).offsets(7.0).scaled(rotation=BREATHING_GAZE_ROTATION_SCALE)


class _NanGaze(IdleMove):
    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets()

    def gaze_offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(yaw_deg=math.inf)


class _GazeBreaksAfter(IdleMove):
    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets()

    def gaze_offsets(self, t: float) -> IdleOffsets:
        if t > 0.1:
            raise RuntimeError("boom")
        return IdleOffsets()


def test_a_bad_gaze_offsets_is_rejected_at_registration() -> None:
    with pytest.raises(ValueError, match="gaze_offsets"):
        MotionSession(
            FakeReachyMini(), presence=True, idle="custom", idle_move=_NanGaze
        )


def test_a_gaze_offsets_raising_while_playing_falls_back_to_the_hold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(
                robot, presence=True, idle="custom", idle_move=_GazeBreaksAfter
            )
        ) as session:
            session.set_gaze(_yaw_pose(AIM_YAW_DEG))
            session.resume()
            await asyncio.sleep(2 * BLEND_S + 1.0)  # breaks, then holds on the aim
            return _yaws(robot)

    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.motion"):
        yaws = asyncio.run(run())
    warnings = [r for r in caplog.records if "custom idle move failed" in r.message]
    assert len(warnings) == 1
    assert yaws[-1] == pytest.approx(AIM_YAW_DEG, abs=1.0)  # the hold, aimed


def test_a_primary_plays_as_recorded_under_an_aim_then_the_head_returns_to_it() -> None:
    async def run() -> tuple[list[tuple[float, float]], list[float]]:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=True, idle="hold")
        ) as session:
            session.set_gaze(_yaw_pose(AIM_YAW_DEG))
            session.resume()
            await asyncio.sleep(BLEND_S + 0.8)
            future = session.submit(_TestPrimary(duration=0.6, z=0.01), None)
            await asyncio.sleep(BLEND_S + 0.1)  # the trajectory is playing
            before = len(robot.targets)
            await asyncio.sleep(0.3)
            during = [
                (_yaw_pitch_deg(h)[0], float(h[2, 3]))
                for h, _, _ in robot.targets[before:]
                if h is not None
            ]
            await asyncio.wrap_future(future)
            before = len(robot.targets)
            await asyncio.sleep(2 * BLEND_S + 0.5)
            return during, _yaws(robot, before)

    during, after = asyncio.run(run())
    assert during
    assert all(abs(y) < 1e-6 and z == pytest.approx(0.01) for y, z in during)
    assert after[-1] == pytest.approx(AIM_YAW_DEG, abs=1.0)
    assert max(abs(b - a) for a, b in itertools.pairwise(after)) < MAX_GAZE_STEP_DEG


def test_with_presence_off_an_aim_moves_nothing() -> None:
    async def run() -> int:
        robot = FakeReachyMini()
        async with _running(
            MotionSession(robot, presence=False, idle="hold")
        ) as session:
            session.resume()
            session.set_gaze(_yaw_pose(AIM_YAW_DEG))
            await asyncio.sleep(0.4)
            return len(robot.targets)

    assert asyncio.run(run()) == 0


def test_the_history_records_the_head_pose_the_robot_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual head, not the commanded one — recorded whether or not the loop
    commands: here a paused loop, while another writer turns the head at 20 deg/s."""
    robot = FakeReachyMini()
    t0 = time.monotonic()
    monkeypatch.setattr(
        robot,
        "get_current_head_pose",
        lambda: _yaw_pose(20.0 * (time.monotonic() - t0)),
    )

    async def run() -> tuple[float, float, float, float]:
        async with _running(
            MotionSession(robot, presence=True, idle="hold")
        ) as session:
            await asyncio.sleep(1.5)
            now = time.monotonic()
            return (
                now - t0,
                _yaw_pitch_deg(session.head_pose_at(now - 0.5))[0],
                _yaw_pitch_deg(session.head_pose_at(now - 10.0))[0],
                _yaw_pitch_deg(session.head_pose_at(now + 1.0))[0],
            )

    elapsed, half_ago, oldest, latest = asyncio.run(run())
    assert robot.targets == []  # paused: nothing commanded
    assert half_ago == pytest.approx(20.0 * (elapsed - 0.5), abs=1.0)
    assert oldest == pytest.approx(0.0, abs=1.0)  # the first recorded
    assert latest == pytest.approx(20.0 * elapsed, abs=1.0)
    assert GAZE_HISTORY_S > 1.5  # all of it kept


def test_head_pose_at_reads_the_robot_before_anything_is_recorded() -> None:
    robot = FakeReachyMini()
    elsewhere = _yaw_pose(12.0)
    robot.set_target(head=elsewhere)
    session = MotionSession(robot, presence=True, idle="hold")  # not started
    np.testing.assert_allclose(session.head_pose_at(time.monotonic() - 0.3), elsewhere)


def test_start_twice_raises_and_stop_is_idempotent() -> None:
    async def run() -> FakeReachyMini:
        robot = FakeReachyMini()
        session = MotionSession(robot, presence=False, idle="hold")
        await session.stop()  # never started: a no-op
        await session.start()
        with pytest.raises(BridgeError):
            await session.start()
        await session.stop()
        await session.stop()  # already stopped: a no-op
        return robot

    robot = asyncio.run(run())
    assert robot.targets == []
