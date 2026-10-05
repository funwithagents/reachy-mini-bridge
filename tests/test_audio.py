"""Functional tests for the audio helpers and the media session (specs/audio/audio.md).

Driven on the ``fake`` backend with a trivial in-test ``SpeechSynthesizer`` (a tone) —
no ``reachy_mini``, no ``tts_engine``, no device. Async code runs via ``asyncio.run``,
matching the fast tier's no-plugin convention (see tests/test_robot.py).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import audio as audio_module
from reachy_mini_bridge.audio import (
    MediaSession,
    _PlaybackTracker,
    cancel_safe_step,
    downmix_to_mono,
    float32_to_int16,
    int16_to_float32,
)
from reachy_mini_bridge.errors import (
    BridgeError,
    SoundInterruptedError,
    SpeechInterruptedError,
)
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


@asynccontextmanager
async def _open(session: MediaSession) -> AsyncIterator[MediaSession]:
    """``start()`` / ``stop()`` around a block — what the bridge does with the session."""
    await session.start()
    try:
        yield session
    finally:
        await session.stop()


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
        async with _open(MediaSession(robot)) as session:
            await session.say("hi", _ToneSynth(16000, chunks=3, block=800))

    asyncio.run(run())
    frames = _pushed_frames(robot)
    # Rate matches, so each 800-sample chunk is pushed 1:1, no resampler tail.
    assert frames == [800, 800, 800]


def test_say_resamples_when_rates_differ() -> None:
    robot = FakeReachyMini()  # speaker is 16 kHz; synth is 8 kHz -> upsample x2

    async def run() -> None:
        async with _open(MediaSession(robot)) as session:
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
        async with _open(MediaSession(robot)) as session:
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
        async with _open(MediaSession(robot)) as session:
            start = time.monotonic()
            await session.say("hi", _ToneSynth(16000, chunks=4, block=800))  # 0.2 s
            return time.monotonic() - start

    assert asyncio.run(run()) >= 0.2
    assert "audio.clear_player" not in _command_names(robot)


def test_say_with_no_audio_returns_at_once() -> None:
    robot = FakeReachyMini()

    async def run() -> float:
        async with _open(MediaSession(robot)) as session:
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
        async with _open(MediaSession(robot)) as session:
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
        async with _open(MediaSession(robot)) as session:
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
        async with _open(MediaSession(robot)) as session:
            with pytest.raises(RuntimeError, match="boom"):
                await session.say("hi", _FailingSynth())

    asyncio.run(run())
    assert _clear_after_first_push(robot)


def test_media_session_opens_and_tears_down_pipeline() -> None:
    robot = FakeReachyMini()

    async def run() -> None:
        async with _open(MediaSession(robot)):
            pass

    asyncio.run(run())
    names = [name for name, _ in robot.commands]
    assert names.index("media.start_recording") < names.index("media.stop_recording")
    assert "media.start_playing" in names and "media.stop_playing" in names


def test_media_session_applies_audio_config_when_given() -> None:
    robot = FakeReachyMini()
    profile = {"noise_suppression": "high"}

    async def run() -> None:
        async with _open(MediaSession(robot, audio_config=profile)):
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
        async with _open(MediaSession(robot)):
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
        async with _open(MediaSession(robot, audio_config={"agc": 1})):
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
        async with _open(session):
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
        async with _open(session):
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
        async with _open(session):
            with pytest.raises(BridgeError):
                async with _open(session):
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
        async with _open(MediaSession(robot)) as session:
            return await _take(session.audio_input(), 2)

    chunks = asyncio.run(run())
    assert len(chunks) == 2
    # 160 frames, mono, int16 -> 320 bytes; whole number of int16 samples.
    assert all(len(c) == 160 * 2 for c in chunks)


def test_audio_input_raw_is_interleaved_stereo() -> None:
    robot = FakeReachyMini()

    async def run() -> tuple[list[bytes], list[bytes]]:
        async with _open(MediaSession(robot)) as session:
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
        async with _open(MediaSession(robot)) as session:
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


# --- stopping a sound file (specs/audio/audio.md "Stopping a sound file") ---


def test_stop_sound_stops_only_the_file_that_holds_the_player() -> None:
    robot = FakeReachyMini()
    session = MediaSession(robot)
    first = session.start_sound(Path("first.wav"))
    second = session.start_sound(Path("second.wav"))
    assert first.replaced.done() and not second.replaced.done()

    session.stop_sound(first)  # replaced: the player holds the second file
    assert _command_names(robot) == ["media.play_sound", "media.play_sound"]

    session.stop_sound(second)  # the owner: stopped, then the wobbler reset
    assert _command_names(robot)[2:] == ["media.stop_sound", "audio.clear_player"]

    session.stop_sound(second)  # nothing holds the player any more
    assert len(robot.commands) == 4


def test_a_released_file_is_not_stopped() -> None:
    robot = FakeReachyMini()
    session = MediaSession(robot)
    token = session.start_sound(Path("done.wav"))
    session.release(token)  # it ended on its own
    session.stop_sound(token)
    assert _command_names(robot) == ["media.play_sound"]


class _StubPlaybin:
    def __init__(self) -> None:
        self.states: list[object] = []

    def set_state(self, state: object) -> None:
        self.states.append(state)


def _robot_with_audio(audio: object) -> Any:
    return cast("Any", SimpleNamespace(media=SimpleNamespace(audio=audio)))


def test_stop_sound_stops_the_local_playbin_and_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gi  # pyright: ignore[reportMissingImports]
    from reachy_mini.media.audio_gstreamer import GStreamerAudio

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # pyright: ignore[reportMissingImports]

    # The stand-in skips the SDK constructor; silence its __del__ on the missing state.
    monkeypatch.setattr(GStreamerAudio, "__del__", lambda self: None)
    audio = GStreamerAudio.__new__(GStreamerAudio)
    playbin = _StubPlaybin()
    audio._playbin = cast("Any", playbin)

    audio_module._stop_sound_file(_robot_with_audio(audio))
    assert playbin.states == [Gst.State.NULL]
    assert audio._playbin is None

    # Nothing playing: a no-op.
    audio_module._stop_sound_file(_robot_with_audio(audio))
    assert playbin.states == [Gst.State.NULL]


def test_stop_sound_on_the_webrtc_backend_posts_to_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient

    monkeypatch.setattr(GstWebRTCClient, "__del__", lambda self: None)
    audio = GstWebRTCClient.__new__(GstWebRTCClient)
    audio.daemon_url = "http://127.0.0.1:8000"
    posted: list[str] = []
    monkeypatch.setattr(audio_module, "_post", posted.append)

    audio_module._stop_sound_file(_robot_with_audio(audio))
    assert posted == ["http://127.0.0.1:8000/api/media/stop_sound"]


def test_stop_sound_on_an_unknown_backend_warns_and_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.audio"):
        audio_module._stop_sound_file(_robot_with_audio(object()))
    assert "unsupported audio backend" in caplog.text


def test_say_missing_synthesizer_is_a_type_the_caller_can_supply() -> None:
    # The session's say always takes an explicit synth; the "no synth configured"
    # error lives at the bridge layer (see tests/test_bridge.py). Here just prove a plain
    # object without the protocol shape is rejected at call time.
    robot = FakeReachyMini()

    async def run() -> None:
        async with _open(MediaSession(robot)) as session:
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
        async with _open(session):
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
        async with _open(MediaSession(robot)) as session:
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
        async with _open(MediaSession(robot)) as session:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(_take(session.audio_input(), 1), 0.2)

    asyncio.run(run())
    # ~20 reads at a 10 ms poll; an unthrottled loop makes thousands. Upper bound only,
    # so a slow machine cannot make it flaky.
    assert calls < 50


# --- cancel-safe bring-up steps (specs/core/bridge.md "Lifecycle") ---


def test_cancel_during_media_open_unwinds_the_started_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = FakeReachyMini()
    original = robot.media.start_recording
    started, release = threading.Event(), threading.Event()

    def slow() -> None:
        started.set()
        release.wait()
        original()

    monkeypatch.setattr(robot.media, "start_recording", slow)
    session = MediaSession(robot)

    async def run() -> None:
        task = asyncio.create_task(session.start())
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert _command_names(robot) == ["media.start_recording", "media.stop_recording"]
    with pytest.raises(BridgeError):
        asyncio.run(session.say("x", _ToneSynth(16000)))


def test_cancel_safe_step_returns_the_result_when_not_cancelled() -> None:
    calls: list[str] = []

    async def run() -> int:
        return await cancel_safe_step(lambda: 42, lambda _: calls.append("undo"))

    assert asyncio.run(run()) == 42
    assert calls == []


def test_cancel_safe_step_propagates_a_failing_step_as_the_cancel() -> None:
    started, release = threading.Event(), threading.Event()
    undone: list[object] = []

    def step() -> int:
        started.set()
        release.wait()
        raise RuntimeError("step failed")

    async def run() -> None:
        task = asyncio.create_task(cancel_safe_step(step, undone.append))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert undone == []


def test_stop_before_start_is_a_noop() -> None:
    robot = FakeReachyMini()
    session = MediaSession(robot)
    asyncio.run(session.stop())
    assert _command_names(robot) == []


# --- one say at a time, the newest wins (specs/audio/audio.md "TTS out") -----------------


def test_a_new_say_interrupts_one_waiting_to_be_heard() -> None:
    robot = FakeReachyMini()

    async def run() -> tuple[float, list[str]]:
        async with _open(MediaSession(robot)) as session:
            long = _ToneSynth(16000, chunks=4, block=4000)  # 1 s, pushed at once
            first = asyncio.create_task(session.say("first", long))
            while len(_pushed_frames(robot)) < 4:  # queued; now waiting to be heard
                await asyncio.sleep(0)
            t0 = time.monotonic()
            second = asyncio.create_task(
                session.say("second", _ToneSynth(16000, chunks=2, block=800))
            )
            with pytest.raises(SpeechInterruptedError):
                await first
            elapsed = time.monotonic() - t0
            await second
            await session.say("third", _SilentSynth())  # the session plays on
            return elapsed, _command_names(robot)

    elapsed, names = asyncio.run(run())
    assert elapsed < 0.2
    flush = names.index("audio.clear_player")
    assert names.count("audio.clear_player") == 1  # the interrupted one, once
    assert names[:flush].count("media.push_audio_sample") == 4  # the first's audio
    assert names[flush:].count("media.push_audio_sample") == 2  # then the second's


def test_a_new_say_interrupts_one_still_synthesizing() -> None:
    robot = FakeReachyMini()

    async def run() -> list[str]:
        async with _open(MediaSession(robot)) as session:
            first = asyncio.create_task(session.say("first", _StallingSynth()))
            while not _pushed_frames(robot):
                await asyncio.sleep(0)
            await session.say("second", _ToneSynth(16000, chunks=1, block=800))
            with pytest.raises(SpeechInterruptedError):
                await first  # its stalled stream was closed
            return _command_names(robot)

    names = asyncio.run(run())
    assert names.count("audio.clear_player") == 1
    assert names.count("media.push_audio_sample") == 2


# --- sound files (specs/audio/audio.md "Sound files") ---


def _wav(directory: Path, seconds: float, name: str = "sound.wav") -> Path:
    """Write ``seconds`` of 16 kHz mono silence as a WAV file."""
    path = directory / name
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def _float_wav(directory: Path, seconds: float) -> Path:
    """Write a float32 WAV (format 3), which the stdlib ``wave`` module refuses."""
    import struct

    rate, frames = 16000, int(16000 * seconds)
    data = b"\x00" * (4 * frames)
    header = b"RIFF" + struct.pack("<I", 4 + 24 + 8 + len(data)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 3, 1, rate, rate * 4, 4, 32)
    path = directory / "float.wav"
    path.write_bytes(header + fmt + b"data" + struct.pack("<I", len(data)) + data)
    return path


@asynccontextmanager
async def _open_session(robot: FakeReachyMini) -> AsyncIterator[MediaSession]:
    session = MediaSession(robot)
    await session.start()
    try:
        yield session
    finally:
        await session.stop()


def test_sound_duration_reads_a_wav_exactly(tmp_path: Path) -> None:
    assert audio_module._sound_duration(_wav(tmp_path, 0.3)) == pytest.approx(
        0.3, abs=1e-3
    )


def _duration_in_a_child_process(path: Path) -> str:
    """``_sound_duration(path)`` run in a fresh interpreter with the GStreamer-bundle
    variables scrubbed. The bundle's startup hook appends its paths to them in every
    Python process, so a pytest-xdist worker (a Python child of pytest) inherits them
    doubled — ``GST_REGISTRY_1_0`` and ``GST_PLUGIN_SCANNER_1_0`` become two paths
    joined — and ``Gst.init`` exits the worker. The daemon launcher scrubs them for the
    same reason (``scrubbed_env``)."""
    import subprocess
    import sys

    from reachy_mini_bridge.daemon import scrubbed_env

    code = (
        "import sys; from pathlib import Path; "
        "from reachy_mini_bridge.audio import _sound_duration\n"
        "try:\n    print(_sound_duration(Path(sys.argv[1])))\n"
        "except ValueError as e:\n    print('ValueError', e)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=scrubbed_env(),
    )
    return done.stdout.strip()


def test_sound_duration_reads_other_files_through_gstreamer(tmp_path: Path) -> None:
    pytest.importorskip("gi")
    path = _float_wav(tmp_path, 0.5)
    with pytest.raises(wave.Error), wave.open(str(path), "rb"):
        pass
    assert float(_duration_in_a_child_process(path)) == pytest.approx(0.5, abs=0.01)


def test_sound_duration_of_an_unreadable_file_is_a_value_error(tmp_path: Path) -> None:
    pytest.importorskip("gi")
    path = tmp_path / "noise.ogg"
    path.write_text("not a sound")
    out = _duration_in_a_child_process(path)
    assert out.startswith("ValueError") and "noise.ogg" in out


def test_a_built_in_sound_resolves_to_the_sdk_asset(tmp_path: Path) -> None:
    path = audio_module._resolve_sound_file("wake_up.wav")
    assert path.name == "wake_up.wav" and path.is_file()
    local = _wav(tmp_path, 0.1)
    assert audio_module._resolve_sound_file(str(local)) == local


def test_a_missing_sound_file_raises_before_anything_plays() -> None:
    robot = FakeReachyMini()

    async def run() -> None:
        async with _open_session(robot) as session:
            with pytest.raises(FileNotFoundError, match="nope.wav"):
                await session.play_sound("nope.wav")

    asyncio.run(run())
    assert "media.play_sound" not in _command_names(robot)


def test_play_sound_requires_an_open_session(tmp_path: Path) -> None:
    with pytest.raises(BridgeError, match="play_sound"):
        asyncio.run(MediaSession(FakeReachyMini()).play_sound(str(_wav(tmp_path, 0.1))))


def test_play_sound_completes_when_the_file_has_been_heard(tmp_path: Path) -> None:
    path = _wav(tmp_path, 0.3)
    robot = FakeReachyMini()

    async def run() -> float:
        async with _open_session(robot) as session:
            t0 = time.monotonic()
            await session.play_sound(str(path))
            return time.monotonic() - t0

    elapsed = asyncio.run(run())
    assert 0.3 <= elapsed < 0.6
    assert ("media.play_sound", {"sound_file": str(path)}) in robot.commands
    assert "media.stop_sound" not in _command_names(robot)  # it ended on its own


def test_a_cancelled_play_sound_stops_its_file_and_the_session_plays_on(
    tmp_path: Path,
) -> None:
    long, short = _wav(tmp_path, 3.0, "long.wav"), _wav(tmp_path, 0.2, "short.wav")
    robot = FakeReachyMini()

    async def run() -> tuple[float, list[str]]:
        async with _open_session(robot) as session:
            task = asyncio.create_task(session.play_sound(str(long)))
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            elapsed = time.monotonic() - t0
            at_cancel = _command_names(robot)  # the stop precedes the CancelledError
            await session.play_sound(str(short))  # the session still works
            return elapsed, at_cancel

    elapsed, at_cancel = asyncio.run(run())
    assert elapsed < 0.05
    assert at_cancel[-2:] == ["media.stop_sound", "audio.clear_player"]
    assert _command_names(robot).count("media.play_sound") == 2


def test_a_cancel_during_the_start_stops_the_file_once_it_has_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _wav(tmp_path, 2.0)
    robot = FakeReachyMini()
    original = robot.media.play_sound

    def slow_start(sound_file: str) -> None:
        time.sleep(0.2)
        original(sound_file)

    monkeypatch.setattr(robot.media, "play_sound", slow_start)

    async def run() -> None:
        async with _open_session(robot) as session:
            task = asyncio.create_task(session.play_sound(str(path)))
            await asyncio.sleep(0.05)  # inside the start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    names = _command_names(robot)
    assert names.index("media.stop_sound") > names.index("media.play_sound")


def test_a_later_sound_file_interrupts_the_play_sound_in_flight(tmp_path: Path) -> None:
    first, second = _wav(tmp_path, 3.0, "first.wav"), _wav(tmp_path, 0.3, "second.wav")
    robot = FakeReachyMini()

    async def run() -> float:
        async with _open_session(robot) as session:
            task = asyncio.create_task(session.play_sound(str(first)))
            await asyncio.sleep(0.1)
            later = asyncio.create_task(session.play_sound(str(second)))
            t0 = time.monotonic()
            with pytest.raises(SoundInterruptedError):
                await task
            elapsed = time.monotonic() - t0
            await later  # the newest plays to its end
            return elapsed

    assert asyncio.run(run()) < 0.05
    assert "media.stop_sound" not in _command_names(robot)


def test_a_sound_file_and_a_say_play_together_and_a_stop_spares_the_say(
    tmp_path: Path,
) -> None:
    path = _wav(tmp_path, 2.0)
    robot = FakeReachyMini()
    synth = _ToneSynth(16000, chunks=10, block=1600)  # one second of speech

    async def run() -> None:
        async with _open_session(robot) as session:
            sound = asyncio.create_task(session.play_sound(str(path)))
            say = asyncio.create_task(session.say("hello", synth))
            while "media.push_audio_sample" not in _command_names(robot):
                await asyncio.sleep(0.01)
            sound.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sound
            await say  # completes: the stop flushed nothing

    asyncio.run(run())
    names = _command_names(robot)
    assert "media.stop_sound" in names
    assert "audio.clear_player" not in names
    assert names.count("media.push_audio_sample") == 10
