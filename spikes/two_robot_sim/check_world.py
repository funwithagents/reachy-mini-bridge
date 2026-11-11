"""Standalone check of the composed model: names, stability, facing, offscreen render."""

import sys
import time

import mujoco
import numpy as np

from spikes.two_robot_sim.world import FACING, build_world

m = build_world()
d = mujoco.MjData(m)
print(
    "nu",
    m.nu,
    "nq",
    m.nq,
    "neq",
    m.neq,
    "ncam",
    m.ncam,
    "nlight",
    m.nlight,
    "nmesh",
    m.nmesh,
)
print("actuators", [m.actuator(i).name for i in range(m.nu)])
print("cameras", [m.camera(i).name for i in range(m.ncam)])
print("named geoms", [m.geom(i).name for i in range(m.ngeom) if m.geom(i).name])
print("colliding geoms", int((m.geom_contype != 0).sum()))
m.opt.timestep = 0.002
t0 = time.time()
for _ in range(2500):
    mujoco.mj_step(m, d)
print(f"5 s of sim in {time.time() - t0:.2f} s; max |qvel| {np.abs(d.qvel).max():.4f}")
for p in FACING:
    cam = m.camera(f"{p.prefix}eye_camera").id
    head = m.site(f"{p.prefix}head").id
    # MuJoCo cameras look along their -z.
    print(
        p.prefix,
        "cam pos",
        d.cam_xpos[cam].round(3),
        "looks along",
        (-d.cam_xmat[cam].reshape(3, 3)[:, 2]).round(3),
        "head site",
        d.site_xpos[head].round(3),
    )
if "--render" in sys.argv:
    from PIL import Image

    r = mujoco.Renderer(m, height=720, width=1280)
    for p in FACING:
        r.update_scene(d, f"{p.prefix}eye_camera")
        im = r.render()
        Image.fromarray(im).save(f"spikes/two_robot_sim/offscreen_{p.prefix}eye.png")
        print(
            p.prefix,
            "frame",
            im.shape,
            "mean",
            im.mean().round(1),
            "std",
            im.std().round(1),
        )
