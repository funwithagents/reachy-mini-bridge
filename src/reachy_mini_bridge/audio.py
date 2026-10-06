"""Audio & media session (specs/audio/audio.md).

Everything about getting sound into and out of the robot correctly, as a single
daemon-owned pipeline with the XVF3800 voice processor in the middle:

- :class:`MediaSession` opens recording + playback once per connection and owns
  teardown, so the on-board acoustic echo cancellation has the far-end reference it
  needs (TTS out through the same pipeline the mic-in stream is cancelled against).
- ``say`` routes a pluggable :class:`SpeechSynthesizer`'s float32 mono PCM to the
  robot speaker, resampling to the speaker rate and fanning mono out to its channels.
- ``play_sound`` plays a sound file on the robot's one file player, to its end; the
  session owns that player, so every start — the verb's, an emotion's — is recorded
  and a stop ends only the file it started.
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
import concurrent.futures
import logging
import threading
import time
import urllib.request
import wave
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import numpy as np
import numpy.typing as npt
import samplerate

from .errors import BridgeError, SoundInterruptedError, SpeechInterruptedError
from .fake_reachy_mini import FakeReachyMini

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .robot import AnyReachyMini

__all__ = [
    "MediaSession",
    "SoundToken",
    "SpeechSynthesizer",
    "TTSEngineSynthesizer",
    "cancel_safe_step",
    "downmix_to_mono",
    "float32_to_int16",
    "int16_to_float32",
]

_logger = logging.getLogger(__name__)

# libsamplerate converter: highest-quality sinc conversion (no boundary clicks on a
# continuous stream). See specs/audio/audio.md "The robot sink".
_CONVERTER = "sinc_best"

# Margin added to the estimated playback end before `say` returns: the sink's ring
# buffer (50 ms on the GStreamer backend) plus device latency. See specs/audio/audio.md
# "`say` completes when the utterance has been heard".
_PLAYBACK_TAIL_S = 0.1

# How long the mic tap waits before re-reading when the daemon has no sample ready
# (one 10 ms capture chunk). See specs/audio/audio.md "Mic in".
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
        # defaults) until the concrete profile settles — specs/audio/audio.md open question 2.
        self._audio_config = audio_config
        # The stops to run at close; the session is open exactly while this is set.
        self._exit_stack: AsyncExitStack | None = None
        # The utterance in flight: a new `say` interrupts it (the newest wins).
        self._saying: _Utterance | None = None
        # The start that holds the robot's one sound file player (specs/audio/audio.md
        # "Sound files"). Started from the motion loop's thread and from worker threads,
        # so the start and this record change together under the lock.
        self._sound_lock = threading.Lock()
        self._sound_owner: SoundToken | None = None

    async def start(self) -> None:
        """Open the session: start recording and playback, apply the audio profile.
        ``BridgeError`` on a session already open; a failure partway unwinds what
        started."""
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

    async def stop(self) -> None:
        """Close the session: every stop runs, even when one raises. A no-op on a
        session that is not open."""
        stack = self._exit_stack
        # Read as closed even if a stop raises; the stack still runs every stop.
        self._exit_stack = None
        if stack is not None:
            await stack.aclose()

    def _require_open(self, verb: str) -> None:
        if self._exit_stack is None:
            raise BridgeError(
                f"{verb} requires an open media session "
                "(the bridge opens it: `await bridge.start()`, "
                "or `async with ReachyMiniBridge(...)`)"
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

        One utterance at a time, the newest wins (specs/audio/audio.md "TTS out"): a
        ``say`` called while one is in flight flushes it and makes that call raise
        :class:`SpeechInterruptedError` in its own task, then plays.
        """
        self._require_open("say")
        previous, utterance = self._saying, _Utterance()
        self._saying = utterance
        if previous is not None:
            _logger.info("say: a new utterance interrupts the one in flight")
            previous.interrupted.set()
            self.clear_player()  # the speaker is silent before the new one is queued
        try:
            await self._speak(text, synth, utterance)
        except BaseException:
            if self._saying is utterance:  # ours to flush; an interrupted one is not
                self.clear_player()
            raise
        finally:
            if self._saying is utterance:
                self._saying = None

    async def _speak(
        self, text: str, synth: SpeechSynthesizer, utterance: _Utterance
    ) -> None:
        """Stream ``text`` to the speaker and wait until it is heard, unless a later
        ``say`` interrupts this one: then stop at once with ``SpeechInterruptedError``."""
        work = asyncio.ensure_future(self._stream_to_speaker(text, synth))
        interrupted = asyncio.ensure_future(utterance.interrupted.wait())
        try:
            done, _pending = await asyncio.wait(
                {work, interrupted}, return_when=asyncio.FIRST_COMPLETED
            )
            if work in done:
                work.result()  # a synthesizer failure propagates
                return
            raise SpeechInterruptedError("say was interrupted by a later say")
        finally:
            for task in (work, interrupted):
                task.cancel()
            await asyncio.gather(work, interrupted, return_exceptions=True)

    async def _stream_to_speaker(self, text: str, synth: SpeechSynthesizer) -> None:
        media = self._robot.media
        out_rate = media.get_output_audio_samplerate()
        out_channels = media.get_output_channels()
        resampler = _StreamResampler(synth.sample_rate, out_rate)
        tracker = _PlaybackTracker(out_rate)
        async for chunk in synth.stream(text):
            mono = resampler.process(np.asarray(chunk, dtype=np.float32).reshape(-1))
            tracker.push(self._push(mono, out_channels))
        tracker.push(self._push(resampler.flush(), out_channels))
        remaining = tracker.remaining()
        if remaining > 0:
            await asyncio.sleep(remaining)

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

        See specs/audio/audio.md.
        """
        audio: Any = self._robot.media.audio  # see note in start() on media.audio
        audio.clear_player()

    # --- sound files (the one file player) ---

    def start_sound(self, path: Path) -> SoundToken:
        """Start the sound file ``path`` and record its token as the player's owner.

        Blocking (call it off the event loop). The robot plays one sound file at a time,
        so the owner before it, if any, is replaced: its ``replaced`` future resolves.
        Works at any time, like :meth:`clear_player`. See specs/audio/audio.md
        "Sound files".
        """
        token = SoundToken(path)
        with self._sound_lock:
            self._robot.media.play_sound(str(path))
            previous, self._sound_owner = self._sound_owner, token
            if previous is not None:
                previous.mark_replaced()
        return token

    def stop_sound(self, token: SoundToken) -> None:
        """Stop the sound file ``token`` started, while it still holds the player —
        never a file started after it — without touching the shared pipeline.

        Then resets the head wobbler through :meth:`clear_player` (the stopped player
        never reaches the end of stream that would reset it), unless a ``say`` is in
        flight: its audio drives the wobbler, and the flush would cut it off. See
        specs/audio/audio.md "Stopping a sound file, per backend".
        """
        with self._sound_lock:
            if token is not self._sound_owner:
                return
            _stop_sound_file(self._robot)
            self._sound_owner = None
        if self._saying is None:
            self.clear_player()

    def release(self, token: SoundToken) -> None:
        """Clear ``token``'s ownership of the player without stopping anything: its file
        ended on its own."""
        with self._sound_lock:
            if token is self._sound_owner:
                self._sound_owner = None

    async def play_sound(self, sound_file: str) -> None:
        """Play a sound file through the robot speaker and wait until it is heard.

        ``sound_file`` is a path on this machine or the name of one of the SDK's
        built-in sounds; ``FileNotFoundError`` when it is neither, ``ValueError`` when
        its duration cannot be read — before anything plays. Cancelling the task stops
        the sound before the ``CancelledError`` propagates. The newest sound file wins:
        when a later one (another ``play_sound``, an emotion's sound) replaces this one,
        the call raises :class:`SoundInterruptedError`. Raises :class:`BridgeError` when
        the session is not open. See specs/audio/audio.md "Sound files".
        """
        self._require_open("play_sound")
        path = await asyncio.to_thread(_resolve_sound_file, sound_file)
        duration = await asyncio.to_thread(_sound_duration, path)
        token = await cancel_safe_step(
            lambda: self.start_sound(path), self._stop_sound_logged
        )
        try:
            await self._wait_heard(token, duration)
        except SoundInterruptedError:
            raise
        except BaseException:
            # Cancelled (or failed): stop our file before the exception propagates.
            await asyncio.shield(asyncio.to_thread(self._stop_sound_logged, token))
            raise
        self.release(token)

    async def _wait_heard(self, token: SoundToken, duration: float) -> None:
        """Wait for the file's duration plus the tail margin, unless a later start
        replaces it first: then raise :class:`SoundInterruptedError`."""
        heard = asyncio.ensure_future(asyncio.sleep(duration + _PLAYBACK_TAIL_S))
        replaced = asyncio.wrap_future(token.replaced)
        try:
            done, _pending = await asyncio.wait(
                {heard, replaced}, return_when=asyncio.FIRST_COMPLETED
            )
            if heard in done:
                return
            _logger.info("play_sound: a later sound file replaced this one")
            raise SoundInterruptedError(
                "play_sound was interrupted by a later sound file"
            )
        finally:
            for future in (heard, replaced):
                future.cancel()
            await asyncio.gather(heard, replaced, return_exceptions=True)

    def _stop_sound_logged(self, token: SoundToken) -> None:
        try:
            self.stop_sound(token)
        except Exception as e:  # noqa: BLE001 - the verb's own exception wins
            _logger.warning("could not stop the sound file: %s", e)


class SoundToken:
    """One start of a sound file on the robot's one file player.

    ``replaced`` resolves when a later start takes the player from this one. A
    ``concurrent.futures.Future`` because the start that replaces it may run on the
    motion loop's thread; the verb awaits it on the event loop.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.replaced: concurrent.futures.Future[None] = concurrent.futures.Future()

    def mark_replaced(self) -> None:
        try:
            self.replaced.set_result(None)
        except concurrent.futures.InvalidStateError:
            pass  # already replaced, or its waiter has gone (cancelled the future)


def _resolve_sound_file(name: str) -> Path:
    """The file ``name`` names: a path on this machine, or one of the SDK's built-in
    sounds, as upstream's local backend resolves it (specs/audio/audio.md "Sound files")."""
    path = Path(name)
    if path.is_file():
        return path
    from reachy_mini.utils.constants import ASSETS_ROOT_PATH

    asset = Path(ASSETS_ROOT_PATH) / name
    if asset.is_file():
        return asset
    raise FileNotFoundError(
        f"sound file {name!r} not found: neither a file on this machine nor one of "
        f"the SDK's built-in sounds ({ASSETS_ROOT_PATH})"
    )


def _sound_duration(path: Path) -> float:
    """The duration of the sound file at ``path``, in seconds: a WAV through the stdlib
    ``wave`` module, any other file (or a WAV ``wave`` cannot read) through GStreamer's
    discoverer. ``ValueError`` when neither can read it."""
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                return wav.getnframes() / wav.getframerate()
        except (wave.Error, EOFError):
            pass  # e.g. floating-point samples: the discoverer reads those
    try:
        # gi ships inside the gstreamer wheel's own site-packages, which pyright does
        # not index.
        import gi  # pyright: ignore[reportMissingImports]

        gi.require_version("Gst", "1.0")
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import (  # pyright: ignore[reportMissingImports]
            Gst,  # pyright: ignore[reportAttributeAccessIssue]
            GstPbutils,  # pyright: ignore[reportAttributeAccessIssue]
        )

        Gst.init(None)
        discoverer = GstPbutils.Discoverer.new(5 * Gst.SECOND)
        info = discoverer.discover_uri(path.resolve().as_uri())
        nanoseconds = info.get_duration()
    except Exception as e:
        raise ValueError(f"cannot read the duration of sound file {path}: {e}") from e
    if not 0 < nanoseconds < Gst.CLOCK_TIME_NONE:
        raise ValueError(f"cannot read the duration of sound file {path}")
    return nanoseconds / Gst.SECOND


def _stop_sound_file(robot: AnyReachyMini) -> None:
    """Backend dispatch behind :meth:`MediaSession.stop_sound` (see specs/audio/audio.md)."""
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
            from gi.repository import (  # pyright: ignore[reportMissingImports]
                Gst,  # pyright: ignore[reportAttributeAccessIssue]
            )

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
    ``asyncio.timeout`` around the bridge's ``async with``. If ``enter`` itself fails
    during that wait there is nothing to undo and the cancel still propagates. The
    wait-and-undo is a task of its own, awaited through any further cancel: a second
    cancel does not shorten it (specs/core/bridge.md "Lifecycle" — nothing is leaked).
    """
    step = asyncio.ensure_future(asyncio.to_thread(enter))
    try:
        return await asyncio.shield(step)
    except asyncio.CancelledError as cancel:
        cleanup = asyncio.create_task(_finish_and_undo(step, undo))
        while not cleanup.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup)
        failure = cleanup.result()
        if failure is not None:
            raise cancel from failure
        raise


async def _finish_and_undo[T](
    step: asyncio.Future[T], undo: Callable[[T], object]
) -> BaseException | None:
    """Wait for the step and undo its result; a failed step's exception is returned
    (there is nothing to undo), never raised."""
    try:
        result = await step
    except BaseException as exc:  # noqa: BLE001 - the step failed: nothing to undo, reported
        _logger.warning("bring-up step failed while being cancelled: %r", exc)
        return exc
    await asyncio.to_thread(undo, result)
    return None


class _Utterance:
    """A ``say`` in flight: set ``interrupted`` and it ends at once."""

    def __init__(self) -> None:
        self.interrupted = asyncio.Event()


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


# --- the default synthesizer adapter (tts-engine) -----------------------------------


class TTSEngineSynthesizer:
    """Default :class:`SpeechSynthesizer`, adapting our first-party ``tts-engine``.

    ``tts-engine`` is a base dependency; the provider the block's ``module.type``
    names comes from the matching extra (``reachy-mini-bridge[tts-elevenlabs]`` /
    ``[tts-gradium]`` / ``[tts-pocket]``), and tts-engine raises its own
    ``ConfigError`` naming the missing one. The import stays local to the constructor so importing this module
    stays cheap for a caller who supplies their own synthesizer. ``tts-engine`` is
    push-based (a sink is fed int16 chunks); this adapter bridges that to the
    pull-based float32 iterator the contract requires via a thread-safe queue,
    converting int16 -> float32 with :func:`int16_to_float32`.
    """

    def __init__(self, config: object) -> None:
        # Local import: only constructing the default adapter needs tts-engine loaded.
        from tts_engine.config import TTSEngineConfig
        from tts_engine.engine import TTSEngine

        engine_config = (
            config
            if isinstance(config, TTSEngineConfig)
            else TTSEngineConfig.from_dict(cast("dict[str, Any]", config))
        )
        self._sink = _QueueSink()
        self._engine = TTSEngine(engine_config, sink=self._sink)
        # The engine serializes producers, but binding the sink must also wait for
        # the previous producer's cancellation/drain to finish.
        self._stream_lock = asyncio.Lock()
        self._cleanups: set[asyncio.Task[None]] = set()

    @property
    def sample_rate(self) -> int:
        return self._engine.sample_rate

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        await self._stream_lock.acquire()
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
            cleanup = asyncio.create_task(self._finish_stream(say_task))
            self._cleanups.add(cleanup)
            cleanup.add_done_callback(self._cleanups.discard)
            # A second cancellation may end the consumer, but cleanup retains the
            # binding until the producer can no longer call feed()/drain().
            await asyncio.shield(cleanup)

    async def _finish_stream(self, producer: asyncio.Task[None]) -> None:
        try:
            await asyncio.gather(producer, return_exceptions=True)
        finally:
            self._sink.unbind()
            self._stream_lock.release()


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
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or not chunk:
            return
        loop.call_soon_threadsafe(queue.put_nowait, chunk)

    def drain(self) -> None:
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        loop.call_soon_threadsafe(queue.put_nowait, None)

    def unbind(self) -> None:
        self._loop = self._queue = None
