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
from contextlib import suppress
from dataclasses import replace
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import ReachyMiniConfig
from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.config import (
    FaceDetectionSettings,
    MotionSettings,
    TrackingSettings,
)
from reachy_mini_bridge.face_detection import (
    TRACK_LOST_S,
    PixelFace,
    UserDeparture,
    UsersReport,
)
from reachy_mini_bridge.testing import BridgeLoop, LiveBridge, _daemon, requires_caps
from reachy_mini_bridge.testing.fixtures import (
    check_required_capabilities,
    probed_capabilities,
)
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

pytestmark = pytest.mark.requires("camera", "props:face")


@pytest.fixture(scope="module")
def live_bridge_custom_faces(
    live_daemon: tuple[str, int],
) -> Iterator[LiveBridge]:
    """`live_bridge`'s twin for the `custom` detection path: the same target, daemon,
    capability probe and event loop (`BridgeLoop`), but a config with
    `face_detection.detector="custom"` and the shipped detector class registered as the
    custom factory. Its own session, because the detector is config-only and two bridge
    sessions on one daemon would be two motion loops writing the head."""
    host, port = live_daemon
    bridge = ReachyMiniBridge(
        ReachyMiniConfig(
            backend=_daemon.backend(),
            robot=_daemon.robot_options(host, port),
            face_detection=FaceDetectionSettings(
                detector="custom", enabled=True, face_detector=YuNetDetector
            ),
            motion=MotionSettings(tracking=TrackingSettings(enabled=True)),
        )
    )
    with BridgeLoop() as loop:
        loop.run(bridge.start())
        try:
            caps = probed_capabilities(bridge, (host, port))
            check_required_capabilities(caps)
            yield LiveBridge(bridge, caps, loop)
        finally:
            loop.run(bridge.stop())


@pytest.fixture
def custom_face_scene(
    live_bridge_custom_faces: LiveBridge,
    sim_scene: SimSceneClient,
) -> Iterator[SimSceneClient]:
    """conftest's `face_scene`, gated on this module's own session."""
    requires_caps(live_bridge_custom_faces, "camera", "props:face")
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
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
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


# --- a recognising detector (specs/vision/user_perception.md "Tracks": "Recognised faces") --


def _signature(image: npt.NDArray[np.uint8], bbox: tuple[float, ...]) -> Any:
    """The face's crop as a 32x32 grey patch, zero-mean and unit-norm: what the test's
    recogniser compares by correlation (no model, no OpenCV)."""
    x, y, w, h = bbox
    height, width = image.shape[:2]
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(width, int(x + w)), min(height, int(y + h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    grey = image[y0:y1, x0:x1].astype(np.float64).mean(axis=2)
    rows = np.linspace(0, grey.shape[0] - 1, 32).astype(int)
    cols = np.linspace(0, grey.shape[1] - 1, 32).astype(int)
    patch = grey[np.ix_(rows, cols)]
    patch -= patch.mean()
    norm = float(np.linalg.norm(patch))
    return None if norm == 0.0 else patch / norm


class _RecognisingDetector:
    """The shipped detector with a recogniser on top, as a developer would wrap theirs:
    each face's crop compared with the enrolled portraits, keyed by the best one when it
    is clearly the best (a score above `_MIN_SCORE`, ahead of the next by `_MARGIN`),
    no key otherwise."""

    _MIN_SCORE = 0.6
    _MARGIN = 0.1

    def __init__(self, gallery: dict[str, Any]) -> None:
        self._inner = YuNetDetector()
        self._gallery = gallery
        self.box_height_m = self._inner.box_height_m

    def detect(self, frame_bgr: npt.NDArray[np.uint8], ts: float) -> list[PixelFace]:
        faces = self._inner.detect(frame_bgr, ts)
        return [replace(f, identity=self.recognise(frame_bgr, f)) for f in faces]

    def recognise(
        self, frame_bgr: npt.NDArray[np.uint8], face: PixelFace
    ) -> str | None:
        signature = _signature(frame_bgr, face.bbox)
        if signature is None or not self._gallery:
            return None
        scores = sorted(
            (
                (float((signature * ref).sum()), name)
                for name, ref in self._gallery.items()
            ),
            reverse=True,
        )
        best, name = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        return (
            name if best >= self._MIN_SCORE and best - second >= self._MARGIN else None
        )


async def _enrol(
    bridge: ReachyMiniBridge, scene: SimSceneClient, names: tuple[str, ...]
) -> dict[str, Any]:
    """Each portrait shown alone ahead, its face's crop from the live frame kept as its
    reference: the recogniser learns the sim's own rendering."""
    gallery: dict[str, Any] = {}
    for name in names:
        scene.spawn(DEFAULT_FACE_POS, name=name)
        deadline = time.perf_counter() + 5.0
        while True:
            report = bridge.faces.value
            if report.faces and report.frame is not None:
                signature = _signature(report.frame.image, report.faces[0].bbox)
                if signature is not None:
                    gallery[name] = signature
                    break
            assert time.perf_counter() < deadline, f"{name}: no face to enrol"
            await asyncio.sleep(0.05)
        scene.despawn(name)
        await asyncio.sleep(0.5)
    return gallery


async def _recognised(bridge: ReachyMiniBridge, key: str, timeout: float = 5.0) -> int:
    """The track id of the face carrying ``key``, once a confirmed user does."""
    deadline = time.perf_counter() + timeout
    while True:
        for user in bridge.users.value.users:
            if user.seen and user.identity == key:
                return user.track_id
        assert time.perf_counter() < deadline, (
            f"no user recognised as {key}: {bridge.faces.value.faces}"
        )
        await asyncio.sleep(0.05)


@pytest.fixture
def recognising(
    live_bridge_custom_faces: LiveBridge, custom_face_scene: SimSceneClient
) -> Iterator[LiveBridge]:
    """This module's session with the recognising detector registered over the shipped
    one, after enrolling `face_1` and `face_2` ahead with tracking off; the shipped
    detector back after the test. The recogniser is the test's own stand-in — a
    correlation against the enrolled crops, reliable where it enrolled (ahead), not a
    model: the live test exercises a key's path through the bridge, the matching rules
    themselves are tests/test_face_detection.py's."""
    bridge, _caps = live_bridge_custom_faces

    async def setup() -> None:
        await bridge.stop_head_tracking()
        gallery = await _enrol(bridge, custom_face_scene, ("face_1", "face_2"))
        await bridge.set_face_detector(lambda: _RecognisingDetector(gallery))

    live_bridge_custom_faces.run(setup())
    yield live_bridge_custom_faces

    async def teardown() -> None:
        await bridge.set_face_detector(YuNetDetector)
        await bridge.start_head_tracking()

    live_bridge_custom_faces.run(teardown())


def test_recognition_keeps_someone_stepping_in_apart(
    recognising: LiveBridge, custom_face_scene: SimSceneClient
) -> None:
    """The followed person (`face_1`) leaves as another (`face_2`) steps into the same
    spot: recognised, the newcomer arrives under their own id, the one who left leaves
    as "left", and the head switches to the newcomer — where geometry alone would let
    the newcomer continue the other's track."""
    bridge, _caps = recognising

    async def scenario() -> tuple[int, int, list[int], list[UserDeparture], int | None]:
        await bridge.set_motors_state("enabled")
        await arm_tracking(bridge)
        custom_face_scene.spawn(DEFAULT_FACE_POS, name="face_1")
        first = await _recognised(bridge, "face_1")
        await track_onto(bridge, "recognised: face_1 ahead", 0.0)
        woken: list[UsersReport] = []

        async def subscribe() -> None:
            async for report in bridge.users.changes(every=True):
                woken.append(report)

        subscriber = asyncio.create_task(subscribe())
        await asyncio.sleep(0)
        custom_face_scene.despawn("face_1")
        custom_face_scene.spawn(DEFAULT_FACE_POS, name="face_2")
        second = await _recognised(bridge, "face_2")
        # the head switches once face_1 has been missing TRACKING_SWITCH_S; face_1
        # leaves at the end of the miss window, TRACK_LOST_S
        deadline = time.perf_counter() + TRACK_LOST_S + 3.0
        while bridge.head_tracking.value.track_id != second or not any(
            r.left for r in woken
        ):
            assert time.perf_counter() < deadline, (
                f"head on {bridge.head_tracking.value.track_id}, "
                f"departures {[d for r in woken for d in r.left]}"
            )
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        subscriber.cancel()
        with suppress(asyncio.CancelledError):
            await subscriber
        arrived = [a for r in woken for a in r.arrived]
        left = [d for r in woken for d in r.left]
        return first, second, arrived, left, bridge.head_tracking.value.track_id

    first, second, arrived, left, followed = recognising.run(scenario())
    print(f"\n[e2e] recognised: {first} -> {second}; arrived {arrived}, left {left}")
    assert second != first  # the newcomer did not take the other's track
    assert arrived == [second]
    assert left == [UserDeparture(first, "left")]
    assert followed == second
