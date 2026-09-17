"""Functional tests for the motion loop (specs/motion.md).

Plain-function tests for the moves and the blend helper; ``MotionSession`` tests drive
it on a bare ``FakeReachyMini`` at real time (the loop runs the same way on both
backends), so timings stay tight but real.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import logging
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest
import websockets.exceptions
from reachy_mini.motion.move import Move

from reachy_mini_bridge.errors import BridgeError
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.motion import (
    ANTENNA_MAX_RAD,
    ANTENNA_MIN_RAD,
    ANTENNA_OUTWARD,
    BLEND_S,
    BREATH_REST_S,
    BREATH_S,
    BREATH_Z_M,
    CONTROL_HZ,
    NEUTRAL,
    NEUTRAL_ANTENNAS,
    NEUTRAL_BODY_YAW,
    NEUTRAL_HEAD,
    BreathingMove,
    HoldMove,
    MotionSession,
    _BreathingFadeOut,
    blend_into,
)

if TYPE_CHECKING:
    import numpy.typing as npt


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
    head, _antennas, _yaw = move.evaluate(BREATH_S / 2)
    assert head is not None
    diff = np.asarray(head) - NEUTRAL[0]
    diff[2, 3] = 0.0
    assert np.allclose(diff, 0.0)  # only z moves


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
    for k in range(6000):  # 120 s at 20 ms
        outward = _outward(move, k * 0.02)
        assert np.all(outward >= ANTENNA_MIN_RAD - 1e-9)
        assert np.all(outward <= ANTENNA_MAX_RAD + 1e-9)


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
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.0003
    assert max(float(np.max(np.abs(b - a))) for a, b in itertools.pairwise(ants)) < 0.02
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


def test_fade_out_lands_at_neutral_at_rest() -> None:
    move = BreathingMove(random.Random(0))
    offset = next(
        k * 0.01
        for k in range(6000)
        if _z(move, k * 0.01) > 0.0
        and np.any(_outward(move, k * 0.01) > ANTENNA_MIN_RAD + 1e-6)
    )
    fade = _BreathingFadeOut(move, t_offset=offset)
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
    # the envelope adds its own slope: 5 mm over BLEND_S at a minjerk peak (1.875x mean)
    # is ~0.31 mm per tick, and up to 15° of antenna lean the same way is ~0.016 rad
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.0005
    assert max(float(np.max(np.abs(b - a))) for a, b in itertools.pairwise(ants)) < 0.03


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
        async with MotionSession(robot, presence=True, breathing=True):
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
        async with MotionSession(robot, presence=True, breathing=True) as session:
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
        async with MotionSession(robot, presence=True, breathing=False) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert zs
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in zs[-5:])


def test_presence_off_goes_quiet_when_idle() -> None:
    async def run() -> tuple[int, int, int]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=False, breathing=True) as session:
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
        async with MotionSession(robot, presence=True, breathing=True) as session:
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
        async with MotionSession(robot, presence=True, breathing=True) as session:
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
        async with MotionSession(robot, presence=True, breathing=True) as session:
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
        async with MotionSession(robot, presence=True, breathing=True) as session:
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
        async with MotionSession(robot, presence=True, breathing=True) as session:
            session.resume()
            future = session.submit(_TestPrimary(0.35, 0.02), None)
            await asyncio.sleep(0.15)
            session.set_breathing(False)
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
        async with MotionSession(robot, presence=True, breathing=True) as session:
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
        async with MotionSession(robot, presence=True, breathing=True) as session:
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
        async with MotionSession(robot, presence=False, breathing=True):
            pass
        return time.monotonic() - t0, len(robot.targets)

    elapsed, count = asyncio.run(run())
    assert elapsed < 0.2
    assert count == 0


def _head_held_elsewhere(
    robot: FakeReachyMini, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer (daemon-side tracking) holds the head 2 cm up: the pose readers
    report it whatever the loop commands. (The fake's readers otherwise return the last
    set_target, which the loop's next tick would overwrite.)"""
    head = np.eye(4)
    head[2, 3] = 0.02
    monkeypatch.setattr(robot, "get_current_head_pose", lambda: head)


def test_reanchor_resumes_idle_from_the_present_pose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=True, breathing=True) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            _head_held_elsewhere(robot, monkeypatch)
            before = len(robot.targets)
            await asyncio.wrap_future(session.reanchor())
            await asyncio.sleep(BLEND_S + 0.2)
            return _head_zs(robot)[before:]

    zs = asyncio.run(run())
    # at most a tick or two of the old idle move before the command is taken, then the
    # blend starts from the present pose…
    start = next(i for i, z in enumerate(zs) if z > 0.015)
    assert start <= 2
    assert zs[start] == pytest.approx(0.02, abs=0.003)
    assert abs(zs[-1]) < BREATH_Z_M + 0.002  # …back into the idle move
    assert max(abs(b - a) for a, b in itertools.pairwise(zs[start:])) < 0.002


def test_reanchor_is_a_noop_during_a_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> tuple[bool, list[float]]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=True, breathing=True) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.1)
            future = session.submit(_TestPrimary(duration=0.6, z=0.01), None)
            await asyncio.sleep(BLEND_S + 0.1)  # the primary's trajectory is playing
            _head_held_elsewhere(robot, monkeypatch)
            before = len(robot.targets)
            done = session.reanchor()
            await asyncio.sleep(0.1)
            resolved = done.done()
            zs = _head_zs(robot)[before:]
            await asyncio.wrap_future(future)
            return resolved, zs

    resolved, zs = asyncio.run(run())
    assert resolved
    # the primary kept playing: no re-blend from the 0.02 present pose
    assert zs and all(z < 0.015 for z in zs)


def test_a_failing_tick_fails_the_primary_and_keeps_the_loop_alive() -> None:
    async def run() -> tuple[BaseException | None, int, int]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=True, breathing=True) as session:
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


# --- a lost connection (specs/motion.md "Lifecycle") ---------------------------------


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
        session = MotionSession(robot, presence=True, breathing=True)
        async with session:
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
