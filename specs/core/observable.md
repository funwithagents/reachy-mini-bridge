---
code:
  - src/reachy_mini_bridge/observable.py
tests:
  - tests/test_observable.py
---

# Observable (`observable.py`)

**Status:** Implemented

## Purpose

`Observable[T]` is a value a caller reads directly and can subscribe to — the C#-style property: `value` for the current state, `changes()` to be woken when it is published. It is the bridge's one event mechanism, generic so every state a caller may want to await uses the same shape. Its first user is the face report ([user_perception.md](../vision/user_perception.md)); `attention` and later states follow.

It exists because the bridge otherwise has only two ways to expose state — a read-only property (poll it) and an async stream (`audio_input`, a firehose) — and neither fits a value that changes rarely and matters when it does: a caller wants to read it any time *and* be told when it changes, without polling and without a queue of every intermediate value.

## Core concepts / Decided

### The surface

```python
class Observable[T]:
    def __init__(self, initial: T) -> None: ...
    @property
    def value(self) -> T: ...                              # the current value; no await
    def changes(self) -> AsyncIterator[T]: ...             # yields each *published* value from now on
    async def wait_for(self, predicate: Callable[[T], bool]) -> T: ...  # the current value if it
                                                           # matches, else the first published one that does
    def set(self, value: T) -> None: ...                   # replace and publish
    def update(self, value: T) -> None: ...                # replace silently (no wake-up)
```

### Semantics

- **Read any time, subscribe from the event loop.** `value` is a plain attribute read from any thread. `changes()` is an async iterator: one bounded queue per subscriber, created when the iterator is first driven — so a subscriber sees only values published after it subscribed — and removed when the iterator is closed. A subscriber that falls behind gets the **latest** value, not every intermediate one (the value is a state, not a log), and nothing ever blocks the producer.
- **Cancellation.** A subscription follows [bridge.md](bridge.md)'s "Cancellation" contract: cancelling the task blocked in `async for` ends the iteration promptly and detaches the subscriber; the observable keeps serving the others.
- **Producers marshal onto the loop.** `set` / `update` are called on the event-loop thread and fail loudly elsewhere (no running loop); a producer on another thread — a detector worker, the motion thread — calls them through `loop.call_soon_threadsafe`. Subscribers are woken in publication order.
- **`update` vs `set` is how the owner defines "a change".** Equality does not decide it; the code that owns the value does. `update` replaces the value for anyone reading it; `set` also wakes the subscribers. The face report, for instance, is `update`d on every poll and `set` only when the number of faces changes or detection starts or stops ([user_perception.md](../vision/user_perception.md) "The detection loop").
- **`wait_for`** returns `value` at once when the predicate already holds, else the first published value that does — the idiom for "wait until someone is there" without a subscription of one's own.
- **Lifetime.** An observable belongs to the object that exposes it (`bridge.faces` to the bridge) and outlives that object's sessions: a caller may keep iterating across sessions and is told, through a published value, when the state resets.

### Testable without a robot

Pure asyncio, so `tests/` pin it directly: reads of the initial and the latest value; `set` wakes, `update` does not; every subscriber receives every published value when it keeps up, the latest of a burst when it does not; a cancelled subscriber detaches and later publications do not fail; `wait_for` both ways; `set` from a plain thread raises.

## Relationship to the other specs

- **[user_perception.md](../vision/user_perception.md):** `bridge.faces` is an `Observable[FaceReport]`; the detection loop is its producer.
- **[head_tracking.md](../motion/head_tracking.md):** `bridge.head_tracking` is an `Observable[HeadTrackingReport]`; the head tracker is its producer.
- **[bridge.md](bridge.md):** the "Cancellation" contract governs `changes()`.

## Open questions

1. **`attention` as an observable.** A derived string property today ([bridge.md](bridge.md)); making it an `Observable[str | None]` is a one-line addition, deferred until a caller wants to await it.
2. **Threadsafe producers.** Whether the class should offer `set_threadsafe(loop, value)` itself rather than leaving `call_soon_threadsafe` to the producer is deferred until a second producer on another thread exists.
