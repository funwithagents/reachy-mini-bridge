"""Functional tests for the audio helpers and the media session (specs/audio.md).

Driven on the ``fake`` backend with a trivial in-test ``SpeechSynthesizer`` (a tone) —
no ``reachy_mini``, no ``tts_engine``, no device. Async code runs via ``asyncio.run``,
matching the fast tier's no-plugin convention (see tests/test_client.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge.audio import (
    MediaSession,
    downmix_to_mono,
    float32_to_int16,
    int16_to_float32,
)
from reachy_mini_bridge.client import FakeReachyMini


class _ToneSynth:
    """Minimal SpeechSynthesizer: emits `chunks` blocks of a constant float32 tone."""

    def __init__(self, sample_rate: int, *, chunks: int = 3, block: int = 800) -> None:
        self._sample_rate = sample_rate
        self._chunks = chunks
        self._block = block

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        for _ in range(self._chunks):
            yield np.full(self._block, 0.25, dtype=np.float32)


def _pushed_frames(robot: FakeReachyMini) -> list[int]:
    return [
        args["frames"]
        for name, args in robot.commands
        if name == "media.push_audio_sample"
    ]


# --- conversion helpers ------------------------------------------------------------


def test_int16_float32_round_trip() -> None:
    original = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
    back = float32_to_int16(int16_to_float32(original))
    # Round-trips within one quantization step (the 32768/32767 asymmetry).
    assert np.all(np.abs(back.astype(np.int32) - original.astype(np.int32)) <= 1)


def test_float32_to_int16_clips_out_of_range() -> None:
    out = float32_to_int16(np.array([2.0, -2.0, 1.0, -1.0], dtype=np.float32))
    assert out.tolist() == [32767, -32767, 32767, -32767]


def test_downmix_to_mono_averages_channels() -> None:
    stereo = np.array([[1.0, 0.0], [0.5, -0.5], [0.2, 0.2]], dtype=np.float32)
    mono = downmix_to_mono(stereo, 2)
    assert mono.shape == (3,)
    assert np.allclose(mono, [0.5, 0.0, 0.2])


def test_downmix_to_mono_passthrough_when_single_channel() -> None:
    mono_in = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    assert np.allclose(downmix_to_mono(mono_in, 1), mono_in)


# --- say sink ----------------------------------------------------------------------


def test_say_pushes_matched_rate_without_resampling() -> None:
    robot = FakeReachyMini()  # speaker reports 16 kHz (see client.py)

    async def run() -> None:
        async with MediaSession(robot) as session:
            await session.say("hi", _ToneSynth(16000, chunks=3, block=800))

    asyncio.run(run())
    frames = _pushed_frames(robot)
    # Rate matches, so each 800-sample chunk is pushed 1:1, no resampler tail.
    assert frames == [800, 800, 800]


def test_say_resamples_when_rates_differ() -> None:
    robot = FakeReachyMini()  # speaker is 16 kHz; synth is 8 kHz -> upsample x2

    async def run() -> None:
        async with MediaSession(robot) as session:
            await session.say("hi", _ToneSynth(8000, chunks=3, block=800))

    asyncio.run(run())
    frames = _pushed_frames(robot)
    assert frames, "expected at least one pushed chunk"
    # 3 * 800 input samples at ratio 2.0 -> ~4800 out; the sinc filter's one-time
    # warmup latency (~288 samples) makes the first chunk short, so allow slack below
    # the ideal. The point is it clearly upsampled (well above the 2400 input length).
    total = sum(frames)
    assert 2400 < total <= 4800
    assert 4800 - total <= 512  # bounded, one-time warmup only


def test_say_fans_mono_out_to_speaker_channels() -> None:
    # The fake speaker reports 2 channels; a pushed chunk stays frame-counted (shape[0])
    # but must be widened to stereo. Assert the pushed array is 2-D with 2 columns.
    robot = FakeReachyMini()
    captured: list[np.ndarray] = []
    original = robot.media.push_audio_sample

    def spy(data: npt.NDArray[np.float32]) -> None:
        captured.append(np.asarray(data))
        original(data)

    robot.media.push_audio_sample = spy  # type: ignore[method-assign]

    async def run() -> None:
        async with MediaSession(robot) as session:
            await session.say("hi", _ToneSynth(16000, chunks=1, block=800))

    asyncio.run(run())
    assert captured, "nothing pushed"
    assert captured[0].shape == (800, 2)
    # Both channels carry the same mono signal (a copy, not silence in one).
    assert np.allclose(captured[0][:, 0], captured[0][:, 1])
    assert np.all(captured[0] > 0)


def test_media_session_opens_and_tears_down_pipeline() -> None:
    robot = FakeReachyMini()

    async def run() -> None:
        async with MediaSession(robot):
            pass

    asyncio.run(run())
    names = [name for name, _ in robot.commands]
    assert names.index("media.start_recording") < names.index("media.stop_recording")
    assert "media.start_playing" in names and "media.stop_playing" in names


def test_media_session_applies_audio_config_when_given() -> None:
    robot = FakeReachyMini()
    profile = {"noise_suppression": "high"}

    async def run() -> None:
        async with MediaSession(robot, audio_config=profile):
            pass

    asyncio.run(run())
    applied = [
        args for name, args in robot.commands if name == "audio.apply_audio_config"
    ]
    assert len(applied) == 1
    assert applied[0]["config"] is profile


# --- mic tap -----------------------------------------------------------------------


async def _take(stream: AsyncIterator[bytes], n: int) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in stream:
        out.append(chunk)
        if len(out) == n:
            break
    return out


def test_audio_input_yields_mono_int16_and_break_stops() -> None:
    robot = FakeReachyMini()  # capture is float32 (160, 2)

    async def run() -> list[bytes]:
        async with MediaSession(robot) as session:
            return await _take(session.audio_input(), 2)

    chunks = asyncio.run(run())
    assert len(chunks) == 2
    # 160 frames, mono, int16 -> 320 bytes; whole number of int16 samples.
    assert all(len(c) == 160 * 2 for c in chunks)


def test_audio_input_raw_is_interleaved_stereo() -> None:
    robot = FakeReachyMini()

    async def run() -> tuple[list[bytes], list[bytes]]:
        async with MediaSession(robot) as session:
            mono = await _take(session.audio_input(mono=True), 1)
            raw = await _take(session.audio_input(mono=False), 1)
            return mono, raw

    mono, raw = asyncio.run(run())
    # Raw keeps both channels, so it is exactly twice the mono byte length.
    assert len(raw[0]) == 2 * len(mono[0])


def test_audio_input_mono_downmix_uses_real_sample_values() -> None:
    robot = FakeReachyMini()
    # A known stereo frame: left 1.0, right 0.0 -> mono average 0.5.
    robot.media.get_audio_sample = lambda: np.full((4, 2), [1.0, 0.0], dtype=np.float32)  # type: ignore[method-assign]

    async def run() -> bytes:
        async with MediaSession(robot) as session:
            return (await _take(session.audio_input(), 1))[0]

    chunk = asyncio.run(run())
    samples = np.frombuffer(chunk, dtype=np.int16)
    assert samples.shape == (4,)
    assert np.all(samples == float32_to_int16(np.array([0.5], np.float32))[0])


def test_mic_properties_read_from_daemon_getters() -> None:
    robot = FakeReachyMini()
    session = MediaSession(robot)
    assert session.mic_sample_rate == 16000
    assert session.mic_channels == 2


def test_clear_player_flushes_speaker() -> None:
    robot = FakeReachyMini()
    MediaSession(robot).clear_player()
    assert any(name == "audio.clear_player" for name, _ in robot.commands)


def test_say_missing_synthesizer_is_a_type_the_caller_can_supply() -> None:
    # The session's say always takes an explicit synth; the "no synth configured"
    # error lives at the api layer (see tests/test_api.py). Here just prove a plain
    # object without the protocol shape is rejected at call time.
    robot = FakeReachyMini()

    async def run() -> None:
        async with MediaSession(robot) as session:
            with pytest.raises(AttributeError):
                await session.say("hi", object())  # type: ignore[arg-type]

    asyncio.run(run())
