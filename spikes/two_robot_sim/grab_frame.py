"""One upstream ``ReachyMini`` client on one port: save what its robot's camera sees.

    uv run python -m spikes.two_robot_sim.grab_frame --port 8011 --out spikes/two_robot_sim/frame_a.png

``--yaw DEG`` turns the head first, so the other robot shifts in this robot's frame;
``--hold S`` keeps that pose for S seconds after the frame is saved, so the other robot's
client can photograph it. The printed ``other robot at x=`` is the centroid column of the
white pixels — the other robot's shell — in the 1280 px wide frame.
"""

from __future__ import annotations

import argparse
import time

import numpy as np
from PIL import Image
from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

from spikes.two_robot_sim.client import bind_camera_to


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--yaw", type=float, default=0.0)
    parser.add_argument("--hold", type=float, default=0.0)
    ns = parser.parse_args()
    report = bind_camera_to(ns.port)
    robot = ReachyMini(host="127.0.0.1", port=ns.port, connection_mode="localhost_only")
    try:
        robot.goto_target(head=create_head_pose(yaw=ns.yaw, degrees=True), duration=1.0)
        time.sleep(1.5)
        frame = None
        deadline = time.monotonic() + 10.0
        while frame is None and time.monotonic() < deadline:
            frame = robot.media.get_frame()
            time.sleep(0.05)
        if frame is None:
            raise SystemExit(
                f"no frame from robot {report['prefix']} on port {ns.port}"
            )
        Image.fromarray(frame[:, :, ::-1]).save(ns.out)  # upstream hands BGR
        white = np.argwhere(frame.min(axis=2) > 200)
        column = float(white[:, 1].mean()) if len(white) else float("nan")
        print(
            f"robot {report['prefix']} (port {ns.port}, socket {report['camera_socket']}), "
            f"head yaw {ns.yaw:+.0f} deg: frame {frame.shape}, {len(white)} white pixels, "
            f"other robot at x={column:.0f} -> {ns.out}",
            flush=True,
        )
        time.sleep(ns.hold)
    finally:
        robot.goto_target(head=create_head_pose(), duration=1.0)
        robot.media.close()
        robot.client.disconnect()


if __name__ == "__main__":
    main()
