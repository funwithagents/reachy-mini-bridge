"""The host's rates over a window: physics Hz, camera fps per robot, draw time per frame.

uv run python -m spikes.two_robot_sim.report_rates [--seconds 10] [--label idle]
"""

from __future__ import annotations

import argparse
import time

from spikes.two_robot_sim.client import robot_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", type=int, nargs=2, default=[8011, 8012])
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--label", default="")
    ns = parser.parse_args()
    t0 = time.monotonic()
    before = [robot_report(port) for port in ns.ports]
    time.sleep(ns.seconds)
    after = [robot_report(port) for port in ns.ports]
    elapsed = time.monotonic() - t0
    physics = (after[0]["physics_steps"] - before[0]["physics_steps"]) / elapsed
    fps = [
        (a["frames_sent"] - b["frames_sent"]) / elapsed
        for a, b in zip(after, before, strict=True)
    ]
    print(
        f"rates [{ns.label}] over {elapsed:.1f} s: physics {physics:.0f} Hz, camera fps "
        f"a {fps[0]:.1f} / b {fps[1]:.1f}, per camera frame ms {after[0]['render_ms']}, "
        f"GL backend {after[0]['render_backend']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
