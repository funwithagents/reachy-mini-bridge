# Measuring what the robot did, and when — tracing

How to see when the robot actually started to speak or to move, for each call you make. A call such as `await bridge.say(...)` returns when the robot has finished; the moment the first sound left for the speaker is inside the call. The bridge reports those moments as [OpenTelemetry](https://opentelemetry.io/) spans, and this page shows how to read them. The names and their meaning are in the [API reference](../reference/api.md#tracing); the design is [specs/core/tracing.md](../../specs/core/tracing.md).

## What you need

The bridge creates its spans through `opentelemetry-api`, which it installs. They go nowhere until your application installs the OpenTelemetry **SDK** and tells it where to send them:

```
pip install opentelemetry-sdk        # or: uv add opentelemetry-sdk
```

There is nothing to switch on in the bridge and no configuration: with an SDK installed the spans arrive, without one they cost a few clock reads per call.

## See the spans

This program runs on the offline `fake`, with a synthesizer that waits a quarter of a second before its first audio — the time a real voice takes to start. It installs the SDK with a processor that prints each span as it ends: how long the call took, how it ended, and when each step happened, counted from the call.

```python
import asyncio

import numpy as np
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider

from reachy_mini_bridge import ReachyMiniBridge


class PrintSteps(SpanProcessor):
    """Prints each span when it ends, with its events as delays from its start."""

    def on_end(self, span: ReadableSpan) -> None:
        start = span.start_time
        steps = ", ".join(f"{e.name} +{(e.timestamp - start) / 1e9:.2f} s" for e in span.events)
        outcome = span.attributes.get("reachy_mini_bridge.outcome", "")
        print(f"{span.name}: {(span.end_time - start) / 1e9:.2f} s {outcome} | {steps}")


class SlowBeep:
    """A voice that thinks for a quarter of a second, then beeps for half a second."""

    sample_rate = 16000

    async def synthesize(self, text: str):
        await asyncio.sleep(0.25)
        t = np.arange(self.sample_rate // 2) / self.sample_rate
        yield (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


async def main() -> None:
    provider = TracerProvider()
    provider.add_span_processor(PrintSteps())
    trace.set_tracer_provider(provider)  # once, at your application's start
    tracer = trace.get_tracer("my_app")

    async with ReachyMiniBridge("fake", synthesizer=SlowBeep()) as bridge:
        await bridge.set_motors_state("enabled")
        with tracer.start_as_current_span("reaction"):  # your own span around a reaction
            await asyncio.gather(bridge.say("hello"), bridge.play_emotion("happy"))


asyncio.run(main())
```

It prints one line per span, in the order they end (your times will differ by a few hundredths):

```
reachy_mini_bridge.set_motors_state: 0.00 s completed | 
reachy_mini_bridge.play_emotion: 0.82 s completed | queued +0.00 s, motion_started +0.02 s, trajectory_started +0.52 s, trajectory_ended +0.82 s
reachy_mini_bridge.say: 0.86 s completed | first_push +0.25 s, playback_ended +0.75 s
reaction: 0.86 s  | 
```

## Read them

- **`first_push +0.25 s`** — the robot started to speak a quarter of a second after you asked: the synthesizer's thinking time. This is the utterance's *effect start*.
- **`motion_started +0.02 s`** — the robot started to move almost at once: the motion loop took the emotion at its next tick and began blending into it. Had another emotion been playing, this number would be the time spent waiting behind it. This is the emotion's effect start.
- **`trajectory_started +0.52 s`** — the blend took half a second, then the recorded move began, with its sound.
- **`playback_ended`, `trajectory_ended`** — the estimated end of what was heard, and the end of the move.
- **`completed`** — how the call ended. A `say` replaced by a newer one ends `interrupted`, a cancelled task `cancelled`, an error `failed` with the exception recorded on the span.

Every span the bridge opens is a child of the span that was current when you made the call — here `reaction` — so one trace holds a whole reaction: your span, the bridge's under it, and under `reachy_mini_bridge.say` the synthesizer's own span when it has one (tts-engine's voices do). An event a call never reached is absent: an emotion cancelled while it waited has `queued` and nothing after.

The [reference](../reference/api.md#tracing) lists every span, event and attribute. Their names are stable; import them from `reachy_mini_bridge.tracing` (`SPAN_SAY`, `EVENT_FIRST_PUSH`, `ATTR_OUTCOME`, …) when your code looks for one.

## Send them somewhere

`PrintSteps` stands for whatever your application does with spans. The SDK's own `ConsoleSpanExporter` prints each span whole, as JSON; an OTLP exporter sends them to a collector or a tracing backend that draws the trace as a timeline; a processor of your own, like the one above, feeds a latency view in your application. That choice is the application's — the bridge only creates the spans.

## Frames, faces and users

What runs continuously — the camera feed, the microphone, face detection, the users present — belongs to no single call, so it has no span. Its values carry a time, `ts`, on the bridge's clock, and `wall_clock` converts one to the UTC time a span uses:

```python
from reachy_mini_bridge.tracing import wall_clock

frame = bridge.camera.latest()
print(wall_clock(frame.ts))  # when that frame was read, as a UTC datetime
```

To measure how fast you reacted to someone arriving: `User.first_seen_ts` is when the detector first saw them, and the report that names them in `arrived` comes once they are confirmed, 0.3 s or more later ([perception-and-tracking.md](perception-and-tracking.md)).

## What the numbers leave out

`first_push` is the moment the audio enters the speaker's queue. The sound reaches the room a little later — a few tens of milliseconds on a robot or a sim connected to this machine, more on a wireless robot, whose audio crosses the network — and the bridge does not measure that last step. `playback_ended` is an estimate for the same reason: the audio's duration counted from the push.
