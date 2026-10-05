# `play_sound` as a spanning verb — the media session owns the one file player, the newest sound wins

**Status:** Done

**Done (2026-10-05):** every step implemented and verified — `ruff check`, `ruff format`, `pyright`, the fast tier (647 tests, the new file-player, bridge, motion and control-panel tests included) and the headless live tier (16 passed, 15 skipped: camera / gravity compensation / the sim's motor modes, as expected; the new `play_sound` live test among the passes). Departures: the `INFO` line of an interruption is logged by the interrupted `play_sound`, not by `start_sound` (an emotion's token is never released, so a start cannot tell a file still playing from one that ended) — `audio.md` says so; `MediaSession.stop_sound` takes the token only, and the bridge hands the motion loop a small `_stop_emotion_sound(token)` that narrows the loop's `object` token to a `SoundToken`; a `play_sound` stopped while it is still resolving or measuring the file has nothing to stop and reports stopped; the control panel's handler also turns a `FileNotFoundError` into a toast (`control_panel.md` "Errors never crash a handler"); the two GStreamer-discoverer tests run `_sound_duration` in a child process with `daemon.scrubbed_env()`, because the gstreamer-bundle's startup hook doubles the `GST_*` variables in any Python child of a Python process, and `Gst.init` then exits an xdist worker. The control panel check of the verification section was done through its fake-backed tests (`tests/test_control_panel.py`), not by hand.

Implements the re-design of [specs/core/bridge.md](../specs/core/bridge.md) ("Cancellation", the `play_sound` entry of "v1 scope", "Errors"), [specs/audio/audio.md](../specs/audio/audio.md) ("Sound files — one file player, each file played to its end"), [specs/motion/motion.md](../specs/motion/motion.md) ("Emotions through the loop", steps 3 and 6, "Interactions between the layers") and [specs/examples/control_panel.md](../specs/examples/control_panel.md) (the spanning verbs, the Speech group). Background and measurements: [analysis/play-sound-cancellation.md](../analysis/play-sound-cancellation.md) (untracked). It delivers:

- **`play_sound` under the cancellation contract**: `await bridge.play_sound(file)` completes when the file has been heard (its duration read before it starts) and cancelling the task stops the sound before the `CancelledError` reaches the caller — every verb of the bridge is then spanning or instant;
- **one owner for the one file player**: `MediaSession` starts and stops every sound file — the verb's and the emotions' — and records which start holds the player, so a stop never ends a file someone else started after it;
- **the newest sound wins**: a sound file starting while another plays replaces it; a replaced `play_sound` raises `SoundInterruptedError`; an emotion whose sound was replaced plays on, silent;
- **a stopped sound file leaves speech alone**: the wobbler reset that follows a stop (`clear_player()`) runs only when no `say` is in flight, so cancelling an emotion or a `play_sound` no longer flushes an utterance;
- **the control panel's Stop sound button**.

It deliberately leaves out: the bookkeeping of the instant verbs `set_wobbling` / `set_motors_state` after a cancel caught inside their SDK call (analysis section 7); the e2e reorganisation of [202610051207](202610051207_e2e-tier-by-scenario-one-daemon-per-run-and-the-gaps.md). Independent of that plan; either order works (Step 7 says where the live test lands).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md) (statuses: the specs are already `Updated` — `bridge.md`, `audio.md`, `control_panel.md`; `motion.md` stays `Stable`; Step 8 flips the three back to `Implemented`; every spanning verb needs a mid-flight cancel test on the `fake`); the four spec sections above; [202610011500](202610011500_verb-state-safety-sound-ownership-and-latest-say-wins.md), whose `say` rule and loop-owned emotion sound this plan extends. Code as it stands: `audio.py` (`MediaSession.say` and its `_Utterance`, `clear_player`, `stop_sound`, `_stop_sound_file`, `cancel_safe_step`, `_PLAYBACK_TAIL_S`), `motion.py` (`MotionSession.__init__`'s `stop_sound`, `_Playing.sound_started`, `_drop_playing`, the sound start in `_tick`), `bridge.py` (`play_sound`, the `MotionSession(...)` construction), `examples/control_panel/controller.py` (`_run_spanning`, `_stop_slot`, `Slot`).
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

  (`ruff format .` would reflow the Python blocks of Markdown files — format the code directories only.)
- **Cancellation.** `play_sound` becomes a spanning verb: it comes with a test on the `fake` that cancels it mid-flight and asserts the sound stopped and the session still works. The fake has real timing for it because the media session reads the duration from the file; tests write short WAV files into `tmp_path` (stdlib `wave`, a few hundred milliseconds).
- **Write specs affirmatively**; keep the `**Status:**` lines and the index rows in sync at each flip.
- **Do not commit** unless asked.

## Scope

- `src/reachy_mini_bridge/errors.py` — `SoundInterruptedError(BridgeError)`. `src/reachy_mini_bridge/__init__.py` — export it. `AGENTS.md` — the `errors.py` row names it.
- `src/reachy_mini_bridge/audio.py` — `SoundToken`; the file player on `MediaSession` (`start_sound`, `stop_sound(token)`, `release`, `play_sound`); the path resolution and duration helpers; the wobbler reset made conditional on no utterance in flight.
- `src/reachy_mini_bridge/motion.py` — `MotionSession(..., start_sound=..., stop_sound=...)`; the token kept on `_Playing`.
- `src/reachy_mini_bridge/bridge.py` — `play_sound` forwards to the media session; the two callables handed to the motion session.
- `examples/control_panel/controller.py`, `examples/control_panel/app.py` — `play_sound` a spanning verb in a `"sound"` slot, `stop_sound()`, the Stop sound button.
- `README.md` — the Speech out row of the verbs table.
- `tests/test_audio.py`, `tests/test_motion.py`, `tests/test_bridge.py`, `tests/test_control_panel.py` — as each step says; `tests-e2e/test_bridge.py` (or `tests-e2e/test_audio.py`, Step 7).
- `specs/core/bridge.md`, `specs/audio/audio.md`, `specs/examples/control_panel.md`, `specs/_index.md`, `plans/_index.md`, this file — statuses (Step 8).

## Steps

### Step 0 — Baseline

Check command green on `main`.

### Step 1 — The specs (done with this plan)

The spec edits listed at the top are written and the three specs are `Updated`. Nothing to do here but read them.

### Step 2 — `SoundInterruptedError`

**Files:** `errors.py`, `__init__.py`, `AGENTS.md`.

- `class SoundInterruptedError(BridgeError)`: "the `play_sound` was interrupted by a later sound file (specs/audio/audio.md "Sound files")". Exported from the package root next to `SpeechInterruptedError`; the `errors.py` row of `AGENTS.md`'s project map lists it.

### Step 3 — The file player on the media session

**Files:** `audio.py`, `tests/test_audio.py`.

- **Helpers** (module-level, private):
  - `_resolve_sound_file(name: str) -> Path`: the path when it exists; otherwise `Path(ASSETS_ROOT_PATH) / name` when that exists (`from reachy_mini.utils.constants import ASSETS_ROOT_PATH`, imported inside the function — `reachy_mini` is a base dependency, and the import stays out of module import time); otherwise `FileNotFoundError` naming both places looked in.
  - `_sound_duration(path: Path) -> float`: `.wav` → `wave.open` (`getnframes() / getframerate()`); on `wave.Error` or any other suffix → GStreamer's discoverer (`gi.require_version("Gst", "1.0")`, `gi.require_version("GstPbutils", "1.0")`, `Gst.init(None)`, `GstPbutils.Discoverer.new(5 * Gst.SECOND).discover_uri(path.resolve().as_uri()).get_duration() / Gst.SECOND`), imported lazily with the same `pyright: ignore[reportMissingImports]` as `_stop_sound_file`. A discoverer failure, a missing GStreamer or a zero/unknown duration → `ValueError` naming the file, chained to the cause.
- **`SoundToken`** (a small class or dataclass): `replaced: concurrent.futures.Future[None]` — resolved by the start that takes the player from it. A `concurrent.futures.Future` because the motion loop's thread may be the one that replaces a verb's token; the verb awaits it through `asyncio.wrap_future`.
- **`MediaSession`**: `self._sound_lock = threading.Lock()`, `self._sound_owner: SoundToken | None = None`.
  - `start_sound(path: Path) -> SoundToken` (blocking; called from a worker thread or the motion loop's): under the lock, `self._robot.media.play_sound(str(path))`, then — if an owner is recorded — log one `INFO` line (`a new sound file replaces the one playing`) and resolve its `replaced` (`set_result(None)` unless already done); record the new token as owner; return it. A `play_sound` that raises leaves the owner unchanged and propagates. Holding the lock across the upstream call keeps owner and player in step; on `webrtc` that includes the upload, which the motion loop already blocks on today (noted, not changed).
  - `stop_sound(token: SoundToken) -> None`: under the lock, return when `token` is not the owner; otherwise `_stop_sound_file(self._robot)` and clear the owner. Then, outside the lock, `self.clear_player()` only when `self._saying is None` (the wobbler reset; skipped while an utterance is in flight — spec "Stopping a sound file, per backend"). The existing no-argument `stop_sound()` goes; its callers are the motion loop (Step 4) and tests.
  - `release(token) -> None`: under the lock, clear the owner when it is `token`; nothing stopped.
  - `async play_sound(sound_file: str) -> None`: `self._require_open("play_sound")`; `path = await asyncio.to_thread(_resolve_sound_file, sound_file)`; `duration = await asyncio.to_thread(_sound_duration, path)`; `token = await cancel_safe_step(lambda: self.start_sound(path), self._stop_sound_off_loop)` (the undo stops it if a cancel arrived during the start); then race `asyncio.sleep(duration + _PLAYBACK_TAIL_S)` against `asyncio.wrap_future(token.replaced)` (the `_speak` shape: `asyncio.wait(..., FIRST_COMPLETED)`, both cancelled and gathered in `finally`). Sleep done → `release(token)`, return. Replaced → raise `SoundInterruptedError` (no stop). `BaseException` (a cancel) → `await asyncio.shield(asyncio.to_thread(self.stop_sound, token))`, swallowing a failure of the stop with a warning, then re-raise — the sound is stopped before the `CancelledError` propagates (on `webrtc` the stop is an HTTP POST, hence off the loop and shielded).
- **Tests** (`test_audio.py`, on the `fake` unless said; a `_wav(tmp_path, seconds)` helper writing 16 kHz mono silence):
  - `_sound_duration` of a 0.3 s WAV is 0.3 within 1 ms; of a float32 WAV written by hand (format 3 header — `wave` refuses it) is its length through the discoverer (`pytest.importorskip("gi")`); of a text file renamed `.ogg` raises `ValueError`.
  - `_resolve_sound_file("wake_up.wav")` finds the SDK asset; `"nope.wav"` raises `FileNotFoundError`; and `play_sound("nope.wav")` raises it with no `media.play_sound` recorded.
  - Ownership: `start_sound(a)`, `start_sound(b)` → `a.replaced` done; `stop_sound(a)` records nothing; `stop_sound(b)` records `media.stop_sound` then `audio.clear_player`; `release` then `stop_sound(b)` records nothing.
  - `play_sound` of a 0.3 s WAV takes at least 0.3 s and under 0.6 s.
  - Cancelled mid-flight: the cancel returns within 0.05 s, `media.stop_sound` is recorded before the `CancelledError` reaches the test, and a second `play_sound` then plays to completion.
  - Cancelled during the start (the fake's `media.play_sound` monkeypatched to sleep 0.2 s): the cancel propagates, the start completes, `media.stop_sound` is recorded after it.
  - Interrupted: a second `play_sound` 0.1 s into the first → the first raises `SoundInterruptedError` within 0.05 s, no `media.stop_sound` is recorded, the second completes.
  - With speech: a `say` and a `play_sound` started together both complete; a `play_sound` cancelled while a `say` plays records `media.stop_sound` and no `audio.clear_player`, and the `say` still completes with all its pushes.
  - `play_sound` on a closed session raises `BridgeError`.

### Step 4 — The motion loop through the file player

**Files:** `motion.py`, `tests/test_motion.py`.

- `MotionSession.__init__(..., start_sound: Callable[[Path], object] | None = None, stop_sound: Callable[[object], None] | None = None)` (type the token as `SoundToken` if the import stays cycle-free; `object` otherwise). With no `start_sound`, the loop starts the file through `self._robot.media.play_sound(str(path))` and keeps `None` as the token — what the loop-level tests constructing a bare `MotionSession` rely on.
- `_Playing.sound_started: bool` becomes `sound_token: object | None` plus the flag (a `None` token from the fallback still counts as started). The sound start in `_tick` stores the token; `_drop_playing` and the failure paths call `self._stop_sound(token)` (logged and survived on failure, as now). No other change to the drop acknowledgement.
- **Tests** (`test_motion.py`, a stub pair recording calls): a cancelled primary whose sound started calls `stop_sound` with exactly the token `start_sound` returned; one cancelled in its entry blend calls neither.

### Step 5 — The bridge

**Files:** `bridge.py`, `tests/test_bridge.py`.

- `play_sound(sound_file)`: `await self._require_media().play_sound(sound_file)`; docstring per [bridge.md](../specs/core/bridge.md) (completes when heard, cancel stops, newest wins, `SoundInterruptedError`, `FileNotFoundError` / `ValueError`, no motors). `set_wobbling`'s docstring unchanged.
- The `MotionSession(...)` construction passes `start_sound=lambda path: self._require_media().start_sound(path)` and `stop_sound=lambda token: self._require_media().stop_sound(token)`.
- **Tests** (`test_bridge.py`, on the `fake`):
  - `play_sound` of a short WAV spans its duration; cancelled mid-flight it stops (`media.stop_sound` recorded), returns within 0.05 s, and `say` works right after; outside a session it raises `BridgeError`. (The existing `test_play_sound_reaches_the_media_layer` plays `wake_up.wav`, now 0.41 s — keep it, asserting the resolved asset path.)
  - Emotion then `play_sound`: start `happy`, wait for its `media.play_sound`, start a `play_sound` of a 1 s WAV, cancel the emotion → no `media.stop_sound` recorded after the WAV's start, and the `play_sound` completes normally.
  - `play_sound` then emotion: a 3 s WAV playing, `play_emotion("happy")` reaches its sound → the `play_sound` raises `SoundInterruptedError`; the emotion completes.
  - Emotion cancelled during a `say`: `say` of a 1 s tone started after the emotion's sound, the emotion cancelled → `media.stop_sound` recorded, no `audio.clear_player` until the `say` ends, and the `say` returns normally. (The existing cancel test's `["media.stop_sound", "audio.clear_player"]` stays true: no `say` is in flight there.)

### Step 6 — The control panel and the README

**Files:** `examples/control_panel/controller.py`, `examples/control_panel/app.py`, `README.md`, `tests/test_control_panel.py`.

- `Slot` gains `"sound"`. `play_sound(path) -> bool` moves to the spanning verbs: `self._run_spanning("sound", self._bridge.play_sound(path))`; `stop_sound() -> int` is `self._stop_slot("sound")`. `_run_spanning` maps `SoundInterruptedError` to `False`, as it does `SpeechInterruptedError`.
- `app.py`: a Stop sound button beside Play sound (`api_name="stop_sound"`); Play sound logs "done" / "stopped" like Say.
- `README.md`: the Speech out row — `play_sound(file)` completes when the sound has been heard; the newest sound file wins (an emotion's included), the replaced one raising `SoundInterruptedError`.
- **Tests:** the existing `play_sound("ding.wav")` test plays a WAV written to `tmp_path` and asserts `True`; a `play_sound` of a 2 s WAV stopped from another thread → `False`, `media.stop_sound` recorded, `busy` shows `"sound"` while it plays and not after; a second `play_sound` during one → `False` for the first, `True` for the second.

### Step 7 — The live check (gated on `audio`)

**Files:** `tests-e2e/test_bridge.py`, or `tests-e2e/test_audio.py` if [202610051207](202610051207_e2e-tier-by-scenario-one-daemon-per-run-and-the-gaps.md) has split the tier by then.

- `play_sound("go_sleep.wav")` (an SDK asset, 3.6 s) takes between 3.6 s and 4.2 s; `play_sound("confused1.wav")` (5.7 s) cancelled after 1 s returns within 0.1 s, and a `say` right after completes. Headless sim; the wireless backend is not covered by the tier.

### Step 8 — Close

- Verification below green. Flip `bridge.md`, `audio.md`, `control_panel.md` to `Implemented` (status line and [specs/_index.md](../specs/_index.md) row). Mark this plan `Done` here and in [_index.md](_index.md), with a **Done** paragraph naming any departure. Add a "Done since" line to finding 5.2 of `analysis/e2e-tier-review-20261001.md` (untracked).

## Verification

- `uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest` — green, the new fake tests of Steps 3 to 6 included (`uv run pytest -n 0` if a timing assertion looks flaky).
- `uv run pytest tests-e2e -rs` (headless sim) — green, the `play_sound` live test of Step 7 among the passes.
- The control panel on a sim config: Play sound on `confused1.wav` plays to its end and logs "done"; Stop sound cuts it; Play sound during an emotion silences the emotion's sound while the head keeps moving.
