# Shipped YuNet detector and opt-in detection — `yunet.py`, `faces.detector: null | yunet | custom`

**Status:** Done

**Done (2026-09-29):** every step implemented and verified — `ruff check`, `ruff format`, `pyright`, the fast tier (517 tests), the headless live tier, and the viewer-sim live tier (four runs of the attention / gaze tests and the custom test). Departures from the steps as written: the "imports no ONNX Runtime" test of Step 2 was dropped, because `reachy_mini` itself imports its vision package at import time (the lazy import in `yunet.py` stays, for the module cycle with `face_detection.py`); `Face.size` became a plain `float`. Step 10's measurement found the delay estimate settling between 0.05 and 0.45 s (mostly 0.1–0.25), so the 0.2 s prior stands, and found it running away toward the range's cap while the portrait glides and the head turns — fixed by capping the range at 0.5 s and taking a refit only when it is distinct (`DELAY_MAX_CONTRAST`, [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) "The delay estimate", pinned offline by a moving-face test); the live gaze tests now check the face at the image centre precisely (0.05) and the head's yaw coarsely (5°), since the idle roam composed under gaze scatters the settled yaw by about ±2° ([specs/testing/sim_scene.md](../specs/testing/sim_scene.md)). The shipped detector reports 10.0 observations/s on the viewer sim.

Implements [specs/vision/user_perception.md](../specs/vision/user_perception.md) as re-designed on 2026-09-29 ("Detectors", "The shipped detector — `yunet.py`", "Configuration", "`fake` backend support") and the matching edits of [specs/core/config.md](../specs/core/config.md) (`faces` block, `motion.tracking`, the cross-block rule), [specs/core/api.md](../specs/core/api.md) (Faces, Attention / gaze, Lifecycle), [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) (the observation's time, the fake), [specs/core/robot.md](../specs/core/robot.md) (the consumed slice), [specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md) (the backend subclass) and [specs/testing/testing_support.md](../specs/testing/testing_support.md) (`live_api`'s config). It delivers: the bridge's shipped face detector — upstream's YuNet model wrapped as a bridge `FaceDetector` in a new `yunet.py` module, named `yunet` in `faces.detector`; detection and tracking **opt-in** (no detector, no detection, no tracking by default; a config names the detector); the `daemon` detection source, its ε-weight arming of the daemon's tracker and the sim launcher's tracking-step correction deleted, the daemon's tracking left untouched everywhere; the fake without a face stand-in, the bridge's own tests driving faces through a stub detector over the fake's frames. It deliberately leaves out: a `daemon` source for the wireless robot (user_perception open question 1), a face stand-in shipped in `reachy_mini_bridge.testing` (open question 6), further detector families (open question 7).

Depends on [202609291000](202609291000_camera-feed-and-custom-face-detectors.md) (the runner over the camera feed that every detector now goes through).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/vision/user_perception.md](../specs/vision/user_perception.md) in full; [specs/core/config.md](../specs/core/config.md) "`faces` block", "`motion` block", "Validation rules"; [specs/core/api.md](../specs/core/api.md) "Lifecycle", "Faces", "Attention / gaze"; [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) "The aim", "`fake` backend support"; [specs/core/robot.md](../specs/core/robot.md) "The consumed slice"; [specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md) "The backend subclass", "Testable without a daemon"; [docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking" (what upstream's detector is and how its tracker sizes frames); the existing YuNet wrapper in `tests-e2e/test_custom_faces.py` (it becomes `yunet.py`).
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

  (`ruff format .` would reflow the Python blocks in `plans/*.md`; format the code directories only.)
- **The bridge never touches the daemon's tracking** once Step 3 lands: `grep -rn "start_head_tracking\|stop_head_tracking\|get_tracked_face\|tracking/face" src tests tests-e2e examples` must find only the api's own verbs of that name, the control panel's calls to them, and nothing on `robot.`.
- **No network in `tests/`.** Nothing in the fast tier may construct `YuNetDetector` with upstream's model: its tests inject a stub in place of upstream's detector (Step 2), and every fast test that needs faces registers a stub detector through `faces.detector: "custom"`.
- **Write specs affirmatively**: the spec edits are done; this plan only flips statuses at the end. Docs (README, `docs/`) describe the current state, not the change.
- **Do not commit** unless asked.

## Scope

- `src/reachy_mini_bridge/config.py` — `FaceSettings.detector: str | None = None`, `detection: bool = False`, `FACE_DETECTORS = ("yunet", "custom")`; `MotionSettings.tracking: bool = False`; the cross-block `ConfigError` in `ReachyMiniConfig.from_dict`.
- `config.example.json` — `"faces": {"detector": "yunet", "detection": true}`, `"tracking": true` (unchanged value, now explicit in intent).
- `src/reachy_mini_bridge/yunet.py` — new: `YuNetDetector`, `DETECT_WIDTH`.
- `src/reachy_mini_bridge/face_detection.py` — the `daemon` source removed (`daemon_face_target`, `report_from_daemon`, `DAEMON_DETECT_WEIGHT`, `_DAEMON_FACE_PATH`, `_arm`, the `robot` argument); `FaceDetection` takes the detector name and resolves its factory (`yunet` → `YuNetDetector`, `custom` → the registered one), builds it on a worker thread at start; `FaceReport.source: str | None`; `Face.size: float`.
- `src/reachy_mini_bridge/api.py` — entry / verbs without a detector; the build failure at bring-up wrapped in `BridgeError`; the inactive report's `source`; `FaceDetection` constructed without the robot.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — `show_face`, `hide_face`, `client.face_target`, `_face_target`, `start_head_tracking`, `stop_head_tracking` removed.
- `src/reachy_mini_bridge/robot.py` — nothing to change (`fetch_daemon_json` stays for the gravity check); the parity test's member list in `tests/test_robot.py` loses the two tracking members and the face REST read.
- `src/reachy_mini_bridge/sim_daemon.py` — the `update_head_kinematics_model` override removed; `corrected_backend` → `bridge_backend`; module docstring and log lines.
- `src/reachy_mini_bridge/testing/fixtures.py` — `live_api`'s config names `yunet` with `detection` and `tracking` on.
- `src/reachy_mini_bridge/__init__.py` — unchanged exports (`YuNetDetector` is imported from its module; a config names it).
- `tests/test_config.py`, `tests/test_yunet.py` (new), `tests/test_face_detection.py`, `tests/test_api.py`, `tests/test_fake_reachy_mini.py`, `tests/test_robot.py`, `tests/test_sim_daemon.py`, `tests/test_control_panel.py`, `tests/test_head_tracking.py` (if it uses `show_face`) — as each step says.
- `tests-e2e/test_api.py` — the ε-weight test and the `get_tracked_face` reads removed; `tests-e2e/test_custom_faces.py` — registers `YuNetDetector` through the `custom` path, runs whenever `camera` + `faces` are probed (the env gate removed).
- `examples/control_panel/` — no code change expected (the panel reads `api.faces`); its tests build their config with a stub detector.
- `README.md`, `docs/custom-face-detector.md`, `docs/testing-with-the-bridge.md`, `docs/running-the-sim-daemon.md` — the current state (Step 9).
- `AGENTS.md` — project map: the new `yunet.py` row, the `face_detection.py` and `sim_daemon.py` rows; the e2e section's custom-detector bullet.
- `specs/vision/user_perception.md` — frontmatter gains `src/reachy_mini_bridge/yunet.py` and `tests/test_yunet.py` (Step 2; not before the files exist — `tests/test_project_map.py` checks every listed path exists); statuses (Step 10).
- `specs/_index.md`, `plans/_index.md`, this file — statuses.

## Steps

### Step 0 — Baseline

Check command green; the camera-feed plan `Done`.

### Step 1 — Config: the detector is opt-in

**Files:** `src/reachy_mini_bridge/config.py`, `config.example.json`, `tests/test_config.py`.

- `FaceSettings`: `detector: str | None = None`, `detection: bool = False`; `FACE_DETECTORS = ("yunet", "custom")`. `from_dict`: `detector` absent or `null` → `None`; a string not in `FACE_DETECTORS` → `ConfigError` listing the names; anything else (a number, a bool) → `ConfigError`.
- `MotionSettings.tracking: bool = False` (default only; validation unchanged).
- `ReachyMiniConfig.from_dict`: after both blocks are built, `faces.detector is None and (faces.detection or motion.tracking)` → `ConfigError` naming `faces.detector` and the switch(es) that are on, pointing at `"yunet"`.
- `config.example.json`: `"detector": "yunet"`.
- Tests: the defaults (`FaceSettings()` is `detector=None, detection=False`; `MotionSettings().tracking is False`; `ReachyMiniConfig()` is valid); `{"faces": {"detector": null}}` and an absent `detector` both give `None`; `"yunet"` / `"custom"` accepted; `"daemon"` rejected with the names in the message; `{"faces": {"detection": true}}` alone, and `{"motion": {"tracking": true}}` alone, each a `ConfigError` naming `faces.detector`; the same with `"detector": "yunet"` valid; the example file still round-trips.
- Check.

### Step 2 — The shipped detector

**Files:** `src/reachy_mini_bridge/yunet.py`, `tests/test_yunet.py`, `specs/vision/user_perception.md` (frontmatter), `AGENTS.md` (project map row).

- `YuNetDetector(upstream: Callable[[], Any] | None = None)`: the constructor calls `upstream()` when given, else imports `reachy_mini.vision.face_detector` and builds `FaceDetector()` — the import inside the constructor. `detect(frame_bgr, ts)`: `step = max(1, width // DETECT_WIDTH)`, a contiguous strided view `frame_bgr[::step, ::step]`, upstream's `detect` on it, every bbox / nose / eye scaled by `step` into `PixelFace`s (eyes as `(right, left)`). `DETECT_WIDTH = 320`.
- `tests/test_yunet.py`: with a stub upstream that records the frame it was given and returns fixed faces — a 1280×720 frame is detected at 320×180 (stride 4) and a 64×48 one whole (stride 1); the returned faces are scaled back by the stride, nose and eyes included; an upstream returning nothing gives `()`; the module imports without `onnxruntime` having been imported (`sys.modules` check after `import reachy_mini_bridge.yunet`). No model, no network.
- Frontmatter: add `src/reachy_mini_bridge/yunet.py` to `code:` and `tests/test_yunet.py` to `tests:` of `specs/vision/user_perception.md`. AGENTS.md project map: a `yunet.py` row (spec: user_perception.md).
- Check (`tests/test_project_map.py` now sees the new module named).

### Step 3 — The detection loop runs one detector, built at start

**Files:** `src/reachy_mini_bridge/face_detection.py`, `tests/test_face_detection.py`.

- Remove the `daemon` source and everything only it used (Scope). `FaceDetection.__init__(*, detector: str, faces, on_observation, feed, detector_factory)` — no `robot`. `start()`: `"custom"` with no factory → `ValueError` (message unchanged); `"yunet"` → the factory is `YuNetDetector`; any other name → `ValueError`. The detector is built **before** the task starts: `await asyncio.to_thread(factory)`; an exception propagates from `start()` unchanged (the api wraps it, Step 4) and leaves the loop not running. `restart(factory)` keeps its contract (the next poll builds the new detector — on the worker thread too, `asyncio.to_thread`).
- `FaceReport.source: str | None`; `FaceReport.inactive(source: str | None)`. `Face.size: float` (always known: the bbox height over the frame's).
- Selection, `report_from_pixels`, the debounce and the down rule are unchanged; the down rule's `WARNING` names the detector.
- Tests: delete the daemon-source tests (`report_from_daemon`, the arming commands); add — the factory runs on a thread other than the event loop's and before `running` turns true; a factory raising makes `start()` raise and `running` stay false, `faces` untouched; `"yunet"` resolves to `YuNetDetector` (assert the class, with the factory substituted so no model loads — a module-level seam `_YUNET_FACTORY` monkeypatched, or `YuNetDetector` patched on the module); the rest of the custom-path tests unchanged.
- Check.

### Step 4 — The api: switches need a detector; bring-up wraps a build failure

**Files:** `src/reachy_mini_bridge/api.py`, `tests/test_api.py`.

- Entry: with `faces.detector is None`, a wanted switch (`faces.detection` or `motion.tracking`, from a dataclass built in code) → `ValueError` before anything starts (mirrors the `custom`-without-factory check). With a detector: the loop is constructed without the robot and started as today; a `start()` that raises anything but `ValueError` is re-raised as `BridgeError("face detector 'yunet' could not be built: …")` chained to the cause, the stack unwinding what started.
- `start_head_tracking()` and `set_face_detection(True)` with no detector → `ValueError` naming `faces.detector`; `set_face_detection(False)` / `stop_head_tracking()` stay no-ops. `set_face_detector(factory)` is accepted whatever the detector (as today: it only takes effect for `custom`).
- The inactive report: `FaceReport.inactive(self._config.faces.detector)`.
- Tests — every test that drove faces through `show_face` / `hide_face` now uses the stub-detector scene already in `tests/test_api.py` (`_custom_config(scene, ...)`): the tracking sequence (`watching` → `engaged` → `watching` → `None`), the head turning to a face at `x = 0.5` and easing back, the emotion over tracking, the `changes()` subscriber tests, the detection / tracking switch matrix. New: `ReachyMiniApi("fake")` enters with `faces.value.active is False` and `source is None`, and `start_head_tracking()` / `set_face_detection(True)` raise `ValueError`; `ReachyMiniConfig(backend="fake", motion=MotionSettings(tracking=True))` fails entry with `ValueError`; a custom factory that raises at build fails entry with `BridgeError` and the fake's `__exit__` recorded (the stack unwound). Delete `EPS` / `DAEMON_DETECT_WEIGHT` and every assertion on `start_head_tracking` / `stop_head_tracking` commands. A report on the fake carries `head_pose` (the stub's frames are the fake's): assert one tracking test aims through the exact-pose path (`delay_s` untouched from its prior while the head converges).
- Check.

### Step 5 — The fake and the seam

**Files:** `src/reachy_mini_bridge/fake_reachy_mini.py`, `tests/test_fake_reachy_mini.py`, `tests/test_robot.py`, `tests/test_head_tracking.py`, `tests/test_control_panel.py`.

- Remove the fake's face target and its two tracking commands (Scope). The parity test's consumed-member list drops `start_head_tracking` / `stop_head_tracking`; the `fetch_daemon_json` test uses `/api/kinematics/info` as its path (the face path is gone).
- `tests/test_control_panel.py`: the panel tests that showed a face build their config with `FaceSettings(detector="custom", face_detector=<scene>.detector)` and script the scene; the face-meter test drives the scene at the fake's frame rate (the meter counts frames the stub saw, ~10/s), not a 20 Hz `show_face` loop.
- `tests/test_head_tracking.py`: if it uses `show_face`, the same substitution; the offline geometry tests are untouched.
- Check.

### Step 6 — The sim launcher

**Files:** `src/reachy_mini_bridge/sim_daemon.py`, `tests/test_sim_daemon.py`.

- Remove the `update_head_kinematics_model` override and the tracking-step mentions from the docstrings and log lines; rename `corrected_backend` → `bridge_backend` (`__all__`, the call in `run_sim_daemon`, tests). The camera-source wiring, the overlay and the extension hooks are unchanged.
- Tests: delete the stepping tests (`backend_with_a_queued_face` and its two asserts); the hooks test stays; a new assert that the subclass defines no `update_head_kinematics_model` / `step_head_tracking` of its own (`"update_head_kinematics_model" not in bridge_backend(...).__dict__`), pinning that the launcher leaves the loop alone.
- Check.

### Step 7 — The live tier

**Files:** `src/reachy_mini_bridge/testing/fixtures.py`, `tests-e2e/test_api.py`, `tests-e2e/test_custom_faces.py`, `tests/test_testing_support.py` (if it inspects the fixture's config).

- `live_api`'s config: `faces=FaceSettings(detector="yunet", detection=True)`, `motion=MotionSettings(tracking=True)`. The model downloads into the Hugging Face cache on the first live run (as the emotions library does); note it in the fixture's docstring.
- `tests-e2e/test_api.py`: delete the test pinning the daemon detecting at `DAEMON_DETECT_WEIGHT`; drop the `robot.get_tracked_face` read from the tracking test's `track` record (the daemon's tracker is never armed; nothing to read). The gaze tests print `api._tracker.delay_s` (or the property the tracker exposes) at settle, for Step 10.
- `tests-e2e/test_custom_faces.py`: `from reachy_mini_bridge.yunet import YuNetDetector` replaces the local wrapper; the fixture no longer reads `REACHY_MINI_E2E_FACE_DETECTOR` (it skips on `camera` / `faces` capabilities alone); the `get_tracked_face` read and the `"daemon-tracked"` branch go — the test asserts `report.source == "custom"`, the convergence, the rate, `attention == "engaged"`.
- Run on the viewer sim: `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` — the attention / gaze tests converge with the shipped detector on the rendered camera; the custom test converges through the same class registered as custom. Read the skips.
- Check.

### Step 8 — Control panel and example config

**Files:** `examples/control_panel/` (verify only), `config.example.json` (done in Step 1), `README.md` config listing.

- `uv run python -m examples.control_panel --config config.example.json`: the panel comes up with the webcam sim, the face markers and the face meter live on `yunet`. No code change expected; if the panel's `snapshot()` reads `faces.value.source`, it shows `yunet`.
- Check.

### Step 9 — Docs and the project map

**Files:** `README.md`, `docs/custom-face-detector.md`, `docs/testing-with-the-bridge.md`, `docs/running-the-sim-daemon.md`, `AGENTS.md`.

- README: the comparison table rows on following a face and the simulator (the bridge detects on the host with upstream's model; the launcher adds the webcam and the overlay); the Faces api row (detection off by default; `faces.detector` names `yunet` / `custom`); "Your own face detector" (the shipped detector is the worked example; a config with no detector detects nothing); the sim launcher list (the "Face tracking works" bullet becomes the camera-stream sentence); the config table's `detector` and `tracking` rows and defaults; the e2e command list (the custom test's env var gone).
- `docs/custom-face-detector.md`: the shipped `YuNetDetector` shown as the reference wrapper (link to the module), the config path (`"detector": "yunet"`) beside the code path (`custom`).
- `docs/testing-with-the-bridge.md`: the `REACHY_MINI_E2E_FACE_DETECTOR` row removed; the `faces` capability row's wording (the bridge's detector on the rendered camera); the wireless note (detection runs on the host, over the WebRTC stream).
- `docs/running-the-sim-daemon.md`: the launcher's additions without the tracking correction.
- AGENTS.md: the project map rows for `face_detection.py` (one detector over the camera feed: `yunet` or a developer's; no daemon source), `sim_daemon.py` (no face-detection correction), `yunet.py` (Step 2); the e2e section's custom-detector bullet (runs with the viewer, no env var; the model download).
- Check.

### Step 10 — Measure, then flip statuses

- On the viewer sim with the gaze tests' printout from Step 7: the delay estimate the tracker converges to against the frame's arrival-time `ts` (expected below the 0.3 s measured against the daemon's report — no detector smoothing, no daemon-side completion time). Set `DELAY_PRIOR_S` to the measured value if it differs by more than 0.1 s and record the number in [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) open question 3; the shipped detector's observation rate on the rendered camera (expected the feed's 10/s) in [specs/vision/user_perception.md](../specs/vision/user_perception.md) open question 3.
- Flip `Updated` → `Implemented` in `specs/vision/user_perception.md`, `head_tracking.md`, `config.md`, `robot.md`, `api.md`, `sim_daemon.md`, `testing_support.md` and their `_index.md` rows; this plan → `Done` here and in `plans/_index.md`.

## Verification

- `uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest` green after every step; the fast tier makes no network access (run it once with `HF_HUB_OFFLINE=1` to prove nothing constructs the model).
- `tests/test_project_map.py` green with `yunet.py` in the map and the frontmatter.
- `grep -rn "start_head_tracking\|stop_head_tracking\|get_tracked_face\|tracking/face\|DAEMON_DETECT\|show_face\|hide_face\|corrected_backend" src tests tests-e2e examples docs README.md AGENTS.md` finds only the api's verbs and the panel's calls to them.
- Live: `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` — the attention / gaze tests pass with the shipped detector and the custom test passes without an env var; `uv run pytest tests-e2e -rs` headless — the tracking tests skip on `camera`, everything else as before; `REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e -rs` on a Lite if one is at hand (the detector on the robot's 1920×1080 frames at stride 6).
- Manual: the control panel on `config.example.json` follows the person in front of the webcam, face markers and meter live.
- Mark this plan `Done` (here and in [_index.md](_index.md)) only once all of the above pass and Step 10's statuses are flipped.
