"""One real ``ReachyMiniBridge`` on one robot of the two-robot host (one process per robot).

    uv run python -m spikes.two_robot_sim.prove_bridge --port 8011 --other-port 8012 \\
        --out spikes/two_robot_sim/bridge_frame_a.png [--seconds 8]

The bridge session runs as it would on a stock sim daemon — motion loop (the head
breathes), camera feed, mic feed, audio devices followed from the daemon's report. The
script saves a frame of ``bridge.camera`` and, over ``--seconds``, samples both daemons'
head pose over REST: how much this robot and the other one moved meanwhile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.request

import numpy as np
from PIL import Image

from reachy_mini_bridge import ReachyMiniBridge, ReachyMiniConfig
from spikes.two_robot_sim.client import bind_camera_to


def head_state(port: int) -> np.ndarray:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/state/full", timeout=5
    ) as reply:
        state = json.load(reply)
    pose = state["head_pose"]
    return np.array(
        [pose[k] for k in ("x", "y", "z", "roll", "pitch", "yaw")]
        + list(state["antennas_position"])
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--other-port", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seconds", type=float, default=8.0)
    ns = parser.parse_args()
    report = bind_camera_to(ns.port)
    config = ReachyMiniConfig.from_dict(
        {
            "backend": "sim",
            "robot": {
                "connection_mode": "network",
                "host": "127.0.0.1",
                "port": ns.port,
                "media_backend": "local",
            },
        }
    )
    async with ReachyMiniBridge(config) as bridge:
        frame = await asyncio.wait_for(
            bridge.camera.wait_for(lambda f: True), timeout=15.0
        )
        Image.fromarray(frame.image[:, :, ::-1]).save(ns.out)
        samples: dict[int, list[np.ndarray]] = {ns.port: [], ns.other_port: []}
        end = time.monotonic() + ns.seconds
        while time.monotonic() < end:
            for port, rows in samples.items():
                rows.append(await asyncio.to_thread(head_state, port))
            await asyncio.sleep(0.1)
        travel = {
            port: float(np.ptp(np.array(rows), axis=0).max())
            for port, rows in samples.items()
        }
        mic = bridge.mic.published_count
        print(
            f"bridge on robot {report['prefix']} (port {ns.port}): camera frame "
            f"{frame.image.shape} id {frame.frame_id} -> {ns.out}; mic chunks {mic}; "
            f"daemon report {bridge.daemon}; head travel over {ns.seconds:.0f} s: this robot "
            f"{travel[ns.port]:.4f}, robot on port {ns.other_port} {travel[ns.other_port]:.4f} "
            "(max range of x/y/z in m, roll/pitch/yaw and antennas in rad)",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
