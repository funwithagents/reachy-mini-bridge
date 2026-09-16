"""E2E tier: ReachyMiniApi over a live daemon (specs/api.md, specs/audio.md).

Target-agnostic: the shipped `live_api` fixture (reachy_mini_bridge.testing.fixtures,
wired in via conftest.py) resolves the target
(`REACHY_MINI_E2E_TARGET`, default `sim`), so the same tests run on the headless sim,
the headfull viewer, and a real robot — each test gated by `requires_caps(...)` on the
capability it needs and skipping cleanly where absent.

Run explicitly:
    uv run pytest tests-e2e/test_api.py
    REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e/test_api.py

The api's methods are async; each test drives them with `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge.api import ReachyMiniApi
from reachy_mini_bridge.audio import TTSEngineSynthesizer
from reachy_mini_bridge.errors import GravityCompensationUnsupportedError
from reachy_mini_bridge.testing import require_env, requires_caps

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


def test_real_audio_format_matches_the_fake_assumptions(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """The live daemon reports the float32 / channel / 16 kHz facts the fake hardcodes.

    This is the check the fast tier structurally cannot make: it confirms the numbers
    the `fake` backend bakes in are what a real daemon actually reports. Partially closes
    specs/audio.md open question 1 (rates + dtype; the physical channel count still
    needs real hardware).
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


def test_wobbling_is_on_by_default_and_sways_the_head(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """Out of the box, speech sways the head, which then returns to rest on its own.

    The fixture's api uses the default config, so wobbling is on without any toggle —
    the same for this tone as for real TTS. With wobbling still on, the head comes back
    to its starting orientation once the audio ends (the motors' own dynamics: about a
    second on the sim, hence the polled deadline).
    """
    requires_caps(live_api, "audio", "motion")
    api, _caps = live_api
    assert api.wobbling is True
    robot: Any = api.robot

    async def scenario() -> tuple[float, float]:
        start = await _still_head_pose(api)
        peak = await _peak_deviation_during_loud_say(api, start)
        deadline = time.monotonic() + 3.0
        while True:
            pose = await asyncio.to_thread(robot.get_current_head_pose)
            settled = _head_deviation_deg(start, pose)
            if settled < 1.0 or time.monotonic() > deadline:
                return peak, settled
            await asyncio.sleep(0.05)

    peak, settled = asyncio.run(scenario())
    print(f"\n[e2e] wobble on: peak {peak:.2f} deg, settled {settled:.2f} deg")
    assert peak > 1.0, f"head did not sway (peak deviation {peak:.2f} deg)"
    assert settled < 1.0, f"head did not return to rest (deviation {settled:.2f} deg)"


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
        start = await _still_head_pose(api)
        await api.set_wobbling(False)
        try:
            return await _peak_deviation_during_loud_say(api, start)
        finally:
            await api.set_wobbling(True)

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


def test_motor_state_reads_and_dispatches_over_the_live_path(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """`get_motors_state` reads a valid mode and each `set_motors_state` reaches the daemon.

    The e2e value here is that the read (a real daemon status round-trip) and each set
    dispatch work over the network — not that a given target *honors* a state. Notably
    the sim daemon ignores every motor-state change: `disabled` keeps reporting `enabled`
    (confirmed on both the headless and the headfull-viewer sim). That is why this asserts
    validity, not equality; the fast tier pins the exact dispatch→state mapping
    deterministically on the fake. Gravity compensation has its own capability-gated test
    below: sending it to a daemon that can't hold it drops the connection.
    """
    requires_caps(live_api, "motion")
    api, _caps = live_api
    valid = {"enabled", "disabled", "gravity_compensation"}

    async def scenario() -> tuple[str, dict[str, str]]:
        original = await api.get_motors_state()
        results: dict[str, str] = {}
        for state in ("enabled", "disabled"):
            await api.set_motors_state(state)
            results[state] = await api.get_motors_state()
        await api.set_motors_state(original)  # restore
        return original, results

    original, results = asyncio.run(scenario())
    print(f"\n[e2e] motor states: original={original!r}, read back={results!r}")
    assert original in valid
    assert all(mode in valid for mode in results.values())


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

    async def scenario() -> str:
        names = await api.list_emotions()
        assert names, "emotions library loaded but empty"
        await api.set_motors_state("enabled")
        await api.play_emotion(names[0])  # completes only if the move actually played
        return names[0]

    played = asyncio.run(scenario())
    print(f"\n[e2e] played emotion: {played!r}")


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
