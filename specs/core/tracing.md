---
code:
  - src/reachy_mini_bridge/tracing.py
  - src/reachy_mini_bridge/bridge.py
  - src/reachy_mini_bridge/audio.py
  - src/reachy_mini_bridge/motion.py
tests:
  - tests/test_tracing.py
---

# Verb tracing (`tracing.py`)

**Status:** Implemented

## Purpose

A caller awaits a verb and learns when the call was made and when it returned. The moments in between are the bridge's: when the first audio of an utterance reached the speaker, when a queued emotion left the queue and the robot started to move. An application that measures how long a person waited for the robot's reaction needs those moments, and only the bridge has them.

The bridge reports them as **OpenTelemetry spans**: one span per call of a verb that makes the robot sound or move, with an event at each of those moments. The application that embeds the bridge collects the spans with the SDK and exporter of its choice, and because OpenTelemetry's context travels through `contextvars`, the bridge's spans join the application's traces with no code specific to either side.

## Core concepts / Decided

### The API alone

The package depends on `opentelemetry-api` (`>=1.20,<2`, the bound tts-engine declares) and on nothing else of OpenTelemetry. With the API alone every span is a no-op, so an application that installs no SDK runs the bridge as before; one that installs an SDK receives the spans. Tracing is always on and has no configuration: its cost is a few clock reads per call. The SDK is a `dev` group dependency, for the in-memory exporter the tests read spans from ([project.md](../project.md)).

### The spans

One tracer, named `reachy_mini_bridge`. One span per call of each of these verbs, opened as the call starts and ended as it returns or raises:

| Span | Verb | Covers |
|---|---|---|
| `reachy_mini_bridge.say` | `say`, `say_stream` | the call, to the estimated end of playback plus the tail margin ([audio.md](../audio/audio.md) "Completion") |
| `reachy_mini_bridge.play_emotion` | `play_emotion` | the call, to the end of the trajectory |
| `reachy_mini_bridge.play_sound` | `play_sound` | the call, to the end of the file plus the tail margin |
| `reachy_mini_bridge.set_motors_state` | `set_motors_state` | the call |
| `reachy_mini_bridge.start_head_tracking` | `start_head_tracking` | the call |
| `reachy_mini_bridge.stop_head_tracking` | `stop_head_tracking` | the call |

These are the verbs through which a caller makes the robot do something at a moment of its choosing. A verb that reads state (`get_motors_state`, `list_emotions`) and a mode setter (`set_tracking`, `set_presence`, `set_idle`, `set_wobbling`, `set_face_detection`, `set_base_following`) opens no span, and `start_head_tracking` calling `set_tracking` is one span, its own.

Every span carries the call's whole life, argument checks included: a `play_emotion` with an unknown name is a span that ends `failed`.

### The events

An event marks a moment inside the call. Each is stamped once per span, at the moment it names.

| Span | Event | The moment |
|---|---|---|
| `say` | `first_text` | the first non-empty piece of text arrived from the text stream (`say_stream` only) |
| `say` | `first_push` | the first audio was handed to the speaker (`media.push_audio_sample`): **the utterance's effect start** |
| `say` | `playback_ended` | the estimated end of what was heard — the playback tracker's end, before the tail margin; present when the utterance played to its end |
| `play_emotion` | `queued` | the move was handed to the motion loop; before it, the call checked the motors, loaded the emotions library (a download on the first call of a machine) and paused wobbling |
| `play_emotion` | `motion_started` | the loop took the move out of its queue and began the blend into its start pose: **the emotion's effect start** — the robot begins to move here |
| `play_emotion` | `trajectory_started` | the blend ended and the recorded move began, with its sound |
| `play_emotion` | `trajectory_ended` | the recorded move played to its end; present when it did |
| `play_sound` | `sound_started` | the file player started the file: **the sound's effect start** |

A step the call did not reach has no event: a `say` cancelled before any audio has no `first_push`, an emotion cancelled in the queue has `queued` alone. `set_motors_state` and the head-tracking verbs have no event — their span's start and end are what there is to know; the head turns toward a face when one is seen, which belongs to no call.

The three loop events of an emotion are stamped by the motion loop at the tick that took the step — the tick's own time, so `motion_started` of a call queued behind another equals the `trajectory_ended` of the one before it — and placed on the span by the bridge when the call ends.

### The attributes

| Attribute | On | Value |
|---|---|---|
| `reachy_mini_bridge.outcome` | every span | how the call ended (below) |
| `reachy_mini_bridge.input` | `say` | `"text"` for `say`, `"text_stream"` for `say_stream` |
| `reachy_mini_bridge.synthesizer` | `say` | the synthesizer's class name |
| `reachy_mini_bridge.streamed` | `say` | `true` when the text is synthesized as it arrives (a `say_stream` through a `StreamingSpeechSynthesizer`); `false` for `say` and for a `say_stream` that speaks the whole text once the stream ends |
| `reachy_mini_bridge.audio_seconds` | `say`, `play_sound` | `say`: the duration of the audio handed to the speaker, up to the call's end; `play_sound`: the file's duration |
| `reachy_mini_bridge.emotion` | `play_emotion` | the name asked for |
| `reachy_mini_bridge.sound_file` | `play_sound` | the file asked for, as given |
| `reachy_mini_bridge.motors_state` | `set_motors_state` | the state asked for |
| `reachy_mini_bridge.focus` | `start_head_tracking` | the `focus` argument, when given |
| `reachy_mini_bridge.track_id` | `start_head_tracking` | the pinned `track_id`, when given |

**The outcome** is one of four values, and a span ends with one on every way out of the call:

- `completed` — the call returned.
- `interrupted` — a newer call took over: a `say` replaced by a later `say` or `say_stream` (`SpeechInterruptedError`), a sound file replaced by a later one (`SoundInterruptedError`). The span's status stays unset: the newest winning is the designed behaviour ([bridge.md](bridge.md) "Cancellation").
- `cancelled` — the awaiting task was cancelled. It is the call's outcome: an instant verb's accepted command still completes ([bridge.md](bridge.md) "Cancellation"), so a cancelled `set_motors_state` span says the caller stopped waiting, and the motors' state is read with `get_motors_state`.
- `failed` — any other exception, recorded on the span (`record_exception`) with the status `ERROR`.

### Parents and children

- **A verb's span is a child of the span current when the verb is awaited.** An application that awaits `say` inside a span of its own finds the bridge's span under it.
- **A span opened by what the verb runs is a descendant of the verb's span.** The verb's span is current for the whole call, and every task the call creates inherits it: a synthesizer that traces its own utterance — tts-engine does — nests under `reachy_mini_bridge.say`, whether it opens its span in the bridge's task or in one it creates.
- **The caller's span is current again when the call returns or raises.**

### The names are a contract

The tracer name, the span names, the event names, the attribute keys and the outcome values are public API, exported as constants from `reachy_mini_bridge.tracing` (`TRACER_NAME`, `SPAN_*`, `EVENT_*`, `ATTR_*`, `Outcome`). An application declares which event of which span marks an effect start and reads the others as the steps before it; a name changes only as a breaking change, and a new event or attribute is an addition.

### Times

- **Wall clock.** A span's start, its events and its end are UTC wall-clock times, comparable with the spans of any other package. Each span anchors once — `time.time_ns()` read at its start — and every later stamp adds the time elapsed on `time.perf_counter()`, the bridge's clock ([camera.md](../vision/camera.md) "The frame's time and the head pose"). The stamps of one span therefore never step backwards, and a moment the motion loop stamped on the bridge's clock lands on the same scale. Two spans' anchors agree to within the two clock reads, microseconds on an idle host.
- **Stamped from any thread.** An event is added to the span object it belongs to, with an explicit timestamp, so a moment observed on the motion loop's thread or in a task the synthesizer created is stamped the same way as one on the caller's task.
- **`wall_clock(ts)`** converts a stamp on the bridge's clock — a `CameraFrame.ts`, a `MicChunk.ts`, a `User.first_seen_ts` — to a UTC `datetime`, reading both clocks at the call. It is what an application uses to place a frame time on a trace: exact for a recent stamp, and independent of which monotonic clock the application's other libraries read (`time.monotonic()` is another clock than `time.perf_counter()` on Windows before Python 3.13).

### Continuous work carries stamps

The camera feed, the mic feed, the detection loop and the head tracker run at their own rate and belong to no single call. Their values carry a `ts` on the bridge's clock, and `wall_clock` converts it. The users present add the stamp an arrival's latency needs: `User.first_seen_ts`, the frame time of the track's first sighting, so the time a detector took to see someone and the confirmation that follows (`USER_CONFIRM_S`, `USER_CONFIRM_OBSERVATIONS`) are told apart ([user_perception.md](../vision/user_perception.md) "The users present").

### The module

```python
TRACER_NAME = "reachy_mini_bridge"
SPAN_SAY = "reachy_mini_bridge.say"          # ... one SPAN_* per span above
EVENT_FIRST_PUSH = "first_push"              # ... one EVENT_* per event above
ATTR_OUTCOME = "reachy_mini_bridge.outcome"  # ... one ATTR_* per attribute above
Outcome = Literal["completed", "interrupted", "cancelled", "failed"]

def wall_clock(ts: float) -> datetime: ...   # a bridge-clock stamp as UTC wall clock

class VerbSpan:                              # one call of a verb; a context manager
    def mark(self, name: str, *, at: float | None = None) -> None: ...
    def set_attribute(self, key: str, value: AttributeValue) -> None: ...

def verb_span(name: str, attributes: dict | None = None) -> VerbSpan: ...
def current() -> VerbSpan | None: ...        # the verb span current in this context
def mark(name: str, *, at: float | None = None) -> None: ...   # on the current one
def set_attribute(key: str, value: AttributeValue) -> None: ...
```

The names and `wall_clock` are for applications. `VerbSpan` and the functions over it are the bridge's own instrument: `ReachyMiniBridge` opens the span around each verb, the media session stamps its events on the span current in its context (`mark`, `set_attribute` — nothing when it runs outside a verb's span), and `motion.py` imports nothing of it: the loop records the time of each step on the primary it plays, on its own clock, and the bridge turns them into events. `mark` stamps a name once per span, the first call winning, and does nothing once the span has ended.

## Open questions

- **The output latency** — the delay between a push and the sound leaving the speaker — has no measurement in the bridge: the local path's sink buffers some tens of milliseconds, and a wireless robot's path (Opus, the network, the daemon's jitter buffer) is unmeasured. An attribute on the `say` span carries it once it is measured per media path. Deferred.
- **A capture time on mic chunks.** `MicChunk.ts` is the chunk's arrival; a wall-clock capture time would let a recognizer's end-of-speech stamp be corrected for the robot's capture latency. Deferred until measurements show the correction matters.
- **A span for bring-up** (`start()`, with an event per session started) would show where a slow start goes. Outside any exchange with a person, so deferred.
