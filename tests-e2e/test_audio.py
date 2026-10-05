"""E2E tier — audio over a live daemon (specs/audio/audio.md, specs/core/bridge.md
"Cancellation"): the daemon's format against the fake's assumptions, the mic tap, `say`
spanning its playback — interrupted by a newer `say`, cancelled —, `play_sound` spanning
its file, the TTS providers end to end (pocket needs no key; ElevenLabs and Gradium are
key-gated), and upstream's head wobbling on the speaker path.

Gated on `audio` (every target; the wobbling tests on `motion` too).

Run explicitly:
    uv run pytest tests-e2e/test_audio.py -rs
    REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_audio.py -rs
    REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e/test_audio.py -rs

Every test awaits the bridge through `live_bridge.run(...)`, the harness's one event
loop (specs/testing/testing_support.md "Public surface"); the daemon is the run's, the
bridge session this module's (specs/testing/testing.md "Daemon lifecycle")."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge.audio import TTSEngineSynthesizer
from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.errors import (
    SpeechInterruptedError,
)
from reachy_mini_bridge.motion import (
    BLEND_S,
)
from reachy_mini_bridge.testing import LiveBridge, require_env, requires_caps


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
    live_bridge: LiveBridge,
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
    live_bridge: LiveBridge,
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

    chunks = live_bridge.run(take(3))
    assert len(chunks) == 3
    for chunk in chunks:
        assert len(chunk) > 0
        assert len(chunk) % 2 == 0  # whole int16 samples (mono)


def test_say_completes_after_the_utterance_has_played(
    live_bridge: LiveBridge,
) -> None:
    """`say` spans the playback, not the (near-instant) queueing of the audio.

    The tone synthesizes in microseconds and the daemon's `appsrc` queues it without
    pacing, so only the bridge's completion wait can make a 1 s utterance take 1 s.
    """
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    start = time.monotonic()
    live_bridge.run(bridge.say("ignored", _ToneSynth(seconds=1.0)))
    assert time.monotonic() - start >= 1.0


def test_a_new_say_interrupts_the_one_playing(live_bridge: LiveBridge) -> None:
    """The newest `say` wins (specs/audio/audio.md "TTS out"): the one in flight ends
    at once with SpeechInterruptedError, and the new one plays in full."""
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    async def scenario() -> tuple[float, float]:
        first = asyncio.create_task(bridge.say("ignored", _ToneSynth(seconds=2.0)))
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        second = asyncio.create_task(bridge.say("ignored", _ToneSynth(seconds=0.5)))
        with pytest.raises(SpeechInterruptedError):
            await first
        interrupted_after = time.monotonic() - t0
        await second
        return interrupted_after, time.monotonic() - t0

    interrupted_after, total = live_bridge.run(scenario())
    assert interrupted_after < 0.3
    assert 0.5 <= total < 1.5


def test_play_sound_spans_the_file_and_a_cancel_stops_it(
    live_bridge: LiveBridge,
) -> None:
    """`play_sound` is a spanning verb (specs/audio/audio.md "Sound files"): it
    completes when the file has been heard — `go_sleep.wav`, an SDK asset, lasts
    3.6 s — and a cancel returns at once, the file stopped, the session usable."""
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    async def scenario() -> tuple[float, float]:
        t0 = time.monotonic()
        await bridge.play_sound("go_sleep.wav")
        heard_after = time.monotonic() - t0
        task = asyncio.create_task(bridge.play_sound("confused1.wav"))  # 5.7 s
        await asyncio.sleep(1.0)
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cancelled_after = time.monotonic() - t0
        await bridge.say("ignored", _ToneSynth(seconds=0.3))  # the session plays on
        return heard_after, cancelled_after

    heard_after, cancelled_after = live_bridge.run(scenario())
    assert 3.6 <= heard_after < 4.2
    assert cancelled_after < 0.1


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


def test_wobbling_is_on_by_default_and_sways_the_head(
    live_bridge: LiveBridge,
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

    peak, settled = live_bridge.run(scenario())
    print(f"\n[e2e] wobble on: peak {peak:.2f} deg, settled {settled:.2f} deg")
    assert peak > 1.0, f"head did not sway (peak deviation {peak:.2f} deg)"
    assert settled < 3.0, f"head did not return to rest (deviation {settled:.2f} deg)"


def test_wobbling_off_keeps_the_head_still_while_audio_plays(
    live_bridge: LiveBridge,
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

    peak = live_bridge.run(scenario())
    print(f"\n[e2e] wobble off: peak {peak:.2f} deg")
    assert peak < 0.5, f"head moved with wobbling off (peak deviation {peak:.2f} deg)"


def test_cancelled_say_stops_the_sound_and_the_next_say_works(
    live_bridge: LiveBridge,
) -> None:
    """specs/core/bridge.md "Cancellation", the spanning verb `say`: cancelling it returns
    at once and the speaker goes quiet — the queued audio flushed from the daemon, not
    only `clear_player` called (the fast tier's view) — and the next `say` plays.

    The proof that the speaker went quiet is the head: with wobbling on and the idle
    held, a loud 3 s tone sways it about 11 deg on the sim; cancelled at 1 s, the sway
    must be gone a second later, where the uncancelled tone would still be driving it.
    """
    requires_caps(live_bridge, "audio", "motion")
    bridge, _caps = live_bridge
    robot: Any = bridge.robot

    async def scenario() -> tuple[float, float, float]:
        await bridge.set_wobbling(True)
        await bridge.set_idle("hold")  # isolate the sway from breathing
        await asyncio.sleep(BLEND_S + 0.5)
        try:
            start = await _still_head_pose(bridge)
            say = asyncio.create_task(
                bridge.say("ignored", _ToneSynth(seconds=3.0, amplitude=0.25))
            )
            await asyncio.sleep(1.0)
            t0 = time.monotonic()
            say.cancel()
            with pytest.raises(asyncio.CancelledError):
                await say
            latency = time.monotonic() - t0
            # Sample the sway after the cancel: the first second is the motors' own
            # settling, the window after it must be still.
            settling: list[float] = []
            quiet: list[float] = []
            while time.monotonic() - t0 < 2.5:
                pose = await asyncio.to_thread(robot.get_current_head_pose)
                deviation = _head_deviation_deg(start, pose)
                (settling if time.monotonic() - t0 < 1.0 else quiet).append(deviation)
                await asyncio.sleep(0.05)
            await bridge.say("ignored", _ToneSynth(seconds=0.3))  # the session plays on
            return latency, max(settling), max(quiet)
        finally:
            await bridge.set_idle("breathing")

    latency, settling_peak, quiet_peak = live_bridge.run(scenario())
    print(
        f"\n[e2e] cancelled say: latency {latency * 1000:.0f} ms, head within "
        f"{settling_peak:.2f} deg while settling, {quiet_peak:.2f} deg after"
    )
    assert latency < 0.1
    assert quiet_peak < 2.0, (
        f"the head still swayed {quiet_peak:.2f} deg a second after the cancel: "
        "is the speaker still playing the cancelled utterance?"
    )


def test_say_with_real_tts_speaks_through_the_robot(
    live_bridge: LiveBridge,
) -> None:
    """Real TTS end-to-end: `TTSEngineSynthesizer` on the local pocket model → speaker.

    Exercises the full real path the tone test can't: tts-engine synthesis (in-process,
    the model loaded from the Hugging Face cache — no key, no network once cached), the
    push→pull queue-bridge sink, int16→float32, and the 24 kHz→16 kHz resample. Gated
    on `audio` and on the provider being installed: the `tts` dependency group carries
    it, default in a local sync, left out in CI (specs/testing/ci.md), where this test
    skips on the missing module rather than fail on tts-engine's ConfigError.
    On the headfull-viewer sim you should hear the phrase; assert it completes.
    """
    pytest.importorskip(
        "pocket_tts", reason="the tts dependency group is not installed"
    )
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge

    synth = TTSEngineSynthesizer({"module": {"type": "pocket", "voice": "george"}})
    # pocket-tts emits its model's native 24 kHz, so the say sink resamples to 16 kHz.
    assert synth.sample_rate == 24000
    live_bridge.run(bridge.say("Hello, I am Reachy Mini.", synth))


def test_say_with_elevenlabs_speaks_through_the_robot(
    live_bridge: LiveBridge,
) -> None:
    """The cloud provider end-to-end: `TTSEngineSynthesizer` (ElevenLabs) → speaker.

    The path the pocket test doesn't cover: synthesis over the network and the
    44.1 kHz→16 kHz resample. Gated on the provider being installed (the `tts`
    dependency group), on `ELEVENLABS_API_KEY` (skips cleanly without a key) and `audio`.
    """
    pytest.importorskip(
        "elevenlabs", reason="the tts dependency group is not installed"
    )
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
    live_bridge.run(bridge.say("Hello, I am Reachy Mini.", synth))


def test_say_with_gradium_speaks_through_the_robot(
    live_bridge: LiveBridge,
) -> None:
    """Another cloud provider end-to-end: `TTSEngineSynthesizer` (Gradium) → speaker.

    Configured at 16 kHz, the speaker rate, so it covers the path the other two
    providers' rates don't: the say sink's resample skipped on real network audio.
    Gated on the provider being installed (the `tts` dependency group), on
    `GRADIUM_API_KEY` (skips cleanly without a key) and `audio`.
    """
    pytest.importorskip("gradium", reason="the tts dependency group is not installed")
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
    live_bridge.run(bridge.say("Hello, I am Reachy Mini.", synth))
