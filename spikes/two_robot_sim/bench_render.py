"""What one eye-camera frame costs to draw, and what would make it cheaper.

    uv run python -m spikes.two_robot_sim.bench_render [--out-dir DIR]

Standalone (no daemon, no stream): the two-robot world awake, robot a's eye camera, the
render trimmed as the host trims it (no shadows or reflections, the camera's own robot
left out; on a software rasterizer no multisampling, skybox, haze or fog). Variants:

* ``whole``: the other robot drawn whole, 1280x720 — what the host streams;
* ``seen``: only the other robot's geoms the camera can ever see. Found by segmentation
  renders while the other robot's head sweeps its range; everything inside the shell
  (motors, boards, most of the Stewart platform) never reaches a pixel;
* each at 640x360 as well, to tell triangle cost from fill cost.

With ``--out-dir`` the ``whole`` and ``seen`` frames are saved, to compare by eye.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import mujoco
import numpy as np

from spikes.two_robot_sim.world import build_world

CAMERA = "a_eye_camera"
SWEEP = [(0, 0)] + [(yaw, pitch) for yaw in (-0.6, 0.6) for pitch in (-0.3, 0.3)]  # rad


def tree_geoms(model: mujoco.MjModel, camera: str) -> list[int]:
    root = int(model.body_rootid[model.cam_bodyid[model.camera(camera).id]])
    return [
        g
        for g in range(model.ngeom)
        if int(model.body_rootid[model.geom_bodyid[g]]) == root
    ]


def triangles(model: mujoco.MjModel, geoms: list[int]) -> int:
    return int(
        sum(
            model.mesh_facenum[model.geom_dataid[g]]
            for g in geoms
            if model.geom_dataid[g] >= 0
        )
    )


def renderer(model: mujoco.MjModel, width: int, height: int) -> mujoco.Renderer:
    r = mujoco.Renderer(model, height=height, width=width)
    off = [mujoco.mjtRndFlag.mjRND_SHADOW, mujoco.mjtRndFlag.mjRND_REFLECTION]
    if os.environ.get("MUJOCO_GL") in ("egl", "osmesa"):
        off += [
            mujoco.mjtRndFlag.mjRND_SKYBOX,
            mujoco.mjtRndFlag.mjRND_HAZE,
            mujoco.mjtRndFlag.mjRND_FOG,
        ]
    for flag in off:
        r.scene.flags[flag] = 0
    return r


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir")
    ns = parser.parse_args()
    model = build_world()
    if os.environ.get("MUJOCO_GL") in ("egl", "osmesa"):
        model.vis.quality.offsamples = 0
    data = mujoco.MjData(model)
    model.opt.timestep = 0.002
    for _ in range(1500):  # ctrl 0: both robots awake, heads up
        mujoco.mj_step(model, data)
    own = tree_geoms(model, CAMERA)
    other = [g for g in tree_geoms(model, "b_eye_camera") if model.geom_group[g] == 2]
    for g in own:  # the camera's own robot is never drawn (the host: a geom group)
        model.geom_rgba[g, 3] = 0.0

    # Which of the other robot's geoms ever reach a pixel, over a sweep of its head.
    seg = renderer(model, 1280, 720)
    seg.enable_segmentation_rendering()
    seen: set[int] = set()
    head = [model.actuator(f"b_{name}").id for name in ("yaw_body",)]
    stewart = [model.actuator(f"b_stewart_{i}").id for i in range(1, 7)]
    for yaw, tilt in SWEEP:
        data.ctrl[head] = yaw
        data.ctrl[stewart] = tilt * np.array([1, -1, 1, -1, 1, -1])  # a crude head tilt
        for _ in range(500):
            mujoco.mj_step(model, data)
        seg.update_scene(data, CAMERA)
        ids = seg.render()
        geom_pixels = ids[:, :, 0][ids[:, :, 1] == int(mujoco.mjtObj.mjOBJ_GEOM)]
        seen |= set(np.unique(geom_pixels).tolist())
    seg.close()
    data.ctrl[:] = 0
    for _ in range(500):
        mujoco.mj_step(model, data)
    seen_other = [g for g in other if g in seen]
    hidden = [g for g in other if g not in seen]
    print(
        f"the other robot: {len(other)} visual geoms, {triangles(model, other)} triangles; "
        f"seen from the eye camera over the sweep: {len(seen_other)} geoms, "
        f"{triangles(model, seen_other)} triangles"
    )
    saved = model.geom_rgba[hidden, 3].copy()
    frames = {}
    for label, hide in (("whole", False), ("seen", True)):
        model.geom_rgba[hidden, 3] = 0.0 if hide else saved
        for width, height in ((1280, 720), (640, 360)):
            r = renderer(model, width, height)
            for _ in range(2):
                r.update_scene(data, CAMERA)
                r.render()
            t0 = time.perf_counter()
            for _ in range(10):
                r.update_scene(data, CAMERA)
                frame = r.render()
            ms = (time.perf_counter() - t0) / 10 * 1e3
            print(
                f"  {label:5s} {width}x{height}: {ms:6.1f} ms per frame ({1e3 / ms:.1f} fps for one camera alone)"
            )
            if width == 1280:
                frames[label] = frame.copy()
            r.close()
    diff = np.abs(frames["whole"].astype(int) - frames["seen"].astype(int))
    print(
        f"  whole against seen at 1280x720: {int((diff.max(axis=2) > 8).sum())} pixels differ "
        f"of {1280 * 720}"
    )
    if ns.out_dir:
        from PIL import Image

        out = Path(ns.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for label, frame in frames.items():
            Image.fromarray(frame).save(out / f"bench_{label}.png")


if __name__ == "__main__":
    main()
