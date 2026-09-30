"""E2E tier: the `custom` detection path on a live daemon (specs/vision/user_perception.md
"Custom detectors", specs/vision/camera.md).

It registers the bridge's shipped detector class through the `custom` path — as a
developer registers a wrapper of their own — on the camera feed of the viewer sim, and
checks that the head converges on the test scene's portrait as it does with the detector
named in the config (tests-e2e/test_bridge.py). It tests the registration and the runner,
not the model. Run it with

    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -k custom

It has its own bridge session (`live_bridge_custom_faces`, below): the detector is config-only,
and a second session on the daemon `live_bridge` drives would be a second motion loop
writing the head — so this module runs after test_bridge.py has released its session, and
exactly one bridge drives the head. The fixture lives here rather than in conftest.py because
nothing else uses it.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from reachy_mini_bridge import ReachyMiniConfig
from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.config import FaceDetectionSettings, MotionSettings
from reachy_mini_bridge.testing import _daemon, requires_caps
from reachy_mini_bridge.testing.fixtures import _probe_capabilities
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient
from reachy_mini_bridge.yunet import YuNetDetector

LATERAL_M = 0.15
YAW_TOLERANCE_DEG = 5.0
SETTLE_WINDOW_S = 2.0
CENTRED = 0.05


def _expected_yaw_deg(lateral: float) -> float:
    return math.degrees(math.atan2(lateral, DEFAULT_FACE_POS[0]))


def _yaw_deg(pose: Any) -> float:
    r = np.asarray(pose)
    return math.degrees(math.atan2(r[1, 0], r[0, 0]))


async def _settled_yaw(
    robot: Any, timeout: float = 10.0, min_seconds: float = 2.5
) -> float:
    """Sample the head's yaw until it holds still (within 1° over a second, at least
    `min_seconds` in) or `timeout`; the mean over the last SETTLE_WINDOW_S."""
    samples: list[tuple[float, float]] = []
    started = time.monotonic()
    while True:
        yaw = _yaw_deg(await asyncio.to_thread(robot.get_current_head_pose))
        now = time.monotonic()
        samples.append((now, yaw))
        recent = [y for t, y in samples if t >= now - 1.0]
        still = now - started >= min_seconds and max(recent) - min(recent) < 1.0
        if still or now >= started + timeout:
            break
        await asyncio.sleep(0.05)
    end = samples[-1][0]
    settled = [y for t, y in samples if t >= end - SETTLE_WINDOW_S]
    return sum(settled) / len(settled)


@pytest.fixture(scope="module")
def live_bridge_custom_faces(
    _live_daemon: tuple[str, int],
) -> Iterator[tuple[ReachyMiniBridge, frozenset[str]]]:
    """`live_bridge`'s twin for the `custom` detection path: the same target, daemon and
    capability probe, but a config with `face_detection.detector="custom"` and the shipped
    detector class registered as the custom factory. Its own session, because the
    detector is config-only and two bridge sessions on one daemon would be two motion loops
    writing the head."""
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
    asyncio.run(bridge.start())
    try:
        caps = _probe_capabilities(bridge.robot, (host, port))
        yield bridge, caps
    finally:
        asyncio.run(bridge.stop())


@pytest.fixture
def face_scene(
    live_bridge_custom_faces: tuple[ReachyMiniBridge, frozenset[str]],
    sim_scene: SimSceneClient,
) -> Iterator[SimSceneClient]:
    requires_caps(live_bridge_custom_faces, "camera", "faces")
    sim_scene.clear()  # nobody in view (specs/testing/sim_scene.md "A pool of portraits")
    yield sim_scene
    sim_scene.clear()


def test_custom_detector_converges_on_the_face(
    live_bridge_custom_faces: tuple[ReachyMiniBridge, frozenset[str]],
    face_scene: SimSceneClient,
) -> None:
    """With the shipped detector class registered as the custom detector, `bridge.faces`
    reports from the `custom` path at the feed's rate and the head turns onto the
    portrait ahead and then onto it moved sideways, settling at the yaw its position
    implies with the face at the image centre."""
    bridge, _caps = live_bridge_custom_faces
    robot: Any = bridge.robot
    x, _y, z = DEFAULT_FACE_POS

    async def scenario() -> tuple[float, float, Any, float, str | None]:
        await bridge.set_motors_state("enabled")
        await bridge.stop_head_tracking()
        await bridge.start_head_tracking()
        face = face_scene.spawn(DEFAULT_FACE_POS)
        ahead = await _settled_yaw(robot)
        face_scene.place(face, (x, LATERAL_M, z), duration=1.0)
        aside = await _settled_yaw(robot)
        report = bridge.faces.value
        seen = {report.ts}
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            seen.add(bridge.faces.value.ts)
            await asyncio.sleep(0.01)
        return ahead, aside, report, (len(seen) - 1) / 2.0, bridge.attention

    ahead, aside, report, rate, attention = asyncio.run(scenario())
    print(
        f"\n[e2e] custom source: yaw ahead {ahead:+.1f}, aside {aside:+.1f} deg "
        f"(expected {_expected_yaw_deg(LATERAL_M):+.1f}), {rate:.1f} reports/s, "
        f"face {report.faces[0] if report.faces else None}"
    )
    assert report.source == "custom" and report.active
    assert report.faces, "the custom detector reports no face"
    assert abs(report.faces[0].x) < CENTRED and abs(report.faces[0].y) < CENTRED
    assert report.faces[0].size is not None and report.faces[0].size > 0.05
    assert ahead == pytest.approx(0.0, abs=YAW_TOLERANCE_DEG)
    assert aside == pytest.approx(_expected_yaw_deg(LATERAL_M), abs=YAW_TOLERANCE_DEG)
    assert rate >= 5.0, f"the custom source reported {rate:.1f} times a second"
    assert attention == "engaged"
