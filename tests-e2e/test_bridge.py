"""E2E tier: ReachyMiniBridge over a live daemon (specs/core/bridge.md, specs/audio/audio.md).

Target-agnostic: the shipped `live_bridge` fixture (reachy_mini_bridge.testing.fixtures,
wired in via conftest.py) resolves the target
(`REACHY_MINI_E2E_TARGET`, default `sim`), so the same tests run on the headless sim,
the headfull viewer, and a real robot — each test gated by `requires_caps(...)` on the
capability it needs and skipping cleanly where absent.

Run explicitly:
    uv run pytest tests-e2e/test_bridge.py
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_bridge.py  # + camera, tracking
    REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e/test_bridge.py

The bridge's methods are async; each test drives them with `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
from reachy_mini import ReachyMini

from reachy_mini_bridge.audio import TTSEngineSynthesizer
from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.errors import GravityCompensationUnsupportedError
from reachy_mini_bridge.face_detection import FACE_ABSENT_S, Face, FaceReport
from reachy_mini_bridge.head_tracking import (
    TRACKING_LOST_S,
    TRACKING_SWITCH_S,
    HeadTrackingReport,
)
from reachy_mini_bridge.motion import (
    BLEND_S,
    BREATH_REST_S,
    BREATH_S,
    IdleMove,
    IdleOffsets,
)
from reachy_mini_bridge.sim_displays import fetch_face_markers
from reachy_mini_bridge.testing import require_env, requires_caps
from reachy_mini_bridge.testing.sim_scene import DEFAULT_FACE_POS, SimSceneClient


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


async def _motors_state_after_set(bridge: ReachyMiniBridge, state: str) -> str:
    """Set a motor state, then read it back once the daemon status reflects it.

    The daemon's status lags a switch by a fraction of a second, so the read polls for up
    to a second; a target that ignores the state (the sim) reads back its old one.
    """
    await bridge.set_motors_state(state)
    deadline = time.monotonic() + 1.0
    while (mode := await bridge.get_motors_state()) != state:
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(0.1)
    return mode


def test_motor_state_reads_and_dispatches_over_the_live_path(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
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

    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    # goto_target is upstream-only (not on the fake); this tier is live-only.
    robot: Any = bridge.robot
    valid = {"enabled", "disabled", "gravity_compensation"}

    async def scenario() -> tuple[str, dict[str, str]]:
        original = await bridge.get_motors_state()
        # The motion loop would otherwise blend back to neutral the moment each
        # goto_target ends (specs/motion/motion.md): a caller driving the head directly needs
        # presence off for its own moves to hold.
        await bridge.set_presence(False)
        try:
            results: dict[str, str] = {}
            results["enabled"] = await _motors_state_after_set(bridge, "enabled")
            await asyncio.to_thread(
                robot.goto_target,
                head=SLEEP_HEAD_POSE,
                antennas=SLEEP_ANTENNAS_JOINT_POSITIONS,
                duration=2.0,
            )
            results["disabled"] = await _motors_state_after_set(bridge, "disabled")
            await bridge.set_motors_state("enabled")
            await asyncio.to_thread(
                robot.goto_target,
                head=INIT_HEAD_POSE,
                antennas=INIT_ANTENNAS_JOINT_POSITIONS,
                duration=1.0,
            )
            await bridge.set_motors_state(original)  # restore
        finally:
            await bridge.set_presence(True)
        return original, results

    original, results = asyncio.run(scenario())
    print(f"\n[e2e] motor states: original={original!r}, read back={results!r}")
    assert original in valid
    assert all(mode in valid for mode in results.values())


def test_real_audio_format_matches_the_fake_assumptions(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """The live daemon reports the float32 / channel / 16 kHz facts the fake hardcodes.

    This is the check the fast tier structurally cannot make: it confirms the numbers
    the `fake` backend bakes in are what a real daemon actually reports (specs/audio/audio.md
    "Background": 2 channels, float32, 16 kHz on sim and hardware).
    """
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge
    media: Any = bridge.robot.media

    assert bridge.mic_sample_rate == 16000
    assert bridge.mic_channels == media.get_input_channels()
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
        assert arr.size % bridge.mic_channels == 0
    else:
        assert arr.shape[1] == bridge.mic_channels


def test_mic_tap_yields_int16_mono_frames(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """Draining the mic tap gives non-empty int16 mono PCM; `break` stops it."""
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    async def take(n: int) -> list[bytes]:
        out: list[bytes] = []
        async for chunk in bridge.audio_input():
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
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """A tone routed through `say` completes without error (daemon accepted the audio).

    Credential-free (in-test tone synth), so it runs on sim without any TTS keys.
    """
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge
    asyncio.run(bridge.say("ignored", _ToneSynth()))


def test_say_completes_after_the_utterance_has_played(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """`say` spans the playback, not the (near-instant) queueing of the audio.

    The tone synthesizes in microseconds and the daemon's `appsrc` queues it without
    pacing, so only the bridge's completion wait can make a 1 s utterance take 1 s.
    """
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    start = time.monotonic()
    asyncio.run(bridge.say("ignored", _ToneSynth(seconds=1.0)))
    assert time.monotonic() - start >= 1.0


def _head_deviation_deg(
    start: npt.NDArray[np.float64], pose: npt.NDArray[np.float64]
) -> float:
    """Rotation angle (degrees) between two 4x4 head poses."""
    from reachy_mini.utils.interpolation import delta_angle_between_mat_rot

    return float(np.degrees(delta_angle_between_mat_rot(start[:3, :3], pose[:3, :3])))


async def _still_head_pose(bridge: ReachyMiniBridge) -> npt.NDArray[np.float64]:
    """The head pose once the head has stopped moving (a previous sway may be decaying)."""
    robot: Any = bridge.robot
    pose = await asyncio.to_thread(robot.get_current_head_pose)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        previous, pose = pose, await asyncio.to_thread(robot.get_current_head_pose)
        if _head_deviation_deg(previous, pose) < 0.05:
            break
    return pose


async def _peak_deviation_during_loud_say(
    bridge: ReachyMiniBridge, start: npt.NDArray[np.float64]
) -> float:
    """Play a loud 1.5 s tone through `say`, sampling the head; return the peak deviation.

    The tone (amplitude 0.25, about -12 dBFS) is well above the wobbler's -35 dBFS
    voice-on threshold. On the sim the motors are always enabled, so any composed sway
    shows in the reported head pose.
    """
    robot: Any = bridge.robot
    say = asyncio.create_task(
        bridge.say("ignored", _ToneSynth(seconds=1.5, amplitude=0.25))
    )
    peak = 0.0
    while not say.done():
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        peak = max(peak, _head_deviation_deg(start, pose))
        await asyncio.sleep(0.05)
    await say
    return peak


def test_breathing_moves_the_head_and_breathing_off_holds_it(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """specs/motion/motion.md: with presence and breathing on, the idle move visibly breathes
    (slow breaths on the z axis, with random rests between them); `set_idle("hold")`
    holds the head still afterwards."""
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def sample_z(seconds: float) -> float:
        zs: list[float] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            zs.append(float(pose[2, 3]))
            await asyncio.sleep(0.1)
        return max(zs) - min(zs)

    async def scenario() -> tuple[float, float]:
        await bridge.set_motors_state("enabled")
        await asyncio.sleep(1.0)
        # long enough to always contain a whole breath, wherever the sample starts
        breathing_range = await sample_z(BREATH_S + BREATH_REST_S[1] + 1.0)
        await bridge.set_idle("hold")
        await asyncio.sleep(BLEND_S + 0.5)
        still_range = await sample_z(3.0)
        await bridge.set_idle("breathing")
        return breathing_range, still_range

    breathing_range, still_range = asyncio.run(scenario())
    print(
        f"\n[e2e] breathing z range {breathing_range:.4f} m, still {still_range:.4f} m"
    )
    assert breathing_range >= 0.002
    assert still_range < 0.001


class _Lift(IdleMove):
    """A custom idle move: the head held 8 mm above neutral."""

    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=8.0)


def test_custom_idle_move_drives_the_head(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """specs/motion/motion.md "Custom idle moves": a registered `IdleMove` plays in the
    `custom` idle mode — the head rises to its offset — and leaving the mode brings the
    head back to neutral."""
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def head_z() -> float:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        return float(pose[2, 3])

    async def scenario() -> tuple[float, float, float]:
        await bridge.set_motors_state("enabled")
        await bridge.set_idle("hold")
        await asyncio.sleep(2 * BLEND_S + 1.0)
        neutral_z = await head_z()
        try:
            await bridge.set_idle_move(_Lift)
            await bridge.set_idle("custom")
            await asyncio.sleep(BLEND_S + 1.5)
            lifted_z = await head_z()
            await bridge.set_idle("hold")
            await asyncio.sleep(2 * BLEND_S + 1.0)
            back_z = await head_z()
        finally:
            await bridge.set_idle_move(None)
            await bridge.set_idle("breathing")
        return neutral_z, lifted_z, back_z

    neutral_z, lifted_z, back_z = asyncio.run(scenario())
    print(
        f"\n[e2e] custom idle: neutral z {neutral_z:.4f} m, lifted {lifted_z:.4f} m, "
        f"back {back_z:.4f} m"
    )
    assert lifted_z - neutral_z >= 0.005  # 8 mm commanded
    assert abs(back_z - neutral_z) < 0.002


def test_wobbling_is_on_by_default_and_sways_the_head(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """Out of the box, speech sways the head, which then returns to rest on its own.

    The fixture's bridge uses the default config, so wobbling is on without any toggle —
    the same for this tone as for real TTS. With wobbling still on, the head comes back
    near its starting orientation once the audio ends (the motors' own dynamics: about a
    second on the sim, hence the polled deadline). "Near" is 3 deg: the check is that the
    sway ends, and hardware can settle a degree or two off after it.
    """
    requires_caps(live_bridge, "audio", "motion")
    bridge, _caps = live_bridge
    assert bridge.wobbling is True
    robot: Any = bridge.robot

    async def scenario() -> tuple[float, float]:
        await bridge.set_idle("hold")  # isolate the wobble from breathing's own sway
        await asyncio.sleep(BLEND_S + 0.5)
        try:
            start = await _still_head_pose(bridge)
            peak = await _peak_deviation_during_loud_say(bridge, start)
            deadline = time.monotonic() + 3.0
            while True:
                pose = await asyncio.to_thread(robot.get_current_head_pose)
                settled = _head_deviation_deg(start, pose)
                if settled < 3.0 or time.monotonic() > deadline:
                    return peak, settled
                await asyncio.sleep(0.05)
        finally:
            await bridge.set_idle("breathing")

    peak, settled = asyncio.run(scenario())
    print(f"\n[e2e] wobble on: peak {peak:.2f} deg, settled {settled:.2f} deg")
    assert peak > 1.0, f"head did not sway (peak deviation {peak:.2f} deg)"
    assert settled < 3.0, f"head did not return to rest (deviation {settled:.2f} deg)"


def test_wobbling_off_keeps_the_head_still_while_audio_plays(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """With wobbling off, the same loud tone leaves the head where it was.

    The sway this tone drives peaks around 11 deg on the sim; with the mode off the head
    must stay within 0.5 deg. Wobbling is restored afterwards, since the module-scoped
    fixture is shared and on by default.
    """
    requires_caps(live_bridge, "audio", "motion")
    bridge, _caps = live_bridge

    async def scenario() -> float:
        await bridge.set_idle("hold")  # isolate stillness from breathing's own sway
        await asyncio.sleep(BLEND_S + 0.5)
        try:
            start = await _still_head_pose(bridge)
            await bridge.set_wobbling(False)
            try:
                return await _peak_deviation_during_loud_say(bridge, start)
            finally:
                await bridge.set_wobbling(True)
        finally:
            await bridge.set_idle("breathing")

    peak = asyncio.run(scenario())
    print(f"\n[e2e] wobble off: peak {peak:.2f} deg")
    assert peak < 0.5, f"head moved with wobbling off (peak deviation {peak:.2f} deg)"


def test_say_with_real_tts_speaks_through_the_robot(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """Real TTS end-to-end: `TTSEngineSynthesizer` on the local pocket model → speaker.

    Exercises the full real path the tone test can't: tts-engine synthesis (in-process,
    the model loaded from the Hugging Face cache — no key, no network once cached), the
    push→pull queue-bridge sink, int16→float32, and the 24 kHz→16 kHz resample. Gated
    on `audio` only, so it runs on every dev sync (the dev group carries `tts-pocket`).
    On the headfull-viewer sim you should hear the phrase; assert it completes.
    """
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    synth = TTSEngineSynthesizer({"module": {"type": "pocket", "voice": "george"}})
    # pocket-tts emits its model's native 24 kHz, so the say sink resamples to 16 kHz.
    assert synth.sample_rate == 24000
    asyncio.run(bridge.say("Hello, I am Reachy Mini.", synth))


def test_say_with_elevenlabs_speaks_through_the_robot(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """The cloud provider end-to-end: `TTSEngineSynthesizer` (ElevenLabs) → speaker.

    The path the pocket test doesn't cover: synthesis over the network and the
    44.1 kHz→16 kHz resample. Gated on `ELEVENLABS_API_KEY` (skips cleanly without a
    key) and `audio`.
    """
    require_env("ELEVENLABS_API_KEY")
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    synth = TTSEngineSynthesizer(
        {
            "module": {
                "type": "elevenlabs",
                "api_key_env": "ELEVENLABS_API_KEY",
                # A public voice used throughout tts-engine's own docs.
                "voice_id": "JBFqnCBsd6RMkjVDRZzb",
            }
        }
    )
    # The real ElevenLabs module emits 44.1 kHz, so the say sink resamples to 16 kHz.
    assert synth.sample_rate == 44100
    asyncio.run(bridge.say("Hello, I am Reachy Mini.", synth))


def test_say_with_gradium_speaks_through_the_robot(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """Another cloud provider end-to-end: `TTSEngineSynthesizer` (Gradium) → speaker.

    Configured at 16 kHz, the speaker rate, so it covers the path the other two
    providers' rates don't: the say sink's resample skipped on real network audio.
    Gated on `GRADIUM_API_KEY` (skips cleanly without a key) and `audio`.
    """
    require_env("GRADIUM_API_KEY")
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    synth = TTSEngineSynthesizer(
        {
            "module": {
                "type": "gradium",
                "api_key_env": "GRADIUM_API_KEY",
                # "Alex", from the flagship catalog, used in tts-engine's own docs.
                "voice_id": "91EdXxJDbWICDBgz",
                "sample_rate": 16000,
            }
        }
    )
    # At `sample_rate: 16000` the module emits the speaker rate: nothing to resample.
    assert synth.sample_rate == 16000
    asyncio.run(bridge.say("Hello, I am Reachy Mini.", synth))


def test_gravity_compensation_dispatches_over_the_live_path(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """`set_motors_state("gravity_compensation")` reaches a daemon that supports it.

    Gated on `gravity_compensation`: hardware whose daemon runs the Placo kinematics engine
    (`reachy-mini[placo_kinematics]`). On the default engine the daemon rejects the mode and
    closes the client connection, which would fail every later test in the module.
    """
    requires_caps(live_bridge, "motion", "gravity_compensation")
    bridge, _caps = live_bridge

    async def scenario() -> tuple[str, str]:
        original = await bridge.get_motors_state()
        await bridge.set_motors_state("gravity_compensation")
        mode = await bridge.get_motors_state()
        await bridge.set_motors_state(original)  # restore
        return original, mode

    original, mode = asyncio.run(scenario())
    print(f"\n[e2e] gravity compensation: original={original!r}, read back={mode!r}")
    assert mode in {"enabled", "disabled", "gravity_compensation"}


def test_gravity_compensation_is_refused_off_placo_and_the_connection_survives(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """On a robot daemon without Placo, the bridge refuses the mode instead of sending it.

    Sent, the daemon would reject it by closing this client's connection. The guard raises
    `GravityCompensationUnsupportedError` first, so the motor state still reads back
    afterwards over the same connection. Skips where there is nothing to refuse: a daemon
    that supports the mode, or a simulation (which ignores motor modes).
    """
    requires_caps(live_bridge, "motion")
    bridge, caps = live_bridge
    if "gravity_compensation" in caps:
        pytest.skip("the daemon supports gravity compensation; nothing to refuse")
    status = bridge.robot.client.get_status()
    if status.simulation_enabled or status.mockup_sim_enabled:
        pytest.skip("a simulation ignores motor modes; the bridge sends them unchecked")

    async def scenario() -> tuple[str, str]:
        before = await bridge.get_motors_state()
        with pytest.raises(GravityCompensationUnsupportedError):
            await bridge.set_motors_state("gravity_compensation")
        return before, await bridge.get_motors_state()

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
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """Actually play an emotion: enumerate the library, then move the robot.

    The daemon preloads the datasets in the background, so on a fresh machine the
    client-side emotions library may not be in the local HuggingFace cache yet. This opt-in live test **downloads it on a
    cache miss** (a one-time cost) so it genuinely exercises the move, skipping only when
    the dataset truly can't be fetched (offline). On the headfull-viewer sim you should
    see the robot perform the move.
    """
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    _require_emotions_library()

    async def scenario() -> tuple[str, float, float]:
        from reachy_mini_bridge.motion import NEUTRAL_ANTENNAS

        names = await bridge.list_emotions()
        assert names, "emotions library loaded but empty"
        await bridge.set_motors_state("enabled")
        # The hold is the still neutral: after the emotion the loop blends back to it
        # and stays there, so the "back to neutral" sample is deterministic. Under
        # breathing (the default idle) the antennas roam 10-25 deg outward after a
        # random 0.4-2.5 s rest, and whether the sample lands in the rest or in the
        # roam was a coin toss (specs/motion/motion.md "Antenna tracks").
        await bridge.set_idle("hold")
        try:
            await bridge.play_emotion(
                names[0]
            )  # completes only if the move actually played
            await asyncio.sleep(BLEND_S + 1.0)  # the return blend eases back to neutral
            robot: Any = bridge.robot
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            _joints, antennas = await asyncio.to_thread(
                robot.get_current_joint_positions
            )
        finally:
            await bridge.set_idle("breathing")
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
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
) -> None:
    """specs/core/bridge.md "Cancellation": cancelling `play_emotion` 3 s into `dance2` returns
    at once, the joints are still afterwards (no sound left driving the wobbler), and
    the local backend's playbin is cleared. Measured before the fix: the sound played
    its remaining 15 s and the head kept swaying 0.1–0.2 rad per half second."""
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    _require_emotions_library()
    robot = bridge.robot
    assert isinstance(robot, ReachyMini)

    async def scenario() -> tuple[float, float, object]:
        await bridge.set_motors_state("enabled")
        await bridge.set_wobbling(True)
        # The hold keeps the joints still after the return blend; breathing would
        # otherwise still be moving them when we sample (specs/motion/motion.md).
        await bridge.set_idle("hold")
        try:
            task = asyncio.create_task(bridge.play_emotion("dance2"))
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
            await bridge.set_idle("breathing")

    latency, travel, playbin = asyncio.run(scenario())
    print(
        f"\n[e2e] cancel latency {latency * 1000:.0f} ms, joint travel after {travel:.4f} rad"
    )
    assert latency < 0.1
    assert travel < 0.02, f"joints still moving after the cancel: {travel:.4f} rad"
    if playbin != "not-local":
        assert playbin is None


def test_camera_frame_delivers_a_frame(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]],
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
# `live_bridge` is module-scoped, so each test re-arms tracking at its start
# (`stop_head_tracking()` then `start_head_tracking()`), withdrawing any aim left over.

LATERAL_M = 0.15
# Where the head settles: its yaw averaged over the last SETTLE_WINDOW_S of the track.
# The head keeps breathing around the aim (its roaming toned down to a quarter, about
# ±2° of yaw on a slow random walk that a 2 s mean does not cancel), so the yaw is a
# coarse check that the head is on the face; the precise one is the face at the image
# centre (CENTRED, below), which is what tracking guarantees.
YAW_TOLERANCE_DEG = 5.0
SETTLE_WINDOW_S = 2.0
# Turning onto a face, the head may swing once past it and creep back. Measured on the
# viewer sim: 3–9.5° with upstream's daemon-side tracking; 0–3° with the bridge's
# tracker, which aims against the reported head pose of the frame's time with the delay
# estimated online (specs/motion/head_tracking.md "The aim"). The head never swings back past
# the face (no oscillation).
OVERSHOOT_MAX_DEG = 12.0
# The settled pitch for a face that only moves sideways (it stays at the same height).
PITCH_TOLERANCE_DEG = 3.0
# The tracked face's normalised image position once centred (|x|, |y| in [-1, 1]): within
# about 3° of the image centre on the sim's 112° camera. Measured at settle on the viewer
# sim: |x|, |y| under 0.025 — the head breathing around the aim moves it a little.
CENTRED = 0.05
# How far off neutral the head may sit once it has been handed back. The idle move
# roams in roll/pitch/yaw (specs/motion/motion.md "The moves"): it averages ~6 deg from neutral
# and reaches 10.3 deg at the corner of its envelope, so this is measured as a mean over
# a window rather than one sample. A head still locked on the face sits at the face's
# 18.4 deg, well clear of the threshold.
NEUTRAL_THRESHOLD_DEG = 12.0
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
        self.times: list[float] = []
        self.face = face  # the target face of `bridge.faces` once settled, or None
        self.delay_s: float | None = None  # the tracker's delay estimate once settled
        self.frame_note = ""  # the camera frame's age and whether it carried a pose

    def _settled(self) -> list[tuple[float, float]]:
        """The samples of the track's last SETTLE_WINDOW_S."""
        end = self.times[-1]
        return [
            s
            for s, t in zip(self.samples, self.times, strict=True)
            if t >= end - SETTLE_WINDOW_S
        ]

    @property
    def yaw(self) -> float:
        """Where the head settled: the mean yaw over the last SETTLE_WINDOW_S."""
        settled = self._settled()
        return sum(y for y, _ in settled) / len(settled)

    @property
    def pitch(self) -> float:
        settled = self._settled()
        return sum(p for _, p in settled) / len(settled)

    @property
    def settle_s(self) -> float:
        """How long the head was sampled before it held still (or the timeout)."""
        return self.times[-1] - self.times[0]

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
    bridge: ReachyMiniBridge,
    where: str,
    lateral: float,
    settle_timeout: float = 10.0,
    min_seconds: float = 2.5,
    expected_yaw_deg: float | None = None,
) -> _Track:
    """Sample the head until it holds still (yaw within 1° over a second, and at least
    `min_seconds` in — detection and the gaze layer's fade take a moment to start the
    head moving), or `settle_timeout`; then read the followed face from `bridge.faces`
    (the one whose track_id `bridge.head_tracking` reports). The expected yaw is a face
    at `lateral` at the default distance, unless given."""
    robot: Any = bridge.robot
    start_yaw, _ = _yaw_pitch_deg(await asyncio.to_thread(robot.get_current_head_pose))
    expected = (
        _expected_yaw_deg(lateral) if expected_yaw_deg is None else expected_yaw_deg
    )
    track = _Track(where, start_yaw, expected, None)
    started = time.monotonic()
    deadline = started + settle_timeout
    while True:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        track.samples.append(_yaw_pitch_deg(pose))
        track.times.append(time.monotonic())
        recent = [y for y, _ in track.samples[-20:]]
        still = len(recent) == 20 and max(recent) - min(recent) < 1.0
        if still and time.monotonic() - started >= min_seconds:
            break
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    track.face = _followed_face(bridge)
    tracker = (
        bridge._tracker
    )  # the estimate the gaze tests print, for the spec's numbers
    track.delay_s = None if tracker is None else tracker.delay_s
    frame = bridge.camera.latest()
    track.frame_note = (
        "no frame"
        if frame is None
        else f"frame age {time.monotonic() - frame.ts:.3f} s, "
        f"pose stamped: {frame.head_pose is not None}, "
        f"report pose: {bridge.faces.value.head_pose is not None}"
    )
    return track


def _followed_face(bridge: ReachyMiniBridge) -> Face | None:
    """The face of `bridge.faces` the head follows, by `bridge.head_tracking`'s track_id."""
    followed = bridge.head_tracking.value.track_id
    return next((f for f in bridge.faces.value.faces if f.track_id == followed), None)


def _assert_tracked(track: _Track, pitch_ahead: float | None = None) -> None:
    """The head moved onto the face: toward it, past it at most once and by a bounded
    amount, never swinging back past it, settled at the expected yaw on average (and, for
    a face that only moved sideways, the same pitch), with the tracked face at the image
    centre."""
    face = track.face
    print(
        f"\n[e2e] {track.where}: yaw {track.start_yaw:+.1f} -> {track.yaw:+.1f} deg "
        f"(expected {track.expected_yaw:+.1f}, overshoot {track.overshoot_deg:.1f}, "
        f"swing back {track.swing_back_deg:.1f}), "
        f"pitch {track.pitch:+.1f}, settled in {track.settle_s:.1f} s, face {face}, "
        f"delay estimate {track.delay_s}, {track.frame_note}"
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
    assert face is not None, f"{track.where}: the bridge reports no tracked face"
    assert abs(face.x) < CENTRED and abs(face.y) < CENTRED, (
        f"{track.where}: the tracked face is not at the image centre "
        f"({face.x:+.2f}, {face.y:+.2f})"
    )


async def _sample_idle(robot: Any, seconds: float) -> tuple[float, float]:
    """The head's z range and its mean angle from neutral over `seconds` — the two
    things the idle move shows: it breathes on z, and it roams a few degrees about
    neutral rather than holding one heading (specs/motion/motion.md "The moves")."""
    zs: list[float] = []
    angles: list[float] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        zs.append(float(pose[2, 3]))
        angles.append(_angle_from_neutral_deg(pose))
        await asyncio.sleep(0.1)
    return max(zs) - min(zs), sum(angles) / len(angles)


@pytest.fixture
def face_scene(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], sim_scene: SimSceneClient
) -> Iterator[SimSceneClient]:
    """The scene with nobody in view — its pool of portraits hidden
    (specs/testing/sim_scene.md "A pool of portraits"); a test spawns the portraits its
    scenario needs, and this fixture clears them again afterwards for whatever runs
    next."""
    requires_caps(live_bridge, "camera", "faces")
    sim_scene.clear()
    yield sim_scene
    sim_scene.clear()


def test_faces_report_someone_appearing_and_leaving(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
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

    appeared, left = asyncio.run(scenario())
    print(f"\n[e2e] appeared: {appeared}\n[e2e] left: {left}")
    assert appeared.source == "yunet"
    assert appeared.faces[0].size > 0.05  # the shipped detector reports sizes
    assert all(-1.0 <= v <= 1.0 for v in (appeared.faces[0].x, appeared.faces[0].y))
    assert left.faces == ()


def test_faces_report_two_portraits_spawned_side_by_side(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
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
            left = face_scene.spawn(_face_at(LATERAL_M))
            face_scene.spawn(_face_at(-LATERAL_M))
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

    both, one = asyncio.run(scenario())
    print(f"\n[e2e] two portraits: {both}\n[e2e] one despawned: {one}")
    xs = sorted(face.x for face in both.faces)
    assert xs[0] < 0.0 < xs[1], "the two portraits are not on either side of the image"
    assert len(one.faces) == 1


def test_head_tracking_turns_onto_a_face_and_follows_it(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
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

    async def scenario() -> list[_Track]:
        await bridge.set_motors_state("enabled")
        await bridge.stop_head_tracking()
        await bridge.start_head_tracking(focus=True)
        assert bridge.tracking, "tracking is on by default from the config"
        assert bridge.tracking_focus
        face = face_scene.spawn(DEFAULT_FACE_POS)
        tracks = [await _track_onto(bridge, "face ahead", 0.0)]
        followed = bridge.head_tracking.value.track_id
        assert followed is not None and followed == tracks[0].face.track_id  # type: ignore[union-attr]
        # the report's frame is the one its faces were found in: the box crops the face
        report = bridge.faces.value
        assert report.frame is not None and report.frame_id == report.frame.frame_id
        x, y, w, h = (round(v) for v in report.faces[0].bbox)
        crop = report.frame.image[max(y, 0) : y + h, max(x, 0) : x + w]
        assert crop.size > 0 and crop.std() > 5.0  # a face, not a flat patch
        for lateral in (LATERAL_M, -LATERAL_M, 0.0):
            face_scene.place(face, _face_at(lateral), duration=1.0)
            # A face that glides over 1 s is followed with a lag the head creeps out of
            # slowly; give the tail time before calling the head settled.
            tracks.append(
                await _track_onto(
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

    first, *moves = asyncio.run(scenario())
    _assert_tracked(first)
    for track in moves:
        _assert_tracked(track, pitch_ahead=first.pitch)


def test_attention_hands_the_head_back_and_reengages_on_the_face(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """specs/core/bridge.md "Attention": alone, the robot idles in full. Once the face has been
    gone for the tracker's loss timeout `attention` reads `watching`, the gaze layer
    fades out, and the head settles back near neutral, breathing. When a face comes back
    on the other side, attention re-engages and the head turns onto its new position."""
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def scenario() -> tuple[_Track, float, float, str | None, _Track]:
        await bridge.set_motors_state("enabled")
        await bridge.stop_head_tracking()
        await bridge.start_head_tracking()
        face = face_scene.spawn(_face_at(LATERAL_M))
        assert await _wait_for(lambda: bridge.attention == "engaged", 6.0)
        first = await _track_onto(bridge, "engaged on the face", LATERAL_M)
        face_scene.despawn(face)
        hand_back = TRACKING_LOST_S + BLEND_S + 4.0
        assert await _wait_for(lambda: bridge.attention == "watching", hand_back), (
            f"attention still {bridge.attention!r} {hand_back:.0f}s after the face left"
        )
        # long enough to always contain a whole breath, wherever the sample starts;
        # `settled` is the mean over that window, since the idle move roams
        z_range, settled = await _sample_idle(robot, BREATH_S + BREATH_REST_S[1] + 1.0)
        face_scene.spawn(_face_at(-LATERAL_M))
        reengaged = await _wait_for(lambda: bridge.attention == "engaged", 8.0)
        again = await _track_onto(
            bridge, "re-engaged on the face's new position", -LATERAL_M
        )
        return first, settled, z_range, bridge.attention if reengaged else None, again

    first, settled, z_range, attention, again = asyncio.run(scenario())
    _assert_tracked(first)
    print(
        f"\n[e2e] settled {settled:.1f} deg from neutral on average while alone, "
        f"breathing z "
        f"range {z_range:.4f} m, attention after the face returned: {attention!r}"
    )
    assert settled <= NEUTRAL_THRESHOLD_DEG, (
        f"head did not settle back into the idle move once alone ({settled:.1f} deg "
        "from neutral on average, the face was at "
        f"{abs(_expected_yaw_deg(LATERAL_M)):.1f})"
    )
    assert z_range >= 0.002, "the head is not breathing after the hand-back"
    assert attention == "engaged", "attention did not re-engage once the face came back"
    _assert_tracked(again)


def test_emotion_plays_over_tracking_and_the_head_returns_to_the_face(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """specs/motion/motion.md "Emotions through the loop": an emotion under full-weight tracking
    plays as recorded (the motion loop leaves the gaze layer out of a primary) — the head
    moves through the choreography rather than staying pinned toward the face — and once
    the move ends the layer fades back in and the head turns back onto the still-visible
    face, attention engaged."""
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot
    _require_emotions_library()

    async def scenario() -> tuple[str, float, _Track]:
        await bridge.set_motors_state("enabled")
        await bridge.stop_head_tracking()
        await bridge.start_head_tracking()
        face_scene.spawn(_face_at(LATERAL_M))
        assert await _wait_for(lambda: bridge.attention == "engaged", 6.0)
        await _track_onto(bridge, "before the emotion", LATERAL_M)
        names = await bridge.list_emotions()
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
            await bridge.play_emotion(emotion)
        finally:
            sampler.cancel()
        move_excursion = (max(angles) - min(angles)) if len(angles) > 1 else 0.0
        after = await _track_onto(bridge, "after the emotion", LATERAL_M)
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
    assert bridge.attention == "engaged"


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
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
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
                assert await _wait_for(
                    lambda: len(_marker_positions(bridge)) == 1, 5.0
                ), f"no marker for the portrait at {place}"
                found.append(await _mean_marker(bridge))
        finally:
            await bridge.set_idle("breathing")
            await bridge.start_head_tracking()  # the module's default state
        return found

    found = asyncio.run(scenario())
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
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """A marker is placed with the head pose its frame was taken from, so a portrait
    that stands still keeps its marker where it is while an emotion swings the head
    under tracking — it does not ride along with the head."""
    requires_caps(live_bridge, "motion", "face_markers")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot
    _require_emotions_library()

    async def scenario() -> tuple[npt.NDArray[np.float64], float]:
        await bridge.set_motors_state("enabled")
        await bridge.stop_head_tracking()
        await bridge.start_head_tracking()
        face_scene.spawn(DEFAULT_FACE_POS)
        assert await _wait_for(lambda: bridge.attention == "engaged", 6.0)
        await _track_onto(bridge, "before the emotion", 0.0)
        emotion = (await bridge.list_emotions())[0]
        samples: list[tuple[float, float, float]] = []
        angles: list[float] = []

        async def sample() -> None:
            while True:
                positions = await asyncio.to_thread(_marker_positions, bridge)
                if len(positions) == 1:
                    samples.append(positions[0])
                pose = await asyncio.to_thread(robot.get_current_head_pose)
                angles.append(_angle_from_neutral_deg(pose))
                await asyncio.sleep(0.05)

        sampler = asyncio.create_task(sample())
        try:
            await bridge.play_emotion(emotion)
        finally:
            sampler.cancel()
        return np.array(samples), max(angles) - min(angles)

    samples, excursion = asyncio.run(scenario())
    assert len(samples) >= 10, f"only {len(samples)} marker samples during the emotion"
    spread = np.linalg.norm(samples - samples.mean(axis=0), axis=1)
    print(
        f"\n[e2e] head moved {excursion:.1f} deg; marker over {len(samples)} samples: "
        f"mean {samples.mean(axis=0).round(3)}, spread max {spread.max():.3f} m, "
        f"p90 {np.percentile(spread, 90):.3f} m"
    )
    assert excursion >= MOVE_THRESHOLD_DEG, "the emotion barely moved the head"
    assert spread.max() <= MARKER_STILL_M


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
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
) -> None:
    bridge, _caps = live_bridge

    async def scenario() -> tuple[_Track, int, int | None]:
        await _prepare(bridge)
        face_scene.spawn((FAR_X, -LATERAL_M, FACE_Z))
        face_scene.spawn((NEAR_X, LATERAL_M, FACE_Z))
        await _both_in_view(bridge)
        near = max(bridge.faces.value.faces, key=lambda f: f.size)
        await bridge.start_head_tracking()
        track = await _track_onto(
            bridge,
            "the nearer of two faces",
            LATERAL_M,
            expected_yaw_deg=_yaw_to(NEAR_X, LATERAL_M),
        )
        return track, near.track_id, bridge.head_tracking.value.track_id

    track, near_id, followed = asyncio.run(scenario())
    _assert_tracked(track)
    assert followed == near_id


def test_a_nearer_face_arriving_does_not_take_the_head(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
) -> None:
    bridge, _caps = live_bridge
    far_yaw = _yaw_to(FAR_X, -LATERAL_M)

    async def scenario() -> tuple[_Track, _Track, int | None, int | None, list[Any]]:
        await _prepare(bridge)
        face_scene.spawn((FAR_X, -LATERAL_M, FACE_Z))
        await bridge.start_head_tracking()
        # The head starts where the previous test left it, up to 40 deg from this face:
        # about 2.5 s of turn, then the 2 s the settled yaw is averaged over.
        first = await _track_onto(
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
        after = await _track_onto(
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

    first, after, followed, still, woken = asyncio.run(scenario())
    _assert_tracked(first)
    _assert_tracked(after)
    assert still == followed and woken == []


def test_a_face_hidden_briefly_is_waited_for_and_followed_again(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
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
        followed_name = face_scene.spawn(_face_at(LATERAL_M))
        await _track_onto(bridge, "the first face", LATERAL_M)
        followed = bridge.head_tracking.value.track_id
        face_scene.spawn(_face_at(-LATERAL_M))
        await _both_in_view(bridge)
        before, _ = _yaw_pitch_deg(await asyncio.to_thread(robot.get_current_head_pose))
        changes = _Changes(bridge)
        face_scene.despawn(followed_name)
        yaws: list[float] = []
        deadline = time.monotonic() + TRACKING_SWITCH_S / 2
        while time.monotonic() < deadline:
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            yaws.append(_yaw_pitch_deg(pose)[0])
            await asyncio.sleep(0.05)
        again = face_scene.spawn(_face_at(LATERAL_M))
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

    before, yaws, followed, after, woken = asyncio.run(scenario())
    print(
        f"\n[e2e] hold: yaw {before:+.1f} deg before, "
        f"{min(yaws):+.1f}..{max(yaws):+.1f} while the face was gone"
    )
    assert all(abs(y - before) < 3.0 for y in yaws), "the head left the vanished face"
    assert after == followed and woken == []


def test_a_face_gone_for_good_hands_over_to_the_other_then_the_head_is_released(
    live_bridge: tuple[ReachyMiniBridge, frozenset[str]], face_scene: SimSceneClient
) -> None:
    """The switch, then the loss: the followed face despawned, the head holds for about
    `TRACKING_SWITCH_S`, then follows the other face (one change naming it) and turns
    onto it; that one despawned too, attention reads `watching` after `TRACKING_LOST_S`."""
    bridge, _caps = live_bridge

    async def scenario() -> tuple[float, _Track, int, list[Any], float]:
        await _prepare(bridge)
        await bridge.start_head_tracking()
        first = face_scene.spawn(_face_at(LATERAL_M))
        await _track_onto(bridge, "the first face", LATERAL_M)
        followed = bridge.head_tracking.value.track_id
        second = face_scene.spawn(_face_at(-LATERAL_M))
        await _both_in_view(bridge)
        other = next(f for f in bridge.faces.value.faces if f.track_id != followed)
        changes = _Changes(bridge)
        face_scene.despawn(first)
        switched_after = await _until(
            lambda: bridge.head_tracking.value.track_id == other.track_id,
            TRACKING_SWITCH_S + 3.0,
        )
        track = await _track_onto(bridge, "switched to the other face", -LATERAL_M)
        face_scene.despawn(second)
        lost_after = await _until(
            lambda: bridge.head_tracking.value.attention == "watching",
            TRACKING_LOST_S + 3.0,
        )
        changes.stop()
        return switched_after, track, other.track_id, changes.woken, lost_after

    switched_after, track, other_id, woken, lost_after = asyncio.run(scenario())
    print(
        f"\n[e2e] switched after {switched_after:.2f} s, released after {lost_after:.2f} s"
    )
    _assert_tracked(track)
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
