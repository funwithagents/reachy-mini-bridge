# The bridge's head tracker and the motion loop's gaze layer

**Status:** Done

Implements [specs/head_tracking.md](../specs/head_tracking.md) in full and [specs/motion.md](../specs/motion.md) "The gaze layer" / "A history of commanded head poses", with the api changes in [specs/api.md](../specs/api.md) "Attention / gaze (autonomous)" (tracking as a mode, `attention` derived, no attention loop, `play_emotion` no longer touching the daemon weight) and the retirement of the sim launcher's aim-side corrections in [specs/sim_daemon.md](../specs/sim_daemon.md). Delivers a tracker that turns the detection loop's target face into a look-at aim — true intrinsics, the head pose at the frame's time, a fixed camera for a webcam — and a gaze layer in `MotionSession` that composes that aim into the idle move, fades it in and out and leaves it out of primaries; the daemon's tracking becomes a detector armed at a negligible weight. Deliberately leaves out the `custom` detection source ([202609261042](202609261042_custom-face-detectors.md)).

Depends on [202609261040](202609261040_face-detection-loop-and-observable.md) (the detection loop and `api.faces`).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/head_tracking.md](../specs/head_tracking.md) in full; [specs/user_perception.md](../specs/user_perception.md) "Detection sources"; [specs/motion.md](../specs/motion.md) "The gaze layer", "The loop", "Emotions through the loop", "Interactions between the layers"; [specs/api.md](../specs/api.md) "Attention / gaze (autonomous)" and "Motors"; [specs/sim_daemon.md](../specs/sim_daemon.md) in full; [specs/sim_scene.md](../specs/sim_scene.md) "Head tracking converges on the face" for the numbers the live tests assert.
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

- **The geometry is upstream's, imported, not rewritten:** `reachy_mini.vision.look_at.look_at_image_pose`, `default_head_to_camera_transform`, `reachy_mini.utils.interpolation.linear_pose_interpolation`, `compose_world_offset`, `reachy_mini.reachy_mini.INIT_HEAD_POSE`. Check each name exists in the installed `reachy_mini` before use (`uv run python -c "from reachy_mini.vision.look_at import look_at_image_pose"`).
- **Tests are functional:** the fast tier asserts on the fake's recorded `targets` (head yaw / pitch read back from the 4×4 with `scipy.spatial.transform.Rotation`) and on the api's properties. The live tier's three attention / gaze tests are the acceptance suite; their assertions on where the head settles do not change, only their timing constants.
- **Do not commit** unless asked.

## Facts you must not violate

1. Only the motion loop calls `set_target`. The tracker never touches the robot; it hands the loop an aim and reads the loop's pose history.
2. The daemon's `ts` is its `time.monotonic()`; comparable with the bridge's only on the same host. The bridge knows the host: `robot.client.host` loopback → same host.
3. The daemon runs its detector only while tracking is armed above zero (previous plan, fact 1). After this plan the bridge **always** arms `DAEMON_DETECT_WEIGHT` in `daemon` mode and never any other weight.
4. An `IdleMove` has two motions: `offsets(t)` from neutral, and `gaze_offsets(t)` from the aim, neutral by default ([specs/motion.md](../specs/motion.md) "The moves"). The loop never scales a move itself; `BreathingMove` scales its own rotations in its `gaze_offsets`.
5. The sim's eye camera is `fovy` 80° vertical at 1280×720 (~112° horizontal); upstream's `MujocoCameraSpecs.K` is wrong for it. A robot's calibrated `K` / `D` is on `robot.media.camera.camera_specs`, at that camera's calibrated resolution — normalise the face into *that* frame's pixels before `look_at_image_pose`.

## Scope

- `src/reachy_mini_bridge/motion.py` — pose history, `head_pose_at`, `set_gaze`, the gaze layer in `_tick`, `IdleMove.gaze_offsets`, `IdleOffsets.scaled`, `BreathingMove.gaze_offsets`, the registration check and the failure path extended to `gaze_offsets`; `reanchor` removed; constants `GAZE_HISTORY_S`, `GAZE_ALPHA`, `BREATHING_GAZE_ROTATION_SCALE`.
- `tests/test_motion.py` — gaze layer tests; re-anchor tests removed.
- `src/reachy_mini_bridge/head_tracking.py` — replaces the placeholder: `pinhole_intrinsics`, `sim_hfov_deg` (moved from `sim_daemon.py`), `SIM_EYE_CAMERA_FOVY_DEG`, `CameraModel`, `HeadTracker`, `TRACKING_LOST_S`, `GAZE_LATENCY_S`.
- `tests/test_head_tracking.py` — new: tracker tests (offline convergence, fixed camera, latency, loss).
- `src/reachy_mini_bridge/api.py` — tracking verbs re-based, `attention` derived, the attention loop and its constants deleted, `play_emotion` simplified, `daemon` arming always ε.
- `tests/test_api.py` — attention tests rewritten; tracking-without-motors test.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — `media.camera.camera_specs` stand-in (K, D); `get_tracked_face` removed.
- `tests/test_robot.py`, `tests/test_fake_reachy_mini.py` — parity list and fake tests updated.
- `src/reachy_mini_bridge/sim_daemon.py` — corrections 2 and 3 removed (`install_tracker_intrinsics`, `_TrackerCamera`, `_tracker_intrinsics`, the `set_tracking_face` override, the relay's hfov reporting reduced to a log line); imports of the pinhole helpers from `head_tracking.py` where the overlay or the relay still uses them.
- `tests/test_sim_daemon.py` — convergence / intrinsics / fixed-webcam tests removed; the stepping test kept and sharpened (an observation on the tracker becomes the backend's face target within a tick).
- `tests-e2e/test_api.py` — timing constants; the settle assertion averaged over a window.
- `examples/control_panel/*` — the Gaze group's Start tracking no longer needs motors; `attention` shown as before.
- `README.md`, `docs/reachy-mini-api.md`, `docs/upstream-head-tracking-after-face-loss.md` ("Workaround used by the bridge" section) — the bridge's tracker described; the workaround paragraph replaced.
- `AGENTS.md` — the `head_tracking.py` row loses its placeholder note; the `face_detection.py` row's stays until the third plan; `sim_daemon.py` row text.
- `specs/sim_daemon.md`, `specs/_index.md` — `Updated` → `Implemented` when this plan is `Done`.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Check command green, and the previous plan `Done`.

### Step 1 — Pose history and `set_gaze` in the motion loop

**File:** `src/reachy_mini_bridge/motion.py`.

- Constants: `GAZE_HISTORY_S = 1.0`, `GAZE_ALPHA = 0.12`, `BREATHING_GAZE_ROTATION_SCALE = 0.25`.
- `IdleOffsets.scaled(self, *, translation: float = 1.0, rotation: float = 1.0, antennas: float = 1.0) -> IdleOffsets`: a copy with `z_mm` × translation, the three angles × rotation, the two antenna leans × antennas.
- `IdleMove.gaze_offsets(self, t: float) -> IdleOffsets`: a concrete method returning `IdleOffsets()`; `BreathingMove.gaze_offsets` returns `self.offsets(t).scaled(rotation=BREATHING_GAZE_ROTATION_SCALE)`.
- `check_idle_move_factory` also calls `gaze_offsets(0.0)` and applies the same finiteness rule; `_CustomIdle` wraps `gaze_offsets` the way it wraps `offsets`, so a raising or malformed `gaze_offsets` surfaces as `_CustomIdleError` (one warning, then the hold).
- History: a `collections.deque` of `(t_monotonic, head_4x4)` appended in `_tick` after `set_target` (the *final* head sent), trimmed to `GAZE_HISTORY_S`, guarded by a `threading.Lock`. `head_pose_at(age_s: float) -> np.ndarray`: the entry whose time is closest to `now − age_s` (the oldest kept when older); when the deque is empty (not commanding), `self._robot.get_current_head_pose()` — call it only from the event loop thread under `asyncio.to_thread`, or from the motion thread.
- `set_gaze(aim: np.ndarray | None, weight: float) -> None`: a command (queue) setting `self._gaze_target` (a copy) and `self._gaze_weight`.
- Thread state: `self._gaze_aim: np.ndarray | None` (the eased aim), `self._gaze_w_eff: float`, `self._gaze_w_from`, `self._gaze_w_to`, `self._gaze_w_start` (the fade: minjerk over `BLEND_S` between `from` and `to`, restarted whenever the target weight changes).

### Step 2 — The gaze layer in `_tick`

In `_tick`, after `head, antennas, body_yaw = stage.evaluate(eval_t)` and before the `final_*` assembly:

1. **Target weight this tick:** `w_target = self._gaze_weight if (playing.primary is None and not playing.exit_blend and self._gaze_target is not None) else 0.0`. Note `exit_blend` is the session-end fade: the layer fades out with it, so the head lands at neutral.
2. **Ease the aim:** if `self._gaze_target is not None`: `self._gaze_aim = target` on the first aim (or `self._last_target[0]` when commanding — the present head — so the first aim never steps), else `linear_pose_interpolation(self._gaze_aim, self._gaze_target, GAZE_ALPHA)`. A withdrawn target (`None`) keeps `_gaze_aim` where it is during the fade-out, then clears it once `w_eff` reaches 0.
3. **Fade the weight** toward `w_target` (restart the minjerk fade when `w_target` changes; `w_eff` is its value at `now`).
4. **Compose** when `w_eff > 0` and the stage belongs to an idle `_Playing`: the idle head and antennas are the stage's `evaluate(eval_t)` as today; the gaze head and antennas come from `gaze_offsets(eval_t).pose()` when the stage is an `IdleMove` (through `_CustomIdle` for a custom one), and from neutral (`IdleOffsets().pose()`) for `HoldMove` and for a blend; then `head = linear_pose_interpolation(idle_head, compose_world_offset(self._gaze_aim, gaze_head), w_eff)` and `antennas = idle_antennas + w_eff · (gaze_antennas − idle_antennas)`. A blend *into* an idle move (stage 0 of an idle `_Playing`) is composed the same way, so the fade-in is continuous.
5. Antennas and body yaw unchanged.

**Tests** (`tests/test_motion.py`, fake robot, `presence=True`, `idle="hold"` for determinism, then `"breathing"` for composition):

- `set_gaze(yaw_30deg_pose, 1.0)` on a hold: the recorded head yaw rises monotonically from 0 toward 30° and is within 2° of it after 1.5 s; pitch stays ~0.
- `set_gaze(None, 1.0)` afterwards: yaw returns to 0 within `BLEND_S + 0.2` s, no step larger than the per-tick easing allows.
- With `idle="breathing"` (seeded) and a held aim: the head's z still breathes (range ≥ 4 mm over a breath) while the mean yaw is within 3° of the aim, and the yaw's excursion around the aim is ≤ `8° × BREATHING_GAZE_ROTATION_SCALE + 1°`.
- A custom idle move that does not override `gaze_offsets`, with a held aim: the recorded head equals the aim (within the easing) and the antennas rest at neutral, while its `offsets` motion shows in full once the aim is withdrawn; one that overrides it with a dedicated pitch nod shows that nod around the aim; `IdleOffsets.scaled` multiplies each group by its factor and nothing else; a factory whose `gaze_offsets(0.0)` returns garbage is rejected at registration, and one whose `gaze_offsets` raises while playing falls back to the hold with one warning.
- A primary submitted while an aim is held: the recorded head during the primary's trajectory equals the move's own poses (the fake stub move's) — the layer is not applied; after it ends the yaw eases back onto the aim.
- `presence=False`: `set_gaze` records no targets.
- `head_pose_at(0.5)` after 1 s of a yaw ramp returns a yaw between the ramp's values at 0.4 and 0.6 s; `head_pose_at(5.0)` returns the oldest kept.
- Remove the two re-anchor tests together with `reanchor` / `_on_reanchor`.

### Step 3 — The tracker

**File:** `src/reachy_mini_bridge/head_tracking.py` (replace the placeholder's body; it imports `FaceReport` from `face_detection.py`, never the reverse).

- Move `pinhole_intrinsics(hfov_deg, size)` and `sim_hfov_deg(fovy_deg, width, height)` here from `sim_daemon.py` (re-export from `sim_daemon.py` only if something there still imports them after Step 6). Add `SIM_EYE_CAMERA_FOVY_DEG = 80.0` and `SIM_CAMERA_SIZE = (1280, 720)`.
- `CameraModel` (frozen): `K`, `D`, `size (w, h)`, `fixed: bool`. Builders: `CameraModel.for_robot(robot)` — `robot.media.camera.camera_specs` when present (the Lite's specs otherwise), `fixed=False`; `CameraModel.for_sim(camera: SimCameraSettings)` — `source == "sim"` → pinhole of `sim_hfov_deg(SIM_EYE_CAMERA_FOVY_DEG, *SIM_CAMERA_SIZE)`, `fixed=False`; `webcam` → pinhole of `camera.hfov_deg`, `fixed=True`; `D` zeros. The api picks `for_sim` when `config.backend == "sim"`, else `for_robot`.
- `HeadTracker(camera: CameraModel, pose_at: Callable[[float], np.ndarray], set_gaze: Callable[[np.ndarray | None, float], None], *, same_host: bool)`:
  - `weight` attribute (set by the verbs), `TRACKING_LOST_S = 2.0`, `GAZE_LATENCY_S = 0.15`.
  - `observe(report: FaceReport) -> None` (called by the detection loop on every poll): with a face — age = `clamp(now − report.ts, 0, GAZE_HISTORY_S)` if `same_host` and `report.ts > 0` else `GAZE_LATENCY_S`; `T_world_head = INIT_HEAD_POSE if camera.fixed else pose_at(age)`; `u = (x + 1)/2 · (w − 1)`, `v = (y + 1)/2 · (h − 1)`; `aim = look_at_image_pose(u, v, K, D, T_world_head, default_head_to_camera_transform())`; `set_gaze(aim, weight)`; `self._seen_at = now`. Without a face — if `now − self._seen_at ≥ TRACKING_LOST_S` and an aim is held: `set_gaze(None, weight)`.
  - `engaged -> bool` (an aim is held), `stop()` → `set_gaze(None, weight)`.
- A `look_at_image_pose` that raises (a face at the frame's edge with strong distortion) is one `DEBUG` line, the previous aim kept.

**Tests** (`tests/test_head_tracking.py`, offline, no daemon — skip nothing: the geometry needs only numpy/scipy and `reachy_mini`'s pure helpers):

- **Convergence (sim geometry).** A closed loop: the fake robot's `MotionSession` (hold idle) + a `HeadTracker` on `CameraModel.for_sim(sim source)`; a "face" at the test scene's default position and at ±0.15 m lateral is projected each poll through the pinhole *from the currently commanded head pose* (the head-mounted camera) into normalised `x, y`, fed as a `FaceReport`; after 4 s the commanded yaw is within 3° of `atan2(y, x)` and the face projects within 0.1 of the image centre; the yaw crosses the target at most once and by at most 10°.
- **Latency.** The same loop with each observation stamped 0.2 s in the past and the head pose read from history at that age still settles within 3°; the same loop with `same_host=False` (fixed `GAZE_LATENCY_S`) also settles.
- **Fixed camera.** `for_sim(webcam)` with a constant observation (`x = 0.3`): the yaw settles at the angle the pinhole implies from the rest pose and holds (no drift over 3 s); with `fixed=False` and the same constant observation it runs away past 30° — the test guards the bug.
- **Loss.** No face for `TRACKING_LOST_S`: `set_gaze(None, …)` is called once; a face again re-aims.

### Step 4 — The api

**File:** `src/reachy_mini_bridge/api.py`.

- Delete `ATTENTION_GRACE_S`, `ATTENTION_WATCH_WEIGHT`, `ATTENTION_POLL_S`, `_run_attention`, `_cancel_attention`, `_daemon_tracking_weight`, `_tracking_lock`, `_moves_in_flight`, `_attention`, `_tracking_weight`; `_restore_layers_after_move` keeps only wobbling.
- `_start_tracking_now` → `_start_tracker(weight)`: ensure the detection loop runs (start it if only tracking wants it), build the `HeadTracker` once per session (camera model from the config/backend, `pose_at=motion.head_pose_at` — a threadsafe read — `set_gaze=motion.set_gaze`, `same_host` from the robot's host), set its weight, register it as the detection loop's `on_observation`. `start_head_tracking(weight)`: `ValueError` outside `[0, 1]`; no motor check; `_tracking_wanted = True`. `stop_head_tracking()`: `tracker.stop()`, `_tracking_wanted = False`, stop the detection loop when `face_detection` is off too.
- `attention` property: `None` when not tracking or not entered; `"engaged"` when `tracker.engaged`, else `"watching"`.
- `FaceDetection` in `daemon` mode always arms the daemon itself: delete the `owns_daemon_arming` constructor argument, `set_owns_daemon_arming` and its docstring — the transitional hand-off from the previous plan — since nothing else sends a tracking weight to the daemon any more.
- Lifecycle: in `__aenter__`, construct the `MotionSession` object *before* the detection step (move the `MotionSession(...)` construction up; its `start()` and the exit callback stay where they are), so `_start_tracker` can hand the tracker `motion.set_gaze` / `motion.head_pose_at` at entry — a `set_gaze` sent before `start()` waits in the session's command queue ([specs/motion.md](../specs/motion.md) "The gaze layer"). The tracker starts with the detection loop at entry when `motion.tracking`; `__aexit__` resets `_tracking_wanted` to the config as before.
- `play_emotion`: remove the tracking dip; keep the wobbling pause / restore.

**Tests** (`tests/test_api.py`): `start_head_tracking` with motors disabled succeeds and records **no** `start_head_tracking` command other than the ε arming; `attention` is `"watching"` after entry with no face, `"engaged"` within 0.5 s of `show_face`, `"watching"` again `TRACKING_LOST_S` (monkeypatched to 0.3) after `hide_face`, `None` after `stop_head_tracking`; with motors enabled and `show_face(0.5, 0.0)` the fake's recorded head yaw turns toward the face's side within 1.5 s and returns toward neutral after the loss; `play_emotion` with a face shown records no tracking command and the emotion's poses play unchanged; the `daemon` arming is exactly `[start_head_tracking(0.001)]` at entry and `[stop_head_tracking()]` at exit whatever `tracking` / `detection` say (as long as one is on); weight `1.5` → `ValueError`. Delete the old attention tests and `_tracking_weights` helper.

### Step 5 — The fake

`_FakeMedia.camera` with `camera_specs` exposing `K` (a pinhole of ~88° at 3840×2592, the Lite's order of magnitude) and `D` (zeros) — the api's `for_robot` path is then exercisable on the fake (`backend="fake"` uses `for_robot`). `_FakeDaemonClient` gains `host = "127.0.0.1"` and `port = 8000`, so the api's `same_host` read works on the fake as on a `ReachyMini` (loopback → same host). Remove `get_tracked_face` and `_FakeFaceTarget`; remove them from `tests/test_robot.py`'s parity list (`get_tracked_face` is no longer consumed; `media.camera.camera_specs` attribute paths are checked under the union by pyright).

### Step 6 — Retire the launcher's aim-side corrections

**File:** `src/reachy_mini_bridge/sim_daemon.py`. Remove `install_tracker_intrinsics`, `_TrackerCamera`, `_tracker_intrinsics` and its call in `run_sim_daemon`; remove the `set_tracking_face` override in `corrected_backend`; the relay's negotiated-hfov reporting becomes the existing log line only (drop the `on_camera_negotiated` plumbing that fed the tracker). Keep the stepping override, the camera sources, the overlay and the extension hooks untouched. `tests/test_sim_daemon.py`: delete the convergence, intrinsics and fixed-webcam tests; keep and sharpen the stepping test (put a `FaceObservation` on the backend's tracker, tick once, `get_tracked_face().detected` is true).

### Step 7 — Live tier and docs

- `tests-e2e/test_api.py`: import `TRACKING_LOST_S` from `head_tracking` instead of `ATTENTION_GRACE_S` from `api`; the hand-back wait becomes `TRACKING_LOST_S + BLEND_S + 4.0`; in `_assert_tracked`, the settle check averages yaw over the last 2 s of the track (the head now breathes around the aim at a quarter of the idle rotation amplitude) and the image-centre check allows `|x|, |y| < 0.15`; keep every other assertion. The section note about "fresh attention loop" goes.
- `README.md`: the "Following a face" and "The simulator" rows, the Gaze verb row: the bridge's own tracker, the head breathes while it looks, the sim launcher fixes detection only.
- `docs/reachy-mini-api.md` "Face tracking": the bridge no longer uses the daemon's aim; `docs/upstream-head-tracking-after-face-loss.md` "Workaround used by the bridge": the bridge arms the daemon at a negligible weight as a detector and aims the head itself.
- `AGENTS.md`: the `sim_daemon.py` row (one correction, not three).
- `specs/sim_scene.md` "Head tracking converges on the face": record the overshoot the bridge tracker shows on the viewer sim in place of the daemon-side figure it quotes as pending.
- Statuses: `specs/head_tracking.md` (built in full by this plan) and `specs/sim_daemon.md`, with their `_index.md` rows → `Implemented`; this plan `Done` here and in `_index.md`.

### Step 8 — The actual-pose history and the online delay estimate

The first viewer-sim run of the acceptance tests (after Step 7) showed the head hunting ±20° around the face, period ~1.9 s, even over the hold. The face the daemon reports matched the commanded head ~0.46 s before, the actual head ~0.30 s before, while `now − ts` said ~0.08 s: `ts` is the detection's time, not the frame's, the daemon smooths the centre in the image, and the actual head lags the commanded one. Adding 0.38 s by hand settled the head within 0.3° of the face after one 4° overshoot. The fix is the design now in [specs/head_tracking.md](../specs/head_tracking.md) "The aim" — no hardcoded latency:

- `motion.py`: the history records the **reported** head pose (`get_current_head_pose()`) on every pass of the thread, commanding or not (a raising read skipped), `GAZE_HISTORY_S = 4.0`; `head_pose_at(t)` takes a monotonic time (nearest recorded; oldest / latest outside the window; the robot's present pose while nothing is recorded).
- `face_detection.py`: `FaceReport.head_pose: npt.NDArray[np.float64] | None = None` (a source that knows the capture pose — the custom detector, later).
- `head_tracking.py`: `GAZE_LATENCY_S` goes; `DELAY_WINDOW_S = 3.0`, `DELAY_MAX_S = 0.8`, `DELAY_STEP_S = 0.02`, `DELAY_MIN_MOTION_DEG = 4.0`, `DELAY_SMOOTHING = 0.3`, `DELAY_PRIOR_S = 0.2`. `observe`: a report with `head_pose` is aimed against it; otherwise `t_obs` = `ts` (same host) or the receipt time of a new detection, the pose is `pose_at(t_obs − delay)`, and each new detection (a new `ts`, or off-host a new face) joins the window, then the estimate is refitted: score every `L` by `1 − |mean of the unit world directions|` over the window (vectorised), move `DELAY_SMOOTHING` toward the argmin when the window's head poses span `DELAY_MIN_MOTION_DEG`. `delay_s` property. A fixed camera skips all of it.
- Tests: `head_pose_at` by time and while paused; the tracker learns an unreported 0.3 s delay in the offline closed loop (estimate within 0.1 s of it) and converges; a report with `head_pose` is aimed against it; the estimate holds while the head is still. The viewer-sim acceptance suite is the check.

## Verification

- Check command green.
- `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -k "face or attention or tracking"`: the three attention / gaze tests and the faces test pass — the head turns onto the portrait ahead and to either side, settles within 3° (averaged) with the face near the image centre, breathes while it holds it, eases back into the idle move once the portrait is hidden, re-engages when it returns, and plays an emotion through unchanged.
- Manual (viewer sim, `config.example.json`, webcam): the simulated robot looks at you and keeps breathing; step aside and it follows; leave and it eases back to idling within a few seconds; come back and it finds you. Then the same on a robot, and note anything about `GAZE_ALPHA` / `BREATHING_GAZE_ROTATION_SCALE` in [specs/motion.md](../specs/motion.md) open question 7.
