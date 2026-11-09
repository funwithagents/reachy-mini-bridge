"""E2E tier — a prop of the tier's own kind in the rendered scene (specs/testing/sim_scene.md
"A prop is an image on a plane", specs/testing/testing_support.md "Props and capabilities a
consumer declares").

The tier's conftest declares `card_1`, a striped 3:2 PNG, through
`pytest_reachy_mini_scene_props`; every sim the harness spawns carries it beside the pool of
portraits, and `props:card` is probed true. Spawned in front of the head, the card changes
the rendered frame where the plane sits — its two stripe colours are there; despawned, it leaves the frame as the empty scene
had it — what a consumer's hand or object does, with no detector.

Gated on `camera` + `props:card`: the viewer sim, or a Linux headless sim (CI); a macOS
headless sim has no camera, a borrowed daemon started without the tier's props no card.
"""

from __future__ import annotations

import time

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge.camera import CameraFrame
from reachy_mini_bridge.testing import LiveBridge
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient

pytestmark = pytest.mark.requires("camera", "props:card")

# A frame drawn after a scene change: the render runs at 25 Hz and the stream adds a few
# frames of latency, so half a second is well past it.
_SETTLE_S = 0.5


def _frame_after(live_bridge: LiveBridge, t: float) -> CameraFrame:
    bridge = live_bridge.bridge
    return live_bridge.run(bridge.camera.wait_for(lambda f: f.ts > t), timeout=5.0)


def _centre(image: npt.NDArray[np.uint8]) -> npt.NDArray[np.float64]:
    """The middle third of the frame, where the card sits ahead of the head."""
    h, w = image.shape[:2]
    return image[h // 3 : 2 * h // 3, w // 3 : 2 * w // 3].astype(np.float64)


# The card's two stripes as the eye camera sees them (BGR; the prop's material is
# emissive, so its pixels reach the camera unshaded): magenta and green.
_STRIPES = {"magenta": (255, 0, 255), "green": (0, 192, 0)}


def _share_of(image: npt.NDArray[np.float64], bgr: tuple[int, int, int]) -> float:
    """The share of ``image``'s pixels within 60 of ``bgr`` on every channel."""
    close = np.all(np.abs(image - np.array(bgr, dtype=np.float64)) < 60, axis=-1)
    return float(close.mean())


def test_a_declared_prop_shows_in_the_frame_and_leaves_it(
    live_bridge: LiveBridge, sim_scene: SimSceneClient
) -> None:
    bridge = live_bridge.bridge
    live_bridge.run(bridge.set_idle("hold"))  # a still head: frames differ by the scene
    sim_scene.clear()
    try:
        time.sleep(1.0)  # the hold settled
        empty = _centre(_frame_after(live_bridge, time.perf_counter()).image)
        name = sim_scene.spawn(DEFAULT_FACE_POS, kind="card")
        state = sim_scene.bodies()[name]
        assert (name, state.kind, state.image) == ("card_1", "card", "card")
        time.sleep(_SETTLE_S)
        shown = _centre(_frame_after(live_bridge, time.perf_counter()).image)
        sim_scene.despawn("card_1")
        time.sleep(_SETTLE_S)
        gone = _centre(_frame_after(live_bridge, time.perf_counter()).image)
    finally:
        sim_scene.clear()
        live_bridge.run(bridge.set_idle(bridge.config.motion.idle))
    shown_change = float(np.abs(shown - empty).mean())
    gone_change = float(np.abs(gone - empty).mean())
    assert shown_change > 40.0, (
        f"the card barely changed the frame ({shown_change:.1f})"
    )
    # It is the card's image that shows, not just something: both stripes are there.
    for colour, bgr in _STRIPES.items():
        assert _share_of(shown, bgr) > 0.1, f"no {colour} stripe in the frame"
        assert _share_of(gone, bgr) < 0.01, f"the {colour} stripe stayed"
    assert gone_change < shown_change / 4, (
        f"the frame did not return to the empty scene ({gone_change:.1f} against "
        f"{shown_change:.1f} with the card)"
    )
