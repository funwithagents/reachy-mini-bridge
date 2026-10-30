"""E2E tier — the motion loop over a live daemon (specs/motion/motion.md): the daemon on the
kinematics engine the run asked for (specs/testing/testing.md "One engine per run"),
breathing on the z axis and the still hold, a caller's custom idle move, an emotion from
the library played through the loop and back to neutral, an emotion cancelled mid-flight
— motion and sound stopped (specs/core/bridge.md "Cancellation").

Gated on `motion` (every target). The emotion tests take the `emotions_library` fixture
(conftest.py), which skips when the library cannot be fetched.

Run explicitly:
    uv run pytest tests-e2e/test_motion.py -rs
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_motion.py -rs
    REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e/test_motion.py -rs
    REACHY_MINI_E2E_KINEMATICS=placo uv run pytest tests-e2e/test_motion.py -rs

Every test awaits the bridge through `live_bridge.run(...)`, the harness's one event
loop (specs/testing/testing_support.md "Public surface"); the daemon is the run's, the
bridge session this module's (specs/testing/testing.md "Daemon lifecycle")."""

from __future__ import annotations

import asyncio
import time
import urllib.error
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
from reachy_mini import ReachyMini

from reachy_mini_bridge.bridge import _daemon_kinematics_engine
from reachy_mini_bridge.config import (
    SIM_CAMERA_ROUTE,
    UPSTREAM_KINEMATICS_ENGINES,
)
from reachy_mini_bridge.motion import (
    BLEND_S,
    BREATH_REST_S,
    BREATH_S,
    IdleMove,
    IdleOffsets,
)
from reachy_mini_bridge.robot import fetch_daemon_json
from reachy_mini_bridge.testing import LiveBridge, _daemon
from reachy_mini_bridge.testing._daemon import kinematics_engine

pytestmark = pytest.mark.requires("motion")


def test_sim_daemon_reports_its_camera(live_bridge: LiveBridge) -> None:
    """A spawned sim daemon answers `GET /api/sim/camera` with its rendered camera, and
    the session's tracker holds the head-mounted model built from it. A real robot has
    no such route; a borrowed upstream daemon answers 404."""
    bridge, _caps = live_bridge
    if _daemon.backend() != "sim":
        pytest.skip("the camera report is the sim launcher's")
    try:
        report = fetch_daemon_json(bridge.robot, SIM_CAMERA_ROUTE)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            pytest.skip("a daemon without the bridge's launcher")
        raise
    assert report == {"source": "sim", "hfov_deg": None}
    tracker = bridge._tracker
    assert tracker is not None and not tracker.camera.fixed


def test_daemon_runs_the_engine_the_run_asked_for(live_bridge: LiveBridge) -> None:
    """The daemon solves this run's targets through the engine `REACHY_MINI_E2E_KINEMATICS`
    names (`analytical` by default) — read back from `GET /api/kinematics/info` through
    the bridge's own reader, the one the gravity-compensation guard uses.

    A failure, never a skip: every other test in the tier runs unchanged on each engine,
    and this is what says which one they ran on. A borrowed daemon on another engine
    fails here instead of silently testing the wrong engine — set the variable to what
    it runs (specs/testing/testing.md "One engine per run").
    """
    bridge, _caps = live_bridge
    asked = kinematics_engine()
    engine = live_bridge.run(asyncio.to_thread(_daemon_kinematics_engine, bridge.robot))
    print(f"\n[e2e] kinematics engine: asked {asked!r}, daemon runs {engine!r}")
    assert engine == UPSTREAM_KINEMATICS_ENGINES[asked], (
        f"the daemon runs the {engine!r} kinematics engine, this run asked for {asked!r}; "
        "a borrowed daemon runs whatever it was started with — set "
        "REACHY_MINI_E2E_KINEMATICS to match it"
    )


def test_breathing_moves_the_head_and_breathing_off_holds_it(
    live_bridge: LiveBridge,
) -> None:
    """specs/motion/motion.md: with presence and breathing on, the idle move visibly breathes
    (slow breaths on the z axis, with random rests between them); `set_idle("hold")`
    holds the head still afterwards."""
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def sample_z(seconds: float) -> float:
        zs: list[float] = []
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
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
        robot: Any = bridge.robot
        try:
            # Where the hold rests *on this engine*: analytical and Placo put the neutral
            # within 0.1 mm of the origin, NN 8.5 mm forward and 1.45 deg pitched (its
            # fit, measured on the headless sim) — "back to neutral" is back to here.
            await asyncio.sleep(BLEND_S + 0.5)
            rest = await asyncio.to_thread(robot.get_current_head_pose)
            await bridge.play_emotion(
                names[0]
            )  # completes only if the move actually played
            await asyncio.sleep(BLEND_S + 1.0)  # the return blend eases back to neutral
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            _joints, antennas = await asyncio.to_thread(
                robot.get_current_joint_positions
            )
        finally:
            await bridge.set_idle("breathing")
        deviation = np.abs(np.asarray(antennas) - NEUTRAL_ANTENNAS)
        back = float(np.linalg.norm(pose[:3, 3] - rest[:3, 3]))
        return names[0], back, float(deviation.max())

    played, translation, antenna_deviation = live_bridge.run(scenario())
    print(
        f"\n[e2e] played emotion: {played!r}, back to the hold's rest: "
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
            t0 = time.perf_counter()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            latency = time.perf_counter() - t0
            # The head reaches its last target: the cancel drops the primary at once,
            # but the idle move then blends in from wherever it caught the head
            # (BLEND_S), so settling takes a bit longer than before the motion loop.
            await asyncio.sleep(BLEND_S + 1.0)
            samples: list[npt.NDArray[np.float64]] = []
            t1 = time.perf_counter()
            while time.perf_counter() - t1 < 2.0:
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
