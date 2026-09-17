"""E2E tier: ReachyMiniApi over a live daemon (specs/api.md, specs/audio.md).

Target-agnostic: the shipped `live_api` fixture (reachy_mini_bridge.testing.fixtures,
wired in via conftest.py) resolves the target
(`REACHY_MINI_E2E_TARGET`, default `sim`), so the same tests run on the headless sim,
the headfull viewer, and a real robot — each test gated by `requires_caps(...)` on the
capability it needs and skipping cleanly where absent.

Run explicitly:
    uv run pytest tests-e2e/test_api.py
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_api.py  # + camera, tracking
    REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e/test_api.py

The api's methods are async; each test drives them with `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
from reachy_mini import ReachyMini

from reachy_mini_bridge.api import ATTENTION_GRACE_S, ReachyMiniApi
from reachy_mini_bridge.audio import TTSEngineSynthesizer
from reachy_mini_bridge.errors import GravityCompensationUnsupportedError
from reachy_mini_bridge.motion import BLEND_S, BREATH_REST_S, BREATH_S
from reachy_mini_bridge.testing import require_env, requires_caps
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient

# A public ElevenLabs voice used throughout tts-engine's own docs; override with
# REACHY_MINI_E2E_TTS_VOICE_ID for an account-specific voice.
_DEFAULT_TTS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"


class _ToneSynth:
    """Credential-free SpeechSynthesizer: a 16 kHz mono tone (no TTS backend).

    `seconds` of audio at `amplitude`, in 100 ms blocks.
    """

    sample_rate = 16000

    def __init__(self, seconds: float = 1.0, amplitude: float = 0.1) -> None:
        self._blocks = round(seconds * 10)
        self._amplitude = amplitude

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        for _ in range(self._blocks):
            yield np.full(1600, self._amplitude, dtype=np.float32)


async def _motors_state_after_set(api: ReachyMiniApi, state: str) -> str:
    """Set a motor state, then read it back once the daemon status reflects it.

    The daemon's status lags a switch by a fraction of a second, so the read polls for up
    to a second; a target that ignores the state (the sim) reads back its old one.
    """
    await api.set_motors_state(state)
    deadline = time.monotonic() + 1.0
    while (mode := await api.get_motors_state()) != state:
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(0.1)
    return mode


def test_motor_state_reads_and_dispatches_over_the_live_path(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """`get_motors_state` reads a valid mode and each `set_motors_state` reaches the daemon.

    The e2e value here is that the read (a real daemon status round-trip) and each set
    dispatch work over the network — not that a given target *honors* a state. Hardware
    honors `disabled`/`enabled`, but the sim daemon ignores every motor-state change:
    `disabled` keeps reporting `enabled` (headless and headfull viewer). That is why this
    asserts validity, not equality; the fast tier pins the exact dispatch→state mapping
    deterministically on the fake. Gravity compensation has its own capability-gated test
    below: sending it to a daemon that can't hold it drops the connection.

    First in the module on purpose, and gentle on hardware: the head is lowered to the
    SDK's sleep pose before torque goes off (so a robot that honors `disabled` rests
    rather than drops), then raised back to the initial (awake) pose once torque is on
    again, so every later test starts upright.
    """
    from reachy_mini.reachy_mini import (
        INIT_ANTENNAS_JOINT_POSITIONS,
        INIT_HEAD_POSE,
        SLEEP_ANTENNAS_JOINT_POSITIONS,
        SLEEP_HEAD_POSE,
    )

    requires_caps(live_api, "motion")
    api, _caps = live_api
    # goto_target is upstream-only (not on the fake); this tier is live-only.
    robot: Any = api.robot
    valid = {"enabled", "disabled", "gravity_compensation"}

    async def scenario() -> tuple[str, dict[str, str]]:
        original = await api.get_motors_state()
        # The motion loop would otherwise blend back to neutral the moment each
        # goto_target ends (specs/motion.md): a caller driving the head directly needs
        # presence off for its own moves to hold.
        await api.set_presence(False)
        try:
            results: dict[str, str] = {}
            results["enabled"] = await _motors_state_after_set(api, "enabled")
            await asyncio.to_thread(
                robot.goto_target,
                head=SLEEP_HEAD_POSE,
                antennas=SLEEP_ANTENNAS_JOINT_POSITIONS,
                duration=2.0,
            )
            results["disabled"] = await _motors_state_after_set(api, "disabled")
            await api.set_motors_state("enabled")
            await asyncio.to_thread(
                robot.goto_target,
                head=INIT_HEAD_POSE,
                antennas=INIT_ANTENNAS_JOINT_POSITIONS,
                duration=1.0,
            )
            await api.set_motors_state(original)  # restore
        finally:
            await api.set_presence(True)
        return original, results

    original, results = asyncio.run(scenario())
    print(f"\n[e2e] motor states: original={original!r}, read back={results!r}")
    assert original in valid
    assert all(mode in valid for mode in results.values())


def test_real_audio_format_matches_the_fake_assumptions(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """The live daemon reports the float32 / channel / 16 kHz facts the fake hardcodes.

    This is the check the fast tier structurally cannot make: it confirms the numbers
    the `fake` backend bakes in are what a real daemon actually reports (specs/audio.md
    "Background": 2 channels, float32, 16 kHz on sim and hardware).
    """
    requires_caps(live_api, "audio")
    api, _caps = live_api
    media: Any = api.robot.media

    assert api.mic_sample_rate == 16000
    assert api.mic_channels == media.get_input_channels()
    assert media.get_output_audio_samplerate() == 16000

    # get_audio_sample() returns None until a frame is ready, so poll briefly (the mic
    # tap tolerates this by skipping None; here we want the raw array to inspect dtype).
    sample = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        sample = media.get_audio_sample()
        if sample is not None and getattr(sample, "size", 0) > 0:
            break
        time.sleep(0.05)
    assert sample is not None, "no mic sample within timeout"

    arr = np.asarray(sample)
    assert arr.dtype == np.float32  # the fake's assumed capture dtype, confirmed live
    # The capture's channel layout is self-consistent with the getter (interleaved).
    if arr.ndim == 1:
        assert arr.size % api.mic_channels == 0
    else:
        assert arr.shape[1] == api.mic_channels


def test_mic_tap_yields_int16_mono_frames(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """Draining the mic tap gives non-empty int16 mono PCM; `break` stops it."""
    requires_caps(live_api, "audio")
    api, _caps = live_api

    async def take(n: int) -> list[bytes]:
        out: list[bytes] = []
        async for chunk in api.audio_input():
            out.append(chunk)
            if len(out) == n:
                break
        return out

    chunks = asyncio.run(take(3))
    assert len(chunks) == 3
    for chunk in chunks:
        assert len(chunk) > 0
        assert len(chunk) % 2 == 0  # whole int16 samples (mono)


def test_say_pipeline_runs_to_the_speaker(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """A tone routed through `say` completes without error (daemon accepted the audio).

    Credential-free (in-test tone synth), so it runs on sim without any TTS keys.
    """
    requires_caps(live_api, "audio")
    api, _caps = live_api
    asyncio.run(api.say("ignored", _ToneSynth()))


def test_say_completes_after_the_utterance_has_played(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """`say` spans the playback, not the (near-instant) queueing of the audio.

    The tone synthesizes in microseconds and the daemon's `appsrc` queues it without
    pacing, so only the bridge's completion wait can make a 1 s utterance take 1 s.
    """
    requires_caps(live_api, "audio")
    api, _caps = live_api

    start = time.monotonic()
    asyncio.run(api.say("ignored", _ToneSynth(seconds=1.0)))
    assert time.monotonic() - start >= 1.0


def _head_deviation_deg(
    start: npt.NDArray[np.float64], pose: npt.NDArray[np.float64]
) -> float:
    """Rotation angle (degrees) between two 4x4 head poses."""
    from reachy_mini.utils.interpolation import delta_angle_between_mat_rot

    return float(np.degrees(delta_angle_between_mat_rot(start[:3, :3], pose[:3, :3])))


async def _still_head_pose(api: ReachyMiniApi) -> npt.NDArray[np.float64]:
    """The head pose once the head has stopped moving (a previous sway may be decaying)."""
    robot: Any = api.robot
    pose = await asyncio.to_thread(robot.get_current_head_pose)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        previous, pose = pose, await asyncio.to_thread(robot.get_current_head_pose)
        if _head_deviation_deg(previous, pose) < 0.05:
            break
    return pose


async def _peak_deviation_during_loud_say(
    api: ReachyMiniApi, start: npt.NDArray[np.float64]
) -> float:
    """Play a loud 1.5 s tone through `say`, sampling the head; return the peak deviation.

    The tone (amplitude 0.25, about -12 dBFS) is well above the wobbler's -35 dBFS
    voice-on threshold. On the sim the motors are always enabled, so any composed sway
    shows in the reported head pose.
    """
    robot: Any = api.robot
    say = asyncio.create_task(
        api.say("ignored", _ToneSynth(seconds=1.5, amplitude=0.25))
    )
    peak = 0.0
    while not say.done():
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        peak = max(peak, _head_deviation_deg(start, pose))
        await asyncio.sleep(0.05)
    await say
    return peak


def test_breathing_moves_the_head_and_breathing_off_holds_it(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """specs/motion.md: with presence and breathing on, the idle move visibly breathes
    (slow breaths on the z axis, with random rests between them); `set_breathing(False)` holds the head still afterwards."""
    requires_caps(live_api, "motion")
    api, _caps = live_api
    robot: Any = api.robot

    async def sample_z(seconds: float) -> float:
        zs: list[float] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            zs.append(float(pose[2, 3]))
            await asyncio.sleep(0.1)
        return max(zs) - min(zs)

    async def scenario() -> tuple[float, float]:
        await api.set_motors_state("enabled")
        await asyncio.sleep(1.0)
        # long enough to always contain a whole breath, wherever the sample starts
        breathing_range = await sample_z(BREATH_S + BREATH_REST_S[1] + 1.0)
        await api.set_breathing(False)
        await asyncio.sleep(BLEND_S + 0.5)
        still_range = await sample_z(3.0)
        await api.set_breathing(True)
        return breathing_range, still_range

    breathing_range, still_range = asyncio.run(scenario())
    print(
        f"\n[e2e] breathing z range {breathing_range:.4f} m, still {still_range:.4f} m"
    )
    assert breathing_range >= 0.002
    assert still_range < 0.001


def test_wobbling_is_on_by_default_and_sways_the_head(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """Out of the box, speech sways the head, which then returns to rest on its own.

    The fixture's api uses the default config, so wobbling is on without any toggle —
    the same for this tone as for real TTS. With wobbling still on, the head comes back
    near its starting orientation once the audio ends (the motors' own dynamics: about a
    second on the sim, hence the polled deadline). "Near" is 3 deg: the check is that the
    sway ends, and hardware can settle a degree or two off after it.
    """
    requires_caps(live_api, "audio", "motion")
    api, _caps = live_api
    assert api.wobbling is True
    robot: Any = api.robot

    async def scenario() -> tuple[float, float]:
        await api.set_breathing(False)  # isolate the wobble from breathing's own sway
        await asyncio.sleep(BLEND_S + 0.5)
        try:
            start = await _still_head_pose(api)
            peak = await _peak_deviation_during_loud_say(api, start)
            deadline = time.monotonic() + 3.0
            while True:
                pose = await asyncio.to_thread(robot.get_current_head_pose)
                settled = _head_deviation_deg(start, pose)
                if settled < 3.0 or time.monotonic() > deadline:
                    return peak, settled
                await asyncio.sleep(0.05)
        finally:
            await api.set_breathing(True)

    peak, settled = asyncio.run(scenario())
    print(f"\n[e2e] wobble on: peak {peak:.2f} deg, settled {settled:.2f} deg")
    assert peak > 1.0, f"head did not sway (peak deviation {peak:.2f} deg)"
    assert settled < 3.0, f"head did not return to rest (deviation {settled:.2f} deg)"


def test_wobbling_off_keeps_the_head_still_while_audio_plays(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """With wobbling off, the same loud tone leaves the head where it was.

    The sway this tone drives peaks around 11 deg on the sim; with the mode off the head
    must stay within 0.5 deg. Wobbling is restored afterwards, since the module-scoped
    fixture is shared and on by default.
    """
    requires_caps(live_api, "audio", "motion")
    api, _caps = live_api

    async def scenario() -> float:
        await api.set_breathing(False)  # isolate stillness from breathing's own sway
        await asyncio.sleep(BLEND_S + 0.5)
        try:
            start = await _still_head_pose(api)
            await api.set_wobbling(False)
            try:
                return await _peak_deviation_during_loud_say(api, start)
            finally:
                await api.set_wobbling(True)
        finally:
            await api.set_breathing(True)

    peak = asyncio.run(scenario())
    print(f"\n[e2e] wobble off: peak {peak:.2f} deg")
    assert peak < 0.5, f"head moved with wobbling off (peak deviation {peak:.2f} deg)"


def test_say_with_real_tts_speaks_through_the_robot(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """Real TTS end-to-end: `TTSEngineSynthesizer` (ElevenLabs) → speaker.

    Exercises the full real path the tone test can't: tts-engine synthesis over the
    network, the push→pull queue-bridge sink, int16→float32, and the 44.1 kHz→16 kHz
    resample. Gated on `ELEVENLABS_API_KEY` (skips cleanly without a key) and `audio`.
    On the headfull-viewer sim you should hear the phrase; assert it completes.
    """
    require_env("ELEVENLABS_API_KEY")
    requires_caps(live_api, "audio")
    api, _caps = live_api

    voice_id = os.environ.get("REACHY_MINI_E2E_TTS_VOICE_ID", _DEFAULT_TTS_VOICE_ID)
    synth = TTSEngineSynthesizer(
        {
            "module": {
                "type": "elevenlabs",
                "api_key_env": "ELEVENLABS_API_KEY",
                "voice_id": voice_id,
            }
        }
    )
    # The real ElevenLabs module emits 44.1 kHz, so the say sink resamples to 16 kHz.
    assert synth.sample_rate == 44100
    asyncio.run(api.say("Hello, I am Reachy Mini.", synth))


def test_gravity_compensation_dispatches_over_the_live_path(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """`set_motors_state("gravity_compensation")` reaches a daemon that supports it.

    Gated on `gravity_compensation`: hardware whose daemon runs the Placo kinematics engine
    (`reachy-mini[placo_kinematics]`). On the default engine the daemon rejects the mode and
    closes the client connection, which would fail every later test in the module.
    """
    requires_caps(live_api, "motion", "gravity_compensation")
    api, _caps = live_api

    async def scenario() -> tuple[str, str]:
        original = await api.get_motors_state()
        await api.set_motors_state("gravity_compensation")
        mode = await api.get_motors_state()
        await api.set_motors_state(original)  # restore
        return original, mode

    original, mode = asyncio.run(scenario())
    print(f"\n[e2e] gravity compensation: original={original!r}, read back={mode!r}")
    assert mode in {"enabled", "disabled", "gravity_compensation"}


def test_gravity_compensation_is_refused_off_placo_and_the_connection_survives(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """On a robot daemon without Placo, the api refuses the mode instead of sending it.

    Sent, the daemon would reject it by closing this client's connection. The guard raises
    `GravityCompensationUnsupportedError` first, so the motor state still reads back
    afterwards over the same connection. Skips where there is nothing to refuse: a daemon
    that supports the mode, or a simulation (which ignores motor modes).
    """
    requires_caps(live_api, "motion")
    api, caps = live_api
    if "gravity_compensation" in caps:
        pytest.skip("the daemon supports gravity compensation; nothing to refuse")
    status = api.robot.client.get_status()
    if status.simulation_enabled or status.mockup_sim_enabled:
        pytest.skip("a simulation ignores motor modes; the api sends them unchecked")

    async def scenario() -> tuple[str, str]:
        before = await api.get_motors_state()
        with pytest.raises(GravityCompensationUnsupportedError):
            await api.set_motors_state("gravity_compensation")
        return before, await api.get_motors_state()

    before, after = asyncio.run(scenario())
    assert after == before


def _require_emotions_library() -> None:
    """Make sure the client-side emotions library is in the local HuggingFace cache.

    Cache hit, else download (a one-time cost), else skip (offline).
    """
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
        except Exception as exc:  # noqa: BLE001  (offline / fetch failure)
            pytest.skip(f"emotions dataset not cached and download failed: {exc}")


def test_play_emotion_plays_a_real_move(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """Actually play an emotion: enumerate the library, then move the robot.

    The daemon preloads the datasets in the background, so on a fresh machine the
    client-side emotions library may not be in the local HuggingFace cache yet. This opt-in live test **downloads it on a
    cache miss** (a one-time cost) so it genuinely exercises the move, skipping only when
    the dataset truly can't be fetched (offline). On the headfull-viewer sim you should
    see the robot perform the move.
    """
    requires_caps(live_api, "motion")
    api, _caps = live_api
    _require_emotions_library()

    async def scenario() -> tuple[str, float, float]:
        from reachy_mini_bridge.motion import NEUTRAL_ANTENNAS

        names = await api.list_emotions()
        assert names, "emotions library loaded but empty"
        await api.set_motors_state("enabled")
        await api.play_emotion(names[0])  # completes only if the move actually played
        await asyncio.sleep(1.5)  # the idle move eases the head back to neutral
        robot: Any = api.robot
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        _joints, antennas = await asyncio.to_thread(robot.get_current_joint_positions)
        deviation = np.abs(np.asarray(antennas) - NEUTRAL_ANTENNAS)
        return names[0], float(np.linalg.norm(pose[:3, 3])), float(deviation.max())

    played, translation, antenna_deviation = asyncio.run(scenario())
    print(
        f"\n[e2e] played emotion: {played!r}, back to neutral: "
        f"translation {translation:.4f} m, antenna deviation {antenna_deviation:.3f} rad"
    )
    assert translation < 0.008
    assert antenna_deviation < 0.1


def test_cancelled_emotion_stops_motion_and_sound(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """specs/api.md "Cancellation": cancelling `play_emotion` 3 s into `dance2` returns
    at once, the joints are still afterwards (no sound left driving the wobbler), and
    the local backend's playbin is cleared. Measured before the fix: the sound played
    its remaining 15 s and the head kept swaying 0.1–0.2 rad per half second."""
    requires_caps(live_api, "motion")
    api, _caps = live_api
    _require_emotions_library()
    robot = api.robot
    assert isinstance(robot, ReachyMini)

    async def scenario() -> tuple[float, float, object]:
        await api.set_motors_state("enabled")
        await api.set_wobbling(True)
        # The hold keeps the joints still after the return blend; breathing would
        # otherwise still be moving them when we sample (specs/motion.md).
        await api.set_breathing(False)
        try:
            task = asyncio.create_task(api.play_emotion("dance2"))
            await asyncio.sleep(3.0)  # long enough to see the dance and hear its sound
            t0 = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            latency = time.monotonic() - t0
            # The head reaches its last target: the cancel drops the primary at once,
            # but the idle move then blends in from wherever it caught the head
            # (BLEND_S), so settling takes a bit longer than before the motion loop.
            await asyncio.sleep(BLEND_S + 1.0)
            samples: list[npt.NDArray[np.float64]] = []
            t1 = time.monotonic()
            while time.monotonic() - t1 < 2.0:
                head, antennas = await asyncio.to_thread(
                    robot.get_current_joint_positions
                )
                samples.append(np.array(list(head) + list(antennas), dtype=np.float64))
                await asyncio.sleep(0.05)
            stacked = np.stack(samples)
            travel = float((stacked.max(axis=0) - stacked.min(axis=0)).max())
            playbin = getattr(robot.media.audio, "_playbin", "not-local")
            return latency, travel, playbin
        finally:
            await api.set_breathing(True)

    latency, travel, playbin = asyncio.run(scenario())
    print(
        f"\n[e2e] cancel latency {latency * 1000:.0f} ms, joint travel after {travel:.4f} rad"
    )
    assert latency < 0.1
    assert travel < 0.02, f"joints still moving after the cancel: {travel:.4f} rad"
    if playbin != "not-local":
        assert playbin is None


def test_camera_frame_delivers_a_frame(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """A live camera frame comes back from `get_camera_frame` as a BGR image.

    Gated on `camera`, which the fixture probes true only where a GL context is
    available — the headfull sim viewer (`REACHY_MINI_E2E_SIM_VIEWER=1`) or a real
    robot. So this **skips** on the headless sim / CI and runs where the camera exists,
    driving the public API rather than reaching into `robot.media`.
    """
    requires_caps(live_api, "camera")
    api, _caps = live_api
    frame = asyncio.run(api.get_camera_frame())
    assert frame is not None, "camera probed but get_camera_frame() returned None"
    assert frame.ndim == 3 and frame.shape[2] == 3, (
        f"expected HxWx3 BGR, got {frame.shape}"
    )
    assert frame.size > 0


# --- attention / gaze: tracking a face in the sim ---------------------------------------
#
# Runs where the harness probed `camera` (the viewer sim) and `faces` (the bridge's test
# scene, which every harness-spawned sim runs: specs/sim_scene.md); skips elsewhere — the
# headless sim renders no camera, a robot has no scriptable face. The portrait plane goes
# through the daemon's real pipeline (render → GStreamer → YuNet → tracking aim → IK) on
# the bridge's sim daemon launcher, whose corrections make tracking converge on the face
# (specs/sim_daemon.md). So these tests check how the head moves and where it settles:
# toward the face, past it once by a bounded amount and never oscillating, onto the yaw
# the face's position implies, with the tracked face at the image centre. Watch the viewer: the head turns onto the portrait and
# follows it, and breathes again once it is gone.
#
# `live_api` is module-scoped, so each test re-arms tracking at its start
# (`stop_head_tracking()` then `start_head_tracking()`): its attention grace timer and
# `engaged` / `watching` state start fresh.

FACE = "face"
# Upstream recentres the head this long after the last detection
# (docs/upstream-head-tracking-after-face-loss.md); the api's grace period runs on top.
DAEMON_LOST_TIMEOUT_S = 2.0
LATERAL_M = 0.15
# Where the head settles.
YAW_TOLERANCE_DEG = 3.0
# Turning onto a face, upstream's tracking swings once past it and creeps back: the aim is
# a detection a few frames old added to the present head pose. Measured on the viewer sim:
# 3–8° following a face that moves, 7–9.5° onto one that appears 18° away — against the
# ~45° the head settled off the face before the tracker's intrinsics were corrected. The
# head never swings back past the face (no oscillation).
OVERSHOOT_MAX_DEG = 12.0
# The settled pitch for a face that only moves sideways (it stays at the same height).
PITCH_TOLERANCE_DEG = 3.0
# The tracked face's normalised image position once centred (|x|, |y| in [-1, 1]).
CENTRED = 0.1
NEUTRAL_THRESHOLD_DEG = 5.0
# An emotion's choreography under tracking, well above breathing's own sway.
MOVE_THRESHOLD_DEG = 5.0


def _face_at(lateral: float) -> tuple[float, float, float]:
    x, _y, z = DEFAULT_FACE_POS
    return (x, lateral, z)


def _expected_yaw_deg(lateral: float) -> float:
    """The eye camera is on the head's forward axis, so it looks at the face when the
    head's heading from its pivot (the world origin) does: 18.4° at ±0.15 m."""
    return math.degrees(math.atan2(lateral, DEFAULT_FACE_POS[0]))


def _yaw_pitch_deg(pose: Any) -> tuple[float, float]:
    r = np.asarray(pose)
    yaw = math.degrees(math.atan2(r[1, 0], r[0, 0]))
    pitch = math.degrees(math.asin(-float(np.clip(r[2, 0], -1.0, 1.0))))
    return yaw, pitch


def _angle_from_neutral_deg(pose: Any) -> float:
    """The head's rotation angle from the identity pose, in degrees."""
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


class _Track:
    """The head's path onto a face: every (yaw, pitch) sampled until it held still."""

    def __init__(
        self, where: str, start_yaw: float, expected_yaw: float, face: Any
    ) -> None:
        self.where = where
        self.start_yaw = start_yaw
        self.expected_yaw = expected_yaw
        self.samples: list[tuple[float, float]] = []
        self.face = face

    @property
    def yaw(self) -> float:
        return self.samples[-1][0]

    @property
    def pitch(self) -> float:
        return self.samples[-1][1]

    def _past_face(self) -> list[float]:
        """Each sample's yaw beyond the expected one, positive in the direction the head
        had to turn (all zero when the face did not move sideways)."""
        if abs(self.expected_yaw - self.start_yaw) < YAW_TOLERANCE_DEG:
            return [0.0 for _ in self.samples]
        direction = math.copysign(1.0, self.expected_yaw - self.start_yaw)
        return [(y - self.expected_yaw) * direction for y, _ in self.samples]

    @property
    def overshoot_deg(self) -> float:
        """How far the head swung past the face on its way there."""
        return max(0.0, *self._past_face())

    @property
    def swing_back_deg(self) -> float:
        """After its furthest point, how far the head came back short of the face — an
        oscillation, where a settle creeps back onto it."""
        past = self._past_face()
        peak = past.index(max(past))
        return max(0.0, *(-p for p in past[peak:]))


async def _track_onto(
    robot: Any,
    where: str,
    lateral: float,
    settle_timeout: float = 10.0,
    min_seconds: float = 2.5,
) -> _Track:
    """Sample the head until it holds still (yaw within 1° over a second, and at least
    `min_seconds` in — detection and the daemon's easing take a moment to start the
    head moving), or `settle_timeout`; then read the tracked face."""
    start_yaw, _ = _yaw_pitch_deg(await asyncio.to_thread(robot.get_current_head_pose))
    track = _Track(where, start_yaw, _expected_yaw_deg(lateral), None)
    started = time.monotonic()
    deadline = started + settle_timeout
    while True:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        track.samples.append(_yaw_pitch_deg(pose))
        recent = [y for y, _ in track.samples[-20:]]
        still = len(recent) == 20 and max(recent) - min(recent) < 1.0
        if still and time.monotonic() - started >= min_seconds:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    track.face = await asyncio.to_thread(robot.get_tracked_face, False)
    return track


def _assert_tracked(track: _Track, pitch_ahead: float | None = None) -> None:
    """The head moved onto the face: toward it, past it at most once and by a bounded
    amount, never swinging back past it, settled at the expected yaw (and, for a face that
    only moved sideways, the same pitch), with the tracked face at the image centre."""
    face = track.face
    print(
        f"\n[e2e] {track.where}: yaw {track.start_yaw:+.1f} -> {track.yaw:+.1f} deg "
        f"(expected {track.expected_yaw:+.1f}, overshoot {track.overshoot_deg:.1f}, "
        f"swing back {track.swing_back_deg:.1f}), "
        f"pitch {track.pitch:+.1f}, face ({face.x}, {face.y})"
    )
    assert track.overshoot_deg <= OVERSHOOT_MAX_DEG, (
        f"{track.where}: the head swung {track.overshoot_deg:.1f} deg past the face"
    )
    assert track.swing_back_deg <= YAW_TOLERANCE_DEG, (
        f"{track.where}: the head oscillated, swinging back {track.swing_back_deg:.1f} "
        "deg short of the face after overshooting"
    )
    assert track.yaw == pytest.approx(track.expected_yaw, abs=YAW_TOLERANCE_DEG), (
        f"{track.where}: the head settled at yaw {track.yaw:+.1f} deg, not on the face "
        f"({track.expected_yaw:+.1f})"
    )
    if pitch_ahead is not None:
        assert track.pitch == pytest.approx(pitch_ahead, abs=PITCH_TOLERANCE_DEG), (
            f"{track.where}: pitch {track.pitch:+.1f} deg, {pitch_ahead:+.1f} with the "
            "face ahead at the same height"
        )
    assert face.detected, f"{track.where}: the daemon reports no tracked face"
    assert abs(face.x) < CENTRED and abs(face.y) < CENTRED, (
        f"{track.where}: the tracked face is not at the image centre "
        f"({face.x:+.2f}, {face.y:+.2f})"
    )


async def _sample_z_range(robot: Any, seconds: float) -> float:
    zs: list[float] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        zs.append(float(pose[2, 3]))
        await asyncio.sleep(0.1)
    return max(zs) - min(zs)


@pytest.fixture
def face_scene(
    live_api: tuple[ReachyMiniApi, frozenset[str]], sim_scene: SimSceneClient
) -> Iterator[SimSceneClient]:
    """The face at its default spot, hidden — the scene's props start hidden
    (specs/sim_scene.md); a test shows it when its scenario needs it, and this fixture
    hides it again afterwards for whatever runs next."""
    requires_caps(live_api, "camera", "faces")
    sim_scene.place(FACE, DEFAULT_FACE_POS)
    sim_scene.hide(FACE)
    yield sim_scene
    sim_scene.hide(FACE)
    sim_scene.place(FACE, DEFAULT_FACE_POS)


def test_head_tracking_turns_onto_a_face_and_follows_it(
    live_api: tuple[ReachyMiniApi, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """specs/api.md "Attention / gaze": with tracking on (the config default), the head
    turns onto a face that appears ahead, then follows it 0.15 m to either side and back:
    each time toward the face, past it at most once by a bounded amount, settling at the yaw its position
    implies with the face at the image centre and the pitch unchanged."""
    api, _caps = live_api
    robot: Any = api.robot

    async def scenario() -> list[_Track]:
        await api.set_motors_state("enabled")
        await api.stop_head_tracking()
        await api.start_head_tracking()  # fresh attention loop, see the section note
        assert api.tracking, "tracking is on by default from the config"
        face_scene.show(FACE)
        tracks = [await _track_onto(robot, "face ahead", 0.0)]
        for lateral in (LATERAL_M, -LATERAL_M, 0.0):
            face_scene.place(FACE, _face_at(lateral), duration=1.0)
            tracks.append(
                await _track_onto(robot, f"face moved to y={lateral:+.2f} m", lateral)
            )
        assert api.attention == "engaged"
        return tracks

    first, *moves = asyncio.run(scenario())
    _assert_tracked(first)
    for track in moves:
        _assert_tracked(track, pitch_ahead=first.pitch)


def test_attention_hands_the_head_back_and_reengages_on_the_face(
    live_api: tuple[ReachyMiniApi, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """specs/api.md "Attention": alone, the robot idles in full. Once the face is gone for
    the daemon's recentre plus the api's grace period the attention loop reads `watching`,
    the head settles back near neutral, and it breathes again (the daemon would otherwise
    keep discarding the idle move's head targets). When a face comes back on the other
    side, attention re-engages and the head turns onto its new position — from the watch
    weight, the path that froze before the tracker's intrinsics were corrected."""
    api, _caps = live_api
    robot: Any = api.robot

    async def scenario() -> tuple[_Track, float, float, str | None, _Track]:
        await api.set_motors_state("enabled")
        await api.stop_head_tracking()
        await api.start_head_tracking()  # fresh attention loop, see the section note
        face_scene.place(FACE, _face_at(LATERAL_M))
        face_scene.show(FACE)
        assert await _wait_for(lambda: api.attention == "engaged", 6.0)
        first = await _track_onto(robot, "engaged on the face", LATERAL_M)
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
        face_scene.place(FACE, _face_at(-LATERAL_M))
        face_scene.show(FACE)
        reengaged = await _wait_for(lambda: api.attention == "engaged", 8.0)
        again = await _track_onto(
            robot, "re-engaged on the face's new position", -LATERAL_M
        )
        return first, settled, z_range, api.attention if reengaged else None, again

    first, settled, z_range, attention, again = asyncio.run(scenario())
    _assert_tracked(first)
    print(
        f"\n[e2e] settled at {settled:.1f} deg from neutral while alone, breathing z "
        f"range {z_range:.4f} m, attention after the face returned: {attention!r}"
    )
    assert settled <= NEUTRAL_THRESHOLD_DEG, (
        f"head did not settle back near neutral once alone ({settled:.1f} deg)"
    )
    assert z_range >= 0.002, "the head is not breathing after the hand-back"
    assert attention == "engaged", "attention did not re-engage once the face came back"
    _assert_tracked(again)


def test_emotion_plays_over_tracking_and_the_head_returns_to_the_face(
    live_api: tuple[ReachyMiniApi, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """specs/motion.md "Emotions through the loop": an emotion under full-weight tracking
    still shows (the api dips tracking to 0 for the move, then restores it) — the head
    moves through the choreography rather than staying pinned toward the face — and once
    the move ends the head turns back onto the still-visible face, attention engaged."""
    requires_caps(live_api, "motion")
    api, _caps = live_api
    robot: Any = api.robot
    _require_emotions_library()

    async def scenario() -> tuple[str, float, _Track]:
        await api.set_motors_state("enabled")
        await api.stop_head_tracking()
        await api.start_head_tracking()  # fresh attention loop, see the section note
        face_scene.place(FACE, _face_at(LATERAL_M))
        face_scene.show(FACE)
        assert await _wait_for(lambda: api.attention == "engaged", 6.0)
        await _track_onto(robot, "before the emotion", LATERAL_M)
        names = await api.list_emotions()
        assert names, "emotions library loaded but empty"
        emotion = names[0]  # the short move test_play_emotion_plays_a_real_move plays
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
        after = await _track_onto(robot, "after the emotion", LATERAL_M)
        return emotion, move_excursion, after

    emotion, move_excursion, after = asyncio.run(scenario())
    print(
        f"\n[e2e] {emotion!r} under tracking: head moved {move_excursion:.1f} deg through "
        "the choreography"
    )
    assert move_excursion >= MOVE_THRESHOLD_DEG, (
        f"the emotion barely moved the head ({move_excursion:.1f} deg) — it may not have played"
    )
    _assert_tracked(after)
    assert api.attention == "engaged"
