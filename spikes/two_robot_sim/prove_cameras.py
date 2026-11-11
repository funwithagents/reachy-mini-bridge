"""Each client's ``get_frame()`` is its own robot's camera, and the other robot is in it.

    uv run python -m spikes.two_robot_sim.prove_cameras --out-dir frames/

Two upstream ``ReachyMini`` clients, one per port. The scene is symmetric, so with both
heads neutral the two frames look alike; the proof turns robot b's head 25 degrees and
checks, on the frames each client reads:

* neutral: both frames show a white robot, centred (the other robot, 0.52 m ahead);
* b turned: in b's frame the other robot moved sideways by more than 150 px; in a's frame
  it stays centred, and a's frame changed where b's head is (a sees b turn);
* the two frames then differ from each other.

Saves the four frames as PNG. Exits non-zero when a check fails.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
from PIL import Image
from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

from spikes.two_robot_sim.client import bind_camera_to

CENTRE_X = 640
MIN_WHITE = 4000  # the other robot's shell is 16 000 to 22 000 white pixels at 1280x720


def fresh_frame(robot: ReachyMini, timeout: float = 15.0) -> np.ndarray:
    """A frame captured after the call: the reader's queue is drained first."""
    deadline = time.monotonic() + timeout
    time.sleep(0.5)
    frame = None
    for _ in range(10):  # drop what was queued before
        robot.media.get_frame()
    while frame is None and time.monotonic() < deadline:
        frame = robot.media.get_frame()
        time.sleep(0.05)
    if frame is None:
        raise SystemExit("no camera frame")
    return np.asarray(frame)


def white(frame: np.ndarray) -> tuple[int, float]:
    pixels = np.argwhere(frame.min(axis=2) > 200)
    return len(pixels), float(pixels[:, 1].mean()) if len(pixels) else float("nan")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", type=int, nargs=2, default=[8011, 8012])
    parser.add_argument("--out-dir", required=True)
    ns = parser.parse_args()
    out = Path(ns.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    robots: dict[str, ReachyMini] = {}
    for name, port in zip("ab", ns.ports, strict=True):
        bind_camera_to(port)
        robots[name] = ReachyMini(
            host="127.0.0.1", port=port, connection_mode="localhost_only"
        )
    checks: list[tuple[str, bool]] = []
    frames: dict[str, np.ndarray] = {}
    neutral = create_head_pose()
    for robot in robots.values():
        robot.goto_target(head=neutral, antennas=[0.0, 0.0], duration=1.0)
    time.sleep(2.0)
    for name, robot in robots.items():
        frames[f"{name}_neutral"] = fresh_frame(robot)
    robots["b"].goto_target(head=create_head_pose(yaw=25, degrees=True), duration=1.0)
    time.sleep(2.5)
    for name, robot in robots.items():
        frames[f"{name}_b_turned"] = fresh_frame(robot)
    robots["b"].goto_target(head=neutral, duration=1.0)
    stats = {}
    for key, frame in frames.items():
        Image.fromarray(frame[:, :, ::-1]).save(out / f"ci_frame_{key}.png")
        stats[key] = white(frame)
        print(
            f"frame {key}: {frame.shape}, {stats[key][0]} white pixels, centroid x={stats[key][1]:.0f}"
        )
    for key in ("a_neutral", "b_neutral", "a_b_turned"):
        count, x = stats[key]
        checks.append(
            (
                f"{key}: the other robot is in view and centred",
                count > MIN_WHITE and abs(x - CENTRE_X) < 40,
            )
        )
    count, x = stats["b_b_turned"]
    checks.append(
        (
            "b turned: the other robot moved sideways in b's frame",
            count > MIN_WHITE and abs(x - CENTRE_X) > 150,
        )
    )
    seen = np.abs(
        frames["a_b_turned"].astype(int) - frames["a_neutral"].astype(int)
    ).mean()
    checks.append(
        (
            f"a's frame changed when b turned its head (mean abs diff {seen:.2f})",
            seen > 0.2,
        )
    )
    apart = np.abs(
        frames["a_b_turned"].astype(int) - frames["b_b_turned"].astype(int)
    ).mean()
    checks.append(
        (f"the two robots' frames differ (mean abs diff {apart:.2f})", apart > 3.0)
    )
    for label, ok in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
    raise SystemExit(0 if all(ok for _, ok in checks) else 1)


if __name__ == "__main__":
    main()
