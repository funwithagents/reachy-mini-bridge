# Observable: the owner's history, the subscriber's `every`

**Status:** Done

Implements `specs/core/observable.md` ("Semantics": the two policies; "The bridge's observables"). The owner of an `Observable` sets how many published values a subscriber may fall behind by (`history`, default 1); a subscriber reads the newest by default, as today, or every value in order with `changes(every=True)`, a lapped one losing the oldest with a logged gap; `wait_for` reads every value. `bridge.faces` and `bridge.head_tracking` keep `CHANGE_HISTORY = 64`, the camera feed's channel keeps 1. Leaves out any new observable (the presence report is its own work) and any change to what the producers publish.

## Scope

- `src/reachy_mini_bridge/observable.py` — `history`, a per-subscriber buffer, `changes(every=...)`, the gap warning, `wait_for` on every value, `CHANGE_HISTORY`
- `src/reachy_mini_bridge/bridge.py` — `faces` and `head_tracking` built with `history=CHANGE_HISTORY`
- `tests/test_observable.py` — the policies pinned
- `specs/core/observable.md` — the design (Updated → Implemented at the end); `specs/_index.md` row
- `specs/vision/camera.md` — the followers' channel named as a history of 1
- `docs/reference/api.md`, `docs/guides/perception-and-tracking.md` — `every=True` documented beside the default

## Steps

1. `Observable.__init__(initial, *, history=1)`: `ValueError` below 1; `history` property.
2. One subscriber record per `changes()`: a deque of pending values, its capacity (`history` with `every`, else 1), an `asyncio.Event`, a lost count. `set` appends to each, dropping the oldest when full (counted for an `every` subscriber), and sets the event.
3. The iterator waits on the event while the deque is empty, logs one `WARNING` with the lost count before yielding after a gap, yields one value per pass; `finally` detaches it.
4. `wait_for` subscribes with `every=True`.
5. `bridge.py`: `faces` and `head_tracking` with `history=CHANGE_HISTORY`.
6. Tests: a burst read in order with `every`, the newest by default, a history of 1 with `every` reading the newest, a burst past the history losing its oldest with one warning, both policies on one observable, history below 1 refused, `wait_for` finding a value overwritten in the same step.
7. Docs and specs as in Scope; the spec back to Implemented, this plan Done.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`.
