"""Two upstream ``ReachyMini`` clients, one per port: each moves only its own robot.

    uv run python -m spikes.two_robot_sim.prove_isolation [--ports 8011 8012]

Needs the host running (``python -m spikes.two_robot_sim.host``). Exits non-zero on a
failed assertion.
"""

from __future__ import annotations

import argparse
import time

import numpy as np
from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

from spikes.two_robot_sim.client import bind_camera_to

MOVED_RAD = 0.1  # 20 degrees of head yaw moves the head joints 0.12 rad (measured)
STILL_RAD = 0.002  # the other robot's joints stay inside this


def joints(robot: ReachyMini) -> np.ndarray:
    head, antennas = robot.get_current_joint_positions()
    return np.array(list(head) + list(antennas))


def settle(
    robots: dict[str, ReachyMini], timeout: float = 10.0
) -> dict[str, np.ndarray]:
    """The joints of every robot once none has moved for half a second (the simulated
    servos creep onto their targets for a second or two after a goto ends)."""
    deadline = time.monotonic() + timeout
    last = {name: joints(robot) for name, robot in robots.items()}
    while True:
        time.sleep(0.5)
        now = {name: joints(robot) for name, robot in robots.items()}
        if (
            all(np.abs(now[n] - last[n]).max() < 2e-4 for n in now)
            or time.monotonic() > deadline
        ):
            return now
        last = now


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", type=int, nargs=2, default=[8011, 8012])
    parser.add_argument(
        "--no-media", action="store_true", help="for a host run with --no-media"
    )
    ns = parser.parse_args()
    robots: dict[str, ReachyMini] = {}
    for name, port in zip("ab", ns.ports, strict=True):
        if ns.no_media:
            # Upstream: a no_media client tells its daemon to release its media, and the
            # camera stream of that robot stops until someone re-acquires it.
            robots[name] = ReachyMini(
                host="127.0.0.1",
                port=port,
                connection_mode="localhost_only",
                media_backend="no_media",
            )
        else:
            # The camera socket is read when the client opens its camera, so two clients
            # in one process work when each is built right after its own bind.
            bind_camera_to(port)
            robots[name] = ReachyMini(
                host="127.0.0.1", port=port, connection_mode="localhost_only"
            )
    neutral = create_head_pose()
    for robot in robots.values():
        robot.goto_target(head=neutral, antennas=[0.0, 0.0], duration=1.0)
    before = settle(robots)
    failures = 0
    for mover, yaw_deg, antennas in (
        ("a", 20.0, [0.6, -0.6]),
        ("b", -20.0, [-0.6, 0.6]),
    ):
        other = "b" if mover == "a" else "a"
        robots[mover].goto_target(
            head=create_head_pose(yaw=yaw_deg, degrees=True),
            antennas=antennas,
            duration=1.0,
        )
        after = settle(robots)
        moved = np.abs(after[mover] - before[mover])
        still = np.abs(after[other] - before[other])
        yaw = np.degrees(np.arctan2(*robots[mover].get_current_head_pose()[[1, 0], 0]))
        ok = (
            moved[:7].max() > MOVED_RAD
            and still.max() < STILL_RAD
            and abs(yaw - yaw_deg) < 2.0
        )
        failures += not ok
        print(
            f"client {mover} commands yaw {yaw_deg:+.0f} deg: robot {mover} head joints moved "
            f"max {moved[:7].max():.3f} rad, antennas {moved[7:].max():.3f} rad (head yaw reads {yaw:+.1f} deg in its own frame), "
            f"robot {other} joints moved max {still.max():.5f} rad -> {'OK' if ok else 'FAIL'}"
        )
        before = after
    for robot in robots.values():
        robot.goto_target(head=neutral, antennas=[0.0, 0.0], duration=1.0)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
