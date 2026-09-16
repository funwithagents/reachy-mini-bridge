"""Audio & media session (specs/audio.md).

Everything about getting sound into and out of the robot correctly, as a single
daemon-owned pipeline with the XVF3800 voice processor in the middle:

- :class:`MediaSession` opens recording + playback once per connection and owns
  teardown, so the on-board acoustic echo cancellation has the far-end reference it
  needs (TTS out through the same pipeline the mic-in stream is cancelled against).
- ``say`` routes a pluggable :class:`SpeechSynthesizer`'s float32 mono PCM to the
  robot speaker, resampling to the speaker rate and fanning mono out to its channels.
- ``audio_input`` exposes the echo-cancelled mic as a clean int16 LE stream for the
  caller's own ASR (mono by default; raw multichannel on request). The bridge embeds
  no ASR.

Formats follow "match the consumer": the synthesizer emits float32 mono ``[-1, 1]``
(consumer is the speaker, which takes float32); the mic yields int16 LE (consumer is
an ASR engine, which wants linear16). The three per-chunk conversions are the public
helpers below. Channel counts and rates are read from the SDK getters, never
hardcoded, so the code is correct across the real / sim / fake backends.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Protocol, Self, cast, runtime_checkable

import numpy as np
import numpy.typing as npt
import samplerate

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .robot import AnyReachyMini

__all__ = [
    "MediaSession",
    "SpeechSynthesizer",
    "TTSEngineSynthesizer",
    "downmix_to_mono",
    "float32_to_int16",
    "int16_to_float32",
]

# libsamplerate converter: highest-quality sinc conversion (no boundary clicks on a
# continuous stream). See specs/audio.md "The robot sink".
_CONVERTER = "sinc_best"

# Margin added to the estimated playback end before `say` returns: the sink's ring
# buffer (50 ms on the GStreamer backend) plus device latency. See specs/audio.md
# "`say` completes when the utterance has been heard".
_PLAYBACK_TAIL_S = 0.1


# --- conversion helpers (pure; shared by the say sink and the mic tap) -------------


def int16_to_float32(pcm: npt.NDArray[np.int16]) -> npt.NDArray[np.float32]:
    """Scale int16 PCM to float32 in ``[-1, 1]`` (divide by 32768).

    For synthesizer authors whose engine emits int16, meeting the float32-only
    :class:`SpeechSynthesizer` contract.
    """
    return (np.asarray(pcm, dtype=np.float32) / 32768.0).astype(np.float32)


def float32_to_int16(pcm: npt.NDArray[np.float32]) -> npt.NDArray[np.int16]:
    """Convert float32 PCM to int16, clipping to ``[-1, 1]`` first (scale by 32767).

    Note the deliberate 32768/32767 asymmetry versus :func:`int16_to_float32`: full-scale
    ``1.0`` maps to the int16 max, and clipping keeps out-of-range values from wrapping.
    """
    clipped = np.clip(np.asarray(pcm, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def downmix_to_mono(
    pcm: npt.NDArray[np.float32], channels: int
) -> npt.NDArray[np.float32]:
    """Average interleaved ``channels`` down to a single channel, returning ``(n,)``.

    Accepts either a 2-D ``(frames, channels)`` array (the capture shape) or a flat
    interleaved ``(frames * channels,)`` array. A ``channels == 1`` input is returned
    flattened, unchanged.
    """
    arr = np.asarray(pcm, dtype=np.float32)
    if channels == 1:
        return arr.reshape(-1)
    if arr.ndim == 1:
        arr = arr.reshape(-1, channels)
    return arr.mean(axis=1).astype(np.float32)


# --- the synthesizer contract ------------------------------------------------------


@runtime_checkable
class SpeechSynthesizer(Protocol):
    """Turns text into a stream of PCM audio the media layer can play.

    Deliberately minimal: text in, PCM out. Voices / rate / SSML stay inside the
    concrete synthesizer's own config. Chunks are **float32 mono in ``[-1, 1]``, shape
    ``(n,)``** at :attr:`sample_rate` Hz.
    """

    @property
    def sample_rate(self) -> int:
        """Hz of the PCM chunks :meth:`stream` yields. Fixed for the synth's lifetime."""
        ...

    def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        """Yield float32 mono ``[-1, 1]`` chunks, shape ``(n,)``, as they synthesize."""
        ...


# --- the media session -------------------------------------------------------------


class MediaSession:
    """The single, connection-scoped media pipeline shared by ``say`` and the mic tap.

    Opened once (``start_recording`` + ``start_playing``, optional XVF3800 config) and
    torn down once. Owning both directions is what makes echo cancellation work.
    Reads all rates/channels from the SDK getters so the same code is correct on the
    real, sim, and fake backends.
    """

    def __init__(
        self, robot: AnyReachyMini, *, audio_config: object | None = None
    ) -> None:
        self._robot = robot
        # The XVF3800 tuning profile applied on start. Left None by default (firmware
        # defaults) until the concrete profile settles — specs/audio.md open question 3.
        self._audio_config = audio_config
        self._open = False

    async def __aenter__(self) -> Self:
        media = self._robot.media
        await asyncio.to_thread(media.start_recording)
        await asyncio.to_thread(media.start_playing)
        if self._audio_config is not None:
            # media.audio's type/optionality diverges between the real MediaManager and
            # the fake; the audio-control surface is exercised loosely (the union still
            # checks every other media call above/below).
            audio: Any = media.audio
            await asyncio.to_thread(audio.apply_audio_config, self._audio_config)
        self._open = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        media = self._robot.media
        await asyncio.to_thread(media.stop_recording)
        await asyncio.to_thread(media.stop_playing)
        self._open = False

    # --- mic in ---

    @property
    def mic_sample_rate(self) -> int:
        """Sample rate (Hz) of the mic stream — read from the daemon (16 kHz)."""
        return self._robot.media.get_input_audio_samplerate()

    @property
    def mic_channels(self) -> int:
        """Channel count of the raw capture — read from the daemon (stereo backend: 2)."""
        return self._robot.media.get_input_channels()

    async def audio_input(self, *, mono: bool = True) -> AsyncIterator[bytes]:
        """Yield echo-cancelled mic PCM as ``bytes`` (int16 LE).

        ``mono=True`` (default) downmixes to one channel — the ASR drop-in. ``mono=False``
        yields the raw interleaved capture at :attr:`mic_channels` channels. A tap over
        the already-running capture: iterate to consume, ``break`` to stop.
        """
        media = self._robot.media
        channels = media.get_input_channels()
        while True:
            sample = await asyncio.to_thread(media.get_audio_sample)
            if sample is None:
                continue
            arr = np.asarray(sample, dtype=np.float32)
            frame = downmix_to_mono(arr, channels) if mono else arr
            yield float32_to_int16(frame).tobytes()

    # --- speaker out ---

    async def say(self, text: str, synth: SpeechSynthesizer) -> None:
        """Synthesize ``text``, stream it to the robot speaker, and wait until it is heard.

        Resamples ``synth.sample_rate`` to the speaker rate (skipped when they already
        match) and fans the mono stream out to the speaker's channel count. Pushing only
        queues audio, so ``say`` then sleeps until the estimated playback end (plus a
        small tail margin) — it returns once the utterance has finished playing.

        On any early exit (the task is cancelled, or the synthesizer raises) the queued
        speaker audio is flushed with :meth:`clear_player` before the exception
        propagates: ``say`` either plays the whole utterance or leaves the speaker silent.
        """
        media = self._robot.media
        out_rate = media.get_output_audio_samplerate()
        out_channels = media.get_output_channels()
        resampler = _StreamResampler(synth.sample_rate, out_rate)
        tracker = _PlaybackTracker(out_rate)
        try:
            async for chunk in synth.stream(text):
                mono = resampler.process(
                    np.asarray(chunk, dtype=np.float32).reshape(-1)
                )
                tracker.push(self._push(mono, out_channels))
            tracker.push(self._push(resampler.flush(), out_channels))
            remaining = tracker.remaining()
            if remaining > 0:
                await asyncio.sleep(remaining)
        except BaseException:
            self.clear_player()
            raise

    def _push(self, mono: npt.NDArray[np.float32], out_channels: int) -> int:
        """Push one mono chunk (fanned out to ``out_channels``); return its frame count."""
        if mono.size == 0:
            return 0
        if out_channels > 1:
            out = np.repeat(mono[:, np.newaxis], out_channels, axis=1)
        else:
            out = mono
        self._robot.media.push_audio_sample(np.ascontiguousarray(out, dtype=np.float32))
        return int(mono.size)

    def clear_player(self) -> None:
        """Flush already-queued speaker audio (barge-in) and reset the head wobbler.

        See specs/audio.md.
        """
        audio: Any = self._robot.media.audio  # see note in __aenter__ on media.audio
        audio.clear_player()


class _PlaybackTracker:
    """Wall-clock estimate of when the audio queued on the speaker finishes playing.

    ``media.push_audio_sample`` queues without blocking or pacing, so the sink keeps its
    own end-of-playback estimate: each non-empty push extends the end from
    ``max(end, now)`` by ``frames / sample_rate`` seconds. Contiguous pushes accumulate;
    a push arriving after the queue has drained starts from *now*. Reads no SDK state,
    so it holds identically on every backend. ``now`` is injectable for tests.
    """

    def __init__(
        self, sample_rate: int, *, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._sample_rate = sample_rate
        self._now = now
        self._end = 0.0
        self._pushed = False

    def push(self, frames: int) -> None:
        """Account for ``frames`` frames just queued (a no-op for ``frames <= 0``)."""
        if frames <= 0:
            return
        self._end = max(self._end, self._now()) + frames / self._sample_rate
        self._pushed = True

    def remaining(self) -> float:
        """Seconds until the queued audio has been heard (``0.0`` if nothing was pushed)."""
        if not self._pushed:
            return 0.0
        return max(0.0, self._end + _PLAYBACK_TAIL_S - self._now())


class _StreamResampler:
    """Stateful synth-rate -> speaker-rate resampler, carrying filter state across chunks.

    A no-op fast path when the rates already match (the documented recommendation is a
    speaker-rate-native synthesizer). Otherwise wraps libsamplerate via ``samplerate``
    so the backing library stays swappable behind this one helper.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self._ratio = dst_rate / src_rate
        self._passthrough = src_rate == dst_rate
        self._resampler = (
            None if self._passthrough else samplerate.Resampler(_CONVERTER, channels=1)
        )

    def process(self, mono: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        if self._passthrough or self._resampler is None:
            return mono
        out = self._resampler.process(mono, self._ratio, end_of_input=False)
        return np.asarray(out, dtype=np.float32)

    def flush(self) -> npt.NDArray[np.float32]:
        """Emit any samples the resampler is still holding (call after the last chunk)."""
        if self._passthrough or self._resampler is None:
            return np.empty(0, dtype=np.float32)
        out = self._resampler.process(
            np.empty(0, dtype=np.float32), self._ratio, end_of_input=True
        )
        return np.asarray(out, dtype=np.float32)


# --- the default synthesizer adapter (optional `tts` extra) ------------------------


class TTSEngineSynthesizer:
    """Default :class:`SpeechSynthesizer`, adapting our first-party ``tts-engine``.

    Lives behind the ``tts`` extra: ``tts_engine`` is imported lazily in the
    constructor, so the bridge core never pulls it in for a caller who supplies their
    own synthesizer. ``tts-engine`` is push-based (a sink is fed int16 chunks); this
    adapter bridges that to the pull-based float32 iterator the contract requires via a
    thread-safe queue, converting int16 -> float32 with :func:`int16_to_float32`.
    """

    def __init__(self, config: object) -> None:
        # Lazy import: only constructing the default adapter needs tts-engine.
        from tts_engine.config import TTSEngineConfig
        from tts_engine.engine import TTSEngine

        engine_config = (
            config
            if isinstance(config, TTSEngineConfig)
            else TTSEngineConfig.from_dict(cast("dict[str, Any]", config))
        )
        self._sink = _QueueSink()
        self._engine = TTSEngine(engine_config, sink=self._sink)

    @property
    def sample_rate(self) -> int:
        return self._engine.sample_rate

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._sink.bind(loop, queue)
        say_task = asyncio.create_task(self._engine.say(text))
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:  # sentinel: sink.drain() ran (success or failure)
                    break
                yield int16_to_float32(np.frombuffer(chunk, dtype=np.int16))
            await (
                say_task
            )  # re-raise any synthesis error now that the stream is drained
        finally:
            if not say_task.done():
                say_task.cancel()


class _QueueSink:
    """A ``tts_engine`` ``AudioSink`` that hands int16 chunks to an asyncio queue.

    ``feed``/``drain`` may run off the event-loop thread, so both marshal onto the
    bound loop with ``call_soon_threadsafe``. ``drain`` pushes a ``None`` sentinel so
    the consumer knows synthesis finished. One sink is reused across calls (tts-engine
    serializes ``say`` with a lock); each call rebinds a fresh queue.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[bytes | None] | None = None

    def bind(
        self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[bytes | None]
    ) -> None:
        self._loop = loop
        self._queue = queue

    def feed(self, chunk: bytes) -> None:
        if self._loop is None or self._queue is None or not chunk:
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)

    def drain(self) -> None:
        if self._loop is None or self._queue is None:
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
