"""E2E tier — the motor states over a live daemon (specs/core/bridge.md "Motors").

Gated on `motion` (every target). The first test lowers the head to the SDK's sleep pose
before torque goes off and raises it back once torque is on, so it is first in this file
and restores the awake pose itself; the file's place in the run does not matter.

Run explicitly:
    uv run pytest tests-e2e/test_motors.py -rs
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_motors.py -rs
    REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e/test_motors.py -rs

Every test awaits the bridge through `live_bridge.run(...)`, the harness's one event
loop (specs/testing/testing_support.md "Public surface"); the daemon is the run's, the
bridge session this module's (specs/testing/testing.md "Daemon lifecycle")."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.errors import (
    GravityCompensationUnsupportedError,
)
from reachy_mini_bridge.testing import LiveBridge, requires_caps


async def _motors_state_after_set(bridge: ReachyMiniBridge, state: str) -> str:
    """Set a motor state, then read it back once the daemon status reflects it.

    The daemon's status lags a switch by a fraction of a second, so the read polls for up
    to a second; a target that ignores the state (the sim) reads back its old one.
    """
    await bridge.set_motors_state(state)
    deadline = time.monotonic() + 1.0
    while (mode := await bridge.get_motors_state()) != state:
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(0.1)
    return mode


def test_motor_state_reads_and_dispatches_over_the_live_path(
    live_bridge: LiveBridge,
) -> None:
    """`get_motors_state` reads a valid mode and each `set_motors_state` reaches the daemon.

    The e2e value here is that the read (a real daemon status round-trip) and each set
    dispatch work over the network, and on a target that honors a state — hardware,
    probed as `motor_states` — that each reads back as set. The sim daemon ignores every
    motor-state change: `disabled` keeps reporting `enabled` (headless and headfull
    viewer), so there it asserts validity only; the fast tier pins the exact
    dispatch→state mapping deterministically on the fake. Gravity compensation has its own capability-gated test
    below: sending it to a daemon that can't hold it drops the connection.

    First in this file on purpose, and gentle on hardware: the head is lowered to the
    SDK's sleep pose before torque goes off (so a robot that honors `disabled` rests
    rather than drops), then raised back to the initial (awake) pose once torque is on
    again, so every later test starts upright.
    """
    from reachy_mini.reachy_mini import (
        INIT_ANTENNAS_JOINT_POSITIONS,
        INIT_HEAD_POSE,
        SLEEP_ANTENNAS_JOINT_POSITIONS,
        SLEEP_HEAD_POSE,
    )

    requires_caps(live_bridge, "motion")
    bridge, caps = live_bridge
    # goto_target is upstream-only (not on the fake); this tier is live-only.
    robot: Any = bridge.robot
    valid = {"enabled", "disabled", "gravity_compensation"}

    async def scenario() -> tuple[str, dict[str, str]]:
        original = await bridge.get_motors_state()
        # The motion loop would otherwise blend back to neutral the moment each
        # goto_target ends (specs/motion/motion.md): a caller driving the head directly needs
        # presence off for its own moves to hold.
        await bridge.set_presence(False)
        try:
            results: dict[str, str] = {}
            results["enabled"] = await _motors_state_after_set(bridge, "enabled")
            await asyncio.to_thread(
                robot.goto_target,
                head=SLEEP_HEAD_POSE,
                antennas=SLEEP_ANTENNAS_JOINT_POSITIONS,
                duration=2.0,
            )
            results["disabled"] = await _motors_state_after_set(bridge, "disabled")
            await bridge.set_motors_state("enabled")
            await asyncio.to_thread(
                robot.goto_target,
                head=INIT_HEAD_POSE,
                antennas=INIT_ANTENNAS_JOINT_POSITIONS,
                duration=1.0,
            )
            await bridge.set_motors_state(original)  # restore
        finally:
            await bridge.set_presence(True)
        return original, results

    original, results = live_bridge.run(scenario())
    print(f"\n[e2e] motor states: original={original!r}, read back={results!r}")
    assert original in valid
    assert all(mode in valid for mode in results.values())
    if "motor_states" in caps:
        assert results == {"enabled": "enabled", "disabled": "disabled"}


def test_gravity_compensation_dispatches_over_the_live_path(
    live_bridge: LiveBridge,
) -> None:
    """`set_motors_state("gravity_compensation")` reaches a daemon that supports it.

    Gated on `gravity_compensation`: hardware whose daemon runs the Placo kinematics engine
    (`reachy-mini[placo_kinematics]`). On the default engine the daemon rejects the mode and
    closes the client connection, which would fail every later test in the module.
    """
    requires_caps(live_bridge, "motion", "gravity_compensation")
    bridge, _caps = live_bridge

    async def scenario() -> tuple[str, str]:
        original = await bridge.get_motors_state()
        await bridge.set_motors_state("gravity_compensation")
        mode = await bridge.get_motors_state()
        await bridge.set_motors_state(original)  # restore
        return original, mode

    original, mode = live_bridge.run(scenario())
    print(f"\n[e2e] gravity compensation: original={original!r}, read back={mode!r}")
    assert mode in {"enabled", "disabled", "gravity_compensation"}


def test_gravity_compensation_is_refused_off_placo_and_the_connection_survives(
    live_bridge: LiveBridge,
) -> None:
    """On a robot daemon without Placo, the bridge refuses the mode instead of sending it.

    Sent, the daemon would reject it by closing this client's connection. The guard raises
    `GravityCompensationUnsupportedError` first, so the motor state still reads back
    afterwards over the same connection. Skips where there is nothing to refuse: a daemon
    that supports the mode, or a simulation (which ignores motor modes).
    """
    requires_caps(live_bridge, "motion")
    bridge, caps = live_bridge
    if "gravity_compensation" in caps:
        pytest.skip("the daemon supports gravity compensation; nothing to refuse")
    status = bridge.robot.client.get_status()
    if status.simulation_enabled or status.mockup_sim_enabled:
        pytest.skip("a simulation ignores motor modes; the bridge sends them unchecked")

    async def scenario() -> tuple[str, str]:
        before = await bridge.get_motors_state()
        with pytest.raises(GravityCompensationUnsupportedError):
            await bridge.set_motors_state("gravity_compensation")
        return before, await bridge.get_motors_state()

    before, after = live_bridge.run(scenario())
    assert after == before
