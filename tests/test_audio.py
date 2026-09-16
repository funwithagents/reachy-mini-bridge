"""Functional tests for the audio helpers and the media session (specs/audio.md).

Driven on the ``fake`` backend with a trivial in-test ``SpeechSynthesizer`` (a tone) —
no ``reachy_mini``, no ``tts_engine``, no device. Async code runs via ``asyncio.run``,
matching the fast tier's no-plugin convention (see tests/test_robot.py).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge.audio import (
    MediaSession,
    _PlaybackTracker,
    downmix_to_mono,
    float32_to_int16,
    int16_to_float32,
)
from reachy_mini_bridge.errors import BridgeError
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


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


class _SilentSynth:
    """A SpeechSynthesizer whose utterance produces no audio at all."""

    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        return
        yield  # an async generator that yields nothing


class _StallingSynth:
    """Yields one chunk, then waits forever (synthesis still in flight when cancelled)."""

    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        yield np.full(800, 0.25, dtype=np.float32)
        await asyncio.Event().wait()


class _FailingSynth:
    """Yields one chunk, then the synthesizer fails mid-stream."""

    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        yield np.full(800, 0.25, dtype=np.float32)
        raise RuntimeError("boom")


def _command_names(robot: FakeReachyMini) -> list[str]:
    return [name for name, _ in robot.commands]


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
    robot = FakeReachyMini()  # speaker reports 16 kHz (see fake_reachy_mini.py)

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


# --- say completion and early-exit flush ---------------------------------------------


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_playback_tracker_accumulates_contiguous_pushes() -> None:
    clock = _Clock(100.0)
    tracker = _PlaybackTracker(16000, now=clock)
    tracker.push(8000)
    tracker.push(8000)
    # Two contiguous half-second pushes, plus the fixed tail margin.
    assert tracker.remaining() == pytest.approx(1.0 + 0.1)
    clock.t = 100.6
    assert tracker.remaining() == pytest.approx(0.4 + 0.1)
    clock.t = 200.0
    assert tracker.remaining() == 0.0


def test_playback_tracker_reanchors_after_the_queue_drains() -> None:
    clock = _Clock(0.0)
    tracker = _PlaybackTracker(16000, now=clock)
    tracker.push(1600)  # 0.1 s, long finished by t = 5
    clock.t = 5.0
    tracker.push(1600)  # starts from now, not from the stale end
    assert tracker.remaining() == pytest.approx(0.1 + 0.1)


def test_playback_tracker_is_zero_when_nothing_was_pushed() -> None:
    tracker = _PlaybackTracker(16000, now=_Clock(3.0))
    tracker.push(0)
    assert tracker.remaining() == 0.0


def test_say_returns_only_after_the_utterance_has_played() -> None:
    robot = FakeReachyMini()

    async def run() -> float:
        async with MediaSession(robot) as session:
            start = time.monotonic()
            await session.say("hi", _ToneSynth(16000, chunks=4, block=800))  # 0.2 s
            return time.monotonic() - start

    assert asyncio.run(run()) >= 0.2
    assert "audio.clear_player" not in _command_names(robot)


def test_say_with_no_audio_returns_at_once() -> None:
    robot = FakeReachyMini()

    async def run() -> float:
        async with MediaSession(robot) as session:
            start = time.monotonic()
            await session.say("hi", _SilentSynth())
            return time.monotonic() - start

    assert asyncio.run(run()) < 0.05
    names = _command_names(robot)
    assert "media.push_audio_sample" not in names
    assert "audio.clear_player" not in names


def _clear_after_first_push(robot: FakeReachyMini) -> bool:
    names = _command_names(robot)
    return "audio.clear_player" in names and names.index(
        "media.push_audio_sample"
    ) < names.index("audio.clear_player")


def test_cancelled_say_flushes_the_speaker() -> None:
    robot = FakeReachyMini()

    async def run() -> None:
        async with MediaSession(robot) as session:
            task = asyncio.create_task(session.say("hi", _StallingSynth()))
            while not _pushed_frames(robot):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert _clear_after_first_push(robot)


def test_cancel_during_the_completion_wait_flushes_the_speaker() -> None:
    robot = FakeReachyMini()

    async def run() -> None:
        async with MediaSession(robot) as session:
            synth = _ToneSynth(16000, chunks=4, block=800)  # 0.2 s
            task = asyncio.create_task(session.say("hi", synth))
            while len(_pushed_frames(robot)) < 4:  # synthesis done; say is now waiting
                await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert _clear_after_first_push(robot)


def test_synthesizer_failure_flushes_the_speaker_and_propagates() -> None:
    robot = FakeReachyMini()

    async def run() -> None:
        async with MediaSession(robot) as session:
            with pytest.raises(RuntimeError, match="boom"):
                await session.say("hi", _FailingSynth())

    asyncio.run(run())
    assert _clear_after_first_push(robot)


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


def _raise(message: str) -> Callable[..., None]:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError(message)

    return fail


def test_failed_open_unwinds_what_started(monkeypatch: pytest.MonkeyPatch) -> None:
    robot = FakeReachyMini()
    monkeypatch.setattr(robot.media, "start_playing", _raise("no speaker"))

    async def run() -> None:
        async with MediaSession(robot):
            pass

    with pytest.raises(RuntimeError, match="no speaker"):
        asyncio.run(run())
    # Recording had started, so it is stopped; playback never started, so it is not.
    assert _command_names(robot) == ["media.start_recording", "media.stop_recording"]


def test_failed_audio_config_unwinds_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = FakeReachyMini()
    monkeypatch.setattr(robot.media.audio, "apply_audio_config", _raise("bad profile"))

    async def run() -> None:
        async with MediaSession(robot, audio_config={"agc": 1}):
            pass

    with pytest.raises(RuntimeError, match="bad profile"):
        asyncio.run(run())
    # Unwound in reverse: playback stops before recording.
    assert _command_names(robot) == [
        "media.start_recording",
        "media.start_playing",
        "media.stop_playing",
        "media.stop_recording",
    ]


def test_exit_stops_playback_even_if_stopping_recording_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = FakeReachyMini()
    monkeypatch.setattr(robot.media, "stop_recording", _raise("stuck mic"))
    session = MediaSession(robot)

    async def run() -> None:
        async with session:
            pass

    with pytest.raises(RuntimeError, match="stuck mic"):
        asyncio.run(run())
    assert "media.stop_playing" in _command_names(robot)
    # The session reads as closed even though a stop raised.
    with pytest.raises(BridgeError):
        session.audio_input()


def test_say_requires_an_open_session() -> None:
    robot = FakeReachyMini()
    session = MediaSession(robot)
    synth = _ToneSynth(16000, chunks=1, block=160)

    async def run() -> None:
        with pytest.raises(BridgeError, match="say"):
            await session.say("hi", synth)
        async with session:
            pass
        with pytest.raises(BridgeError, match="say"):
            await session.say("hi", synth)

    asyncio.run(run())
    assert "media.push_audio_sample" not in _command_names(robot)


def test_audio_input_requires_an_open_session() -> None:
    session = MediaSession(FakeReachyMini())
    # Raised at call time, before any iteration.
    with pytest.raises(BridgeError, match="audio_input"):
        session.audio_input()


def test_double_open_raises() -> None:
    robot = FakeReachyMini()
    session = MediaSession(robot)

    async def run() -> None:
        async with session:
            with pytest.raises(BridgeError):
                async with session:
                    pass

    asyncio.run(run())
    names = _command_names(robot)
    assert names.count("media.start_recording") == 1
    assert names[-2:] == ["media.stop_playing", "media.stop_recording"]


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


def test_mic_tap_ends_when_the_session_closes() -> None:
    robot = FakeReachyMini()

    async def run() -> int:
        chunks = 0

        async def drain(stream: AsyncIterator[bytes]) -> None:
            nonlocal chunks
            async for _ in stream:
                chunks += 1

        session = MediaSession(robot)
        async with session:
            task = asyncio.create_task(drain(session.audio_input()))
            await asyncio.sleep(0.05)
        await asyncio.wait_for(task, 1.0)  # finishes on its own; no timeout
        return chunks

    assert asyncio.run(run()) > 0


def test_mic_tap_waits_out_missing_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    robot = FakeReachyMini()
    frame = np.full((4, 2), [1.0, 0.0], dtype=np.float32)
    samples: list[npt.NDArray[np.float32] | None] = [None, None, None, frame]
    monkeypatch.setattr(robot.media, "get_audio_sample", lambda: samples.pop(0))

    async def run() -> bytes:
        async with MediaSession(robot) as session:
            return (await _take(session.audio_input(), 1))[0]

    chunk = asyncio.run(run())
    assert chunk == float32_to_int16(np.full(4, 0.5, dtype=np.float32)).tobytes()


def test_mic_tap_does_not_busy_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    robot = FakeReachyMini()
    calls = 0

    def empty() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(robot.media, "get_audio_sample", empty)

    async def run() -> None:
        async with MediaSession(robot) as session:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(_take(session.audio_input(), 1), 0.2)

    asyncio.run(run())
    # ~20 reads at a 10 ms poll; an unthrottled loop makes thousands. Upper bound only,
    # so a slow machine cannot make it flaky.
    assert calls < 50
