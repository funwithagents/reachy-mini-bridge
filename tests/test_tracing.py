"""The verbs' spans on the fake backend (specs/core/tracing.md): what an application
that installed an OpenTelemetry SDK reads for each call — one span, a child of the
caller's, its events at the moments they name, its outcome on every way out."""

from __future__ import annotations

import asyncio
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, StatusCode

from reachy_mini_bridge import ReachyMiniBridge, tracing
from reachy_mini_bridge.config import (
    FaceDetectionSettings,
    MotionSettings,
    ReachyMiniConfig,
    TrackingSettings,
)
from reachy_mini_bridge.errors import SoundInterruptedError, SpeechInterruptedError
from reachy_mini_bridge.face_detection import PixelFace
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.motion import BLEND_S

_exporter = InMemorySpanExporter()
_installed = False

RATE = 16000
SILENCE_S = 0.15  # what the slow synthesizer waits before its first audio
TONE_S = 0.2  # the audio of one utterance
NS = 1e9
ANCHOR_NS = 1e6  # two spans' wall-clock anchors agree to well within a millisecond


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """The spans ended since the test started. The SDK's provider is process-wide and
    set once, so it is installed on first use and the exporter emptied per test."""
    global _installed
    if not _installed:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
        _installed = True
    _exporter.clear()
    return _exporter


class _SlowSynth:
    """Silent for ``SILENCE_S``, then ``TONE_S`` of audio in two chunks."""

    sample_rate = RATE

    async def synthesize(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        await asyncio.sleep(SILENCE_S)
        for _ in range(2):
            yield np.full(int(RATE * TONE_S / 2), 0.2, dtype=np.float32)


class _StreamingSynth(_SlowSynth):
    """Speaks each piece of text as it arrives."""

    async def synthesize_stream(
        self, text_stream: AsyncIterator[str]
    ) -> AsyncIterator[npt.NDArray[np.float32]]:
        async for piece in text_stream:
            if piece:
                yield np.full(int(RATE * TONE_S / 2), 0.2, dtype=np.float32)


class _TracedSynth(_SlowSynth):
    """A synthesizer that traces itself the way tts-engine does: a span of its own,
    opened in a task it creates while the bridge pulls its audio."""

    async def synthesize(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        async def produce() -> npt.NDArray[np.float32]:
            with trace.get_tracer("a_synthesizer").start_as_current_span("utterance"):
                return np.full(int(RATE * TONE_S), 0.2, dtype=np.float32)

        yield await asyncio.create_task(produce())


class _StallingSynth:
    """One chunk of ``TONE_S / 2``, then nothing more: an utterance caught mid-flight
    for as long as the test wants, whatever the host's timers do."""

    sample_rate = RATE

    async def synthesize(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        yield np.full(int(RATE * TONE_S / 2), 0.2, dtype=np.float32)
        await asyncio.Event().wait()


async def _speaking(bridge: ReachyMiniBridge) -> None:
    """Wait until audio has been pushed to the fake's speaker."""
    robot = bridge.robot
    assert isinstance(robot, FakeReachyMini)
    while not any(name == "media.push_audio_sample" for name, _ in robot.commands):
        await asyncio.sleep(0.005)


async def _pieces(*pieces: str, delay: float = 0.0) -> AsyncIterator[str]:
    for piece in pieces:
        await asyncio.sleep(delay)
        yield piece


def _named(spans: Sequence[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]


def _one(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    (span,) = _named(exporter.get_finished_spans(), name)
    return span


def _events(span: ReadableSpan) -> dict[str, int]:
    names = [event.name for event in span.events]
    assert len(names) == len(set(names))  # each event once
    return {event.name: event.timestamp for event in span.events}


def _attr(span: ReadableSpan, key: str) -> object:
    assert span.attributes is not None
    return span.attributes.get(key)


def _outcome(span: ReadableSpan) -> object:
    return _attr(span, tracing.ATTR_OUTCOME)


def _context(span: ReadableSpan) -> SpanContext:
    context = span.get_span_context()
    assert context is not None
    return context


def _start(span: ReadableSpan) -> int:
    assert span.start_time is not None
    return span.start_time


def _end(span: ReadableSpan) -> int:
    assert span.end_time is not None
    return span.end_time


def _wav(directory: Path, seconds: float) -> Path:
    path = directory / "sound.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"\x00\x00" * int(RATE * seconds))
    return path


async def _cancel_after(work: Awaitable[None], delay: float) -> None:
    task = asyncio.ensure_future(work)
    await asyncio.sleep(delay)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- say ---------------------------------------------------------------------------


def test_say_is_one_span_under_the_callers_with_its_first_push_and_playback_end(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> tuple[int, int]:
        async with ReachyMiniBridge("fake", synthesizer=_SlowSynth()) as bridge:
            with trace.get_tracer("caller").start_as_current_span("command") as parent:
                before = time.time_ns()
                await bridge.say("hello")
            return parent.get_span_context().span_id, before

    parent_id, before = asyncio.run(run())
    span = _one(spans, tracing.SPAN_SAY)
    events = _events(span)

    assert span.parent is not None and span.parent.span_id == parent_id
    assert list(events) == [tracing.EVENT_FIRST_PUSH, tracing.EVENT_PLAYBACK_ENDED]
    # Wall clock, and at the push: the synthesizer's silence is before it, the audio's
    # duration between it and the estimated end of playback, the call's return after.
    assert abs(_start(span) - before) < 0.05 * NS
    first_push = events[tracing.EVENT_FIRST_PUSH]
    assert first_push - _start(span) >= SILENCE_S * NS
    played = events[tracing.EVENT_PLAYBACK_ENDED] - first_push
    assert played == pytest.approx(TONE_S * NS, abs=0.05 * NS)
    assert _end(span) >= events[tracing.EVENT_PLAYBACK_ENDED]
    assert _attr(span, tracing.ATTR_INPUT) == "text"
    assert _attr(span, tracing.ATTR_SYNTHESIZER) == "_SlowSynth"
    assert _attr(span, tracing.ATTR_STREAMED) is False
    assert _attr(span, tracing.ATTR_AUDIO_SECONDS) == pytest.approx(TONE_S)
    assert _outcome(span) == "completed"


def test_say_stream_stamps_the_first_text_before_the_first_push(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake", synthesizer=_StreamingSynth()) as bridge:
            await bridge.say_stream(_pieces("", "hello ", "there", delay=0.1))

    asyncio.run(run())
    span = _one(spans, tracing.SPAN_SAY)
    events = _events(span)

    assert list(events) == [
        tracing.EVENT_FIRST_TEXT,
        tracing.EVENT_FIRST_PUSH,
        tracing.EVENT_PLAYBACK_ENDED,
    ]
    # The first piece is empty: the text started with the second, 0.2 s in.
    assert events[tracing.EVENT_FIRST_TEXT] - _start(span) >= 0.2 * NS
    assert _attr(span, tracing.ATTR_INPUT) == "text_stream"
    assert _attr(span, tracing.ATTR_STREAMED) is True
    assert _outcome(span) == "completed"


def test_say_stream_through_a_whole_text_synthesizer_is_not_streamed(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake", synthesizer=_SlowSynth()) as bridge:
            await bridge.say_stream(_pieces("hello ", "there", delay=0.1))

    asyncio.run(run())
    span = _one(spans, tracing.SPAN_SAY)
    events = _events(span)

    assert _attr(span, tracing.ATTR_STREAMED) is False
    # Speech waits for the whole text: both pieces, then the synthesizer's silence.
    waited = events[tracing.EVENT_FIRST_PUSH] - events[tracing.EVENT_FIRST_TEXT]
    assert waited >= (0.1 + SILENCE_S) * NS


def test_a_synthesizers_own_span_nests_under_the_says(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake", synthesizer=_TracedSynth()) as bridge:
            await bridge.say("hello")

    asyncio.run(run())
    say = _one(spans, tracing.SPAN_SAY)
    utterance = _one(spans, "utterance")

    assert utterance.parent is not None
    assert utterance.parent.span_id == _context(say).span_id
    assert _context(utterance).trace_id == _context(say).trace_id


def test_an_interrupted_say_ends_its_span_interrupted(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            first = asyncio.create_task(bridge.say("first", _StallingSynth()))
            await _speaking(bridge)
            await bridge.say("second", _SlowSynth())
            with pytest.raises(SpeechInterruptedError):
                await first

    asyncio.run(run())
    first, second = sorted(
        _named(spans.get_finished_spans(), tracing.SPAN_SAY), key=_start
    )

    assert _outcome(first) == "interrupted"
    assert tracing.EVENT_FIRST_PUSH in _events(first)
    assert tracing.EVENT_PLAYBACK_ENDED not in _events(first)  # never heard to its end
    assert first.status.status_code is StatusCode.UNSET
    assert _outcome(second) == "completed"
    assert _end(first) <= _end(second)


def test_a_cancelled_say_ends_its_span_cancelled(spans: InMemorySpanExporter) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake", synthesizer=_StallingSynth()) as bridge:
            task = asyncio.create_task(bridge.say("hello"))
            await _speaking(bridge)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    span = _one(spans, tracing.SPAN_SAY)

    assert _outcome(span) == "cancelled"
    assert list(_events(span)) == [tracing.EVENT_FIRST_PUSH]
    # What was pushed before the cancel.
    assert _attr(span, tracing.ATTR_AUDIO_SECONDS) == pytest.approx(TONE_S / 2)


def test_a_say_with_no_synthesizer_ends_its_span_failed(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            with pytest.raises(Exception, match="requires a SpeechSynthesizer"):
                await bridge.say("hello")

    asyncio.run(run())
    span = _one(spans, tracing.SPAN_SAY)

    assert _outcome(span) == "failed"
    assert span.status.status_code is StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]


# --- play_emotion ------------------------------------------------------------------


def test_play_emotion_stamps_the_queue_the_blend_and_the_trajectory(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> int:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            with trace.get_tracer("caller").start_as_current_span("command") as parent:
                await bridge.play_emotion("curious")
            return parent.get_span_context().span_id

    parent_id = asyncio.run(run())
    span = _one(spans, tracing.SPAN_PLAY_EMOTION)
    events = _events(span)

    assert span.parent is not None and span.parent.span_id == parent_id
    assert list(events) == [
        tracing.EVENT_QUEUED,
        tracing.EVENT_MOTION_STARTED,
        tracing.EVENT_TRAJECTORY_STARTED,
        tracing.EVENT_TRAJECTORY_ENDED,
    ]
    stamps = list(events.values())
    assert stamps == sorted(stamps)
    assert _start(span) <= stamps[0] and stamps[-1] <= _end(span)
    # The blend into the move's start pose, then the fake's 0.3 s trajectory: each
    # step is stamped at the loop's tick that took it, a tick or more past its length.
    blend = (
        events[tracing.EVENT_TRAJECTORY_STARTED] - events[tracing.EVENT_MOTION_STARTED]
    )
    assert BLEND_S * NS <= blend < (BLEND_S + 0.25) * NS
    played = (
        events[tracing.EVENT_TRAJECTORY_ENDED]
        - events[tracing.EVENT_TRAJECTORY_STARTED]
    )
    assert 0.3 * NS <= played < 0.55 * NS
    assert _attr(span, tracing.ATTR_EMOTION) == "curious"
    assert _outcome(span) == "completed"


@pytest.mark.realtime  # the second call lands while the first still plays
def test_an_emotion_called_while_another_plays_starts_moving_when_it_ends(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            first = asyncio.create_task(bridge.play_emotion("happy"))
            await asyncio.sleep(0.1)
            await bridge.play_emotion("sad")
            await first

    asyncio.run(run())
    emotions = {
        _attr(span, tracing.ATTR_EMOTION): _events(span)
        for span in _named(spans.get_finished_spans(), tracing.SPAN_PLAY_EMOTION)
    }
    first, second = emotions["happy"], emotions["sad"]

    # The second waited its turn: queued while the first played, moving only once the
    # first's trajectory had ended.
    assert second[tracing.EVENT_QUEUED] < first[tracing.EVENT_TRAJECTORY_ENDED]
    # The loop takes the next move at the tick that ends the first: the same instant,
    # read through each span's own anchor on the wall clock (ANCHOR_NS apart at most).
    assert (
        second[tracing.EVENT_MOTION_STARTED]
        >= first[tracing.EVENT_TRAJECTORY_ENDED] - ANCHOR_NS
    )
    waited = second[tracing.EVENT_MOTION_STARTED] - second[tracing.EVENT_QUEUED]
    assert waited > 0.3 * NS


@pytest.mark.realtime  # the second call lands while the first still plays
def test_a_cancelled_emotion_ends_its_span_cancelled_at_the_step_it_reached(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            playing = asyncio.create_task(bridge.play_emotion("happy"))
            await asyncio.sleep(0.1)  # in its entry blend
            await _cancel_after(bridge.play_emotion("sad"), 0.05)  # still queued
            playing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await playing

    asyncio.run(run())
    emotions = {
        _attr(span, tracing.ATTR_EMOTION): span
        for span in _named(spans.get_finished_spans(), tracing.SPAN_PLAY_EMOTION)
    }

    assert _outcome(emotions["sad"]) == "cancelled"
    assert list(_events(emotions["sad"])) == [tracing.EVENT_QUEUED]  # never moved
    assert _outcome(emotions["happy"]) == "cancelled"
    assert list(_events(emotions["happy"])) == [
        tracing.EVENT_QUEUED,
        tracing.EVENT_MOTION_STARTED,
    ]


def test_an_unknown_emotion_ends_its_span_failed(spans: InMemorySpanExporter) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            with pytest.raises(ValueError, match="not found"):
                await bridge.play_emotion("nonexistent")

    asyncio.run(run())
    span = _one(spans, tracing.SPAN_PLAY_EMOTION)

    assert _outcome(span) == "failed"
    assert span.status.status_code is StatusCode.ERROR
    assert _attr(span, tracing.ATTR_EMOTION) == "nonexistent"


# --- the other verbs ---------------------------------------------------------------


def test_play_sound_stamps_the_start_of_the_file(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    sound = _wav(tmp_path, 0.2)

    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.play_sound(str(sound))

    asyncio.run(run())
    span = _one(spans, tracing.SPAN_PLAY_SOUND)
    events = _events(span)

    assert list(events) == [tracing.EVENT_SOUND_STARTED]
    assert _end(span) - events[tracing.EVENT_SOUND_STARTED] >= 0.2 * NS
    assert _attr(span, tracing.ATTR_SOUND_FILE) == str(sound)
    assert _attr(span, tracing.ATTR_AUDIO_SECONDS) == pytest.approx(0.2)
    assert _outcome(span) == "completed"


def test_a_replaced_and_a_cancelled_sound_end_their_spans_so(
    spans: InMemorySpanExporter, tmp_path: Path
) -> None:
    sound = _wav(tmp_path, 1.0)

    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            first = asyncio.create_task(bridge.play_sound(str(sound)))
            await asyncio.sleep(0.1)
            await _cancel_after(bridge.play_sound(str(sound)), 0.1)
            with pytest.raises(SoundInterruptedError):
                await first

    asyncio.run(run())
    first, second = sorted(
        _named(spans.get_finished_spans(), tracing.SPAN_PLAY_SOUND), key=_start
    )

    assert _outcome(first) == "interrupted"
    assert _outcome(second) == "cancelled"
    assert tracing.EVENT_SOUND_STARTED in _events(second)


def test_a_missing_sound_file_ends_its_span_failed_before_any_start(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            with pytest.raises(FileNotFoundError):
                await bridge.play_sound("no-such-file.wav")

    asyncio.run(run())
    span = _one(spans, tracing.SPAN_PLAY_SOUND)

    assert _outcome(span) == "failed"
    assert [event.name for event in span.events] == ["exception"]


def test_set_motors_state_is_one_span_with_the_state_asked(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            with pytest.raises(ValueError, match="unknown motor state"):
                await bridge.set_motors_state("floppy")

    asyncio.run(run())
    enabled, floppy = sorted(
        _named(spans.get_finished_spans(), tracing.SPAN_SET_MOTORS_STATE), key=_start
    )

    assert _attr(enabled, tracing.ATTR_MOTORS_STATE) == "enabled"
    assert _outcome(enabled) == "completed"
    assert _attr(floppy, tracing.ATTR_MOTORS_STATE) == "floppy"
    assert _outcome(floppy) == "failed"


class _NoFace:
    def detect(self, frame_bgr: npt.NDArray[np.uint8], ts: float) -> list[PixelFace]:
        return []


def test_head_tracking_verbs_are_one_span_each(spans: InMemorySpanExporter) -> None:
    config = ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="custom", enabled=False, face_detector=_NoFace
        ),
        motion=MotionSettings(tracking=TrackingSettings(enabled=False)),
    )

    async def run() -> None:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.start_head_tracking(focus=True)
            await bridge.stop_head_tracking()
            with pytest.raises(ValueError, match="no user 7 is present"):
                await bridge.start_head_tracking(track_id=7)

    asyncio.run(run())
    finished = spans.get_finished_spans()
    started, refused = sorted(
        _named(finished, tracing.SPAN_START_HEAD_TRACKING), key=_start
    )
    (stopped,) = _named(finished, tracing.SPAN_STOP_HEAD_TRACKING)

    assert _attr(started, tracing.ATTR_FOCUS) is True
    assert _attr(started, tracing.ATTR_TRACK_ID) is None  # the automatic target
    assert _outcome(started) == "completed"
    assert _outcome(stopped) == "completed"
    assert _end(started) <= _start(stopped)
    assert _attr(refused, tracing.ATTR_TRACK_ID) == 7
    assert _outcome(refused) == "failed"


def test_a_verb_that_only_reads_state_opens_no_span(
    spans: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.get_motors_state()
            await bridge.list_emotions()

    asyncio.run(run())
    assert spans.get_finished_spans() == ()


# --- the span itself ---------------------------------------------------------------


@pytest.mark.parametrize(
    "verb",
    [
        pytest.param(lambda b: b.say("hello"), id="say"),
        pytest.param(lambda b: b.say_stream(_pieces("hello")), id="say_stream"),
        pytest.param(lambda b: b.play_emotion("happy"), id="play_emotion"),
        pytest.param(lambda b: b.play_sound("wake_up.wav"), id="play_sound"),
    ],
)
def test_each_call_is_one_span_and_leaves_the_callers_current(
    spans: InMemorySpanExporter,
    verb: Callable[[ReachyMiniBridge], Awaitable[None]],
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake", synthesizer=_SlowSynth()) as bridge:
            await bridge.set_motors_state("enabled")
            spans.clear()
            with trace.get_tracer("caller").start_as_current_span("command") as parent:
                await verb(bridge)
                assert trace.get_current_span() is parent
            assert tracing.current() is None

    asyncio.run(run())
    bridge_spans = [
        span
        for span in spans.get_finished_spans()
        if span.name.startswith("reachy_mini_bridge.")
    ]
    assert len(bridge_spans) == 1
    assert _outcome(bridge_spans[0]) == "completed"


def test_an_event_is_stamped_once_and_never_after_the_end(
    spans: InMemorySpanExporter,
) -> None:
    with tracing.verb_span("reachy_mini_bridge.test") as span:
        at = time.perf_counter() - 0.5  # a moment the bridge stamped on its own clock
        span.mark("step", at=at)
        span.mark("step")
        tracing.mark("current")  # through the context
    span.mark("late")
    span.set_attribute("late", True)

    finished = _one(spans, "reachy_mini_bridge.test")
    events = _events(finished)
    assert list(events) == ["step", "current"]
    assert events["current"] - events["step"] == pytest.approx(0.5 * NS, abs=0.05 * NS)
    assert events["step"] < _start(finished)  # a stamp is placed where it happened
    assert _attr(finished, "late") is None


def test_wall_clock_converts_a_stamp_of_the_bridges_clock() -> None:
    stamp = time.perf_counter() - 2.0
    converted = tracing.wall_clock(stamp)

    assert converted.tzinfo is UTC
    ago = (datetime.now(UTC) - converted).total_seconds()
    assert ago == pytest.approx(2.0, abs=0.05)
