"""E2E tier — the camera feed and the faces report over a live daemon
(specs/vision/camera.md, specs/vision/user_perception.md, specs/testing/sim_scene.md "A pool
of portraits"): a live frame at a live rate, and `bridge.faces` reporting the test scene's
portraits through the real detector — someone appearing and leaving, two side by side.

Gated on `camera` — the viewer sim (`REACHY_MINI_E2E_SIM_VIEWER=1`) or a robot — and, for
the portraits, on `faces` (the test scene, which every harness-spawned sim runs). The faces
tests stop tracking for their duration: the head stays out of it.

Run explicitly (the viewer sim: the camera needs its GL context; headless the whole
module skips on `camera`):
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_perception.py -rs

Every test awaits the bridge through `live_bridge.run(...)`, the harness's one event
loop (specs/testing/testing_support.md "Public surface"); the daemon is the run's, the
bridge session this module's (specs/testing/testing.md "Daemon lifecycle")."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import numpy as np

from reachy_mini_bridge.face_detection import FACE_ABSENT_S, FaceReport
from reachy_mini_bridge.testing import LiveBridge, requires_caps
from reachy_mini_bridge.testing.gaze import (
    LATERAL_M,
    face_at,
    wait_for,
)
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient


def test_camera_frame_delivers_a_frame(
    live_bridge: LiveBridge,
) -> None:
    """The camera feed publishes a live frame: `bridge.camera.latest()` is a `CameraFrame`
    whose image is BGR `HxWx3` uint8 (specs/vision/camera.md).

    Gated on `camera`, which the fixture probes true only where a GL context is
    available — the headfull sim viewer (`REACHY_MINI_E2E_SIM_VIEWER=1`) or a real
    robot. So this **skips** on the headless sim / CI and runs where the camera exists,
    driving the public API rather than reaching into `robot.media`.
    """
    requires_caps(live_bridge, "camera")
    bridge, _caps = live_bridge
    deadline = time.monotonic() + 2.0  # the first frame follows the session's entry
    while bridge.camera.latest() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    frame = bridge.camera.latest()
    assert frame is not None, "camera probed but bridge.camera.latest() stayed None"
    assert frame.image.ndim == 3 and frame.image.shape[2] == 3, (
        f"expected HxWx3 BGR, got {frame.image.shape}"
    )
    assert frame.image.dtype == np.uint8
    assert frame.frame_id >= 1 and frame.ts > 0.0
    before = bridge.camera.published_count
    time.sleep(1.0)
    rate = bridge.camera.published_count - before
    print(f"\n[e2e] camera feed: {rate} frames/s, first frame {frame.image.shape}")
    assert rate >= 5, f"the feed published {rate} frames in a second"
    # No assertion on `frame.head_pose`: the feed attaches a pose only to a frame with a
    # capture time, and today's daemon stamps none (its frames carry pts 0 — an upstream
    # matter, specs/vision/camera.md), so the live frame's pose is None by design.
    print(f"[e2e] camera frame pose stamped: {frame.head_pose is not None}")


def test_detection_runs_on_the_live_camera(live_bridge: LiveBridge) -> None:
    """specs/vision/user_perception.md: the configured detector (`yunet`) runs on the
    live camera's frames — the report is active and advances at the loop's rate, whether
    or not anyone is in view. Gated on `camera` alone, so it runs on a robot too: there
    is no portrait to spawn there, and this is the one live evidence that the model
    loads and runs on the robot's own frames at the configured width and ceiling."""
    requires_caps(live_bridge, "camera")
    bridge, _caps = live_bridge

    async def scenario() -> tuple[float, FaceReport, tuple[int, ...] | None]:
        await bridge.set_face_detection(True)
        assert await wait_for(lambda: bridge.faces.value.active, 3.0), (
            "the detection loop did not report active"
        )
        seen = {bridge.faces.value.ts}
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            seen.add(bridge.faces.value.ts)
            await asyncio.sleep(0.01)
        frame = bridge.camera.latest()
        shape = None if frame is None else frame.image.shape
        return (len(seen) - 1) / 2.0, bridge.faces.value, shape

    rate, report, shape = live_bridge.run(scenario())
    print(
        f"\n[e2e] detection on the live camera: {rate:.1f} reports/s on {shape} frames, "
        f"{len(report.faces)} face(s) in view"
    )
    assert report.source == "yunet" and report.active
    assert rate >= 5.0, f"the detector reported {rate:.1f} times a second"


def test_faces_report_someone_appearing_and_leaving(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    """specs/vision/user_perception.md "The report is an observable": a subscriber of
    `bridge.faces.changes()` is woken with one face when the portrait is shown, and with
    none once it has been hidden past the absence window. Tracking is stopped for the
    test (the head stays out of it); the detection loop runs the configured `yunet`
    detector for the caller's switch alone."""
    bridge, _caps = live_bridge

    async def next_count(changes: AsyncIterator[FaceReport], count: int) -> FaceReport:
        async for report in changes:  # skips e.g. the `active` flip of a loop start
            if report.active and len(report.faces) == count:
                return report
        raise AssertionError("the faces subscription ended")

    async def scenario() -> tuple[FaceReport, FaceReport]:
        await bridge.set_motors_state("enabled")
        await bridge.set_face_detection(True)
        await bridge.stop_head_tracking()
        changes = bridge.faces.changes()
        try:
            appeared = asyncio.ensure_future(next_count(changes, 1))
            await asyncio.sleep(0)  # subscribed before the face shows
            face = face_scene.spawn(DEFAULT_FACE_POS)
            first = await asyncio.wait_for(appeared, 3.0)
            face_scene.despawn(face)
            left = await asyncio.wait_for(next_count(changes, 0), FACE_ABSENT_S + 3.0)
        finally:
            await changes.aclose()  # type: ignore[attr-defined]
            await (
                bridge.start_head_tracking()
            )  # the module's default state for what follows
        return first, left

    appeared, left = live_bridge.run(scenario())
    print(f"\n[e2e] appeared: {appeared}\n[e2e] left: {left}")
    assert appeared.source == "yunet"
    assert appeared.faces[0].size > 0.05  # the shipped detector reports sizes
    assert all(-1.0 <= v <= 1.0 for v in (appeared.faces[0].x, appeared.faces[0].y))
    assert left.faces == ()


def test_faces_report_two_portraits_spawned_side_by_side(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    """specs/testing/sim_scene.md "A pool of portraits": two portraits spawned from the
    pool at once are both in view — `bridge.faces` reports two faces — and despawning one
    leaves one. Tracking is stopped (the head stays ahead, both portraits in frame)."""
    bridge, _caps = live_bridge

    async def scenario() -> tuple[FaceReport, FaceReport]:
        await bridge.set_motors_state("enabled")
        await bridge.set_face_detection(True)
        await bridge.stop_head_tracking()
        try:
            left = face_scene.spawn(face_at(LATERAL_M))
            face_scene.spawn(face_at(-LATERAL_M))
            both = await asyncio.wait_for(
                bridge.faces.wait_for(lambda r: r.active and len(r.faces) == 2), 5.0
            )
            face_scene.despawn(left)
            one = await asyncio.wait_for(
                bridge.faces.wait_for(lambda r: r.active and len(r.faces) == 1),
                FACE_ABSENT_S + 3.0,
            )
        finally:
            await bridge.start_head_tracking()  # the module's default state
        return both, one

    both, one = live_bridge.run(scenario())
    print(f"\n[e2e] two portraits: {both}\n[e2e] one despawned: {one}")
    xs = sorted(face.x for face in both.faces)
    assert xs[0] < 0.0 < xs[1], "the two portraits are not on either side of the image"
    assert len(one.faces) == 1
