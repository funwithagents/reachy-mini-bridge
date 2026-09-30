# Face report tracks and detection knobs — `track_id`, `bbox`, `frame`, orientation; the `face_detection` block with `width` and `target_fps`

**Status:** Todo

Implements the 2026-09-30 re-design of [specs/vision/user_perception.md](../specs/vision/user_perception.md) ("The face report", "The report is an observable", "The detection loop", "Tracks", "The shipped detector — `yunet.py`", "Custom detectors", "Configuration", "`fake` backend support") and the matching edits of [specs/core/config.md](../specs/core/config.md) (the `face_detection` block, validation), [specs/core/bridge.md](../specs/core/bridge.md) (Faces, the `head_tracking` observable), [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) ("Whom the head follows", "Loss", "The head tracking report"), [specs/testing/testing_support.md](../specs/testing/testing_support.md) (`live_bridge`'s config) and [specs/examples/control_panel.md](../specs/examples/control_panel.md) (the config's blocks). It delivers:

- **the report a client enriches**: a `track_id` on every face, carried by nearest matching (upstream's association rule applied to every face), a pixel `bbox`, `pitch` / `yaw` beside `roll`; on the report, the `frame` the faces were found in (`frame_id` delegating to it);
- **whom the head follows, chosen by the head tracker**: the biggest face at first, held while it is reported whatever else appears; when it goes missing the head holds toward its last position for `TRACKING_SWITCH_S` before switching to the biggest other face, and hands back after `TRACKING_LOST_S` with nobody to switch to — the detection loop's own target selection removed, the report a plain list in id order;
- **the head tracker's state as an observable**: `bridge.head_tracking: Observable[HeadTrackingReport]` — `active`, `focus`, `attention`, the `track_id` of the face the head follows — waking on a start / stop, a focus switch, an attention change or the head passing to another person;
- **the detector contract's additions**: `PixelFace.orientation` (preferred over the eye-line roll) and an optional `close()` the loop calls when it lets go of a detector;
- **the config block renamed** — `faces` → `face_detection`, `detection` → `enabled`, `FaceSettings` → `FaceDetectionSettings`, a clean break — with its two cost knobs: `width` (the shipped detector's working width, default `null` — the detector's own: 320 for `yunet`) and `target_fps` (a ceiling on detections per second the loop enforces for every detector);
- **the detector's cost logged** once per run at `INFO`.

It deliberately leaves out: any second shipped detector — other detectors, a vision library's included, plug in as `custom` (user_perception "The shipped detector — `yunet.py`"); a preferred track (user_perception open question 7); a graph-driven source (open question 8).

Depends on [202609300830](202609300830_test-scene-portrait-pool.md) for its live tests (the test scene's pool of portraits, `spawn` / `despawn`).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/vision/user_perception.md](../specs/vision/user_perception.md) in full; [specs/core/config.md](../specs/core/config.md) "`face_detection` block", "Validation rules"; [specs/core/bridge.md](../specs/core/bridge.md) "Faces"; [specs/vision/camera.md](../specs/vision/camera.md) "The frame" (frames are shared read-only, `frame_id` counts across sessions); `src/reachy_mini_bridge/face_detection.py` and `yunet.py` as they stand.
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

  (`ruff format .` would reflow the Python blocks in `plans/*.md`; format the code directories only.)
- **No network in `tests/`.** Every fast test that needs faces registers a stub detector through `face_detection.detector: "custom"`; `YuNetDetector` is tested on a stub upstream.
- **Cancellation.** No new spanning verb; the loop's own cancel path (`stop()`) gains the detector's `close()` — test that a `stop()` cancelling a loop mid-`detect` still closes the detector and leaves the bridge restartable.
- **Write specs affirmatively**: the spec edits are done; this plan only flips statuses at the end. Docs describe the current state, not the change.
- **Do not commit** unless asked.

## Scope

- `src/reachy_mini_bridge/config.py` — `FaceDetectionSettings` (`detector`, `enabled`, `width: int | None = None`, `target_fps: float | None = None`, `face_detector`); `ReachyMiniConfig.face_detection`; the top-level key `face_detection`; validation; `FACE_DETECTORS` unchanged at `(\"yunet\", \"custom\")`.
- `config.example.json` — `"face_detection": {"detector": "yunet", "enabled": true, "width": null, "target_fps": null}`.
- `src/reachy_mini_bridge/face_detection.py` — `Face` (`track_id`, `bbox`, `pitch`, `yaw`), `FaceReport` (`frame`, `frame_id`), `PixelFace.orientation`, the tracks (`_FaceTracks` replacing `_FaceSelector`), `report_from_pixels`, the target-swap publish, `target_fps`, `close()`, the cost log; `FaceDetection(width=..., target_fps=...)`.
- `src/reachy_mini_bridge/yunet.py` — `YuNetDetector(upstream=None, *, width=None)`, `None` its own `DETECT_WIDTH`.
- `src/reachy_mini_bridge/head_tracking.py` — the choice of whom to follow (`TRACKING_SWITCH_S`, `TRACKING_MIN_SIZE`); `HeadTrackingReport`, published by the tracker.
- `src/reachy_mini_bridge/__init__.py` — exports `HeadTrackingReport`.
- `src/reachy_mini_bridge/bridge.py` — the `head_tracking` observable; `self._config.face_detection.*` everywhere `faces.*` was; `FaceDetection` built with `width` / `target_fps`; the messages naming `FaceDetectionSettings.face_detector`.
- `src/reachy_mini_bridge/testing/fixtures.py` — `FaceDetectionSettings(detector="yunet", enabled=True)`.
- `examples/control_panel/app.py` — `config.face_detection.enabled`.
- `tests/test_config.py`, `tests/test_face_detection.py`, `tests/test_yunet.py`, `tests/test_head_tracking.py`, `tests/test_bridge.py`, `tests/test_control_panel.py`, `tests-e2e/test_custom_faces.py`, `tests-e2e/test_bridge.py` — as each step says.
- `README.md`, `docs/custom-face-detector.md`, `docs/testing-with-the-bridge.md`, `AGENTS.md` (the `yunet.py` row names `face_detection.detector`) — Step 7.
- `specs/_index.md`, `plans/_index.md`, this file — statuses (Step 8).

## Steps

### Step 0 — Baseline

Check command green on `main`.

### Step 1 — Config: the `face_detection` block

**Files:** `src/reachy_mini_bridge/config.py`, `config.example.json`, `tests/test_config.py`.

- Rename `FaceSettings` → `FaceDetectionSettings` (`__all__` too), `detection` → `enabled`, `ReachyMiniConfig.faces` → `ReachyMiniConfig.face_detection`, the top-level key `faces` → `face_detection`; no alias — `faces` is an unknown top-level key (`ConfigError` naming it, as any unknown key).
- `width`: absent or `null` → `None` (the detector's own width); else a positive `int` that is not a `bool` (`ConfigError` naming `face_detection.width` otherwise — `0`, `-1`, `320.0`, `true`, `"320"`).
- `target_fps`: absent / `null` → `None`; else a positive finite `int` / `float` that is not a `bool` (`ConfigError` naming `face_detection.target_fps` otherwise — `0`, `-2`, `nan`, `inf`, `true`).
- The cross-block rule names `face_detection.detector` and `face_detection.enabled` / `motion.tracking`; the `face_detector` rejection message points at `FaceDetectionSettings(face_detector=...)`.
- `config.example.json`: the new block, all four JSON fields spelled out.
- Tests: rename the existing ones; add the defaults (`width is None`, `target_fps is None`), `width: 640` kept, the rejections above, `target_fps: 2.5` and `target_fps: 5` accepted, `{"faces": {...}}` a `ConfigError` naming `faces`, the trio on `FaceDetectionSettings`, the example file round-trips.
- Check (the rest of the suite fails to import `FaceSettings` until Step 2 lands its renames — do Steps 1 and 2 together if that is simpler, one check at the end of Step 2).

### Step 2 — The rename through the code

**Files:** `src/reachy_mini_bridge/bridge.py`, `face_detection.py` (the `ValueError` message), `testing/fixtures.py`, `examples/control_panel/app.py`, `tests/test_bridge.py`, `tests/test_control_panel.py`, `tests-e2e/test_custom_faces.py`.

- Every `config.faces.X` → `config.face_detection.X` (`detection` → `enabled`); every `FaceSettings(...)` → `FaceDetectionSettings(...)`. `grep -rn "FaceSettings\|\.faces\.detect\|\.faces\.face_detector\|\"faces\"" src tests tests-e2e examples` finds only the testing harness's `faces` capability (`caps.add("faces")`, `requires_caps(..., "faces")`) and the control panel test's table assertion.
- No behaviour change. Check.

### Step 3 — Tracks and the report's new fields

**Files:** `src/reachy_mini_bridge/face_detection.py`, `tests/test_face_detection.py`.

- `Face`: append `track_id: int = 0`, `bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)`, `pitch: float | None = None`, `yaw: float | None = None` (after the four existing fields, so positional construction still works).
- `FaceReport`: append `frame: CameraFrame | None = field(default=None, compare=False)` and a `frame_id` property (`frame.frame_id` or `0`). `inactive()` leaves `frame` at `None`.
- `PixelFace`: append `orientation: tuple[float, float, float] | None = None`.
- Replace `_FaceSelector` with `_FaceTracks` implementing "Tracks" exactly: normalised centres (`_pixel_centre` → `_normalised`); candidate pairs within `TRACK_MAX_JUMP`, taken nearest first, each side once; unmatched faces open tracks from a counter **owned by `FaceDetection`** (passed in, or a callable), so ids keep counting across `start()` / `stop()` and across restarts of the custom factory; unmatched tracks count a miss and are dropped after `TRACK_MAX_MISSES`. `update(faces, size) -> list[int]` (the id of each face). `SELECT_MAX_JUMP` / `SELECT_MAX_MISSES` become `TRACK_MAX_JUMP` / `TRACK_MAX_MISSES` (same values); `SELECT_MIN_AREA_FRAC` leaves the module (the tracker's `TRACKING_MIN_SIZE`, Step 4).
- `report_from_pixels(faces, size, frame, track_ids, *, source)`: faces ordered by `track_id` ascending; each `Face` gets its `track_id`, `bbox` (the `PixelFace`'s, already in the frame's pixels), roll/pitch/yaw from `orientation` when given, else roll from the eyes; the report gets `frame`.
- The debounce is unchanged: `set` on a count change or an `active` flip only.
- Tests (pure, on `_FaceTracks` and `report_from_pixels`, plus a few through the loop on the fake with a scripted stub): a face moving a little each frame keeps its id; two faces keep theirs while both move; two faces swapping sides across frames in small steps do not trade ids; a gap of `TRACK_MAX_MISSES` misses keeps the id, one more opens a new id; ids never reused after a drop; the faces in id order whatever order the stub returns them in; `report.frame is` the frame handed to the stub, `report.frame_id == frame.frame_id`, `bbox` equal to the stub's; `orientation` preferred over the eyes' roll, `pitch` / `yaw` filled; without `orientation` the eye-line roll and `pitch` / `yaw` `None`; two reports differing only by `frame` compare equal; ids continue across a `stop()` / `start()` of the loop; a face merely moving publishes nothing.
- Existing tests that relied on the detection loop putting the selected face first (in `tests/test_face_detection.py`, `tests/test_head_tracking.py`, `tests/test_bridge.py`) move to Step 4's tracker tests or are rewritten on track ids. Until Step 4 lands, the tracker aims `faces[0]` of a list in id order — land Steps 3 and 4 together if the live behaviour between them matters.
- Check.

### Step 4 — Whom the head follows, and the head tracking report

**Files:** `src/reachy_mini_bridge/head_tracking.py`, `bridge.py`, `__init__.py`, `tests/test_head_tracking.py`, `tests/test_bridge.py`.

- `head_tracking.py`, the choice ([specs/motion/head_tracking.md](../specs/motion/head_tracking.md) "Whom the head follows"): the tracker holds `_following: int | None` and `_missing_since: float | None`. `observe(report)`: the followed id in the report → aim that face, `_missing_since = None`; followed but absent → `_missing_since` set on the first such observation, the previous aim standing (no `set_gaze`); absent for `TRACKING_SWITCH_S` → follow the face of largest `size` with `size >= TRACKING_MIN_SIZE` if any, aim it; absent for `TRACKING_LOST_S` with none eligible → follow nobody, withdraw the aim (`set_gaze(None)`); following nobody → acquire the largest eligible face at once. Module constants `TRACKING_SWITCH_S = 1.0`, `TRACKING_MIN_SIZE = 0.07`, read at run time so tests shorten them. The loss rule's clock is the tracker's (`time.monotonic()`), as today.
- `HeadTrackingReport(active, focus, attention, track_id, ts)` (frozen) with an `inactive()` classmethod. `HeadTracker` takes an `Observable[HeadTrackingReport]` (or a publish callback) and publishes from `observe` / focus changes / `stop()`: `track_id` = the followed id, `None` once watching; `set` when `active`, `focus`, `attention` or `track_id` changes, `update` otherwise (the aimed report's `ts`).
- `bridge.py`: `head_tracking` property — the observable, created at construction with the inactive value, outliving sessions; the active value `set` when tracking starts, the inactive value `set` when it stops and at `stop()`; `tracking_focus` and `attention` read the report. `__init__.py` exports `HeadTrackingReport`.
- Tests — the choice, on `HeadTracker` directly with scripted reports and a patched clock (no fake needed), each asserting the `set_gaze` calls and the published reports: the biggest eligible face acquired; a bigger face appearing beside the followed one → still the followed one's aim; the followed face absent for less than `TRACKING_SWITCH_S` with another in view → no new aim (the last one stands), and the same `track_id` followed when it returns; absent for `TRACKING_SWITCH_S` → one `set` naming the biggest remaining eligible face and an aim at it; only faces below `TRACKING_MIN_SIZE` in view → no switch, the hold continuing, a loss at `TRACKING_LOST_S`; an eligible face appearing between the switch and loss times → followed at once; a followed face shrinking below the minimum → still followed. Then on the fake, through `bridge.head_tracking.changes()`: start (`active`), engaging a shown face (`"engaged"`, its `track_id`), the loss (`"watching"`, `track_id None`), a `focus` switch, stop (inactive); no wake while a face merely moves (`value.ts` advancing); the value persisting across two sessions of one bridge object; a subscriber cancelled mid-wait ending cleanly.
- Check.

### Step 5 — The cost knobs and the detector's release

**Files:** `src/reachy_mini_bridge/face_detection.py`, `yunet.py`, `bridge.py`, `tests/test_face_detection.py`, `tests/test_yunet.py`, `tests/test_bridge.py`.

- `YuNetDetector(upstream=None, *, width: int | None = None)`: `target = DETECT_WIDTH if width is None else width`, `step = max(1, frame_width // target)`. `_yunet_factory` becomes a factory built with the configured width (`functools.partial(YuNetDetector, width=width)`); `FaceDetection(..., width: int | None = None, target_fps: float | None = None)`; the bridge passes the config's values.
- `target_fps` in `_poll`: remember the monotonic start time of the last `detect`; a new frame arriving before `1 / target_fps` has passed is marked seen (`_last_frame_id`) and returns `None` without counting as a failure. The down rule's window becomes `max(FACE_SOURCE_DOWN_S, 2 / target_fps)` when a ceiling is set.
- `close()`: a helper `_release(detector)` running `detector.close()` on a worker thread when it is callable, logging a raise at `DEBUG`; called from `stop()` (after the task is cancelled) and from the poll that replaces a detector after `restart(...)` (the old one is kept until then, or released in `restart` itself — pick one and test it).
- The cost log: from the first observation of a run, accumulate the `detect` call durations and the observation count; at `FACE_COST_LOG_S = 10.0` (module constant, read at run time so tests shorten it) log one `INFO` line — detector name, `width`, `target_fps`, mean call time in ms, observations/s — and stop accumulating for the run.
- Tests: `YuNetDetector(width=640)` on 1280×720 → stride 2; `width=None` → stride 4 on 1280×720 (its own 320); `width=1920` → stride 1 on 1920×1080; the bridge built with `width=640` hands it to the shipped factory (patch the module's factory seam, no model). On the fake with a counting stub: `target_fps=2` → about two calls a second over two seconds (tolerance for timing; the fake's frame rate is higher), `None` → one call per fake frame; the detector not reported down with `target_fps=0.1` within `FACE_SOURCE_DOWN_S` (shortened) while frames arrive. A stub with `close()`: closed once at `stop()`, closed once when `set_face_detector` swaps it, closed when `stop()` cancels a `detect` in flight (the stub blocks on an event); a `close()` raising does not break `stop()`. The cost line appears once (`caplog`, `FACE_COST_LOG_S` shortened) with the stub's name and a positive rate.
- Check.

### Step 6 — Live tier

**Files:** `tests-e2e/test_bridge.py`, `tests-e2e/test_custom_faces.py`.

- Headless: `uv run pytest tests-e2e -rs` green (the renamed config in the fixture).
- Viewer sim (`REACHY_MINI_E2E_SIM_VIEWER=1`): the attention / gaze tests and the custom test green; add to the tracking test an assertion that the portrait keeps one `track_id` while it moves and is the `track_id` `bridge.head_tracking` reports while engaged, and that `report.frame.image` crops to a non-empty image at `faces[0].bbox`. Add the multi-portrait tests of [specs/testing/sim_scene.md](../specs/testing/sim_scene.md) "The testing harness" — the biggest first, stickiness, the hold, the switch, the loss — each spawning its portraits from the pool (`sim_scene.spawn(...)`, near at 0.35 m and far at 0.60 m for the size difference, ±0.15 m to either side) and asserting through `bridge.head_tracking` (`track_id`, `attention`, the published changes) and the head's yaw (`atan2(y, x)` within 5°). Record the cost line the log shows (detector, 320, rate, mean ms) in the Done note.
- One manual run at `width: 640` on the viewer sim: the tracking tests still green; note the cost line.

### Step 7 — Docs and the project map

**Files:** `README.md` (the config tables and examples: `face_detection`, `enabled`, `width`, `target_fps`; the Faces row of the verb table: track ids, boxes, frame; the Gaze row: `head_tracking` and its report), `docs/custom-face-detector.md` (the config names; `orientation` and the optional `close()`; the knobs — `target_fps` applies, `width` is the author's; the section on a vision graph rewritten to the enrichment path: a graph reads `bridge.faces` — frame, boxes, track ids — and joins its results by `track_id`, a detector object registered with the bridge is run by the bridge), `docs/testing-with-the-bridge.md` (config names), `AGENTS.md` (the `yunet.py` row: `face_detection.detector: "yunet"`, the width from `face_detection.width`).

### Step 8 — Statuses

- `specs/motion/head_tracking.md`, `specs/testing/sim_scene.md` and `specs/examples/control_panel.md` → `Implemented` (file and index).
- `specs/vision/user_perception.md`, `specs/core/config.md`, `specs/core/bridge.md` and `specs/testing/testing_support.md` → `Implemented` (file and index).
- This plan `Done` (file and [_index.md](_index.md)) with a Done note: departures, the cost numbers measured in Step 6.

## Verification

- The check command green after every step; the fast tier's new tests as listed (tracks, whom the head follows, the head tracking report, frame / bbox, orientation, `target_fps`, `close()`, cost log, width stride).
- `uv run pytest tests-e2e -rs` green headless; `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` green on the viewer sim (the attention / gaze tests with the new track-id and crop assertions, the custom test), skips read and reported.
- `grep -rn "FaceSettings\|faces\.detection\|\"faces\": {" src tests tests-e2e examples docs README.md config.example.json` finds nothing.
