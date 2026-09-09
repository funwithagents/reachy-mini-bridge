"""E2E tier: the client drives a live daemon and reads real motor state.

Driven by the `live_robot` fixture (conftest.py), which resolves the target
(`REACHY_MINI_E2E_TARGET`, default `sim`), brings up / borrows a daemon, and probes
capabilities. `requires_caps("motion")` gates the test on the motion capability, so it
runs on sim (headless or viewer) and on a real robot alike, and skips cleanly where
the target isn't reachable. On the default `sim` target it drives the real MuJoCo
physics simulator, not the kinematic mock (the in-process `FakeReachyMini` covers that
level in the fast tier).

Run explicitly: ``uv run pytest tests-e2e/test_robot.py``.
"""

from __future__ import annotations

from typing import Any

from support import requires_caps

from reachy_mini_bridge.robot import AnyReachyMini


def test_robot_connects_and_reads_motor_state(
    live_robot: tuple[AnyReachyMini, frozenset[str]],
) -> None:
    """Read the live daemon's motor state via the client over the network."""
    requires_caps(live_robot, "motion")
    robot, _caps = live_robot
    status = robot.client.get_status()
    backend = status.backend_status
    assert backend is not None
    mode = backend.motor_control_mode
    assert getattr(mode, "value", mode) in {
        "enabled",
        "disabled",
        "gravity_compensation",
    }


def test_camera_delivers_a_frame(
    live_robot: tuple[AnyReachyMini, frozenset[str]],
) -> None:
    """A live camera frame comes back as a BGR image.

    Gated on `camera`, which the fixture probes true only where a GL context is
    available — the headfull sim viewer (`REACHY_MINI_E2E_SIM_VIEWER=1`) or a real
    robot. So this **skips** on the headless sim / CI and runs where the camera exists,
    proving the harness both probes and delivers frames (not just that the flag is set).
    """
    requires_caps(live_robot, "camera")
    robot, _caps = live_robot
    media: Any = robot.media  # real MediaManager; typed loosely (fake lacks get_frame)
    frame = media.get_frame()
    assert frame is not None, "camera probed but get_frame() returned None"
    assert frame.ndim == 3 and frame.shape[2] == 3, (
        f"expected HxWx3 BGR, got {frame.shape}"
    )
    assert frame.size > 0
