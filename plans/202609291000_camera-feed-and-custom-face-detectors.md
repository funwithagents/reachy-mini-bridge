# Camera feed and custom face detectors — `api.camera` and the `custom` detection source

**Status:** Done

Implements [specs/vision/camera.md](../specs/vision/camera.md) in full — the `CameraFeed` / `CameraFrame` single reader of the robot's camera, `api.camera`, the retirement of `get_camera_frame()`, the capture-time rule — and, over it, [specs/vision/user_perception.md](../specs/vision/user_perception.md) "Custom detectors — the `FaceDetector` protocol" and the `custom` row of "Detection sources", with the `face_detector` field of [specs/core/config.md](../specs/core/config.md) and the `set_face_detector` verb of [specs/core/bridge.md](../specs/core/bridge.md). Delivers the feed, the runner that samples it and hands frames to a developer's detector, the bridge's own selection of the faces it gets back, the `PixelFace` / `FaceDetector` contract with its registration checks, and the documentation showing upstream's YuNet detector plugged in as a custom one. Deliberately ships **no detector**: the bridge carries no vision code ([specs/vision/user_perception.md](../specs/vision/user_perception.md) "Named detectors" is where shipped detectors arrive, each behind an extra, in a later plan).

Depends on [202609261041](202609261041_bridge-head-tracker-and-gaze-layer.md) (the tracker consumes what the runner produces). Replaces the earlier `202609261042_custom-face-detectors` plan, which read frames through `get_frame()` per poll and so competed with every other reader of the camera.

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/vision/camera.md](../specs/vision/camera.md) in full; [specs/vision/user_perception.md](../specs/vision/user_perception.md) "The pipeline", "Detection sources", "Custom detectors", "Named detectors"; [specs/motion/motion.md](../specs/motion/motion.md) "Custom idle moves" (the registration pattern to mirror) and "A history of head poses"; [specs/core/config.md](../specs/core/config.md) "`faces` block"; [specs/core/bridge.md](../specs/core/bridge.md) "Lifecycle".
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

- **The feed is the only caller of `media.get_frame`** in the bridge once Step 1 lands; `grep` for it at the end of every step.
- **Selection is geometry, written here, not imported from upstream's private tracker classes** (`Tracker`, `_AdaptiveCenterFilter` are implementation details of `reachy_mini.vision.face_tracking` and may move). Their constants are copied so the `custom` source behaves like the daemon's: acquire the largest face above 0.3 % of the frame area, associate the nearest within a jump of 0.5 (normalised), drop after 20 misses.
- **Do not commit** unless asked.

## Scope

- `src/reachy_mini_bridge/camera.py` — `CameraFrame`, `CameraFeed`, `CAMERA_RETRY_S`, `CAMERA_DOWN_S` (the placeholder module filled in).
- `tests/test_camera.py` — new: the feed on a stub reader, the structural-shape test.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — the paced `get_frame` (`FAKE_FRAME_HZ`).
- `src/reachy_mini_bridge/api.py` — `camera`; the `MotionSession` constructed before the feed; the feed in the lifecycle; `get_camera_frame` removed; `set_face_detector` / `face_detector`; `custom` accepted at entry with a factory, `ValueError` without.
- `src/reachy_mini_bridge/face_detection.py` — `PixelFace`, `FaceDetector` (the placeholder protocol filled in), `FaceDetectorFactory`, `check_face_detector_factory`, `_FaceSelector`, the feed-sampling runner in `FaceDetection`, `report_from_pixels`.
- `tests/test_face_detection.py` — runner, selection, registration checks, restart on re-registration.
- `tests/test_api.py` — the camera tests moved onto `api.camera`; custom source on the fake with a stub detector.
- `src/reachy_mini_bridge/config.py` — `FaceSettings.face_detector` (already declared by the first plan; the entry-time check lands here).
- `src/reachy_mini_bridge/__init__.py` — export `CameraFrame`, `PixelFace`, `FaceDetector`.
- `examples/control_panel/controller.py` — `camera_frame_rgb()` reads the feed; `specs/examples/control_panel.md` → `Implemented`.
- `src/reachy_mini_bridge/testing/fixtures.py` — untouched: the `camera` probe reads `robot.media.get_frame` before any session exists.
- `tests-e2e/test_api.py` — `test_camera_frame_delivers_a_frame` reads the feed; `tests-e2e/test_custom_faces.py` — new: an opt-in convergence test with the YuNet wrapper as a custom detector on the viewer sim, in a module of its own with its own api session (Step 7).
- `README.md`, `docs/testing-with-the-bridge.md`, a new `docs/custom-face-detector.md` — `api.camera` replaces `get_camera_frame()` everywhere; the YuNet wrapper example; a vision graph over the feed shown in one snippet.
- `AGENTS.md` — the `camera.py` and `face_detection.py` rows' placeholder notes removed.
- `specs/vision/camera.md` — open question 1 answered with the measurement (Step 2); `Stable` → `Implemented`. `specs/vision/user_perception.md`, `specs/_index.md` — `Stable` → `Implemented`; `specs/_face_tracking_analysis.md` deleted. `specs/_framelike_analysis.md`, `specs/_vision_modules_dependency_split_analysis.md`, `specs/_frame_pose_analysis.md` were written for the vision-modules repo: delete them here once they have been carried over.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Check command green; the two previous plans `Done`.

### Step 1 — The camera feed

**Files:** `src/reachy_mini_bridge/camera.py`, `src/reachy_mini_bridge/fake_reachy_mini.py`, `src/reachy_mini_bridge/api.py`, `src/reachy_mini_bridge/__init__.py`, `examples/control_panel/controller.py`, `tests/test_camera.py`, `tests/test_api.py`, `tests-e2e/test_api.py`, `tests/test_control_panel*.py`.

- `CameraFrame` and `CameraFeed(read_frame, pose_at)` exactly as the spec: the reader thread (daemon thread, named), the latest-frame slot under a lock, `frame_id` from 1 continuing across `start()` / `stop()`, `published_count`, the retry and down-warning rules, `latest()` reset to `None` at `stop()`.
- The fake's `get_frame` paced at `FAKE_FRAME_HZ = 10`: the first call immediate, each later call blocking until the next frame period has elapsed since the previous one — never `None`. The fake↔upstream parity test keeps passing (same signature).
- `api.py`: `camera` is a `CameraFeed` built in the constructor with `read_frame` bound at session entry (the robot exists only then — bind through a small indirection, or build the feed at entry and keep a stable `api.camera` object by giving the feed a `bind(read_frame, pose_at)` called at entry; pick the one that keeps `latest()` `None` outside the session and the object identity constant). The `MotionSession` object is constructed right after the robot is entered (construction starts no thread — confirm in `motion.py`), the feed started right after the media session with `pose_at=motion.head_pose_at`, stopped right before the media session is torn down; bring-up cancel / failure unwinds it like every other step. `get_camera_frame()` deleted; `CameraFrame` exported.
- `examples/control_panel/controller.py`: `camera_frame_rgb()` reads `api.camera.latest()` and flips a **copy** to RGB (the feed's image is shared and read-only).
- `tests-e2e/test_api.py`: the camera test waits for `api.camera.latest()` (a short bounded loop — the feed's first frame follows session entry within a frame period) and asserts a BGR `HxWx3` `uint8` image; the `camera` gate unchanged.

**Tests** (`tests/test_camera.py`, on a stub `read_frame` returning scripted arrays / `None` / raising, and a stub `pose_at`): `frame_id` and `published_count` advance once per frame returned and not on `None`; `latest()` `None` before `start()`, the last frame while the reader waits on `None`, `None` after `stop()`; a raising reader keeps the last frame and recovers; `head_pose` is `pose_at(ts)` of that frame and `None` with `pose_at=None`; two consumers sampling the feed both see every `frame_id` (the single-reader property). The **shape test**: a `Protocol` written in the test with read-only `frame_id` / `ts` / `image` properties and an `Upstream` protocol with `latest()`; assignments `frame_like: FrameLike = CameraFrame(...)` and `upstream: Upstream[FrameLike] = feed` type-check under pyright, and a `@runtime_checkable` `isinstance` passes. `tests/test_api.py`: the existing camera test moves onto `api.camera` (on the fake: a frame within one frame period of entry, `None` after exit); a frame published while the fake's head is at pose P carries P.

### Step 2 — The capture time

**Files:** `src/reachy_mini_bridge/camera.py`, `specs/vision/camera.md`, `docs/reachy-mini-api.md`.

Settle [specs/vision/camera.md](../specs/vision/camera.md) open question 1 by measurement before the runner is written:

- On the viewer sim (rendered camera and webcam) and, when at hand, a robot: pull samples from `robot.media.camera._appsink_video` and compare the buffer `pts` (mapped through the pipeline's clock and base time to `time.monotonic()`) with the arrival time `get_frame()` would stamp. Record the offset and its spread, and whether the value moves with the camera's timing (capture) or the relay's (restamp).
- If the `pts` is usable: the feed's reader pulls the sample itself through that pinned internal (guarded: on a backend without the attribute — the fake, WebRTC — it falls back to `get_frame()` and arrival time), stamps `ts` from it and attaches `head_pose`. If not: the feed keeps `get_frame()`, arrival time, `head_pose=None`, and the tracker's delay estimate stays in charge for `custom` reports too.
- Write the finding into `specs/vision/camera.md` (the open question closed, the "Capture time" bullet stating the path taken) and `docs/reachy-mini-api.md` "Perception"; draft the upstream ask (`get_frame_with_timestamp`) in `docs/` if the internal is the only way.

**Tests:** the mapping from `pts` to monotonic is a pure function, tested with fixed numbers; the fallback path is the fake's.

### Step 3 — The detector contract and its check

**File:** `src/reachy_mini_bridge/face_detection.py`.

- `PixelFace` and `FaceDetector` exactly as the spec; `FaceDetectorFactory = Callable[[], FaceDetector]`. Keep the box convention as the spec states it (`bbox` = `x, y, w, h` in pixels of the frame given); a vision-modules `faces` family aligns on it from its side.
- `check_face_detector_factory(factory) -> None`: `ValueError` when not callable, when the call raises (chained), or when the result has no callable `detect`. Mirrors `check_idle_move_factory`.
- `report_from_pixels(faces: Sequence[PixelFace], size: tuple[int, int], frame: CameraFrame, target_index: int | None) -> FaceReport`: normalise every face (nose, else bbox centre) into `[-1, 1]`, roll from the eyes when given, `size = bbox_h / frame_h`; the target face first; `ts=frame.ts`, `head_pose=frame.head_pose`.

**Tests:** the check accepts a class and a lambda returning a valid object, rejects a non-callable, a raising factory and a result without `detect`, each with a message naming the problem; normalisation puts a nose at the frame centre at `(0, 0)` and at the bottom-right corner at `(1, 1)`; the report carries the frame's `ts` and `head_pose`.

### Step 4 — Selection (no smoothing)

`_FaceSelector(min_area_frac=0.003, max_jump=0.5, max_misses=20)` with `select(faces, size) -> int | None` (the index of the target), pure. **No centre smoother:** smoothing across frames mixes pixels taken from different head poses, and the tracker smooths the aim in the world frame instead ([specs/vision/user_perception.md](../specs/vision/user_perception.md) "The pipeline"; [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) "The aim"). **Tests:** the largest face is acquired, the nearest kept over a larger newcomer, an association dropped after the misses, a jump beyond the gate treated as a miss.

### Step 5 — The runner over the feed

**File:** `src/reachy_mini_bridge/face_detection.py`.

`FaceDetection` accepts `source="custom"` with a `detector_factory` and the feed: at `start()` it calls the factory once (on the event loop thread — the factory was already checked) and, per poll at `FACE_POLL_HZ = 30`, reads `frame = feed.latest()`; a `None`, or a `frame_id` equal to the last one processed, is a skipped poll (two polls in three on the daemon's 10 fps feed; on the fake, the same); otherwise `faces = detector.detect(frame.image, frame.ts)` runs under `asyncio.to_thread` (one worker call per poll; a poll still running when the next is due is skipped, never queued), the pixel faces go through the selector, then `report_from_pixels(..., frame, ...)` — the report's `ts` and `head_pose` are the frame's, so the tracker aims each face against the pose its frame was taken from when the feed knows it, and estimates the delay otherwise. The debounce and `active` rules are the ones the loop already has. `restart(detector_factory)` swaps the detector between polls (used by `set_face_detector`). The daemon's tracking is not armed in this mode. `media.get_frame` is not called here — `grep` it.

**Tests** (fake robot, a stub detector returning scripted `PixelFace` lists per call): the report carries its frame's `ts` and `head_pose` and the tracker aims against that pose; the report shows the stub's faces normalised against the fake's 64×48 frame; the target is the largest; the detector runs once per feed frame, not once per poll (call count over 1 s ≈ `FAKE_FRAME_HZ`, not `FACE_POLL_HZ`); a detector raising on every call trips `active=False` after the source-down window; a slow detector (sleeping past the frame period) causes skipped frames, not a growing backlog; no `start_head_tracking` command is recorded in `custom` mode; the control panel's display sampling the feed at the same time loses the detector no frames (the single-reader property, end to end).

### Step 6 — The api and config

- `FaceSettings.face_detector` honoured: `__aenter__` with `faces.detector == "custom"` runs `check_face_detector_factory` (a bad or missing one fails bring-up with `ValueError`, nothing entered).
- `set_face_detector(factory | None)` / `face_detector`: check, store, and if the loop runs in `custom` mode, `restart` it; outside a session the property reads the config's value; exit resets it.
- Exports in `__init__.py`.

**Tests** (`tests/test_api.py`): a `custom` config with a stub factory enters and `api.faces` reports the stub's faces; without a factory entry raises `ValueError`; `set_face_detector` swaps detectors mid-session (the report follows the new stub within two frames); a bad factory raises and leaves the old one; with tracking on and the stub reporting a face at `x = 0.5`, the fake's recorded head yaw turns toward it (the whole pipeline on the fake).

### Step 7 — Documentation and the live check

- `README.md`: `api.camera.latest()` replaces `get_camera_frame()` in the quickstart, the verb table (a property: the newest `CameraFrame`, `None` when no frame is available), the simulator section; one snippet shows a vision graph plugged onto the feed (`HandStage(api.camera, ...)`, provider-neutral wording: any latest-value graph with that shape); the `faces` table's `custom` row reads as available.
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

  State plainly that the bridge ships no detector and why, that `reachy_mini`'s base dependencies suffice for this one, and that the same detector object runs in a vision graph over `api.camera`.
- `tests-e2e/test_custom_faces.py` (new module): `test_custom_detector_converges_on_the_face`, gated on `camera` + `faces` and on `REACHY_MINI_E2E_FACE_DETECTOR=yunet` (`require_env`). The detection source is config-only, so the shared `live_api` session cannot switch to it, and a second `ReachyMiniApi` on the same daemon would be a second motion loop writing `set_target` against the first's breathing. So the module uses **its own session**: a module-scoped `live_api_custom_faces` fixture in the test module itself (nothing else uses it, so it stays out of the shared conftest), built like `live_api` (same target resolution, same own-it-or-borrow-it daemon, same capability probe, `sim_scene` client) but with `faces.detector="custom"` and the YuNet wrapper as `face_detector`, entered only for this module — the `live_api` module before it has exited and released its daemon by then, so exactly one api drives the head. Assert the same convergence as the daemon-source test. Document the variable in [specs/testing/testing.md](../specs/testing/testing.md) "E2E targets & capabilities" and in [AGENTS.md](../AGENTS.md) "Running the live e2e tests".
- `AGENTS.md`: drop the placeholder notes from the `camera.py` and `face_detection.py` rows.
- Statuses: `specs/vision/camera.md`, `specs/vision/user_perception.md`, `specs/examples/control_panel.md` and their `_index.md` rows → `Implemented`; delete `specs/_face_tracking_analysis.md` (and the three vision-modules analysis docs once carried over); this plan `Done` here and in `_index.md`.

## Verification

- Check command green; `grep -rn "get_frame\|get_camera_frame" src examples tests tests-e2e README.md docs` shows `media.get_frame` in `camera.py`, the fake and the e2e probe only, and `get_camera_frame` nowhere.
- `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -k camera`: the live camera test passes on the viewer sim.
- `REACHY_MINI_E2E_SIM_VIEWER=1 REACHY_MINI_E2E_FACE_DETECTOR=yunet uv run pytest tests-e2e -rs -k custom`: the custom-detector convergence test passes on the viewer sim.
- Manual: the control panel on the viewer sim with the webcam, `"faces": {"detector": "custom"}` and the YuNet wrapper set from code — the camera image refreshes at 5 Hz **and** the face update rate in the panel stays at the feed's rate (about 10/s), the head follows you as the `daemon` source does; `api.faces.value.faces` lists two faces when two people stand in front of the camera.
