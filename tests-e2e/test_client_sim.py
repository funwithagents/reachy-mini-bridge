"""E2E tier: the client drives a full **MuJoCo sim** daemon (specs/client.md).

The `--sim` backend runs the real MuJoCo physics simulator; the `sim_daemon` fixture
launches it `--headless` (physics, no interactive viewer), so this tier needs no
display and runs anywhere — including CI. It mirrors the mockup-sim tier's client
assertions against the real simulator rather than the kinematic mock.

Run explicitly: ``uv run pytest tests-e2e/test_client_sim.py``.
"""

from __future__ import annotations

from reachy_mini_bridge.client import build_robot


def test_client_connects_and_reads_motor_state(sim_daemon: tuple[str, int]) -> None:
    """Connect over the network and read the simulator's motor state via the client."""
    host, port = sim_daemon
    with build_robot("real", connection_mode="network", host=host, port=port) as robot:
        status = robot.client.get_status()
        backend = status.backend_status
        assert backend is not None
        mode = backend.motor_control_mode
        assert getattr(mode, "value", mode) in {
            "enabled",
            "disabled",
            "gravity_compensation",
        }
