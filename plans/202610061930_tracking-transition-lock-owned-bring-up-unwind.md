# The tracking transition under one lock, the bring-up unwind owned past repeated cancels

**Status:** Done

Restores two contracts of [bridge.md](../specs/core/bridge.md) the code let slip: "Cancellation" (one mode verb at a time per mode — the last call's value stands as a whole) for `start_head_tracking` / `stop_head_tracking`, and "Lifecycle" (a cancelled bring-up leaks nothing, a second cancel does not shorten its cleanup) for the whole `start()` unwind rather than the one step in flight. Fixes F1 and F2 of the [post-fix review of 2026-10-06](../analysis/20261006_post-fix-and-documentation-review.md); F3–F5 of the same review were documentation and went in the commit before this one. No spec changes: the code moves to the spec, so `bridge.md` stays `Implemented`.

## Scope

- `src/reachy_mini_bridge/bridge.py` — `_tracking_sync`, an `asyncio.Lock` made beside `_detection_sync` (per session, re-made in `start()`) that `start_head_tracking` and `stop_head_tracking` hold over their whole transition: the `_tracking_wanted` flip, the `_sync_detection()` call and the tracker's `start(focus=…)` / `stop()`; `_owned(work)`, a module helper running a coroutine as a task awaited through any cancel of the caller and reporting whether one was absorbed; `start()`'s failure path unwinds its `AsyncExitStack` through `_owned`, and a start that failed on its own and was then cancelled during that unwind propagates the `CancelledError` chained to the failure.
- `tests/test_bridge.py` — `test_a_stop_during_a_held_tracking_start_leaves_tracking_off`; `test_startup_cancelled_twice_still_disables_the_held_wobbling_enable`.
- This plan and [the plans index](_index.md).

## Steps

1. **F2 — the transition as a whole.** `_sync_detection` locks the detection loop's start and stop, not the tracker state around it: a `stop_head_tracking()` scheduled while a `start_head_tracking()` builds the detector clears the flag and stops the tracker, then waits for the loop lock; the start resumes and calls `tracker.start()` after that stop; the stop's sync then stops the loop. Final state: `tracking` false, `head_tracking.value.active` true, `attention` `"watching"`, `faces` inactive. The per-mode lock serialises the two verbs end to end (the detection lock is taken inside it, always in that order), so the last call's value stands for the flag, the tracker and the loop together. `set_face_detection` needs no such lock: its only state is the flag the locked sync reads at execution time.

2. **F1 — the unwind owned.** `_WobblingSession.stop` shields the tail it waits on, but the `await` of that shield is itself cancellable, and `start()` awaited `stack.aclose()` bare: a second cancel while the cleanup waited on a held `enable_wobbling` cut the wobbling callback short before its `disable_wobbling`, the other callbacks closed the camera, the media session and the connection, and the held enable then landed on a closed connection — the wobbler left on for every app on a borrowed daemon. The unwind now runs through `_owned`, the same shape as `cancel_safe_step`'s finish-and-undo task: every callback runs to its end whatever cancels arrive, then the original exception is re-raised (or the absorbed cancel, when the original was a plain failure).

3. **Tests, each observed failing on the previous code.** The tracking one holds a custom factory, schedules the start then the stop, releases, and asserts the four public values above are all off and that tracking starts again afterwards. The bring-up one holds the fake's `enable_wobbling`, cancels the start twice (the second while the cleanup waits), releases, and asserts the wobbling commands read enable then disable, the disable precedes the connection's `__exit__`, and the bridge restarts.

## Verification

`uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` — 742 passed on 2026-10-06; the two new tests fail on the previous `bridge.py`.
