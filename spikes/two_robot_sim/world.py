"""The shared world: upstream's empty scene with two namespaced Reachy Minis facing each other.

Spike code. Composes the model with ``mujoco.MjSpec.attach`` instead of a scene file, so
upstream's robot description is used as shipped (meshes resolved from its own ``meshdir``).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files

import mujoco
import numpy as np

STREAM_SIZE = (1280, 720)


@dataclass(frozen=True)
class RobotPlacement:
    """Where one robot stands: a name prefix and its base frame in the world."""

    prefix: str
    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float]  # w, x, y, z

    def base_pose(self) -> np.ndarray:
        """The base frame as a 4x4 world transform."""
        pose = np.eye(4)
        rot = np.zeros(9)
        mujoco.mju_quat2Mat(rot, np.array(self.quat, dtype=float))
        pose[:3, :3] = rot.reshape(3, 3)
        pose[:3, 3] = self.pos
        return pose


# The robot looks along its own +x. A at the origin looking along world +x, B 0.6 m in
# front of it, turned half a turn about z, looking back along world -x.
FACING = (
    RobotPlacement("a_", (0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
    RobotPlacement("b_", (0.6, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
)


def mjcf_root() -> str:
    import reachy_mini

    return str(files(reachy_mini).joinpath("descriptions/reachy_mini/mjcf"))


def build_world(placements: tuple[RobotPlacement, ...] = FACING) -> mujoco.MjModel:
    """Upstream's ``empty`` scene (skybox, checker floor, light) with one robot per placement."""
    root = mjcf_root()
    world = mujoco.MjSpec()
    world.modelname = "two_robot_scene"
    # What upstream's scenes/empty.xml sets, restated on the parent spec.
    world.visual.headlight.diffuse = [0.6, 0.6, 0.6]
    world.visual.headlight.ambient = [0.3, 0.3, 0.3]
    world.visual.headlight.specular = [0, 0, 0]
    world.visual.rgba.haze = [0.15, 0.25, 0.35, 1]
    world.visual.global_.azimuth = 160
    world.visual.global_.elevation = -20
    world.visual.global_.offwidth = STREAM_SIZE[0]
    world.visual.global_.offheight = STREAM_SIZE[1]
    world.add_texture(
        type=mujoco.mjtTexture.mjTEXTURE_SKYBOX,
        builtin=mujoco.mjtBuiltin.mjBUILTIN_GRADIENT,
        rgb1=[0.3, 0.5, 0.7],
        rgb2=[0, 0, 0],
        width=512,
        height=3072,
    )
    world.add_texture(
        name="groundplane",
        type=mujoco.mjtTexture.mjTEXTURE_2D,
        builtin=mujoco.mjtBuiltin.mjBUILTIN_CHECKER,
        mark=mujoco.mjtMark.mjMARK_EDGE,
        rgb1=[0.2, 0.3, 0.4],
        rgb2=[0.1, 0.2, 0.3],
        markrgb=[0.8, 0.8, 0.8],
        width=300,
        height=300,
    )
    ground = world.add_material(name="groundplane", texuniform=True, texrepeat=[5, 5])
    ground.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = "groundplane"
    # reflectance 0, as the bridge's test scene: a mirrored robot is a second robot to a detector.
    ground.reflectance = 0.0
    world.worldbody.add_light(
        pos=[0.3, 0, 3.5], dir=[0, 0, -1], type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL
    )
    world.worldbody.add_geom(
        name="floor",
        type=mujoco.mjtGeom.mjGEOM_PLANE,
        size=[0, 0, 0.05],
        material="groundplane",
    )
    for placement in placements:
        robot = mujoco.MjSpec.from_file(f"{root}/reachy_mini.xml")
        frame = world.worldbody.add_frame(
            pos=list(placement.pos), quat=list(placement.quat)
        )
        world.attach(robot, prefix=placement.prefix, frame=frame)
    return world.compile()
