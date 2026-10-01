# Face marker publisher as an asyncio task — the sim's face markers sent from the bridge's loop

**Status:** Done

**Done (2026-10-01):** every step implemented and verified — `ruff check`, `ruff format` (code dirs), `pyright`, the fast tier three times in a row (627 passed each time, the new held-request test included), the headless live tier (15 passed, 15 skipped: camera, gravity compensation, the sim ignoring motor modes, as expected) and the viewer-sim live tier (28 passed, 2 skipped: gravity compensation, motor modes). No departure from the steps.

- **The viewer sim sends its markers from the task.** Both marker tests pass, with the `face_markers` capability present. Markers land within 0.008 m sideways and 3 % in distance of the portraits, as before.
- **The marker of a still portrait, through an emotion:** 0.032 m at most before the change (one run); 0.038, 0.032, 0.030 and 0.027 m after (four runs), for a bound of 0.05 m. The same run-to-run range as with the thread (0.023–0.034 m).
- `bridge.py` needed no change: the publisher's `start()` / `stop()` kept their shape.

Implements the re-design this plan's Step 1 makes in [specs/daemon/sim_displays.md](../specs/daemon/sim_displays.md) ("Bridge → daemon: the displays route", "Testing") and [specs/core/bridge.md](../specs/core/bridge.md) ("Lifecycle"). It delivers `FaceMarkerPublisher` as an **asyncio task on the bridge's event loop**, like the detection loop, in place of the thread it is today. Its behaviour stays the same: what it sends, when, to where, its warnings, and a `stop()` that does not wait for the daemon.

It deliberately leaves out any change to the markers themselves (geometry, size, the one face height), to the daemon side (the route, the scene layer), and to the config.

## Why

The publisher became a thread on 2026-10-01 for one reason: the testing harness started the bridge under one `asyncio.run` and ran each test under another, so a task created in `start()` was dead by the time a test ran, and no marker was sent on the viewer sim ([202609301600](202609301600_sim-displays-and-inject-routes.md), Done note). Commit 3fb2129 ([202610011455](202610011455_detection-loop-robustness-and-harness-event-loop.md)) removed that reason: `live_bridge` keeps one event loop from `start()` to `stop()` (`BridgeLoop`) and tests await through `LiveBridge.run`. The bridge is loop-bound by design, and a consumer's app has one loop for the bridge's lifetime.

With the reason gone, a task is the better fit:

- **One model.** The bridge's own polling workers are tasks on its loop (the detection loop). The publisher matches it.
- **No cross-thread reads.** As a task it reads `bridge.faces.value`, the tracker's `delay_s` and the pose history on the loop thread, where the first two are written.
- **An exact stop.** Cancelling the task ends it at its next await. The thread version signals, joins for 0.1 s, and leaves the thread to finish on its own.
- **The spec's stated reason is false today.** `sim_displays.md` and the class docstring both say the harness uses several loops.

## How to work this plan

- **Read first:**
  - [AGENTS.md](../AGENTS.md) (statuses: Step 1 flips the two specs to `Updated`, Step 5 back to `Implemented`);
  - [specs/daemon/sim_displays.md](../specs/daemon/sim_displays.md) "Bridge → daemon: the displays route" and "Testing";
  - `src/reachy_mini_bridge/sim_displays.py` (`FaceMarkerPublisher`, `_put_markers`, the `_PUBLISH_*` constants);
  - `src/reachy_mini_bridge/bridge.py` (where `start()` builds the publisher and pushes its `stop` on the exit stack);
  - `src/reachy_mini_bridge/face_detection.py` (`FaceDetection.start` / `stop` / `_run`: the task shape to match);
  - `tests/test_sim_displays.py` (the five publisher tests) and `tests/test_bridge.py` (the three face-marker tests).
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

  Don't run `ruff format .`: it reflows the Markdown in `plans/` and `docs/`.
- **The viewer sim is the acceptance** (Step 4): `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` from the agent session, with the Mac awake and unlocked. This is the tier where the task version sent nothing before 3fb2129, so it is the one that proves the change.
- **Cancellation** (AGENTS.md "Testing"): the publisher's effect spans time. Keep the test that stops it mid-request and asserts it ended at once and the session still works.
- **Write specs affirmatively** (current state, never the path taken).
- **Do not commit** unless asked.

## Scope

- `specs/daemon/sim_displays.md` — the "Bridge side" paragraph and its bullets, the publisher's line in "Testing" (Step 1).
- `specs/core/bridge.md` — one phrase in "Lifecycle": the publisher is a task (Step 1).
- `specs/_index.md` — the two rows' statuses, at each flip.
- `src/reachy_mini_bridge/sim_displays.py` — `FaceMarkerPublisher` as a task; `_PUBLISH_STOP_JOIN_S` removed; the class docstring.
- `src/reachy_mini_bridge/bridge.py` — nothing expected: `await publisher.start()` and `stack.push_async_callback(publisher.stop)` keep their shape. Reread the comment above them.
- `tests/test_sim_displays.py` — the publisher tests, adjusted where they depend on the thread (Step 3).
- `tests/test_bridge.py` — nothing expected; the three face-marker tests must pass unchanged.
- `plans/_index.md`, this file — statuses.

## Steps

### Step 0 — Baseline

The check command is green on `main`. Run the viewer-sim marker tests once to have the "before" numbers:

```
REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_bridge.py -k face_marker -rs -s
```

### Step 1 — The specs

Set both specs to `Updated` (the `**Status:**` line and the `specs/_index.md` row).

**`specs/daemon/sim_displays.md`, "Bridge → daemon: the displays route".** Replace the "Bridge side" paragraph with:

> **Bridge side.** `FaceMarkerPublisher` is an asyncio task on the bridge's event loop, started and stopped through an async `start()` / `stop()` pair like the detection loop. It runs for the whole session when the backend is `sim` and `daemon.sim_displays.face_markers` is on ([bridge.md](../core/bridge.md) "Lifecycle"), whether or not detection is switched on: an inactive report is what it sees while detection is off. It reads the face report, the tracker's delay and the pose history on the loop, and hands each request to a worker thread (`asyncio.to_thread`), so a slow daemon never holds the loop.

In the bullets under it:

- The second bullet keeps its content and says the `PUT` runs on a worker thread: "It is `PUT` from a worker thread, with `urllib` and a 0.5 s timeout, to the daemon's address…".
- The last bullet becomes: "Nothing it does raises into the session. `stop()` cancels the task and returns without waiting for a request in flight: the request ends on its own within its timeout, on its worker thread, and the session's teardown carries on."

**`specs/daemon/sim_displays.md`, "Testing".** In "The publisher." bullet, keep the list and end it with: "a stop mid-request returning at once with the task ended, and a fresh start sending again."

**`specs/core/bridge.md`, "Lifecycle".** In the sentence that introduces the face marker publisher, "a thread that polls `bridge.faces`" becomes "a task that polls `bridge.faces`".

No other spec changes: `testing_support.md` already describes the one-loop harness, and `head_tracking.md` / `user_perception.md` do not mention how the publisher runs.

### Step 2 — The publisher as a task

In `src/reachy_mini_bridge/sim_displays.py`:

- **State.** `self._task: asyncio.Task[None] | None` replaces `self._stop` and `self._thread`.
- **`running`** — the task exists and is not done. It reads false once the publisher stopped itself on a `404`.
- **`start()`** — when no task exists, `asyncio.create_task(self._run(), name="face-marker-publisher")`. A second `start()` on a running publisher is a no-op, as today. A `start()` after `stop()`, or after the task ended on a `404`, creates a fresh task.
- **`stop()`** — take the task, cancel it, and `await asyncio.gather(task, return_exceptions=True)`. Idempotent.
- **`_run()`** — the same loop as today, awaited:
  - read `self._faces.value` and compute the key `(active, frame_id, ts)`;
  - on a new key, build the markers on the loop (`self._markers(report)`), then `found = await asyncio.to_thread(self._send, self._url, markers)`;
  - an exception from either is one `WARNING` (with the traceback), then `DEBUG`, and the loop goes on; `asyncio.CancelledError` is not caught (it is a `BaseException`), so a cancel during the send ends the task;
  - `found` false is the one `WARNING` naming `--sim-display face_markers`, then the task returns;
  - `await asyncio.sleep(self._period)`.
- **Remove** `_PUBLISH_STOP_JOIN_S` and the per-start `threading.Event`. `threading` stays imported for the overlay and the scene layer.
- **Docstring.** State what it is today: a task on the bridge's loop, requests on a worker thread. Drop the sentence about the harness's loops.

`_put_markers`, `face_markers_url`, `fetch_face_markers` and `_markers` are unchanged.

In `src/reachy_mini_bridge/bridge.py`, the wiring keeps working as written. The exit stack still stops the publisher after the motion session and before the detection loop.

### Step 3 — The fast tests

`tests/test_sim_displays.py`, the five publisher tests. They already drive `start()` / `stop()` from inside `asyncio.run`, so most pass as they are. Check each:

- **one set per new report**, **the same marker whatever the camera**, **a failing daemon**: unchanged.
- **a daemon without the route**: `publisher.running` reads false once the task returned. Keep the assertion that a later report sends nothing.
- **stopping mid-request**: the stand-in `send` blocks on a `threading.Event` in the worker thread. Assert that `stop()` returns in well under the 5 s the send would take, that `running` is false, and that a fresh `start()` sends again. Release the event before the test's loop closes: `asyncio.run` waits for the default executor's threads when it shuts down, so a send still blocked there would hold the test for its full wait.
- **Add: the loop stays responsive during a send.** With `send` blocked in its worker thread, a coroutine on the same loop (an `asyncio.sleep(0.05)` timed with `time.monotonic()`) completes on time. This pins that the request is off the loop, which is what the task version must not get wrong.

`tests/test_bridge.py`, the three face-marker tests (the sim session sending its faces, nothing with the display off, a session stopped mid-request starting again): unchanged, and they must pass.

Run the fast tier three times in a row. The publisher tests are timing-sensitive, and a flake shows within a few runs.

### Step 4 — The viewer sim

```
REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -s
```

- `test_a_face_marker_lands_on_the_portrait` and `test_a_face_marker_stays_put_while_the_head_moves` pass, with numbers in line with Step 0's (markers within 0.03 m sideways and 15 % in distance; a still portrait's marker within 0.05 m while the head moves).
- The `face_markers` capability is probed present, so neither test skips. A skip here is not a pass: report it.
- The rest of the viewer tier is unaffected.

Run the headless tier too (`uv run pytest tests-e2e -rs`): unchanged results, the marker tests skipping on `camera`.

### Step 5 — Statuses

- `specs/daemon/sim_displays.md` and `specs/core/bridge.md` back to `Implemented`, in the files and in `specs/_index.md`.
- This plan `Done` with a Done note (the test counts, the Step 4 numbers, any departure), here and in `plans/_index.md`.
- Add one line to the Done note of [202609301600](202609301600_sim-displays-and-inject-routes.md), under its "the publisher is a thread" departure, pointing here.

## Verification

- Lint, format (code dirs only), `pyright`, the full fast tier, three runs.
- `tests/test_project_map.py` (the spec frontmatter is unchanged: no file added or removed).
- The headless live tier: unchanged.
- The viewer live tier: the two marker tests pass and send markers; everything else as before.
