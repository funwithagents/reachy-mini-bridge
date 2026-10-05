"""E2E tier — the motion loop over a live daemon (specs/motion/motion.md): breathing on the
z axis and the still hold, a caller's custom idle move, an emotion from the library
played through the loop and back to neutral, an emotion cancelled mid-flight — motion
and sound stopped (specs/core/bridge.md "Cancellation").

Gated on `motion` (every target). The emotion tests take the `emotions_library` fixture
(conftest.py), which skips when the library cannot be fetched.

Run explicitly:
    uv run pytest tests-e2e/test_motion.py -rs
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_motion.py -rs
    REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e/test_motion.py -rs

Every test awaits the bridge through `live_bridge.run(...)`, the harness's one event
loop (specs/testing/testing_support.md "Public surface"); the daemon is the run's, the
bridge session this module's (specs/testing/testing.md "Daemon lifecycle")."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
from reachy_mini import ReachyMini

from reachy_mini_bridge.motion import (
    BLEND_S,
    BREATH_REST_S,
    BREATH_S,
    IdleMove,
    IdleOffsets,
)
from reachy_mini_bridge.testing import LiveBridge, requires_caps


def test_breathing_moves_the_head_and_breathing_off_holds_it(
    live_bridge: LiveBridge,
) -> None:
    """specs/motion/motion.md: with presence and breathing on, the idle move visibly breathes
    (slow breaths on the z axis, with random rests between them); `set_idle("hold")`
    holds the head still afterwards."""
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def sample_z(seconds: float) -> float:
        zs: list[float] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            zs.append(float(pose[2, 3]))
            await asyncio.sleep(0.1)
        return max(zs) - min(zs)

    async def scenario() -> tuple[float, float]:
        await bridge.set_motors_state("enabled")
        await asyncio.sleep(1.0)
        # long enough to always contain a whole breath, wherever the sample starts
        breathing_range = await sample_z(BREATH_S + BREATH_REST_S[1] + 1.0)
        await bridge.set_idle("hold")
        await asyncio.sleep(BLEND_S + 0.5)
        still_range = await sample_z(3.0)
        await bridge.set_idle("breathing")
        return breathing_range, still_range

    breathing_range, still_range = live_bridge.run(scenario())
    print(
        f"\n[e2e] breathing z range {breathing_range:.4f} m, still {still_range:.4f} m"
    )
    assert breathing_range >= 0.002
    assert still_range < 0.001


class _Lift(IdleMove):
    """A custom idle move: the head held 8 mm above neutral."""

    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=8.0)


def test_custom_idle_move_drives_the_head(
    live_bridge: LiveBridge,
) -> None:
    """specs/motion/motion.md "Custom idle moves": a registered `IdleMove` plays in the
    `custom` idle mode — the head rises to its offset — and leaving the mode brings the
    head back to neutral."""
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def head_z() -> float:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        return float(pose[2, 3])

    async def scenario() -> tuple[float, float, float]:
        await bridge.set_motors_state("enabled")
        await bridge.set_idle("hold")
        await asyncio.sleep(2 * BLEND_S + 1.0)
        neutral_z = await head_z()
        try:
            await bridge.set_idle_move(_Lift)
            await bridge.set_idle("custom")
            await asyncio.sleep(BLEND_S + 1.5)
            lifted_z = await head_z()
            await bridge.set_idle("hold")
            await asyncio.sleep(2 * BLEND_S + 1.0)
            back_z = await head_z()
        finally:
            await bridge.set_idle_move(None)
            await bridge.set_idle("breathing")
        return neutral_z, lifted_z, back_z

    neutral_z, lifted_z, back_z = live_bridge.run(scenario())
    print(
        f"\n[e2e] custom idle: neutral z {neutral_z:.4f} m, lifted {lifted_z:.4f} m, "
        f"back {back_z:.4f} m"
    )
    assert lifted_z - neutral_z >= 0.005  # 8 mm commanded
    assert abs(back_z - neutral_z) < 0.002


def test_play_emotion_plays_a_real_move(
    live_bridge: LiveBridge,
    emotions_library: None,
) -> None:
    """Actually play an emotion: enumerate the library, then move the robot.

    The daemon preloads the datasets in the background, so on a fresh machine the
    client-side emotions library may not be in the local HuggingFace cache yet. This opt-in live test **downloads it on a
    cache miss** (a one-time cost) so it genuinely exercises the move, skipping only when
    the dataset truly can't be fetched (offline). On the headfull-viewer sim you should
    see the robot perform the move.
    """
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge

    async def scenario() -> tuple[str, float, float]:
        from reachy_mini_bridge.motion import NEUTRAL_ANTENNAS

        names = await bridge.list_emotions()
        assert names, "emotions library loaded but empty"
        await bridge.set_motors_state("enabled")
        # The hold is the still neutral: after the emotion the loop blends back to it
        # and stays there, so the "back to neutral" sample is deterministic. Under
        # breathing (the default idle) the antennas roam 10-25 deg outward after a
        # random 0.4-2.5 s rest, and whether the sample lands in the rest or in the
        # roam was a coin toss (specs/motion/motion.md "Antenna tracks").
        await bridge.set_idle("hold")
        try:
            await bridge.play_emotion(
                names[0]
            )  # completes only if the move actually played
            await asyncio.sleep(BLEND_S + 1.0)  # the return blend eases back to neutral
            robot: Any = bridge.robot
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            _joints, antennas = await asyncio.to_thread(
                robot.get_current_joint_positions
            )
        finally:
            await bridge.set_idle("breathing")
        deviation = np.abs(np.asarray(antennas) - NEUTRAL_ANTENNAS)
        return names[0], float(np.linalg.norm(pose[:3, 3])), float(deviation.max())

    played, translation, antenna_deviation = live_bridge.run(scenario())
    print(
        f"\n[e2e] played emotion: {played!r}, back to neutral: "
        f"translation {translation:.4f} m, antenna deviation {antenna_deviation:.3f} rad"
    )
    assert translation < 0.008
    assert antenna_deviation < 0.1


def test_cancelled_emotion_stops_motion_and_sound(
    live_bridge: LiveBridge,
    emotions_library: None,
) -> None:
    """specs/core/bridge.md "Cancellation": cancelling `play_emotion` 3 s into `dance2` returns
    at once, the joints are still afterwards (no sound left driving the wobbler), and
    the local backend's playbin is cleared. Measured before the fix: the sound played
    its remaining 15 s and the head kept swaying 0.1–0.2 rad per half second."""
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    robot = bridge.robot
    assert isinstance(robot, ReachyMini)

    async def scenario() -> tuple[float, float, object]:
        await bridge.set_motors_state("enabled")
        await bridge.set_wobbling(True)
        # The hold keeps the joints still after the return blend; breathing would
        # otherwise still be moving them when we sample (specs/motion/motion.md).
        await bridge.set_idle("hold")
        try:
            task = asyncio.create_task(bridge.play_emotion("dance2"))
            await asyncio.sleep(3.0)  # long enough to see the dance and hear its sound
            t0 = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            latency = time.monotonic() - t0
            # The head reaches its last target: the cancel drops the primary at once,
            # but the idle move then blends in from wherever it caught the head
            # (BLEND_S), so settling takes a bit longer than before the motion loop.
            await asyncio.sleep(BLEND_S + 1.0)
            samples: list[npt.NDArray[np.float64]] = []
            t1 = time.monotonic()
            while time.monotonic() - t1 < 2.0:
                head, antennas = await asyncio.to_thread(
                    robot.get_current_joint_positions
                )
                samples.append(np.array(list(head) + list(antennas), dtype=np.float64))
                await asyncio.sleep(0.05)
            stacked = np.stack(samples)
            travel = float((stacked.max(axis=0) - stacked.min(axis=0)).max())
            playbin = getattr(robot.media.audio, "_playbin", "not-local")
            return latency, travel, playbin
        finally:
            await bridge.set_idle("breathing")

    latency, travel, playbin = live_bridge.run(scenario())
    print(
        f"\n[e2e] cancel latency {latency * 1000:.0f} ms, joint travel after {travel:.4f} rad"
    )
    assert latency < 0.1
    assert travel < 0.02, f"joints still moving after the cancel: {travel:.4f} rad"
    if playbin != "not-local":
        assert playbin is None
