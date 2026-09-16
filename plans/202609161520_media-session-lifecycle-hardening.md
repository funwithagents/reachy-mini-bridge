# Media session lifecycle hardening

**Status:** Done

Implements `specs/audio.md` ("One media session, owned here", "Mic in") and `specs/api.md` (a new "Lifecycle" bullet): the media session and the api tear down exactly what they started — even when opening or closing fails partway — `say` / `audio_input` refuse to run outside an open session, and the mic tap stops busy-polling when the daemon has no sample ready. Clears the `_open` flag and both "Robustness gaps" items in [specs/_analysis.md](../specs/_analysis.md). Deliberately leaves out `stop_talking` / barge-in (pending an owner decision) and the ignored `apply_audio_config` return value (tracked in the analysis).

## Design decisions (settled with the user)

- **The open state becomes a guard.** The user chose to use `_open` rather than delete it. `MediaSession.say` and `MediaSession.audio_input` raise `BridgeError` when the session isn't open (before `__aenter__`, or after `__aexit__`), and a second `__aenter__` on an open session raises `BridgeError`. The daemon-format getters (`mic_sample_rate`, `mic_channels`) and `clear_player` work at any time: they read or flush and open nothing. This matters because today `audio_input()` before opening gets `None` from a real daemon forever and spins indefinitely, and `say` pushes into a playback pipeline that was never started.
- **One source of truth for "open".** The `_open: bool` is replaced by the session's `contextlib.AsyncExitStack`: the session is open exactly when `self._exit_stack is not None`, checked through a private `_require_open(verb)` helper. A separate flag could disagree with the stack.
- **`audio_input` raises at call time, and the tap ends when the session closes.** `MediaSession.audio_input(*, mono=True)` becomes a plain method that calls `_require_open("audio_input")` and returns a private async generator (`_tap`). The error therefore surfaces where the caller asked, not at the first `async for`. The generator loops while the session is open, so a consumer task still draining the mic ends cleanly on `__aexit__` rather than polling a stopped pipeline forever.
- **Unwinding goes through `AsyncExitStack`.** `MediaSession.__aenter__` registers each `stop_*` (via `push_async_callback(asyncio.to_thread, …)`) right after its `start_*` succeeds. On any later failure, including `apply_audio_config` raising, the stack unwinds what already started, in reverse, and the original exception propagates. `__aexit__` closes the stack, which runs every registered stop even if an earlier one raises; the stack chains the exceptions. `ReachyMiniApi` uses the same shape: `stack.enter_context(self._robot)`, then `await stack.enter_async_context(self._media)`. A media open failure therefore exits the robot, and exit tears down media then the robot even when media teardown raises. The stack holds no loop-bound state, so the e2e `live_api` fixture keeps working when it opens in one `asyncio.run` and closes in another.
- **Mic poll interval.** On a `None` sample, the tap runs `await asyncio.sleep(_MIC_POLL_INTERVAL_S)` with `_MIC_POLL_INTERVAL_S = 0.01` (10 ms, one capture chunk as the fake models it). On the GStreamer backend, `get_audio_sample` already waits up to 20 ms in `try_pull_sample`, so the sleep adds little latency there. It bounds the spin on the paths where upstream returns `None` immediately: no appsink, or no audio backend.

## Scope

- `specs/audio.md` — "One media session, owned here": state the lifecycle guarantees. A failed open unwinds what already started, teardown runs every stop even if one fails, `say` / `audio_input` require an open session and raise `BridgeError` otherwise, a double open raises `BridgeError`, and the getters and `clear_player` work at any time. "Mic in": the tap waits out empty reads with a short poll and ends when the session closes. Use affirmative, current-state wording. Status `Implemented → Updated`, back to `Implemented` on completion.
- `specs/api.md` — the **Lifecycle** bullet in "Core concepts / Decided" already states these guarantees, and [202609141536_config-and-daemon-lifecycle.md](202609141536_config-and-daemon-lifecycle.md) (which runs first) builds the api-level `AsyncExitStack` — daemon → robot → media, robot construction in `__aenter__`, `robot` guarded. Nothing in `api.md` changes here, and its status is untouched. This plan's api work reduces to the tests in step 2 (`tests/test_api.py`), which must inject failures through a patched `build_robot` returning a prepared `FakeReachyMini` rather than through `api.robot` (unavailable before entry); step 4 is already done and is skipped.
- `specs/_index.md` — keep both rows' Status in sync.
- `src/reachy_mini_bridge/audio.py` — `AsyncExitStack` lifecycle, `_require_open`, `audio_input` → check + `_tap`, poll sleep, `_MIC_POLL_INTERVAL_S`, import `BridgeError`.
- `src/reachy_mini_bridge/api.py` — no change expected (the `AsyncExitStack` lifecycle is already there from the config plan); touch only if a media-session guard needs surfacing.
- `tests/test_audio.py` — lifecycle, guard, and mic-poll tests (below).
- `tests/test_api.py` — api-level lifecycle and guard tests (below).
- `specs/_analysis.md` — delete the items this plan clears.
- `plans/_index.md` — this plan's row.

## Steps

1. **Specs first.** Edit `specs/audio.md` and `specs/api.md` as scoped; set both `**Status:**` to `Updated` and sync [specs/_index.md](../specs/_index.md).
2. **Tests first (red).** Failures are injected by replacing a fake method with a raising function, the same way `test_audio.py` already swaps `get_audio_sample`. In `tests/test_audio.py`:
   - `test_failed_open_unwinds_what_started`: `media.start_playing` raises. `async with MediaSession(robot)` re-raises, the commands show `media.start_recording` followed by `media.stop_recording`, and there is no `media.stop_playing` (playback never started).
   - `test_failed_audio_config_unwinds_both_directions`: `media.audio.apply_audio_config` raises with `audio_config` given. It re-raises, and both `media.stop_recording` and `media.stop_playing` are recorded.
   - `test_exit_stops_playback_even_if_stopping_recording_fails`: `media.stop_recording` raises. Leaving the `async with` re-raises, and `media.stop_playing` is still recorded.
   - `test_say_requires_an_open_session`: `say` before opening and after closing raises `BridgeError`, and no `media.push_audio_sample` is recorded.
   - `test_audio_input_requires_an_open_session`: calling `session.audio_input()` unopened raises `BridgeError` immediately, without iterating.
   - `test_double_open_raises`: a nested `async with session` on an open session raises `BridgeError`, and the outer session still tears down cleanly.
   - `test_mic_tap_ends_when_the_session_closes`: a consumer task drains `audio_input()`. Exiting the session makes the task finish on its own (`asyncio.wait_for(task, 1.0)` returns, no timeout).
   - `test_mic_tap_waits_out_missing_samples`: `get_audio_sample` returns `None` three times, then a known stereo frame. The tap yields that frame's int16 mono bytes.
   - `test_mic_tap_does_not_busy_poll`: `get_audio_sample` always returns `None` and counts its calls. Drain the tap under `asyncio.wait_for(..., 0.2)` (expect `TimeoutError`), then assert calls `< 50`. That bound is about 20 expected at 10 ms, while an unthrottled loop makes thousands. Only an upper bound is asserted, so a slow machine can't make it flaky.

   In `tests/test_api.py`:
   - `test_open_failure_exits_the_robot`: `media.start_recording` on `api.robot` raises. `async with ReachyMiniApi("fake")` re-raises, and the fake records `__exit__`.
   - `test_exit_exits_the_robot_even_if_media_teardown_fails`: `media.stop_playing` raises. Leaving the block re-raises, and `__exit__` is recorded.
   - `test_audio_verbs_require_an_open_api`: `await api.say("hi", synth)` and `api.audio_input()` on an unopened api raise `BridgeError`.
3. **`MediaSession`.** Replace `_open` with `self._exit_stack: AsyncExitStack | None = None`. `__aenter__`:
   - raise `BridgeError` if already open;
   - build a local `AsyncExitStack`;
   - `start_recording`, then `push_async_callback(asyncio.to_thread, media.stop_recording)`;
   - `start_playing`, then `push_async_callback(asyncio.to_thread, media.stop_playing)`;
   - apply the config;
   - on success, `self._exit_stack = stack.pop_all()`; on `BaseException`, `await stack.aclose()` and re-raise.

   `__aexit__`: take the stack, set `self._exit_stack = None`, then `await stack.aclose()`. Clearing it first means the session reads as closed even when a stop raises. Add `_require_open(verb)`, and call it at the top of `say`. Make `audio_input` a plain method that runs `_require_open("audio_input")` and then returns `self._tap(mono)`. `_tap` is the current loop body with `while self._exit_stack is not None:` and `await asyncio.sleep(_MIC_POLL_INTERVAL_S)` on `None`.
4. **`ReachyMiniApi`.** `__aenter__`: build a local `AsyncExitStack`, `stack.enter_context(self._robot)`, `await stack.enter_async_context(self._media)`, store `stack.pop_all()` in `self._exit_stack`. On a failure in between, the local stack unwinds via `async with` / `aclose`. `__aexit__`: take the stack, clear the attribute, then `await stack.aclose()`. `ReachyMiniApi.audio_input` stays a thin delegate, so the session's call-time guard surfaces directly.
5. **Live check (optional, sim).** Run `uv run pytest tests-e2e/test_api.py` on the headless sim. It confirms the `live_api` fixture, which opens and closes the api across separate `asyncio.run` calls, still works with the stack-based lifecycle.
6. **Analysis.** Delete from [specs/_analysis.md](../specs/_analysis.md) the `_open` item and both "Robustness gaps" items.
7. **Statuses.** Once verification passes, flip `specs/audio.md` and `specs/api.md` back to `Implemented` (file + `_index.md`), and set this plan to `Done` here and in [_index.md](_index.md). If the front-door plan also has `api.md` at `Updated`, it returns to `Implemented` only once both plans are `Done`.

## Verification

- `uv run ruff check .` and `uv run ruff format .` are clean.
- `uv run pyright` is clean, including `AsyncExitStack.enter_context(self._robot)` type-checking against both members of the `AnyReachyMini` union.
- `uv run pytest`: every test listed in step 2 passes, and the existing `test_audio.py` / `test_api.py` tests still pass. The getters and `clear_player` are still called on unopened sessions there, which checks that they stay unguarded.
- `grep -n "_open" src/reachy_mini_bridge/audio.py` finds nothing.
