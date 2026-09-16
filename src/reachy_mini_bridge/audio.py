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
import logging
import time
import urllib.request
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Protocol, Self, cast, runtime_checkable

import numpy as np
import numpy.typing as npt
import samplerate

from .errors import BridgeError
from .fake_reachy_mini import FakeReachyMini

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .robot import AnyReachyMini

__all__ = [
    "MediaSession",
    "SpeechSynthesizer",
    "TTSEngineSynthesizer",
    "cancel_safe_step",
    "downmix_to_mono",
    "float32_to_int16",
    "int16_to_float32",
]

_logger = logging.getLogger(__name__)

# libsamplerate converter: highest-quality sinc conversion (no boundary clicks on a
# continuous stream). See specs/audio.md "The robot sink".
_CONVERTER = "sinc_best"

# Margin added to the estimated playback end before `say` returns: the sink's ring
# buffer (50 ms on the GStreamer backend) plus device latency. See specs/audio.md
# "`say` completes when the utterance has been heard".
_PLAYBACK_TAIL_S = 0.1

# How long the mic tap waits before re-reading when the daemon has no sample ready
# (one 10 ms capture chunk). See specs/audio.md "Mic in".
_MIC_POLL_INTERVAL_S = 0.01

# Timeout for the one daemon HTTP call the media layer makes (`stop_sound` on webrtc).
_DAEMON_HTTP_TIMEOUT_S = 2.0


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
    Teardown stops exactly what started, even when opening or closing fails partway;
    ``say`` and ``audio_input`` raise :class:`BridgeError` outside an open session.
    Reads all rates/channels from the SDK getters so the same code is correct on the
    real, sim, and fake backends.
    """

    def __init__(
        self, robot: AnyReachyMini, *, audio_config: object | None = None
    ) -> None:
        self._robot = robot
        # The XVF3800 tuning profile applied on start. Left None by default (firmware
        # defaults) until the concrete profile settles — specs/audio.md open question 2.
        self._audio_config = audio_config
        # The stops to run at close; the session is open exactly while this is set.
        self._exit_stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        if self._exit_stack is not None:
            raise BridgeError("MediaSession is already open")
        media = self._robot.media
        stack = AsyncExitStack()
        try:
            await cancel_safe_step(
                media.start_recording, lambda _: media.stop_recording()
            )
            stack.push_async_callback(asyncio.to_thread, media.stop_recording)
            await cancel_safe_step(media.start_playing, lambda _: media.stop_playing())
            stack.push_async_callback(asyncio.to_thread, media.stop_playing)
            if self._audio_config is not None:
                # media.audio's type/optionality diverges between the real MediaManager
                # and the fake; the audio-control surface is exercised loosely (the
                # union still checks every other media call above/below).
                audio: Any = media.audio
                await asyncio.to_thread(audio.apply_audio_config, self._audio_config)
        except BaseException:
            await stack.aclose()
            raise
        self._exit_stack = stack.pop_all()
        return self

    async def __aexit__(self, *exc: object) -> None:
        stack = self._exit_stack
        # Read as closed even if a stop raises; the stack still runs every stop.
        self._exit_stack = None
        if stack is not None:
            await stack.aclose()

    def _require_open(self, verb: str) -> None:
        if self._exit_stack is None:
            raise BridgeError(
                f"{verb} requires an open media session "
                "(inside `async with ReachyMiniApi(...)`)"
            )

    # --- mic in ---

    @property
    def mic_sample_rate(self) -> int:
        """Sample rate (Hz) of the mic stream — read from the daemon (16 kHz)."""
        return self._robot.media.get_input_audio_samplerate()

    @property
    def mic_channels(self) -> int:
        """Channel count of the raw capture — read from the daemon (stereo backend: 2)."""
        return self._robot.media.get_input_channels()

    def audio_input(self, *, mono: bool = True) -> AsyncIterator[bytes]:
        """Yield echo-cancelled mic PCM as ``bytes`` (int16 LE).

        ``mono=True`` (default) downmixes to one channel — the ASR drop-in. ``mono=False``
        yields the raw interleaved capture at :attr:`mic_channels` channels. A tap over
        the already-running capture: iterate to consume, ``break`` to stop. Raises
        :class:`BridgeError` right here when the session is not open; the stream ends
        on its own when the session closes.
        """
        self._require_open("audio_input")
        return self._tap(mono)

    async def _tap(self, mono: bool) -> AsyncIterator[bytes]:
        media = self._robot.media
        channels = media.get_input_channels()
        while self._exit_stack is not None:
            sample = await asyncio.to_thread(media.get_audio_sample)
            if sample is None:
                # Nothing buffered yet: wait a chunk rather than spin on the daemon.
                await asyncio.sleep(_MIC_POLL_INTERVAL_S)
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
        Raises :class:`BridgeError` when the session is not open.
        """
        self._require_open("say")
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

    def stop_sound(self) -> None:
        """Stop the sound file the SDK is playing, without touching the shared pipeline.

        An emotion's sidecar sound or a `play_sound` call. Then resets the head wobbler
        through :meth:`clear_player` (the stopped player never reaches the EOS that
        would reset it). A no-op when no sound plays. Works at any time, like
        :meth:`clear_player`. See specs/audio.md "Stopping a sound file".
        """
        _stop_sound_file(self._robot)
        self.clear_player()


def _stop_sound_file(robot: AnyReachyMini) -> None:
    """Backend dispatch behind :meth:`MediaSession.stop_sound` (see specs/audio.md)."""
    if isinstance(robot, FakeReachyMini):
        robot.media.stop_sound()
        return
    audio: Any = robot.media.audio
    if audio is None:
        return
    # Lazy imports: only a real/sim robot reaches this branch.
    from reachy_mini.media.audio_gstreamer import GStreamerAudio
    from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient

    if isinstance(audio, GStreamerAudio):
        # The bridge's one reach into SDK internals: the exact body of the daemon-side
        # MediaServer.stop_sound(), pinned by tests/test_robot.py. Replace with
        # media.stop_sound() once upstream ships it.
        playbin = audio._playbin
        if playbin is not None:
            # gi ships inside the gstreamer wheel's own site-packages, which pyright
            # does not index.
            import gi  # pyright: ignore[reportMissingImports]

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst  # pyright: ignore[reportMissingImports]

            playbin.set_state(Gst.State.NULL)
            audio._playbin = None
    elif isinstance(audio, GstWebRTCClient):
        if audio.daemon_url:
            _post(f"{audio.daemon_url}/api/media/stop_sound")
    else:
        _logger.warning(
            "stop_sound: unsupported audio backend %s; the sound plays on",
            type(audio).__name__,
        )


def _post(url: str) -> None:
    """POST to the daemon's HTTP API with an empty body (patched by tests)."""
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=_DAEMON_HTTP_TIMEOUT_S):
        pass


async def cancel_safe_step[T](enter: Callable[[], T], undo: Callable[[T], object]) -> T:
    """Run the blocking ``enter`` off the loop; on a cancel, finish it, undo it, re-raise.

    ``asyncio.to_thread`` cannot interrupt its thread. If the awaiting task is
    cancelled while ``enter`` runs, this waits for ``enter`` to finish, runs ``undo`` on
    its result (also off the loop), and then re-raises the ``CancelledError`` — so a
    daemon spawn, a robot connect, or a media ``start_*`` is never leaked by an
    ``asyncio.timeout`` around the api's ``async with``. If ``enter`` itself fails
    during that wait there is nothing to undo and the cancel still propagates. A
    second cancel during the wait abandons the step (accepted, documented in
    specs/api.md "Lifecycle").
    """
    step = asyncio.ensure_future(asyncio.to_thread(enter))
    try:
        return await asyncio.shield(step)
    except asyncio.CancelledError as cancel:
        try:
            result = await step
        except BaseException as exc:  # the step failed: nothing to undo
            _logger.warning("bring-up step failed while being cancelled: %r", exc)
            raise cancel from exc
        await asyncio.to_thread(undo, result)
        raise


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
