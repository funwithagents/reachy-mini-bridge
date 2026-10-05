"""E2E tier: the `custom` detection path on a live daemon (specs/vision/user_perception.md
"Custom detectors", specs/vision/camera.md).

It registers the bridge's shipped detector class through the `custom` path — as a
developer registers a wrapper of their own — on the camera feed of the viewer sim, and
checks that the head converges on the test scene's portrait as it does with the detector
named in the config (tests-e2e/test_head_tracking.py), through the same convergence kit
(reachy_mini_bridge.testing.gaze). It tests the registration and the runner, not the
model. Run it with

    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -k custom

It has its own bridge session (`live_bridge_custom_faces`, below): the detector is
config-only, and a second session on a daemon another module's `live_bridge` drives would
be a second motion loop writing the head — pytest finalises a module's fixtures when it
leaves the module, so this session opens on the run's daemon once the previous module has
released its own, and exactly one bridge drives the head. The fixture lives here rather
than in conftest.py because nothing else uses it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest

from reachy_mini_bridge import ReachyMiniConfig
from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.config import FaceDetectionSettings, MotionSettings
from reachy_mini_bridge.testing import BridgeLoop, LiveBridge, _daemon, requires_caps
from reachy_mini_bridge.testing.fixtures import probed_capabilities
from reachy_mini_bridge.testing.gaze import (
    LATERAL_M,
    Track,
    arm_tracking,
    assert_tracked,
    face_at,
    track_onto,
)
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient
from reachy_mini_bridge.yunet import YuNetDetector


@pytest.fixture(scope="module")
def live_bridge_custom_faces(
    _live_daemon: tuple[str, int],
) -> Iterator[LiveBridge]:
    """`live_bridge`'s twin for the `custom` detection path: the same target, daemon,
    capability probe and event loop (`BridgeLoop`), but a config with
    `face_detection.detector="custom"` and the shipped detector class registered as the
    custom factory. Its own session, because the detector is config-only and two bridge
    sessions on one daemon would be two motion loops writing the head."""
    host, port = _live_daemon
    bridge = ReachyMiniBridge(
        ReachyMiniConfig(
            backend=_daemon.backend(),
            robot={
                "connection_mode": "network",
                "host": host,
                "port": port,
                "media_backend": "local",
            },
            face_detection=FaceDetectionSettings(
                detector="custom", enabled=True, face_detector=YuNetDetector
            ),
            motion=MotionSettings(tracking=True),
        )
    )
    with BridgeLoop() as loop:
        loop.run(bridge.start())
        try:
            caps = probed_capabilities(bridge.robot, (host, port))
            yield LiveBridge(bridge, caps, loop)
        finally:
            loop.run(bridge.stop())


@pytest.fixture
def custom_face_scene(
    live_bridge_custom_faces: LiveBridge,
    sim_scene: SimSceneClient,
) -> Iterator[SimSceneClient]:
    """conftest's `face_scene`, gated on this module's own session."""
    requires_caps(live_bridge_custom_faces, "camera", "faces")
    sim_scene.clear()  # nobody in view (specs/testing/sim_scene.md "A pool of portraits")
    yield sim_scene
    sim_scene.clear()


def test_custom_detector_converges_on_the_face(
    live_bridge_custom_faces: LiveBridge,
    custom_face_scene: SimSceneClient,
) -> None:
    """With the shipped detector class registered as the custom detector, `bridge.faces`
    reports from the `custom` path at the feed's rate and the head converges on the
    portrait ahead and then on it moved sideways — the same convergence the config-named
    detector gives (tests-e2e/test_head_tracking.py), asserted with the same kit."""
    bridge, _caps = live_bridge_custom_faces

    async def scenario() -> tuple[Track, Track, Any, float, str | None]:
        await bridge.set_motors_state("enabled")
        await arm_tracking(bridge)
        face = custom_face_scene.spawn(DEFAULT_FACE_POS)
        ahead = await track_onto(bridge, "custom: face ahead", 0.0)
        custom_face_scene.place(face, face_at(LATERAL_M), duration=1.0)
        aside = await track_onto(
            bridge, "custom: face moved aside", LATERAL_M, min_seconds=5.0
        )
        report = bridge.faces.value
        seen = {report.ts}
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            seen.add(bridge.faces.value.ts)
            await asyncio.sleep(0.01)
        return ahead, aside, report, (len(seen) - 1) / 2.0, bridge.attention

    ahead, aside, report, rate, attention = live_bridge_custom_faces.run(scenario())
    print(
        f"\n[e2e] custom source: {rate:.1f} reports/s, "
        f"face {report.faces[0] if report.faces else None}"
    )
    assert report.source == "custom" and report.active
    assert report.faces, "the custom detector reports no face"
    assert report.faces[0].size is not None and report.faces[0].size > 0.05
    assert_tracked(ahead)
    assert_tracked(aside, pitch_ahead=ahead.pitch)
    assert rate >= 5.0, f"the custom source reported {rate:.1f} times a second"
    assert attention == "engaged"
