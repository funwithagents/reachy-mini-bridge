"""Verb tracing: one OpenTelemetry span per call of a verb that makes the robot sound or
move, with an event at each moment the caller cannot see (specs/core/tracing.md).

The names below are a public contract: a trace reader finds a verb by the tracer and the
span name, and reads its steps from the event names. The package depends on
``opentelemetry-api`` alone, so every span is a no-op until the application installs an
SDK.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from datetime import UTC, datetime
from typing import Literal, Self

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.util.types import AttributeValue

from .errors import SoundInterruptedError, SpeechInterruptedError

__all__ = [
    "ATTR_AUDIO_SECONDS",
    "ATTR_EMOTION",
    "ATTR_FOCUS",
    "ATTR_INPUT",
    "ATTR_MOTORS_STATE",
    "ATTR_OUTCOME",
    "ATTR_SOUND_FILE",
    "ATTR_STREAMED",
    "ATTR_SYNTHESIZER",
    "ATTR_TRACK_ID",
    "EVENT_FIRST_PUSH",
    "EVENT_FIRST_TEXT",
    "EVENT_MOTION_STARTED",
    "EVENT_PLAYBACK_ENDED",
    "EVENT_QUEUED",
    "EVENT_SOUND_STARTED",
    "EVENT_TRAJECTORY_ENDED",
    "EVENT_TRAJECTORY_STARTED",
    "SPAN_PLAY_EMOTION",
    "SPAN_PLAY_SOUND",
    "SPAN_SAY",
    "SPAN_SET_MOTORS_STATE",
    "SPAN_START_HEAD_TRACKING",
    "SPAN_STOP_HEAD_TRACKING",
    "TRACER_NAME",
    "Outcome",
    "VerbSpan",
    "current",
    "mark",
    "set_attribute",
    "verb_span",
    "wall_clock",
]

TRACER_NAME = "reachy_mini_bridge"

SPAN_SAY = "reachy_mini_bridge.say"
SPAN_PLAY_EMOTION = "reachy_mini_bridge.play_emotion"
SPAN_PLAY_SOUND = "reachy_mini_bridge.play_sound"
SPAN_SET_MOTORS_STATE = "reachy_mini_bridge.set_motors_state"
SPAN_START_HEAD_TRACKING = "reachy_mini_bridge.start_head_tracking"
SPAN_STOP_HEAD_TRACKING = "reachy_mini_bridge.stop_head_tracking"

EVENT_FIRST_TEXT = "first_text"
EVENT_FIRST_PUSH = "first_push"
EVENT_PLAYBACK_ENDED = "playback_ended"
EVENT_QUEUED = "queued"
EVENT_MOTION_STARTED = "motion_started"
EVENT_TRAJECTORY_STARTED = "trajectory_started"
EVENT_TRAJECTORY_ENDED = "trajectory_ended"
EVENT_SOUND_STARTED = "sound_started"

ATTR_OUTCOME = "reachy_mini_bridge.outcome"
ATTR_INPUT = "reachy_mini_bridge.input"
ATTR_SYNTHESIZER = "reachy_mini_bridge.synthesizer"
ATTR_STREAMED = "reachy_mini_bridge.streamed"
ATTR_AUDIO_SECONDS = "reachy_mini_bridge.audio_seconds"
ATTR_EMOTION = "reachy_mini_bridge.emotion"
ATTR_SOUND_FILE = "reachy_mini_bridge.sound_file"
ATTR_MOTORS_STATE = "reachy_mini_bridge.motors_state"
ATTR_FOCUS = "reachy_mini_bridge.focus"
ATTR_TRACK_ID = "reachy_mini_bridge.track_id"

Outcome = Literal["completed", "interrupted", "cancelled", "failed"]

_current: contextvars.ContextVar[VerbSpan | None] = contextvars.ContextVar(
    "reachy_mini_bridge_verb_span", default=None
)


class VerbSpan:
    """One call of a verb as a span: a child of the span current when it is created,
    current itself inside its ``with`` block — so a span opened by anything the verb
    runs nests under it — and ended on every way out with the call's outcome.

    Times are wall clock, anchored once: the span's start is ``time.time_ns()`` and
    every later stamp adds the time elapsed on ``time.perf_counter()``, the bridge's
    clock, so the stamps of one span never step backwards and a moment the bridge
    stamped on its own clock (:meth:`at`) lands on the same scale.
    """

    def __init__(
        self, name: str, attributes: dict[str, AttributeValue] | None = None
    ) -> None:
        self._wall_start = time.time_ns()
        self._mono_start = time.perf_counter_ns()
        self._lock = threading.Lock()
        self._marked: set[str] = set()
        self._ended = False
        self._tokens: (
            tuple[
                contextvars.Token[otel_context.Context],
                contextvars.Token[VerbSpan | None],
            ]
            | None
        ) = None
        self._span = trace.get_tracer(TRACER_NAME).start_span(
            name, start_time=self._wall_start, attributes=attributes
        )

    def now(self) -> int:
        """The present, in wall-clock nanoseconds on the span's anchor."""
        return self._wall_start + time.perf_counter_ns() - self._mono_start

    def at(self, ts: float) -> int:
        """The wall-clock nanoseconds of ``ts``, a ``time.perf_counter()`` reading."""
        return self._wall_start + round(ts * 1e9) - self._mono_start

    def mark(self, name: str, *, at: float | None = None) -> None:
        """Add the event ``name`` once — the first call wins, from whichever thread —
        at ``at`` (a ``time.perf_counter()`` reading), else now. A no-op once the span
        has ended."""
        with self._lock:
            if self._ended or name in self._marked:
                return
            self._marked.add(name)
            stamp = self.now() if at is None else self.at(at)
            self._span.add_event(name, timestamp=stamp)

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        with self._lock:
            if not self._ended:
                self._span.set_attribute(key, value)

    def __enter__(self) -> Self:
        self._tokens = (
            otel_context.attach(trace.set_span_in_context(self._span)),
            _current.set(self),
        )
        return self

    def __exit__(
        self, _exc_type: object, exc: BaseException | None, _tb: object
    ) -> None:
        if self._tokens is not None:
            span_token, current_token = self._tokens
            self._tokens = None
            _current.reset(current_token)
            otel_context.detach(span_token)
        self.end(exc)

    def end(self, exc: BaseException | None = None) -> None:
        """End the span with the outcome ``exc`` gives: none is ``completed``, a newer
        utterance or sound file taking over ``interrupted``, a cancel ``cancelled``,
        anything else ``failed`` with the exception recorded."""
        outcome: Outcome
        with self._lock:
            if self._ended:
                return
            self._ended = True
            if exc is None:
                outcome = "completed"
            elif isinstance(exc, SpeechInterruptedError | SoundInterruptedError):
                outcome = "interrupted"
            elif isinstance(exc, asyncio.CancelledError):
                outcome = "cancelled"
            else:
                outcome = "failed"
                self._span.record_exception(exc)
                self._span.set_status(Status(StatusCode.ERROR, str(exc)))
            self._span.set_attribute(ATTR_OUTCOME, outcome)
            self._span.end(end_time=self.now())


def verb_span(
    name: str, attributes: dict[str, AttributeValue] | None = None
) -> VerbSpan:
    """The span of one verb call, to hold in a ``with`` block around the call."""
    return VerbSpan(name, attributes)


def current() -> VerbSpan | None:
    """The verb span current in this context, for a stamp made later from elsewhere."""
    return _current.get()


def mark(name: str, *, at: float | None = None) -> None:
    """:meth:`VerbSpan.mark` on the verb span current in this context; nothing when a
    session's method runs outside any verb's span."""
    span = _current.get()
    if span is not None:
        span.mark(name, at=at)


def set_attribute(key: str, value: AttributeValue) -> None:
    """:meth:`VerbSpan.set_attribute` on the verb span current in this context."""
    span = _current.get()
    if span is not None:
        span.set_attribute(key, value)


def wall_clock(ts: float) -> datetime:
    """The UTC wall-clock time of ``ts``, a stamp on the bridge's clock
    (``time.perf_counter()``: a frame's ``ts``, a mic chunk's, a user's sighting).

    The two clocks are read together at the call, so the conversion carries no drift
    accumulated since the process started; it is exact for a recent stamp and drifts
    with the clocks' difference for an old one.
    """
    return datetime.fromtimestamp(time.time() - (time.perf_counter() - ts), UTC)
