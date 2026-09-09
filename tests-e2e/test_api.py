"""E2E tier: ReachyMiniApi over a live daemon (specs/api.md, specs/audio.md).

Target-agnostic: the `live_api` fixture (conftest.py) resolves the target
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
from support import require_env, requires_caps

from reachy_mini_bridge.api import ReachyMiniApi
from reachy_mini_bridge.audio import TTSEngineSynthesizer

# A public ElevenLabs voice used throughout tts-engine's own docs; override with
# REACHY_MINI_E2E_TTS_VOICE_ID for an account-specific voice.
_DEFAULT_TTS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"


class _ToneSynth:
    """Credential-free SpeechSynthesizer: a short 16 kHz mono tone (no TTS backend)."""

    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        for _ in range(5):
            yield np.full(1600, 0.1, dtype=np.float32)  # 100 ms blocks


def test_real_audio_format_matches_the_fake_assumptions(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """The live daemon reports the float32 / channel / 16 kHz facts the fake hardcodes.

    This is the check the fast tier structurally cannot make: it confirms the numbers
    the `fake` backend bakes in are what a real daemon actually reports. Closes
    specs/audio.md open question 1.
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
    """`get_motors_state` reads a valid mode and every `set_motors_state` reaches the daemon.

    The e2e value here is that the read (a real daemon status round-trip) and each set
    dispatch work over the network — not that a given target *honors* a state. Notably
    the sim daemon ignores every motor-state change: `disabled` and `gravity_compensation`
    both keep reporting `enabled` (confirmed on both the headless and the headfull-viewer
    sim). That is why this asserts validity, not equality; the fast tier pins the exact
    dispatch→state mapping deterministically on the fake, and honoring is expected only
    on real hardware.
    """
    requires_caps(live_api, "motion")
    api, _caps = live_api
    valid = {"enabled", "disabled", "gravity_compensation"}

    async def scenario() -> tuple[str, dict[str, str]]:
        original = await api.get_motors_state()
        results: dict[str, str] = {}
        for state in ("enabled", "gravity_compensation", "disabled"):
            await api.set_motors_state(state)
            results[state] = await api.get_motors_state()
        await api.set_motors_state(original)  # restore
        return original, results

    original, results = asyncio.run(scenario())
    assert original in valid
    assert all(mode in valid for mode in results.values())


def test_play_emotion_plays_a_real_move(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """Actually play an emotion: enumerate the library, then move the robot.

    The sim daemon launches `--no-preload-datasets`, so the client-side emotions library
    may not be in the local HuggingFace cache. This opt-in live test **downloads it on a
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
