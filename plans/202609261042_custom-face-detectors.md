# Custom face detectors — the `custom` detection source

**Status:** Todo

Implements [specs/user_perception.md](../specs/user_perception.md) "Custom detectors — the `FaceDetector` protocol" and the `custom` row of "Detection sources", with the `face_detector` field of [specs/config.md](../specs/config.md) and the `set_face_detector` verb of [specs/api.md](../specs/api.md). Delivers the frame runner that pulls camera frames and hands them to a developer's detector, the bridge's own selection and smoothing of the faces it gets back, the `PixelFace` / `FaceDetector` contract with its registration checks, and the documentation showing upstream's YuNet detector plugged in as a custom one. Deliberately ships **no detector**: the bridge carries no vision code ([specs/user_perception.md](../specs/user_perception.md) open question 1).

Depends on [202609261041](202609261041_bridge-head-tracker-and-gaze-layer.md) (the tracker consumes what the runner produces).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/user_perception.md](../specs/user_perception.md) "The pipeline", "Detection sources", "Custom detectors"; [specs/motion.md](../specs/motion.md) "Custom idle moves" (the registration pattern to mirror); [specs/config.md](../specs/config.md) "`faces` block".
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

- **Selection and smoothing are geometry, written here, not imported from upstream's private tracker classes** (`Tracker`, `_AdaptiveCenterFilter` are implementation details of `reachy_mini.vision.face_tracking` and may move). Their constants are copied so the `custom` source behaves like the daemon's: acquire the largest face above 0.3 % of the frame area, associate the nearest within a jump of 0.5 (normalised), drop after 20 misses; smooth the centre with alpha 0.3 (0.6 when the input moved more than 0.15), dead zone 0.02.
- **Do not commit** unless asked.

## Scope

- `src/reachy_mini_bridge/face_detection.py` — `PixelFace`, `FaceDetector` (the placeholder protocol filled in), `FaceDetectorFactory`, `check_face_detector_factory`, `_FaceSelector`, `_CenterSmoother`, the frame-runner source in `FaceDetection`, `report_from_pixels`.
- `tests/test_face_detection.py` — runner, selection, smoothing, registration checks, restart on re-registration.
- `src/reachy_mini_bridge/config.py` — `FaceSettings.face_detector` (already declared by the first plan; the entry-time check lands here).
- `src/reachy_mini_bridge/api.py` — `set_face_detector` / `face_detector`; `custom` accepted at entry with a factory, `ValueError` without.
- `tests/test_api.py` — custom source on the fake with a stub detector.
- `src/reachy_mini_bridge/__init__.py` — export `PixelFace`, `FaceDetector`.
- `docs/testing-with-the-bridge.md` or a new `docs/custom-face-detector.md` — the YuNet wrapper example; `README.md` — one line pointing at it.
- `tests-e2e/test_custom_faces.py` — new: an opt-in convergence test with the YuNet wrapper as a custom detector on the viewer sim, in a module of its own with its own api session (Step 5).
- `AGENTS.md` — the `face_detection.py` row's placeholder note removed.
- `specs/user_perception.md`, `specs/_index.md` — `Stable` → `Implemented`; `specs/_face_tracking_analysis.md` deleted.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Check command green; the two previous plans `Done`.

### Step 1 — The contract and its check

**File:** `src/reachy_mini_bridge/face_detection.py`.

- `PixelFace` and `FaceDetector` exactly as the spec; `FaceDetectorFactory = Callable[[], FaceDetector]`.
- `check_face_detector_factory(factory) -> None`: `ValueError` when not callable, when the call raises (chained), or when the result has no callable `detect`. Mirrors `check_idle_move_factory`.
- `report_from_pixels(faces: Sequence[PixelFace], size: tuple[int, int], ts: float, target_index: int | None) -> FaceReport`: normalise every face (nose, else bbox centre) into `[-1, 1]`, roll from the eyes when given, `size = bbox_h / frame_h`; the target face first.

**Tests:** the check accepts a class and a lambda returning a valid object, rejects a non-callable, a raising factory and a result without `detect`, each with a message naming the problem; normalisation puts a nose at the frame centre at `(0, 0)` and at the bottom-right corner at `(1, 1)`.

### Step 2 — Selection and smoothing

`_FaceSelector(min_area_frac=0.003, max_jump=0.5, max_misses=20)` with `select(faces, size) -> int | None` (the index of the target), and `_CenterSmoother` with `update(center) -> center` / `reset()`, both pure. **Tests:** the largest face is acquired, the nearest kept over a larger newcomer, an association dropped after the misses, a jump beyond the gate treated as a miss; the smoother's dead zone and the fast alpha on a large movement.

### Step 3 — The frame runner in the detection loop

`FaceDetection` accepts `source="custom"` with a `detector_factory`: at `start()` it calls the factory once (on the event loop thread — the factory was already checked) and, per poll, runs `frame = robot.media.get_frame()` and `faces = detector.detect(frame, ts)` together under one `asyncio.to_thread` (one worker call per poll; a poll still running when the next is due is skipped, never queued); `ts = time.monotonic()` taken before the frame read. `None` frame → skipped poll: upstream's `get_frame()` returns each frame once and `None` until the next, so at `FACE_POLL_HZ = 30` on the daemon's 10 fps feed two polls in three are skipped and the detector runs once per new frame (the fake always has a frame, so there it runs at the poll rate). The pixel faces go through the selector and the smoother, then `report_from_pixels`; the debounce and `active` rules are the ones the loop already has. `restart(detector_factory)` swaps the detector between polls (used by `set_face_detector`). The daemon's tracking is not armed in this mode.

**Tests** (fake robot, a stub detector returning scripted `PixelFace` lists per call): the report shows the stub's faces normalised against the fake's 64×48 frame; the target is the largest; a detector raising on every call trips `active=False` after the source-down window; a slow detector (sleeping past the poll period) causes skipped polls, not a growing backlog (assert the call count over 1 s is bounded); no `start_head_tracking` command is recorded in `custom` mode.

### Step 4 — The api and config

- `FaceSettings.face_detector` honoured: `__aenter__` with `faces.detector == "custom"` runs `check_face_detector_factory` (a bad or missing one fails bring-up with `ValueError`, nothing entered).
- `set_face_detector(factory | None)` / `face_detector`: check, store, and if the loop runs in `custom` mode, `restart` it; outside a session the property reads the config's value; exit resets it.
- Exports in `__init__.py`.

**Tests** (`tests/test_api.py`): a `custom` config with a stub factory enters and `api.faces` reports the stub's faces; without a factory entry raises `ValueError`; `set_face_detector` swaps detectors mid-session (the report follows the new stub within two polls); a bad factory raises and leaves the old one; with tracking on and the stub reporting a face at `x = 0.5`, the fake's recorded head yaw turns toward it (the whole pipeline on the fake).

### Step 5 — Documentation and the live check

- Write the YuNet example (in `docs/custom-face-detector.md`, linked from the README's simulator / faces sections):

  ```python
  from reachy_mini.vision.face_detector import FaceDetector as YuNet
  from reachy_mini_bridge import PixelFace

  class YuNetDetector:
      def __init__(self) -> None:
          self._yunet = YuNet()  # downloads the model into the Hugging Face cache on first use
      def detect(self, frame_bgr, ts):
          return [PixelFace(bbox=f.bbox, nose=f.nose, eyes=(f.right_eye, f.left_eye))
                  for f in self._yunet.detect(frame_bgr)]

  config = ReachyMiniConfig.from_json_file("robot.json")   # "faces": {"detector": "custom"}
  config.faces.face_detector = YuNetDetector
  ```

  State plainly that the bridge ships no detector and why, and that `reachy_mini`'s base dependencies suffice for this one.
- `tests-e2e/test_custom_faces.py` (new module): `test_custom_detector_converges_on_the_face`, gated on `camera` + `faces` and on `REACHY_MINI_E2E_FACE_DETECTOR=yunet` (`require_env`). The detection source is config-only, so the shared `live_api` session cannot switch to it, and a second `ReachyMiniApi` on the same daemon would be a second motion loop writing `set_target` against the first's breathing. So the module uses **its own session**: a module-scoped `live_api_custom_faces` fixture in the e2e conftest, built like `live_api` (same target resolution, same own-it-or-borrow-it daemon, same capability probe, `sim_scene` client) but with `faces.detector="custom"` and the YuNet wrapper as `face_detector`, entered only for this module — the `live_api` module before it has exited and released its daemon by then, so exactly one api drives the head. Assert the same convergence as the daemon-source test. Document the variable in [specs/testing.md](../specs/testing.md) "E2E targets & capabilities" and in [AGENTS.md](../AGENTS.md) "Running the live e2e tests".
- `AGENTS.md`: drop the placeholder note from the `face_detection.py` row.
- Statuses: `specs/user_perception.md` and its `_index.md` row → `Implemented`; delete `specs/_face_tracking_analysis.md`; this plan `Done` here and in `_index.md`.

## Verification

- Check command green.
- `REACHY_MINI_E2E_SIM_VIEWER=1 REACHY_MINI_E2E_FACE_DETECTOR=yunet uv run pytest tests-e2e -rs -k custom`: the custom-detector convergence test passes on the viewer sim.
- Manual: the viewer sim with the webcam and `"faces": {"detector": "custom"}` plus the YuNet wrapper set from code follows you as the `daemon` source does; `api.faces.value.faces` lists two faces when two people stand in front of the camera.
