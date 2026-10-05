"""E2E tier — the bridge's head tracking over a live daemon (specs/motion/head_tracking.md,
specs/core/bridge.md "Attention / gaze", specs/motion/motion.md "Emotions through the
loop"): the head converges on the test scene's portrait and follows it, hands itself back
to the idle move when alone and re-engages, plays an emotion over tracking, and chooses
whom to follow among several portraits.

Gated on `camera` + `faces`: the viewer sim. The convergence kit — thresholds, the
sampler, the assertions — is `reachy_mini_bridge.testing.gaze`
(specs/testing/testing_support.md "Public surface").

Run explicitly (the viewer sim: the camera needs its GL context; headless the whole
module skips on `camera`):
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_head_tracking.py -rs

Every test awaits the bridge through `live_bridge.run(...)`, the harness's one event
loop (specs/testing/testing_support.md "Public surface"); the daemon is the run's, the
bridge session this module's (specs/testing/testing.md "Daemon lifecycle")."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import Any

from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.head_tracking import (
    TRACKING_LOST_S,
    TRACKING_SWITCH_S,
    HeadTrackingReport,
)
from reachy_mini_bridge.motion import (
    BLEND_S,
    BREATH_REST_S,
    BREATH_S,
)
from reachy_mini_bridge.testing import LiveBridge, requires_caps
from reachy_mini_bridge.testing.gaze import (
    LATERAL_M,
    MOVE_THRESHOLD_DEG,
    NEUTRAL_THRESHOLD_DEG,
    Track,
    angle_from_neutral_deg,
    arm_tracking,
    assert_tracked,
    expected_yaw_deg,
    face_at,
    sample_idle,
    track_onto,
    wait_for,
    yaw_pitch_deg,
)
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient

# --- attention / gaze: tracking a face in the sim ---------------------------------------
#
# Runs where the harness probed `camera` (the viewer sim) and `faces` (the bridge's test
# scene, which every harness-spawned sim runs: specs/testing/sim_scene.md); skips elsewhere — the
# headless sim renders no camera, a robot has no scriptable face. The portrait plane goes
# through the real pipeline: rendered by the daemon, streamed to the client, found by the
# bridge's shipped detector on the camera feed (the `yunet` detector `live_bridge` configures,
# specs/vision/user_perception.md), and the bridge's own tracker aims the head
# (specs/motion/head_tracking.md). So these tests check how the head moves and where it settles:
# toward the face, past it once by a bounded amount and never oscillating, onto the yaw
# the face's position implies, with the tracked face at the image centre. Watch the
# viewer: the head turns onto the portrait, keeps breathing while it looks, follows it,
# and idles in full again once it is gone.
#
# `live_bridge` is module-scoped and its detection loop lives the whole module, so a
# tracking test starts by `arm_tracking` (reachy_mini_bridge.testing.gaze — the
# convergence kit every assertion here comes from): tracking on (a detection-only test
# before it may have turned it off) and the previous test's aim released — which the
# tracker only does if the loop kept ticking it between tests.


def test_head_tracking_turns_onto_a_face_and_follows_it(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    """specs/core/bridge.md "Attention / gaze": with tracking on (the config default), the head
    turns onto a face that appears ahead, then follows it 0.15 m to either side and back:
    each time toward the face, past it at most once by a bounded amount, settling at the
    yaw its position implies with the face at the image centre and the pitch unchanged.

    Tracks with **focus**: the head holds exactly on the aim, the idle move's head motion
    left out (its antennas kept), so the settled yaw and pitch read the tracker alone.
    Under the default composition breathing roams the head +-2 deg of yaw around the aim
    and a gliding face settled 3-4.5 deg short of its 5 deg tolerance; the other gaze
    tests keep the default and cover that path."""
    bridge, _caps = live_bridge

    async def scenario() -> list[Track]:
        await bridge.set_motors_state("enabled")
        await arm_tracking(bridge, focus=True)
        assert bridge.tracking, "tracking is on by default from the config"
        assert bridge.tracking_focus
        face = face_scene.spawn(DEFAULT_FACE_POS)
        tracks = [await track_onto(bridge, "face ahead", 0.0)]
        followed = bridge.head_tracking.value.track_id
        assert followed is not None and followed == tracks[0].face.track_id  # type: ignore[union-attr]
        # the report's frame is the one its faces were found in: the box crops the face
        report = bridge.faces.value
        assert report.frame is not None and report.frame_id == report.frame.frame_id
        x, y, w, h = (round(v) for v in report.faces[0].bbox)
        crop = report.frame.image[max(y, 0) : y + h, max(x, 0) : x + w]
        assert crop.size > 0 and crop.std() > 5.0  # a face, not a flat patch
        for lateral in (LATERAL_M, -LATERAL_M, 0.0):
            face_scene.place(face, face_at(lateral), duration=1.0)
            # A face that glides over 1 s is followed with a lag the head creeps out of
            # slowly; give the tail time before calling the head settled.
            tracks.append(
                await track_onto(
                    bridge,
                    f"face moved to y={lateral:+.2f} m",
                    lateral,
                    min_seconds=5.0,
                )
            )
        assert bridge.attention == "engaged"
        # one person throughout: the portrait kept its track_id while it moved
        assert bridge.head_tracking.value.track_id == followed
        return tracks

    first, *moves = live_bridge.run(scenario())
    assert_tracked(first)
    for track in moves:
        assert_tracked(track, pitch_ahead=first.pitch)


def test_attention_hands_the_head_back_and_reengages_on_the_face(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    """specs/core/bridge.md "Attention": alone, the robot idles in full. Once the face has been
    gone for the tracker's loss timeout `attention` reads `watching`, the gaze layer
    fades out, and the head settles back near neutral, breathing. When a face comes back
    on the other side, attention re-engages and the head turns onto its new position."""
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def scenario() -> tuple[Track, float, float, str | None, Track]:
        await bridge.set_motors_state("enabled")
        await arm_tracking(bridge)
        face = face_scene.spawn(face_at(LATERAL_M))
        assert await wait_for(lambda: bridge.attention == "engaged", 6.0)
        first = await track_onto(bridge, "engaged on the face", LATERAL_M)
        face_scene.despawn(face)
        hand_back = TRACKING_LOST_S + BLEND_S + 4.0
        assert await wait_for(lambda: bridge.attention == "watching", hand_back), (
            f"attention still {bridge.attention!r} {hand_back:.0f}s after the face left"
        )
        # long enough to always contain a whole breath, wherever the sample starts;
        # `settled` is the mean over that window, since the idle move roams
        z_range, settled = await sample_idle(robot, BREATH_S + BREATH_REST_S[1] + 1.0)
        face_scene.spawn(face_at(-LATERAL_M))
        reengaged = await wait_for(lambda: bridge.attention == "engaged", 8.0)
        again = await track_onto(
            bridge, "re-engaged on the face's new position", -LATERAL_M
        )
        return first, settled, z_range, bridge.attention if reengaged else None, again

    first, settled, z_range, attention, again = live_bridge.run(scenario())
    assert_tracked(first)
    print(
        f"\n[e2e] settled {settled:.1f} deg from neutral on average while alone, "
        f"breathing z "
        f"range {z_range:.4f} m, attention after the face returned: {attention!r}"
    )
    assert settled <= NEUTRAL_THRESHOLD_DEG, (
        f"head did not settle back into the idle move once alone ({settled:.1f} deg "
        "from neutral on average, the face was at "
        f"{abs(expected_yaw_deg(LATERAL_M)):.1f})"
    )
    assert z_range >= 0.002, "the head is not breathing after the hand-back"
    assert attention == "engaged", "attention did not re-engage once the face came back"
    assert_tracked(again)


def test_emotion_plays_over_tracking_and_the_head_returns_to_the_face(
    live_bridge: LiveBridge,
    face_scene: SimSceneClient,
    emotions_library: None,
) -> None:
    """specs/motion/motion.md "Emotions through the loop": an emotion under full-weight tracking
    plays as recorded (the motion loop leaves the gaze layer out of a primary) — the head
    moves through the choreography rather than staying pinned toward the face — and once
    the move ends the layer fades back in and the head turns back onto the still-visible
    face, attention engaged."""
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def scenario() -> tuple[str, float, Track]:
        await bridge.set_motors_state("enabled")
        await arm_tracking(bridge)
        face_scene.spawn(face_at(LATERAL_M))
        assert await wait_for(lambda: bridge.attention == "engaged", 6.0)
        await track_onto(bridge, "before the emotion", LATERAL_M)
        names = await bridge.list_emotions()
        assert names, "emotions library loaded but empty"
        emotion = names[0]  # the short move test_play_emotion_plays_a_real_move plays
        angles: list[float] = []

        async def sample_during_move() -> None:
            while True:
                pose = await asyncio.to_thread(robot.get_current_head_pose)
                angles.append(angle_from_neutral_deg(pose))
                await asyncio.sleep(0.05)

        sampler = asyncio.create_task(sample_during_move())
        try:
            await bridge.play_emotion(emotion)
        finally:
            sampler.cancel()
        move_excursion = (max(angles) - min(angles)) if len(angles) > 1 else 0.0
        after = await track_onto(bridge, "after the emotion", LATERAL_M)
        return emotion, move_excursion, after

    emotion, move_excursion, after = live_bridge.run(scenario())
    print(
        f"\n[e2e] {emotion!r} under tracking: head moved {move_excursion:.1f} deg through "
        "the choreography"
    )
    assert move_excursion >= MOVE_THRESHOLD_DEG, (
        f"the emotion barely moved the head ({move_excursion:.1f} deg) — it may not have played"
    )
    assert_tracked(after)
    assert bridge.attention == "engaged"


# --- whom the head follows, with several portraits (specs/motion/head_tracking.md "Whom the
# head follows", specs/testing/sim_scene.md "The testing harness") ---------------------------
#
# Portraits from the test scene's pool: near at 0.35 m and far at 0.60 m give the size
# difference, ±0.15 m to either side. Portraits that must be in view together are spawned
# with tracking stopped and tracking started once both are reported, so the choice is made
# with both there. Each asserts through `bridge.head_tracking` and the head's yaw.

NEAR_X, FAR_X = 0.35, 0.60
FACE_Z = DEFAULT_FACE_POS[2]


def _yaw_to(x: float, y: float) -> float:
    return math.degrees(math.atan2(y, x))


class _Changes:
    """Records what `bridge.head_tracking.changes()` wakes on while it runs."""

    def __init__(self, bridge: ReachyMiniBridge) -> None:
        self.woken: list[HeadTrackingReport] = []
        self._task = asyncio.create_task(self._run(bridge))

    async def _run(self, bridge: ReachyMiniBridge) -> None:
        async for report in bridge.head_tracking.changes():
            self.woken.append(report)

    def stop(self) -> None:
        self._task.cancel()


async def _both_in_view(bridge: ReachyMiniBridge, count: int = 2) -> None:
    """Poll the report's value: a face returning within the absence window is a silent
    `update` (the published count never dropped), which `wait_for` would not see."""
    deadline = time.monotonic() + 5.0
    while not (bridge.faces.value.active and len(bridge.faces.value.faces) == count):
        assert time.monotonic() < deadline, (
            f"not {count} faces in view after 5 s: {bridge.faces.value.faces}"
        )
        await asyncio.sleep(0.05)


async def _until(predicate: Callable[[], bool], timeout: float) -> float:
    """Seconds until `predicate` held (an AssertionError past `timeout`)."""
    started = time.monotonic()
    while not predicate():
        assert time.monotonic() - started < timeout, "condition not met in time"
        await asyncio.sleep(0.05)
    return time.monotonic() - started


async def _prepare(bridge: ReachyMiniBridge) -> None:
    await bridge.set_motors_state("enabled")
    await bridge.set_face_detection(True)
    await bridge.stop_head_tracking()


def test_the_head_follows_the_biggest_of_two_faces(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    bridge, _caps = live_bridge

    async def scenario() -> tuple[Track, int, int | None]:
        await _prepare(bridge)
        face_scene.spawn((FAR_X, -LATERAL_M, FACE_Z))
        face_scene.spawn((NEAR_X, LATERAL_M, FACE_Z))
        await _both_in_view(bridge)
        near = max(bridge.faces.value.faces, key=lambda f: f.size)
        await bridge.start_head_tracking()
        track = await track_onto(
            bridge,
            "the nearer of two faces",
            LATERAL_M,
            expected_yaw_deg=_yaw_to(NEAR_X, LATERAL_M),
        )
        return track, near.track_id, bridge.head_tracking.value.track_id

    track, near_id, followed = live_bridge.run(scenario())
    assert_tracked(track)
    assert followed == near_id


def test_a_nearer_face_arriving_does_not_take_the_head(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    bridge, _caps = live_bridge
    far_yaw = _yaw_to(FAR_X, -LATERAL_M)

    async def scenario() -> tuple[Track, Track, int | None, int | None, list[Any]]:
        await _prepare(bridge)
        face_scene.spawn((FAR_X, -LATERAL_M, FACE_Z))
        await bridge.start_head_tracking()
        # The head starts where the previous test left it, up to 40 deg from this face:
        # about 2.5 s of turn, then the 2 s the settled yaw is averaged over.
        first = await track_onto(
            bridge,
            "the far face, alone",
            -LATERAL_M,
            expected_yaw_deg=far_yaw,
            min_seconds=4.5,
        )
        followed = bridge.head_tracking.value.track_id
        changes = _Changes(bridge)
        face_scene.spawn((NEAR_X, LATERAL_M, FACE_Z))
        await _both_in_view(bridge)
        await asyncio.sleep(TRACKING_SWITCH_S + 2.0)
        after = await track_onto(
            bridge,
            "still the far face, a nearer one beside it",
            -LATERAL_M,
            expected_yaw_deg=far_yaw,
            min_seconds=1.0,
        )
        changes.stop()
        return (
            first,
            after,
            followed,
            bridge.head_tracking.value.track_id,
            changes.woken,
        )

    first, after, followed, still, woken = live_bridge.run(scenario())
    assert_tracked(first)
    assert_tracked(after)
    assert still == followed and woken == []


def test_a_face_hidden_briefly_is_waited_for_and_followed_again(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    """The hold: the followed face gone for half the switch time, the head holds toward
    where it was — not turning to the other face — and follows it again, same track_id,
    with nothing published on `bridge.head_tracking` meanwhile."""
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def scenario() -> tuple[
        float, list[float], int | None, int | None, list[Any]
    ]:
        await _prepare(bridge)
        await bridge.start_head_tracking()
        followed_name = face_scene.spawn(face_at(LATERAL_M))
        await track_onto(bridge, "the first face", LATERAL_M)
        followed = bridge.head_tracking.value.track_id
        face_scene.spawn(face_at(-LATERAL_M))
        await _both_in_view(bridge)
        before, _ = yaw_pitch_deg(await asyncio.to_thread(robot.get_current_head_pose))
        changes = _Changes(bridge)
        face_scene.despawn(followed_name)
        yaws: list[float] = []
        deadline = time.monotonic() + TRACKING_SWITCH_S / 2
        while time.monotonic() < deadline:
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            yaws.append(yaw_pitch_deg(pose)[0])
            await asyncio.sleep(0.05)
        again = face_scene.spawn(face_at(LATERAL_M))
        assert again == followed_name  # the pool hands the same portrait back
        await _both_in_view(bridge)
        await asyncio.sleep(1.0)
        changes.stop()
        return (
            before,
            yaws,
            followed,
            bridge.head_tracking.value.track_id,
            changes.woken,
        )

    before, yaws, followed, after, woken = live_bridge.run(scenario())
    print(
        f"\n[e2e] hold: yaw {before:+.1f} deg before, "
        f"{min(yaws):+.1f}..{max(yaws):+.1f} while the face was gone"
    )
    assert all(abs(y - before) < 3.0 for y in yaws), "the head left the vanished face"
    assert after == followed and woken == []


def test_a_face_gone_for_good_hands_over_to_the_other_then_the_head_is_released(
    live_bridge: LiveBridge, face_scene: SimSceneClient
) -> None:
    """The switch, then the loss: the followed face despawned, the head holds for about
    `TRACKING_SWITCH_S`, then follows the other face (one change naming it) and turns
    onto it; that one despawned too, attention reads `watching` after `TRACKING_LOST_S`."""
    bridge, _caps = live_bridge

    async def scenario() -> tuple[float, Track, int, list[Any], float, tuple[Any, Any]]:
        await _prepare(bridge)
        await bridge.start_head_tracking()
        first = face_scene.spawn(face_at(LATERAL_M))
        await track_onto(bridge, "the first face", LATERAL_M)
        followed = bridge.head_tracking.value.track_id
        second = face_scene.spawn(face_at(-LATERAL_M))
        await _both_in_view(bridge)
        other = next(f for f in bridge.faces.value.faces if f.track_id != followed)
        changes = _Changes(bridge)
        face_scene.despawn(first)
        switched_after = await _until(
            lambda: bridge.head_tracking.value.track_id == other.track_id,
            TRACKING_SWITCH_S + 3.0,
        )
        track = await track_onto(bridge, "switched to the other face", -LATERAL_M)
        face_scene.despawn(second)
        lost_after = await _until(
            lambda: bridge.head_tracking.value.attention == "watching",
            TRACKING_LOST_S + 3.0,
        )
        changes.stop()
        # What the detector and the tracker hold at the end — the evidence when a face
        # shows up after both portraits are gone (a phantom seen once on a CI runner).
        final = (bridge.head_tracking.value, bridge.faces.value)
        return switched_after, track, other.track_id, changes.woken, lost_after, final

    switched_after, track, other_id, woken, lost_after, final = live_bridge.run(
        scenario()
    )
    print(
        f"\n[e2e] switched after {switched_after:.2f} s, released after {lost_after:.2f} s"
    )
    print(f"[e2e] at the end: tracking {final[0]}, faces {final[1]}")
    assert_tracked(track)
    assert switched_after >= TRACKING_SWITCH_S - 0.2  # the head waited first
    states = [(r.attention, r.track_id) for r in woken]
    print(f"[e2e] head tracking changes: {states}")
    # the other face first, the release last; between them only re-engagements — the
    # detector may re-identify the portrait under a new track_id while the head sweeps
    # across it, and the tracker then waits and switches to it, as it should
    assert states[0] == ("engaged", other_id)
    assert states[-1] == ("watching", None)
    assert all(attention == "engaged" for attention, _ in states[:-1])
    assert lost_after >= TRACKING_LOST_S - 0.2
