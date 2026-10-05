"""E2E tier — the sim displays over a live viewer daemon (specs/daemon/sim_displays.md "The
face markers"): the marker the bridge sends for a portrait lands where the portrait is,
and stays put while an emotion swings the head.

Gated on `face_markers` (the viewer sim the harness spawns, with the display on), and on
`camera` + `faces` through the `face_scene` fixture.

Run explicitly (the viewer sim: the camera needs its GL context; headless the whole
module skips on `camera`):
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_sim_displays.py -rs

Every test awaits the bridge through `live_bridge.run(...)`, the harness's one event
loop (specs/testing/testing_support.md "Public surface"); the daemon is the run's, the
bridge session this module's (specs/testing/testing.md "Daemon lifecycle")."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import numpy as np
import numpy.typing as npt

from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.face_detection import FACE_ABSENT_S
from reachy_mini_bridge.sim_displays import fetch_face_markers
from reachy_mini_bridge.testing import LiveBridge, requires_caps
from reachy_mini_bridge.testing.gaze import (
    LATERAL_M,
    MOVE_THRESHOLD_DEG,
    angle_from_neutral_deg,
    arm_tracking,
    track_onto,
    wait_for,
)
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient

# --- the face markers of the viewer (specs/daemon/sim_displays.md "The face markers") ---------
#
# On the viewer sim the harness turns `sim_displays.face_markers` on: the bridge sends
# where it places each face to the daemon, which draws it in the viewer window and
# returns it (`GET /api/sim/displays/face_markers`, MuJoCo world coordinates). The
# portrait's true pose is known, so these check the bridge's geometry itself: the
# direction, the distance estimated from the face's size, and the head pose of the frame.
# Watch the viewer: a green ellipsoid sits on the portrait and stays there while the
# head moves.


# Sideways, the marker is on the portrait; in height it is on the face's nose, a few
# centimetres under the portrait's centre; in distance it is as good as the assumed
# height of the detector's box (calibrated on this portrait: FACE_BOX_HEIGHT_M).
MARKER_LATERAL_M = 0.03
MARKER_BELOW_CENTRE_M = (0.0, 0.09)
MARKER_DISTANCE_RATIO = 0.15
# How far the marker of a still portrait may wander while an emotion swings the head.
MARKER_STILL_M = 0.05


FACE_Z = DEFAULT_FACE_POS[2]


def _marker_positions(bridge: ReachyMiniBridge) -> list[tuple[float, float, float]]:
    """The fresh markers the daemon holds, in MuJoCo world coordinates."""
    robot = bridge.config.robot
    state = fetch_face_markers(str(robot["host"]), int(robot["port"]))
    if state is None or state["age_s"] is None or state["age_s"] > 0.5:
        return []
    return [tuple(marker["world_pos"]) for marker in state["markers"]]


async def _mean_marker(
    bridge: ReachyMiniBridge, seconds: float = 1.5
) -> npt.NDArray[np.float64]:
    """The one marker's world position, averaged over `seconds`."""
    samples: list[tuple[float, float, float]] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        positions = await asyncio.to_thread(_marker_positions, bridge)
        if len(positions) == 1:
            samples.append(positions[0])
        await asyncio.sleep(0.1)
    assert len(samples) >= 5, f"only {len(samples)} marker samples in {seconds} s"
    return np.mean(np.array(samples), axis=0)


def test_a_face_marker_lands_on_the_portrait(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    """The marker the bridge sends for a portrait is where the portrait is: at its side
    of the robot, on its face, at its distance — near, at the default distance and
    far, ahead and to either side."""
    requires_caps(live_bridge, "face_markers")
    bridge, _caps = live_bridge
    places = [
        (0.35, 0.0, FACE_Z),
        (0.45, LATERAL_M, FACE_Z),
        (0.45, -LATERAL_M, FACE_Z),
        (0.60, 0.0, FACE_Z),
    ]

    async def scenario() -> list[npt.NDArray[np.float64]]:
        await bridge.set_motors_state("enabled")
        await bridge.stop_head_tracking()  # the head stays out of it
        await bridge.set_face_detection(True)
        await bridge.set_idle("hold")
        found = []
        try:
            for place in places:
                face_scene.clear()
                await asyncio.sleep(FACE_ABSENT_S + 0.3)
                face_scene.spawn(place)
                assert await wait_for(
                    lambda: len(_marker_positions(bridge)) == 1, 5.0
                ), f"no marker for the portrait at {place}"
                found.append(await _mean_marker(bridge))
        finally:
            await bridge.set_idle("breathing")
            await bridge.start_head_tracking()  # the module's default state
        return found

    found = live_bridge.run(scenario())
    for place, marker in zip(places, found, strict=True):
        print(
            f"\n[e2e] portrait at {place}: marker at "
            f"({marker[0]:.3f}, {marker[1]:.3f}, {marker[2]:.3f})"
        )
    for place, marker in zip(places, found, strict=True):
        assert abs(marker[1] - place[1]) <= MARKER_LATERAL_M, (place, marker)
        low, high = MARKER_BELOW_CENTRE_M
        assert low <= place[2] - marker[2] <= high, (place, marker)
        assert abs(marker[0] - place[0]) <= MARKER_DISTANCE_RATIO * place[0], (
            place,
            marker,
        )


def test_a_face_marker_stays_put_while_the_head_moves(
    live_bridge: LiveBridge,
    face_scene: SimSceneClient,
    emotions_library: None,
) -> None:
    """A marker is placed with the head pose its frame was taken from, so a portrait
    that stands still keeps its marker where it is while an emotion swings the head
    under tracking — it does not ride along with the head."""
    requires_caps(live_bridge, "motion", "face_markers")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def scenario() -> tuple[npt.NDArray[np.float64], float]:
        await bridge.set_motors_state("enabled")
        await arm_tracking(bridge)
        face_scene.spawn(DEFAULT_FACE_POS)
        assert await wait_for(lambda: bridge.attention == "engaged", 6.0)
        await track_onto(bridge, "before the emotion", 0.0)
        emotion = (await bridge.list_emotions())[0]
        samples: list[tuple[float, float, float]] = []
        angles: list[float] = []

        async def sample() -> None:
            while True:
                positions = await asyncio.to_thread(_marker_positions, bridge)
                if len(positions) == 1:
                    samples.append(positions[0])
                pose = await asyncio.to_thread(robot.get_current_head_pose)
                angles.append(angle_from_neutral_deg(pose))
                await asyncio.sleep(0.05)

        sampler = asyncio.create_task(sample())
        try:
            await bridge.play_emotion(emotion)
        finally:
            sampler.cancel()
        return np.array(samples), max(angles) - min(angles)

    samples, excursion = live_bridge.run(scenario())
    assert len(samples) >= 10, f"only {len(samples)} marker samples during the emotion"
    spread = np.linalg.norm(samples - samples.mean(axis=0), axis=1)
    print(
        f"\n[e2e] head moved {excursion:.1f} deg; marker over {len(samples)} samples: "
        f"mean {samples.mean(axis=0).round(3)}, spread max {spread.max():.3f} m, "
        f"p90 {np.percentile(spread, 90):.3f} m"
    )
    assert excursion >= MOVE_THRESHOLD_DEG, "the emotion barely moved the head"
    assert spread.max() <= MARKER_STILL_M
