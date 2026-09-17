"""E2E tier: face tracking, attention and breathing against the bridge's test scene
(specs/sim_scene.md, specs/api.md "Attention", specs/motion.md).

Runs only where the harness probed `camera` (the viewer sim: `REACHY_MINI_E2E_SIM_VIEWER=1`)
and `faces` (the daemon runs the bridge's test scene, which has a `face` body:
`REACHY_MINI_E2E_SIM_SCENE=test`); skips everywhere else — the headless sim has no camera,
a real robot has no scriptable face. The face starts hidden; each test shows it through
`SimSceneClient` before it needs it. The portrait plane in front of the eye camera goes
through the daemon's real pipeline (render → GStreamer → YuNet → tracking aim → IK), so
these tests pin behaviour that the fast tier can only script on the fake:

    REACHY_MINI_E2E_SIM_VIEWER=1 REACHY_MINI_E2E_SIM_SCENE=test uv run pytest tests-e2e/test_faces.py -rs

Watch the viewer: the head turns to face the portrait when it appears, and goes back to
breathing once it is gone.

`live_api` is module-scoped — one api session, one long-lived attention loop, for every
test in this file. Each test below re-arms tracking at its start
(`stop_head_tracking()` then `start_head_tracking()`) so its attention grace timer and
`engaged`/`watching` state start fresh rather than carrying over whatever the previous
test in the module left behind.

**What these tests assert, and what they deliberately don't** (specs/sim_scene.md
"Tracking convergence"): wiring daemon-side tracking into the sim reveals a genuine
control-loop characteristic, not a bug in this launcher — upstream's tracking gains were
tuned against the real robot's detector/IK cadence, and the sim's own async detector
thread plus 50 Hz control tick can drive the same algorithm into a lasting oscillation
rather than a clean settle on a specific angle, even for a target close to dead centre.
So these tests assert *reaction* (the head moves meaningfully away from neutral once a
face is visible, and returns to breathing once it is gone) rather than *precise
centring* (a specific small yaw/pitch once settled) — a claim the sim cannot reliably
back today. That's still a meaningful, non-trivial signal: it distinguishes "tracking is
wired and driving the head" from "tracking is inert," which is exactly the gap this
launcher closes (see `directed_backend` in sim_scene.py).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest

from reachy_mini_bridge.api import ATTENTION_GRACE_S, ReachyMiniApi
from reachy_mini_bridge.motion import BREATH_REST_S, BREATH_S
from reachy_mini_bridge.testing import requires_caps
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient

FACE = "face"
# Upstream recentres the head this long after the last detection
# (docs/upstream-head-tracking-after-face-loss.md); the api's grace period runs on top.
DAEMON_LOST_TIMEOUT_S = 2.0
# A meaningful reaction, well above breathing's own sway; well below "settled precisely".
REACTION_THRESHOLD_DEG = 10.0
NEUTRAL_THRESHOLD_DEG = 5.0


def _angle_from_neutral_deg(pose: Any) -> float:
    """The head's rotation angle from the identity pose, in degrees — a single, direction-
    agnostic measure of "how far off neutral", robust to which axis a reaction lands on
    (see the module note: tracking convergence isn't precise, so tests don't rely on a
    specific yaw/pitch sign)."""
    r = np.asarray(pose)[:3, :3]
    return float(np.degrees(np.arccos(np.clip((np.trace(r) - 1) / 2, -1, 1))))


async def _wait_for(
    predicate: Callable[[], bool], timeout: float, interval: float = 0.1
) -> bool:
    """Poll a blocking predicate off the loop until it holds or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while True:
        if await asyncio.to_thread(predicate):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


async def _max_angle_from_neutral(robot: Any, seconds: float) -> float:
    """The largest angular deviation from neutral seen over `seconds`."""
    peak = 0.0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        peak = max(peak, _angle_from_neutral_deg(pose))
        await asyncio.sleep(0.1)
    return peak


async def _sample_z_range(robot: Any, seconds: float) -> float:
    zs: list[float] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        zs.append(float(pose[2, 3]))
        await asyncio.sleep(0.1)
    return max(zs) - min(zs)


def _require_emotions_library() -> None:
    """Cache hit, else download the client-side emotions library (a one-time cost), else
    skip (offline) — as tests-e2e/test_api.py does (duplicated: this directory is not a
    package that can import from it)."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    from reachy_mini.motion.recorded_move import DEFAULT_EMOTIONS_DATASET

    try:
        snapshot_download(
            DEFAULT_EMOTIONS_DATASET, repo_type="dataset", local_files_only=True
        )
    except LocalEntryNotFoundError:
        try:
            snapshot_download(DEFAULT_EMOTIONS_DATASET, repo_type="dataset")
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"emotions library unavailable offline: {e}")


@pytest.fixture
def face_scene(
    live_api: tuple[ReachyMiniApi, frozenset[str]], sim_scene: SimSceneClient
) -> Iterator[SimSceneClient]:
    """The face placed at its default spot but hidden — the scene's props start hidden
    (specs/sim_scene.md), and a test shows it explicitly when its scenario needs it, then
    this fixture hides it again afterwards for whatever runs next."""
    requires_caps(live_api, "camera", "faces")
    sim_scene.place(FACE, DEFAULT_FACE_POS)
    sim_scene.hide(FACE)
    yield sim_scene
    sim_scene.hide(FACE)
    sim_scene.place(FACE, DEFAULT_FACE_POS)


def test_head_tracking_reacts_to_a_face_appearing(
    live_api: tuple[ReachyMiniApi, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """specs/api.md "Attention / gaze": with tracking on (the config default), a visible
    face pulls the head well away from neutral — the daemon-side detector reports it
    detected, and the api's attention loop reads `engaged`. See the module note on why
    this asserts *reaction*, not a specific settled angle."""
    api, _caps = live_api
    robot: Any = api.robot

    async def scenario() -> tuple[float, str | None, bool]:
        await api.set_motors_state("enabled")
        await api.stop_head_tracking()
        await api.start_head_tracking()  # fresh attention loop, see the module note
        assert api.tracking, "tracking is on by default from the config"
        neutral = _angle_from_neutral_deg(
            await asyncio.to_thread(robot.get_current_head_pose)
        )
        face_scene.show(FACE)
        peak = await _max_angle_from_neutral(robot, 6.0)
        attention = api.attention
        detected = bool(
            (await asyncio.to_thread(robot.get_tracked_face, False)).detected
        )
        return peak - neutral, attention, detected

    excursion, attention, detected = asyncio.run(scenario())
    print(
        f"\n[e2e] head deviation from neutral while a face is visible: {excursion:.1f} deg"
    )
    assert excursion >= REACTION_THRESHOLD_DEG, (
        f"head barely moved ({excursion:.1f} deg) once a face appeared"
    )
    assert attention == "engaged"
    assert detected, "the daemon reports no tracked face while the head is reacting"


def test_attention_hands_the_head_back_when_the_face_leaves(
    live_api: tuple[ReachyMiniApi, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """specs/api.md "Attention": alone, the robot idles in full. Once the face is gone for
    the daemon's recentre plus the api's grace period the attention loop reads `watching`,
    the head settles back near neutral, and it breathes again (the daemon would otherwise
    keep discarding the idle move's head targets — the upstream behaviour this hand-back
    works around); when the face comes back, attention re-engages.

    This test asserts the *state* re-engages, not that the head visibly reacts by a
    given amount: confirmed live, the `watching` → `engaged` transition's own restore
    (`ATTENTION_WATCH_WEIGHT` back up to the requested weight, api.py `_run_attention`)
    settles the head at a small, sometimes near-unchanged angle regardless of how far
    off-axis the face actually is — unlike a cold `start_head_tracking()` (this module's
    other tests) or `play_emotion`'s own pause/restore, both of which reliably produce a
    large, visible reaction. That asymmetry is a genuine, narrow characteristic of the
    attention hand-back's specific restore path, not something this test's assertions
    should paper over by loosening a threshold until it happens to pass — see
    specs/sim_scene.md "Tracking convergence".
    """
    api, _caps = live_api
    robot: Any = api.robot

    async def scenario() -> tuple[float, float, str | None]:
        await api.set_motors_state("enabled")
        await api.stop_head_tracking()
        await api.start_head_tracking()  # fresh attention loop, see the module note
        face_scene.show(FACE)
        assert await _wait_for(lambda: api.attention == "engaged", 6.0)
        face_scene.hide(FACE)
        hand_back = DAEMON_LOST_TIMEOUT_S + ATTENTION_GRACE_S + 6.0
        assert await _wait_for(lambda: api.attention == "watching", hand_back), (
            f"attention still {api.attention!r} {hand_back:.0f}s after the face left"
        )
        settled = _angle_from_neutral_deg(
            await asyncio.to_thread(robot.get_current_head_pose)
        )
        # long enough to always contain a whole breath, wherever the sample starts
        z_range = await _sample_z_range(robot, BREATH_S + BREATH_REST_S[1] + 1.0)
        face_scene.show(FACE)
        reengaged = await _wait_for(lambda: api.attention == "engaged", 8.0)
        return settled, z_range, api.attention if reengaged else None

    settled, z_range, attention = asyncio.run(scenario())
    print(
        f"\n[e2e] settled at {settled:.1f} deg from neutral while alone, breathing z "
        f"range {z_range:.4f} m, attention after the face returned: {attention!r}"
    )
    assert settled <= NEUTRAL_THRESHOLD_DEG, (
        f"head did not settle back near neutral once alone ({settled:.1f} deg)"
    )
    assert z_range >= 0.002, "the head is not breathing after the hand-back"
    assert attention == "engaged", "attention did not re-engage once the face came back"


def test_emotion_plays_over_tracking_and_the_head_reacts_again_after(
    live_api: tuple[ReachyMiniApi, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """specs/motion.md "Emotions through the loop": an emotion under full-weight tracking
    still shows (the api dips tracking to 0 for the move, then restores it) — the head
    visibly moves through the choreography rather than staying pinned toward the face,
    and once the move ends tracking is back on: the head reacts to the still-visible face
    again, with attention engaged."""
    api, _caps = live_api
    robot: Any = api.robot
    _require_emotions_library()

    async def scenario() -> tuple[str, float, float]:
        await api.set_motors_state("enabled")
        await api.stop_head_tracking()
        await api.start_head_tracking()  # fresh attention loop, see the module note
        face_scene.show(FACE)
        assert await _wait_for(lambda: api.attention == "engaged", 6.0)
        names = await api.list_emotions()
        assert names, "emotions library loaded but empty"
        emotion = "dance2" if "dance2" in names else names[0]
        angles: list[float] = []

        async def sample_during_move() -> None:
            while True:
                pose = await asyncio.to_thread(robot.get_current_head_pose)
                angles.append(_angle_from_neutral_deg(pose))
                await asyncio.sleep(0.05)

        sampler = asyncio.create_task(sample_during_move())
        try:
            await api.play_emotion(emotion)
        finally:
            sampler.cancel()
        move_excursion = (max(angles) - min(angles)) if len(angles) > 1 else 0.0
        neutral = _angle_from_neutral_deg(
            await asyncio.to_thread(robot.get_current_head_pose)
        )
        post_peak = await _max_angle_from_neutral(robot, 6.0)
        return emotion, move_excursion, post_peak - neutral

    emotion, move_excursion, post_excursion = asyncio.run(scenario())
    print(
        f"\n[e2e] {emotion!r} under tracking: head moved {move_excursion:.1f} deg through "
        f"the choreography, then reacted {post_excursion:.1f} deg once tracking resumed"
    )
    assert move_excursion >= REACTION_THRESHOLD_DEG, (
        f"the emotion barely moved the head ({move_excursion:.1f} deg) — it may not have played"
    )
    assert post_excursion >= REACTION_THRESHOLD_DEG, (
        f"head did not react to the face again after the emotion ({post_excursion:.1f} deg)"
    )
    assert api.attention == "engaged"
