# Verb state safety — the loop owns the emotion's sound, one wobbling lease, `set_presence` fails without side effects, the latest `say` wins

**Status:** Done

**Done (2026-10-01):** every step implemented and verified — `ruff check`, `ruff format`, `pyright`, the fast tier (626 tests, the new sound-ownership, wobbling-lease, `set_presence` and latest-`say` tests included) and the headless live tier (15 passed, 15 skipped: camera / gravity compensation, as expected; the interrupted-`say` live test among the passes). Departures: `MotionSession.submit` returns the primary's record (`done`, the future, and `dropped`, the loop's acknowledgement) rather than the bare future, because the "Cancellation" contract's second guarantee — the effect stopped *before* the `CancelledError` propagates — needs `play_emotion` to wait for the loop's drop (the sound stop now happens on the loop thread, at its next tick; `_DROP_ACK_S = 0.5` bounds the wait), and the loop learns of a cancel through a done-callback on that future so a queued primary is dropped at once too, not when it reaches the head of the queue; a cancel caught inside the wobbling disable call returns at once (the contract's first guarantee), the call completes in its thread and the restore follows its completion through a done-callback, instead of the release waiting for the call; `motion.md` stayed `Stable` (it is not `Implemented` yet — its plan is `In progress` pending the on-robot checklist), so it was edited in place without an `Updated` flip. The control panel check of the verification section was done through its fake-backed tests (`tests/test_control_panel.py`: a `say` during a `say` returns `False` for the first, `True` for the second), not by hand.

Implements the re-design this plan's Step 1 makes in [specs/core/bridge.md](../specs/core/bridge.md) ("Cancellation", "v1 scope" — `say`, the modes, "Errors"), [specs/motion/motion.md](../specs/motion/motion.md) ("Emotions through the loop") and [specs/audio/audio.md](../specs/audio/audio.md) ("TTS out", "Stopping a sound file"), with an editorial touch on [specs/examples/control_panel.md](../specs/examples/control_panel.md) — findings 2 and 5 of [analysis/repo-spec-code-consistency-review-20260929.md](../analysis/repo-spec-code-consistency-review-20260929.md) (untracked; restated below) and the `say` concurrency rule settled on 2026-10-01. It delivers:

- **the emotion's sound stopped by the one party that started it**: the motion loop stops the sidecar sound of the primary it is playing when that primary is cancelled or fails after the sound started; a `play_emotion` cancelled while queued stops nothing, so it can no longer silence the emotion playing ahead of it;
- **one wobbling pause across consecutive emotions**: a counted lease on the bridge — disabled when the first emotion begins, restored when the last one ends, on every exit path including a cancel caught inside the disable call;
- **`set_presence` without side effects outside a session**: it requires the motion session before it records anything, so a failed call leaves `presence` at the config's value;
- **the latest `say` wins**: a `say` is exclusive on a session; a new one interrupts the one running — its queued audio flushed, one `INFO` line — which raises `SpeechInterruptedError` in its caller, while the new one plays. A caller's task is never cancelled by the bridge: the interrupted verb ends with an exception the caller handles.

It deliberately leaves out: a stop for `play_sound` (bridge.md open question 3); any change to the detection and tracking code of [202610011455](202610011455_detection-loop-robustness-and-harness-event-loop.md). Independent of that plan; either order works.

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md) (statuses: Step 1 flips three specs to `Updated`, Step 6 back to `Implemented`; every spanning verb needs a mid-flight cancel test on the `fake`); [specs/core/bridge.md](../specs/core/bridge.md) "Cancellation" in full; [specs/motion/motion.md](../specs/motion/motion.md) "Emotions through the loop", "Interactions between the layers"; [specs/audio/audio.md](../specs/audio/audio.md) "TTS out", "Shared" (barge-in, `stop_sound`); `src/reachy_mini_bridge/motion.py` (`submit`, `_tick`'s drop path, the `_Playing.sound_started` flag, the failure paths that `set_exception`), `bridge.py` (`play_emotion`, `_restore_layers_after_move`, `set_presence`), `audio.py` (`MediaSession.say`, `clear_player`, `stop_sound`), `examples/control_panel/controller.py` (`say` / `stop_saying`) as they stand.
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

- **Cancellation.** `say` and `play_emotion` are spanning verbs: each change below comes with a test on the `fake` that cancels mid-flight and asserts the effect stopped and the session still works; the fake keeps real timing so there is a mid-flight.
- **Write specs affirmatively**; keep the `**Status:**` lines and the index rows in sync at each flip.
- **Do not commit** unless asked.

## Scope

- `specs/core/bridge.md`, `specs/motion/motion.md`, `specs/audio/audio.md`, `specs/examples/control_panel.md`, `specs/_index.md` — Step 1 (and Step 6).
- `src/reachy_mini_bridge/errors.py` — `SpeechInterruptedError(BridgeError)`; `src/reachy_mini_bridge/__init__.py` — export; `AGENTS.md` — the `errors.py` row.
- `src/reachy_mini_bridge/bridge.py` — `set_presence` ordering; `play_emotion` without its own `stop_sound`; the wobbling lease.
- `src/reachy_mini_bridge/motion.py` — `MotionSession(..., stop_sound=...)`; the sound stopped on the drop and failure paths of the primary that started it.
- `src/reachy_mini_bridge/audio.py` — `MediaSession.say` exclusive, latest wins.
- `examples/control_panel/controller.py` — `say` relies on the bridge's rule.
- `README.md` — the `say` row of the verbs table (interruption), the errors list if it has one.
- `tests/test_bridge.py`, `tests/test_motion.py`, `tests/test_audio.py`, `tests/test_control_panel.py`, `tests-e2e/test_bridge.py` — as each step says.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Check command green on `main`.

### Step 1 — The spec updates (set `bridge.md`, `motion.md`, `audio.md` `Updated`)

- **`specs/core/bridge.md` "Cancellation"**, guarantee 3: a cancelled `play_emotion` undoes its own effect only — cancelled while queued it has none (its sound never started, the emotion playing ahead of it plays on); the loop stops the sound it started for the cancelled primary. A new paragraph after the two classes: **`say` is exclusive**: a `say` arriving while one plays interrupts it — the bridge flushes the running one's queued audio and the running call raises `SpeechInterruptedError` — then plays; the latest call wins. This is `say`'s defined effect on a running `say`, not a breach of isolation, and it is the only way one verb ends another; the interrupted caller's task is not cancelled, it gets the exception at its next `await`. "v1 scope", the `say` bullet: the same in one sentence (an agent with something new to say does not queue behind the old sentence); the modes bullet (`set_presence` / `set_idle` / `set_idle_move`): outside a session they raise `BridgeError` and leave their property at the config's value. "Errors": `SpeechInterruptedError(BridgeError)` — raised by the `say` another `say` interrupted. "Emotions pause wobbling": paused from the first emotion's start to the last one's end when several queue, restored on every exit path.
- **`specs/motion/motion.md` "Emotions through the loop"**: step 6 — the loop stops the sound it started (through the `stop_sound` callable the bridge hands it: [audio.md](../specs/audio/audio.md)'s `MediaSession.stop_sound`, which also resets the wobbler) when the primary that started it is cancelled or fails; a primary cancelled while queued stops nothing. The wobbling bullet: the bridge holds one pause across consecutive primaries — `disable_wobbling()` when the first `play_emotion` begins, `enable_wobbling()` when the last one in flight ends, whatever the exit path — a lease counted per in-flight `play_emotion`, taken before the disable call is awaited so a cancel caught inside it still releases. A caller's `set_wobbling(False)` during an emotion stays authoritative: the release restores the bridge's *current* record.
- **`specs/audio/audio.md` "TTS out"**: a new bullet — **one `say` at a time, the latest wins.** The media session plays one utterance; a `say` called while one is in flight (streaming, or waiting for its queued audio to be heard) interrupts it: flushes the speaker (`clear_player`, the wobbler reset with it), logs one `INFO` line, and the interrupted call raises `SpeechInterruptedError`; the new utterance then plays from a silent speaker. Cancelling the running `say` is unchanged. "Stopping a sound file": called by the motion loop for the sound it started (the cancelled or failed emotion), and by `play_emotion` no longer. "`fake` backend support": nothing new (`clear_player` and `stop_sound` are already recorded).
- **`specs/examples/control_panel.md`** (editorial): the panel's `say` button relies on the bridge's latest-wins rule — a press during an utterance interrupts it; the panel reports the interrupted one as stopped.
- Flip the three `**Status:**` lines and index rows to `Updated`.

### Step 2 — `set_presence` (finding 5)

**Files:** `src/reachy_mini_bridge/bridge.py`, `tests/test_bridge.py`.

- `set_presence`: `motion = self._require_motion()` first, then `motion.set_presence(enabled)`, then `self._presence = enabled`. Same order check on `set_idle` and `set_idle_move` (already right; keep).
- Tests: `set_presence(False)` on a bridge outside a session raises `BridgeError`, `presence` still reads the config's `True`, and the next session starts with presence on (the fake's targets show the idle move).

### Step 3 — The loop stops the sound it started (finding 2, sound)

**Files:** `src/reachy_mini_bridge/motion.py`, `bridge.py`, `tests/test_motion.py`, `tests/test_bridge.py`.

- `MotionSession.__init__(..., stop_sound: Callable[[], None] | None = None)`; the bridge passes `media.stop_sound`. In `_tick`'s drop path (a cancelled primary) and on the failure paths that `set_exception` on the primary: if `playing.sound_started`, call `stop_sound()` once (on the loop thread, as `play_sound` already is; an exception from it is logged and never stops the loop).
- `ReachyMiniBridge.play_emotion`: the `except BaseException` branch keeps `future.cancel()` and drops its `media.stop_sound()`.
- Tests on the `fake` (`test_bridge.py`): start `happy`, start a second `happy` 50 ms later, after the first's entry blend cancel the **second** — the fake records `play_sound` once and no `stop_sound`, the first completes, the session plays a third emotion; cancel the **playing** one after its sound started — `stop_sound` recorded exactly once; cancel it during its entry blend (before the sound) — no `stop_sound`; a move whose `evaluate` raises after the sound started — `stop_sound` recorded, the verb raises. `test_motion.py`: the loop-level counterpart with a stub `stop_sound`.

### Step 4 — One wobbling lease (finding 2, wobbling)

**Files:** `src/reachy_mini_bridge/bridge.py`, `tests/test_bridge.py`.

- `self._emotion_leases = 0`. `play_emotion`: `self._emotion_leases += 1` **before** `await asyncio.to_thread(robot.disable_wobbling)`, which runs only when the count went to 1 and `self._wobbling`; `finally`: `self._emotion_leases -= 1`, and when it reaches 0 and `self._wobbling`, `enable_wobbling` (the current `_restore_layers_after_move` body, warning on failure).
- Tests: two FIFO emotions — the fake's commands show one `disable_wobbling` before the first and one `enable_wobbling` after the second, nothing between; a cancel arriving while the disable call runs (monkeypatch the fake's `disable_wobbling` to sleep) — wobbling re-enabled once the cancel has propagated; `set_wobbling(False)` during an emotion — stays off afterwards (the existing test, kept).

### Step 5 — The latest `say` wins

**Files:** `src/reachy_mini_bridge/errors.py`, `__init__.py`, `audio.py`, `bridge.py` (docstring), `examples/control_panel/controller.py`, `AGENTS.md`, `README.md`, `tests/test_audio.py`, `tests/test_bridge.py`, `tests/test_control_panel.py`, `tests-e2e/test_bridge.py`.

- `SpeechInterruptedError(BridgeError)`: "the `say` was interrupted by a later `say` (specs/audio/audio.md "TTS out")". Exported from the package root; the `errors.py` row of `AGENTS.md`'s module map names it.
- `MediaSession.say`: an `_Utterance` record of the running call with an `asyncio.Event` `interrupted`. On entry, if one is in flight: `_logger.info(...)`, set its event, `clear_player()`, then take the slot. The running call races its work against the event — the synthesizer stream is consumed in a task the interruption cancels (closing the stream), and the final `sleep(remaining)` is `wait_for(event.wait(), remaining)`-shaped — and raises `SpeechInterruptedError` when the event fired; the `except BaseException: clear_player()` path stays for a cancel or a synthesizer failure (the slot released on every exit). The interrupted call does not flush a second time (the speaker already holds the new utterance's audio).
- `examples/control_panel/controller.py`: `say` no longer calls `stop_saying()` first; `_run_spanning("say", ...)` maps `SpeechInterruptedError` to the `False` it already returns for a stopped verb. The "Stop" button keeps cancelling.
- `README.md`: the `say` row says a new `say` interrupts the one playing (`SpeechInterruptedError` in the interrupted caller).
- Tests. `test_audio.py` on the `fake`: two concurrent `say` calls with the fake's real timing — the first raises `SpeechInterruptedError`, `clear_player` is recorded once between the two, the second plays to completion (its pushes all recorded after the flush), a third `say` afterwards works; interrupting a `say` that is already in its playback wait (all chunks pushed) raises the same; cancelling the second while the first is already interrupted leaves the session usable. `test_bridge.py`: the same through `bridge.say`. `test_control_panel.py`: a `say` pressed during one returns `False` for the first, `True` for the second. `tests-e2e/test_bridge.py` (gated on `audio`): a 1 s tone `say` interrupted by a second one — the first raises `SpeechInterruptedError` within a fraction of a second, the second completes.

### Step 6 — Close

- Verification below green. Flip `bridge.md`, `motion.md`, `audio.md` to `Implemented` (status line and index). Delete findings 2 and 5 from `analysis/repo-spec-code-consistency-review-20260929.md` (untracked). Mark this plan `Done` here and in [_index.md](_index.md).

## Verification

- `uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest` — green, the new fake tests of Steps 2 to 5 included (`uv run pytest -n 0` if a timing assertion looks flaky).
- `uv run pytest tests-e2e -rs` (headless sim) — green, the interrupted-`say` live test included.
- The control panel (`uv run python -m examples.control_panel --config <sim config>`): pressing Say during an utterance cuts it and plays the new one; two emotions queued keep the head still of wobble until the second ends.
