---
code:
  - src/reachy_mini_bridge/observable.py
tests:
  - tests/test_observable.py
---

# Observable (`observable.py`)

**Status:** Implemented

## Purpose

`Observable[T]` is a value a caller reads directly and can subscribe to — the C#-style property: `value` for the current state, `changes()` to be woken when it is published. It is the bridge's one event mechanism, generic so every state a caller may want to await uses the same shape. Its first user is the face report ([user_perception.md](../vision/user_perception.md)); the head tracking report and the camera feed's followers ([camera.md](../vision/camera.md) "Followers") use it too, `attention` and later states follow.

It exists because the bridge otherwise has only two ways to expose state — a read-only property (poll it) and an async stream (`audio_input`, a firehose) — and neither fits a value that changes rarely and matters when it does: a caller wants to read it any time *and* be told when it changes, without polling and without a queue of every intermediate value.

## Core concepts / Decided

### The surface

```python
class Observable[T]:
    def __init__(self, initial: T, *, history: int = 1) -> None: ...
    @property
    def value(self) -> T: ...                              # the current value; no await
    @property
    def history(self) -> int: ...                          # how far behind a subscriber may fall
    def changes(self, *, every: bool = False) -> AsyncIterator[T]: ...
                                                           # yields published values from now on:
                                                           # the newest (default), or each in order
    async def wait_for(self, predicate: Callable[[T], bool]) -> T: ...  # the current value if it
                                                           # matches, else the first published one that does
    def set(self, value: T) -> None: ...                   # replace and publish
    def update(self, value: T) -> None: ...                # replace silently (no wake-up)
    def set_threadsafe(self, loop: asyncio.AbstractEventLoop, value: T) -> None: ...
                                                           # set, from another thread, through loop

CHANGE_HISTORY = 64                                        # the history of the bridge's state observables
```

### Semantics

- **Read any time, subscribe from the event loop.** `value` is a plain attribute read from any thread. `changes()` is an async iterator: each subscriber has its own buffer of pending values, created when the iterator is first driven — so a subscriber sees only values published after it subscribed — and removed when the iterator is closed. Nothing ever blocks the producer, and a slow subscriber costs no other.
- **Two policies, split between the owner and the subscriber.** The owner knows what a value costs to keep and whether skipping one loses anything; the subscriber knows what it does with them. So:
  - **The owner sets the history** at construction (`history`, at least 1, default 1): how many published values a subscriber may fall behind by. A value that is a whole state and cheap to keep — a report published a few times a minute — gets a history; a value that is costly to hold or useless once superseded — a camera frame — keeps 1.
  - **The subscriber chooses how it reads** within it. `changes()` — the default — yields the **newest** published value whenever it comes back: a subscriber that keeps up sees every value, one that falls behind skips to the latest, never a backlog. A client reacting to state after a slow action (a greeting that takes seconds) wants the present, not the states it missed. `changes(every=True)` yields **every** published value in order, up to the owner's history: a client that counts or records events — an arrival, a departure — misses none. A subscriber that falls further behind than the history loses the oldest values it had not read; the gap is logged at `WARNING` with the number lost, once per gap, as a lapped mic subscriber's is ([microphone.md](../audio/microphone.md)), and costs no other subscriber.
  - With `history=1`, `every=True` reads the same as the default: the owner keeps nothing older for anyone.
  - The default is the newest so that a subscriber written for one value per wake-up keeps that behaviour whatever the owner's history; reading every value is a choice the subscriber states.
- **One value per iteration.** Either way the iterator yields one `T` per pass of `async for`; `every` changes which values are yielded, not their type.
- **Cancellation.** A subscription follows [bridge.md](bridge.md)'s "Cancellation" contract: cancelling the task blocked in `async for` ends the iteration promptly and detaches the subscriber; the observable keeps serving the others.
- **Producers marshal onto the loop.** `set` / `update` are called on the event-loop thread and fail loudly elsewhere (no running loop). A producer on another thread — the camera feed's reader, a detector worker, the motion thread — calls `set_threadsafe(loop, value)`, which queues the `set` on the loop it is given (`call_soon_threadsafe`) and raises `RuntimeError` on a closed loop, the producer's to handle. Subscribers are woken in publication order, and `value` changes on the loop's thread, a hop after the call: a producer that needs its own value at once keeps its own copy, as the camera feed's `latest()` does ([camera.md](../vision/camera.md) "Followers").
- **`update` vs `set` is how the owner defines "a change".** Equality does not decide it; the code that owns the value does. `update` replaces the value for anyone reading it; `set` also wakes the subscribers. The face report, for instance, is `update`d on every poll and `set` only when the set of faces in view changes or detection starts or stops ([user_perception.md](../vision/user_perception.md) "The detection loop").
- **`wait_for`** returns `value` at once when the predicate already holds, else the first published value that does — the idiom for "wait until someone is there" without a subscription of one's own. It reads every published value within the history (`every=True`), so a matching value followed by a non-matching one within the same loop step is still found.
- **The bridge's observables.** `bridge.faces`, `bridge.users` and `bridge.head_tracking` are states published on meaningful changes only — a few a minute — so they keep `CHANGE_HISTORY = 64` values: a client subscribed with `every=True` reads each change in order through any slow step it takes. The camera feed's followers' channel keeps 1 ([camera.md](../vision/camera.md) "Followers"): a frame superseded is a frame to skip, and frames are megabytes.
- **Lifetime.** An observable belongs to the object that exposes it (`bridge.faces` to the bridge) and outlives that object's sessions: a caller may keep iterating across sessions and is told, through a published value, when the state resets.

### Testable without a robot

Pure asyncio, so `tests/` pin it directly: reads of the initial and the latest value; `set` wakes, `update` does not; every subscriber receives every published value when it keeps up, the latest of a burst when it does not (default), every value of a burst in order with `every=True`, the newest only with `every=True` on a history of 1; a burst longer than the history losing its oldest values, with one `WARNING` naming the count, and the subscriber reading on; a default subscriber and an `every` one on the same observable each served by its own policy; a history below 1 refused; a cancelled subscriber detaches and later publications do not fail; `wait_for` both ways, and finding a matching value published just before a non-matching one; `set` from a plain thread raises, `set_threadsafe` from one publishes on the loop and raises on a closed loop.

## Relationship to the other specs

- **[user_perception.md](../vision/user_perception.md):** `bridge.faces` (`Observable[FaceReport]`) and `bridge.users` (`Observable[UsersReport]`) keep `CHANGE_HISTORY`; the detection loop is their producer.
- **[head_tracking.md](../motion/head_tracking.md):** `bridge.head_tracking` is an `Observable[HeadTrackingReport]` keeping `CHANGE_HISTORY`; the head tracker is its producer.
- **[camera.md](../vision/camera.md):** the camera feed's followers are served by an `Observable[CameraFrame | None]` with a history of 1, which its reader thread publishes into through `set_threadsafe`.
- **[microphone.md](../audio/microphone.md):** the mic feed's subscribers read every chunk in order through a ring of its own, the same policy as `every=True` sized in seconds of audio; the gap of a lapped subscriber is reported the same way.
- **[bridge.md](bridge.md):** the "Cancellation" contract governs `changes()`.

## Open questions

1. **`attention` as an observable.** A derived string property today ([bridge.md](bridge.md)); making it an `Observable[str | None]` is a one-line addition, deferred until a caller wants to await it.
