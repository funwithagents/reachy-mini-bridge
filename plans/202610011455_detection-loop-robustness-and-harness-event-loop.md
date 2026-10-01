# Detection loop robustness and the harness event loop — `live_bridge` keeps its loop, loss without observations, the custom detector built once

**Status:** Done

**Done (2026-10-01):** every step implemented and verified — `ruff check`, `ruff format`, `pyright`, the fast tier (616 tests, the new harness-loop, tick, silent-detector and factory tests included), the headless live tier (14 passed, 15 skipped: camera / gravity compensation, as expected) and the viewer-sim live tier (27 passed, 2 skipped: gravity compensation, the sim ignoring motor modes) — the tracking tests now arming tracking without stopping it first. Departures: `LiveBridge` is a frozen dataclass with an `__iter__` yielding `(bridge, capabilities)` rather than a `NamedTuple` (a named tuple cannot carry the loop beside its two fields and still unpack as two); the live tests' per-test `stop_head_tracking()` / `start_head_tracking()` pair became `_arm_tracking`, which turns tracking on and then **waits for the previous test's aim to be released** — the release only happens if the detection loop kept ticking the tracker between tests, so each tracking test proves the loop lived through the module rather than merely assuming it; a `custom` factory swapped in mid-run whose result has no `detect` is not validated at the swap (the loop's existing detector-down rule covers it: skipped polls, then the inactive report with one warning), only a start validates; Step 5 needed no code, the face-marker publisher and the control panel already treat `active=False` as no faces, and their tests passed unchanged.

Implements the re-design this plan's Step 1 makes in [specs/testing/testing_support.md](../specs/testing/testing_support.md) ("Public surface"), [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) ("Inputs and outputs", "Easing, loss, focus"), [specs/vision/user_perception.md](../specs/vision/user_perception.md) ("The face report", "The detection loop", "Custom detectors", "Building the detector") and [specs/core/bridge.md](../specs/core/bridge.md) ("Faces") — findings 1, 3 and 4 of [analysis/repo-spec-code-consistency-review-20260929.md](../analysis/repo-spec-code-consistency-review-20260929.md) (untracked; restated below). It delivers:

- **a harness with one event loop**: `live_bridge` runs the bridge's lifecycle on a background thread's loop that lives from `start()` to `stop()`, and tests run their coroutines on it through the fixture's `run(...)` — the detection task created at entry survives the fixture's setup, so detection and tracking run throughout a live module as the spec has always said;
- **loss measured in time, not in reports**: the tracker withdraws its aim `TRACKING_LOST_S` after the last observation that showed the followed face, whether later observations lack it or no observation arrives at all (a dead camera, a detector that keeps raising) — and an inactive face report always carries no faces;
- **the custom detector built once, on a worker**: registration checks that the factory is callable and nothing more; the loop's start builds the detector and rejects one without a callable `detect`; clearing the factory while the loop runs it is refused.

It deliberately leaves out: a thread-based detection loop (the bridge stays loop-bound by design — the observable publishes on the loop thread; a consumer's app has one loop for the bridge's lifetime, only the pytest harness needed one); the sound/wobbling and `say` changes of [202610011500](202610011500_verb-state-safety-sound-ownership-and-latest-say-wins.md).

Do [202610011450](202610011450_docs-reconciliation-after-the-detector-migration.md) first (it corrects the testing specs this plan edits).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md) (statuses: Step 1 flips the four specs to `Updated`, Step 7 back to `Implemented`); [specs/vision/user_perception.md](../specs/vision/user_perception.md) in full; [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) "Whom the head follows", "Easing, loss, focus", "The head tracking report"; [specs/testing/testing_support.md](../specs/testing/testing_support.md) "Public surface"; [specs/core/observable.md](../specs/core/observable.md) (`set` / `update` run on the loop thread — the reason the harness loop must stay alive); `src/reachy_mini_bridge/testing/fixtures.py`, `face_detection.py` (`FaceDetection.start` / `_run`), `head_tracking.py` (`HeadTracker.observe`), `bridge.py` (`_on_face_observation`, `_sync_detection`, `set_face_detector`, `start()`'s custom check) as they stand.
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

- **No network in `tests/`.** Every fast test that needs faces registers a stub detector through `face_detection.detector: "custom"` on the `fake`, with the shortened timing constants `tests/test_bridge.py`'s `fast_faces` fixture patches.
- **Cancellation.** No new spanning verb. `FaceDetection.start()` gains a validation step on the worker; keep the existing test that a bring-up cancelled mid-start unwinds cleanly, and add the failed-validation case.
- **Write specs affirmatively** (current state, never the path taken); keep the `**Status:**` line and the `_index.md` row in sync at each flip.
- **Do not commit** unless asked.

## Scope

- `specs/testing/testing_support.md`, `specs/motion/head_tracking.md`, `specs/vision/user_perception.md`, `specs/core/bridge.md`, `specs/_index.md` — Step 1 (and Step 7).
- `src/reachy_mini_bridge/testing/support.py` — `BridgeLoop`: the background event loop a bridge lifecycle runs on; `src/reachy_mini_bridge/testing/__init__.py` — export it.
- `src/reachy_mini_bridge/testing/fixtures.py` — `LiveBridge` (the fixture's value: `bridge`, `capabilities`, `run`), `live_bridge` on a `BridgeLoop`.
- `src/reachy_mini_bridge/head_tracking.py` — `HeadTracker.tick()`, the loss clock.
- `src/reachy_mini_bridge/face_detection.py` — `on_observation(None)` for a poll without a report, the empty inactive report, `check_face_detector_factory` (callable only), `FaceDetection.start()` validating `detect`.
- `src/reachy_mini_bridge/bridge.py` — `_on_face_observation`, `set_face_detector(None)` refused while the loop runs, `start()`'s custom check.
- `tests/test_testing_support.py`, `tests/test_head_tracking.py`, `tests/test_face_detection.py`, `tests/test_bridge.py` — as each step says.
- `tests-e2e/test_bridge.py`, `tests-e2e/test_custom_faces.py`, `tests-e2e/test_sim_displays.py` (every `asyncio.run` on the live bridge) — `run(...)`.
- `docs/testing-with-the-bridge.md` "E2E tests", `README.md` "Testing your own project", `docs/custom-face-detector.md` (the registration check) — Step 6.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Check command green on `main`.

### Step 1 — The spec updates (set the four specs `Updated`)

- **`specs/testing/testing_support.md` "Public surface"**, the `live_bridge` bullet: the fixture yields a `LiveBridge` — a two-field named tuple `(bridge, capabilities)` (so `bridge, caps = live_bridge` and `requires_caps(live_bridge, …)` read as before) with a `run(coro, timeout=None)` method that executes a coroutine on the harness's event loop and returns its result. The bridge's lifecycle runs on one event loop, on a background thread, from `start()` to `stop()`: the bridge is loop-bound (its detection loop is an asyncio task, its observables publish on the loop thread — [observable.md](../specs/core/observable.md)), so every coroutine a test awaits on the bridge goes through `run`, never `asyncio.run` (which would run it on a second loop and, at setup time, kill the task the first one created). `BridgeLoop` is public in `reachy_mini_bridge.testing` for a consumer writing a fixture of their own (a second session, as the bridge's `live_bridge_custom_faces` does). "The gotchas move into the shipped code" gains this one. The consumer example uses `run`.
- **`specs/motion/head_tracking.md`**: "Inputs and outputs" — in: every poll of the detection loop, `observe(report)` for an observation, `tick()` for a poll without one. "Easing, loss, focus", the Loss bullet: the followed face is missing from the last observation that showed it; after `TRACKING_LOST_S` of that, whether the observations since lacked it or none arrived at all (no camera frame, a detector that keeps failing), the tracker follows nobody, withdraws the aim and `attention` reads `"watching"`. `TRACKING_SWITCH_S` keeps its meaning (a switch needs an observation showing someone else). The report section: the `set` at the loss happens on the tick too.
- **`specs/vision/user_perception.md`**: "The detection loop" — the loop tells its observer about every poll: `on_observation(report)` for an observation, `on_observation(None)` for a poll that produced none (no new frame, or a failed `detect`); the source-down report is `FaceReport.inactive(...)`, no faces. "The face report" (the tri-state bullet): an `active=False` report always carries `faces=()` — a caller waiting on `r.faces` never gets a stale face. "Custom detectors" "Checked at registration": the registered value must be callable (`ValueError` otherwise, the registered factory unchanged) — nothing is constructed at registration; "Building the detector": the loop's start calls the factory on a worker thread and rejects a result without a callable `detect` (`ValueError`, the loop not running; at session entry the bring-up fails with it, mid-session the verb raises it and the switches are left as they were) — so a factory is called once per detection start and nothing is built while nobody needs faces, which the spec already promises; `set_face_detector(None)` while the loop runs in `custom` mode (detection or tracking on) raises `ValueError` and changes nothing — stop tracking and detection first; registering another factory while it runs still swaps between two polls.
- **`specs/core/bridge.md` "Faces"**, the `set_face_detector` bullet: the same refusal, one sentence.
- Flip the four `**Status:**` lines and index rows to `Updated`.

### Step 2 — The harness loop (finding 1)

**Files:** `src/reachy_mini_bridge/testing/support.py`, `testing/__init__.py`, `testing/fixtures.py`, `tests/test_testing_support.py`, `tests-e2e/*.py`.

- `BridgeLoop`: `start()` creates a loop and a daemon thread running `run_forever()`; `run(coro, timeout=None)` submits through `asyncio.run_coroutine_threadsafe` and blocks on the future (a `KeyboardInterrupt` or timeout while blocking cancels the future, so a Ctrl+C in a live run cancels the verb — the cancellable-verb contract makes that clean); `stop()` stops the loop thread-safely and joins it. A context manager over the pair.
- `LiveBridge(NamedTuple)`: `bridge`, `capabilities`, plus `run` delegating to the loop (a `NamedTuple` with two fields and a method keeps the 2-tuple unpacking consumers use).
- `live_bridge`: `with BridgeLoop() as loop: loop.run(bridge.start()); … yield LiveBridge(bridge, caps, loop.run); finally loop.run(bridge.stop())`. The probes stay synchronous. Drop the docstring's claim that nothing binds to a loop.
- `tests-e2e/test_custom_faces.py`'s `live_bridge_custom_faces` uses `BridgeLoop` the same way. Every `asyncio.run(...)` on a live bridge in `tests-e2e/` becomes `run(...)`; the per-test re-arming of tracking (the comment at the top of the tracking section) goes — the detection task now lives the whole module, which is what the tests then prove.
- `tests/test_testing_support.py`: through `BridgeLoop`, start a `fake` bridge with a stub detector that alternates a face on and off over time and tracking on; across a plain `time.sleep` (no coroutine running), assert `bridge.faces.value` keeps changing and `bridge.head_tracking.value.attention` engages; after `stop()` the detection task is done and the loop thread has exited. Also: `run` propagates the coroutine's exception; a `run` after `stop()` raises.

### Step 3 — Loss measured in time (finding 3)

**Files:** `src/reachy_mini_bridge/head_tracking.py`, `face_detection.py`, `bridge.py`, `tests/test_head_tracking.py`, `tests/test_face_detection.py`, `tests/test_bridge.py`.

- `HeadTracker`: record `_seen_at` on every observation that shows the followed face; `tick(now=None)` runs the missing / loss logic against it (`observe` calls the same logic after matching). Loss on a tick withdraws the aim and publishes the report, as on an observation.
- `FaceDetection._run`: `on_observation(None)` on every poll that yields no report (the type becomes `Callable[[FaceReport | None], None]`); the source-down publish is `FaceReport.inactive(self._name)`.
- `ReachyMiniBridge._on_face_observation`: `None` → `tracker.tick()`, a report → `observe`.
- Tests. `test_head_tracking.py`: a face observed, then ticks only — the aim stands until `TRACKING_LOST_S`, then `set_gaze(None)`, `attention == "watching"`, the report `set` once; a face observed, a report without it, then no more observations — the loss still comes `TRACKING_LOST_S` after the last sighting. `test_face_detection.py`: a stub whose `detect` raises after the first observation — the report turns inactive after `FACE_SOURCE_DOWN_S` with `faces == ()`; the observer saw `None` polls. `test_bridge.py`: the integration under `fast_faces` — a stub shows a face then raises forever: the gaze fades out and `attention` reads `"watching"` after the loss timeout, `faces` goes inactive and empty after the source-down timeout, the stub recovering re-engages the head.

### Step 4 — The custom detector, built once (finding 4)

**Files:** `src/reachy_mini_bridge/face_detection.py`, `bridge.py`, `tests/test_face_detection.py`, `tests/test_bridge.py`.

- `check_face_detector_factory`: callable or `ValueError`; it no longer calls the factory. `FaceDetection.start()`: after `await asyncio.to_thread(factory)`, a result without a callable `detect` raises `ValueError` (the loop not running; the built object `close()`d if it has one). `ReachyMiniBridge.start()`'s custom check becomes the callable check.
- `set_face_detector(None)` while `self._detection.running` and the detector is `custom`: `ValueError`, nothing changed. A non-`None` factory still swaps a running loop.
- Tests: a factory that records its call count and `threading.get_ident()` — not called at registration nor at a session entry with both switches off, called once on a worker thread when tracking starts, once more on a restart; a factory returning an object without `detect` — `set_face_detector` accepts it, session entry with `enabled=True` fails with `ValueError` (the session unwound), `start_head_tracking()` mid-session raises it with `tracking` still `False`; clearing while tracking → `ValueError`, `tracking` and `face_detector` unchanged, the head still aimed; clearing after `stop_head_tracking()` works and the next `start_head_tracking()` refuses for lack of a factory.

### Step 5 — The sim displays and the control panel still read

The `FaceMarkerPublisher` subscribes to `faces` and the control panel reads `faces` / `head_tracking`: an inactive report with no faces must clear the markers and the panel's overlay. `tests/test_sim_displays.py` / `tests/test_control_panel.py`: a source-down report clears them (adjust where they assumed the last faces survived).

### Step 6 — Docs

- `docs/testing-with-the-bridge.md` "E2E tests" and `README.md` "Testing your own project": the snippets use `run(...)`; one sentence on why (one loop for the bridge's lifetime). `docs/custom-face-detector.md`: registration checks the callable, the loop's start builds and validates; clearing while tracking is refused.

### Step 7 — Close

- Verification below green. Flip the four specs to `Implemented` (status line and index). Delete findings 1, 3 and 4 from `analysis/repo-spec-code-consistency-review-20260929.md` (untracked). Mark this plan `Done` here and in [_index.md](_index.md).

## Verification

- `uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest` — green, with the new tests of Steps 2 to 5.
- `uv run pytest tests-e2e -rs` (headless sim) — green; skips are camera / gravity compensation only.
- `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` (viewer sim, Mac awake and unlocked) — the tracking, attention, multi-portrait, custom-faces and face-marker tests pass **without** the per-test re-arming; a tracking test that follows a portrait across the module boundary of a previous test is the proof that the detection task lived through setup.
