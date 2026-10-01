# Sim displays — the camera overlay moved, the robot's gaze and face markers added; the inject routes renamed

**Status:** Done

**Done (2026-10-01):** every step implemented and verified — `ruff check`, `ruff format` (code dirs), `pyright`, the fast tier (603 passed, once the example-config test, red since commit 3356abe, was brought in line with `config.example.json`: `face_detection.width: 640` and every sim display on), the headless live tier (14 passed, 15 skipped: camera / gravity compensation / motor modes, as expected) and the viewer-sim live tier, run three times in full: 27 passed and 2 skipped on the first and the last, and one failure in between on an existing tracking test, made less marginal before the last run (below).

- **Calibration (Step 8).** YuNet's box on the test scene's portrait measured 0.124, 0.124 and 0.129 m tall at 0.35, 0.45 and 0.60 m: `FACE_BOX_HEIGHT_M = 0.125`. The portrait is smaller than life, so one constant cannot serve a person too. **Departure:** a second constant, `PERSON_FACE_BOX_HEIGHT_M = 0.18` (a nominal adult's, not measured), is used for a webcam; the publisher picks by the camera model. The spec's open question 1 now covers measuring it.
- **Simplified after the first manual test (2026-10-01), at the user's request.** One height for every camera: `PERSON_FACE_BOX_HEIGHT_M` is gone and `FACE_BOX_HEIGHT_M = 0.125` places every face (a person is drawn about a third too near). A marker is always the same size (`FACE_MARKER_ASPECT`); the detector's box no longer shapes it, because an upright box squares up around a tilted face and deformed the marker.
- **Measured (Step 9).** Markers land 0.006–0.008 m sideways of the portrait, 0.02 m under its centre, within 3 % of its distance. Through an emotion that moves the head 32°, a still portrait's marker moves 0.023–0.034 m at most (bound 0.05 m).
- **By eye (Step 6).** The gaze line runs through the portrait's face and the followed marker; the marker carries its track id as a label (MuJoCo draws a user geom's label); the camera overlay shows neither.
- **Departure: the publisher is a thread**, not an asyncio task. The testing harness starts the bridge under one `asyncio.run` and runs each test under another, so a task died with the first loop and no marker was sent; a thread runs for the whole session whatever loop the host uses. The spec says so. Since the harness keeps one loop (commit 3fb2129), the publisher is a task again: [202610011706](202610011706_face-marker-publisher-as-an-asyncio-task.md).
- **Departure: a `face_markers` capability**, probed like `faces` (the route answers), gates the marker tests, so a borrowed daemon without the display skips them instead of failing them.
- **Departure: reserved prop names are refused by `write_test_scene`**, with the other name checks, rather than in `FacePlane` itself.
- **An existing viewer test made less marginal:** `test_a_nearer_face_arriving_does_not_take_the_head` sampled the head 2.5 s into a 38° turn and averaged its yaw over a window that still held part of the turn (settled yaws from −6.6° to −14.3° for −14° ± 5°; it failed one of the full runs). An A/B with the face markers off gave the same spread, so the displays are not the cause; the step now waits 4.5 s before it averages.

Implements [specs/daemon/sim_displays.md](../specs/daemon/sim_displays.md) in full, and the matching edits of 2026-09-30 to:

- [specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md) ("Sim displays": the launcher keeps the wiring, the displays move to `sim_displays.py`);
- [specs/core/config.md](../specs/core/config.md) (`sim_displays.robot_gaze` / `.face_markers`);
- [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) ("One rule, shared": `frame_head_pose`);
- [specs/core/bridge.md](../specs/core/bridge.md) ("Lifecycle": the face marker publisher);
- [specs/testing/sim_scene.md](../specs/testing/sim_scene.md) (the inject router at `/api/sim/inject/bodies`, the reserved prop names);
- [specs/testing/testing_support.md](../specs/testing/testing_support.md) (`face_markers` on for the viewer sim).

It delivers:
- the camera overlay moved from `sim_daemon.py` into `sim_displays.py`, unchanged in behaviour;
- two new sim displays, `robot_gaze` (the eye camera's optical axis as a line) and `face_markers` (an ellipsoid per detected face, placed by the bridge and pushed to the daemon);
- the scene layer they draw through;
- the bridge-side publisher;
- `FACE_BOX_HEIGHT_M` calibrated on the portrait;
- the test scene's routes moved from `/api/sim-scene/…` to `/api/sim/inject/bodies/…`.

It deliberately leaves out the spec's open questions: a person-specific box height, a sphere at the tracker's aim, and face boxes on the camera overlay.

## How to work this plan

- **Read first:**
  - [AGENTS.md](../AGENTS.md);
  - the six specs above, `sim_displays.md` in full;
  - `src/reachy_mini_bridge/sim_daemon.py` (`ViewerOverlay`, `overlay_rect`, `resample_nearest`, `_capture_viewer`, `_TappedRenderer`, `_Displays`, `bridge_backend`, `run_sim_daemon`'s `create_app` wrap);
  - `src/reachy_mini_bridge/testing/sim_scene.py` (router, client, `_ROUTE_PREFIX`);
  - `src/reachy_mini_bridge/head_tracking.py` (`_aim`, `_head_at_frame`, `delay_s`);
  - `src/reachy_mini_bridge/bridge.py` (`start()` / `stop()` and the exit stack);
  - `tests/test_sim_daemon.py` (the fake viewer handle).
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

  Don't run `ruff format .`: it reflows the Markdown in `plans/` and `docs/`.
- **The viewer sim is the acceptance** (Steps 8–9): `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` from the agent session, with the Mac awake and unlocked.
- **Do not commit** unless asked.

## Scope

- `src/reachy_mini_bridge/sim_displays.py` — **new**:
  - moved from `sim_daemon.py`: `ViewerOverlay`, `overlay_rect`, `resample_nearest`, `_capture_viewer` (as `capture_viewer`), the `OVERLAY_*` constants;
  - pure: `FaceMarker`, `face_marker`, and the constants (`SCENE_LAYER_HZ`, `ROBOT_GAZE_LENGTH_M`, `ROBOT_GAZE_WIDTH_PX`, the colours, `FACE_MARKER_THICKNESS_M`, `FACE_BOX_HEIGHT_M`, `FACE_MARKERS_STALE_S`);
  - daemon side: `SceneLayer`, `RobotGazeView`, `FaceMarkersView`, `build_router`;
  - bridge side: `FaceMarkerPublisher`.
- `src/reachy_mini_bridge/sim_daemon.py`:
  - the overlay's classes and helpers imported from `sim_displays.py` (the relay's overlay branch and `_TappedRenderer` stay: camera-source wiring);
  - `_Displays` gains `robot_gaze` / `face_markers`;
  - `capture_viewer` (Step 1) hands the handle to every display that is on;
  - `bridge_backend` builds the scene layer and its views when either display is on;
  - `run_sim_daemon` mounts the displays router with `face_markers`.
- `src/reachy_mini_bridge/config.py` — `SIM_DISPLAYS` and `SimDisplaySettings` gain `robot_gaze`, `face_markers`.
- `config.example.json` — both fields, `false`.
- `src/reachy_mini_bridge/head_tracking.py` — `frame_head_pose(report, camera, history, delay, now)`; `_aim` / `_head_at_frame` call it.
- `src/reachy_mini_bridge/bridge.py` — the publisher on the exit stack, between the detection loop and the motion session.
- `src/reachy_mini_bridge/testing/sim_scene.py`:
  - the router at `/api/sim/inject` with every route under `bodies/`;
  - `FacePlane` refusing the names `spawn` / `clear`;
  - `SimSceneClient` on the new paths;
  - docstrings.
- `src/reachy_mini_bridge/testing/fixtures.py`, `src/reachy_mini_bridge/testing/_daemon.py`:
  - `_probe_faces` on the new path;
  - `face_markers` on in the spawned viewer daemon and in `live_bridge`'s config.
- `tests/test_sim_displays.py` — **new**: the overlay's tests moved from `tests/test_sim_daemon.py`, and the new displays'.
- `tests/test_sim_daemon.py`, `tests/test_config.py`, `tests/test_daemon.py`, `tests/test_head_tracking.py`, `tests/test_bridge.py`, `tests/test_sim_scene.py`, `tests/test_testing_support.py` — as the steps say.
- `tests-e2e/test_bridge.py` — the two marker checks.
- `docs/running-the-sim-daemon.md`:
  - the `curl` example on the new path;
  - a "Sim displays" paragraph (flags, config, what each shows).
- `AGENTS.md`:
  - a Project-map row for `sim_displays.py`;
  - the `testing/` row's `/api/sim-scene` → `/api/sim/inject`.
- `specs/daemon/sim_displays.md` frontmatter — add `src/reachy_mini_bridge/sim_displays.py`, `tests/test_sim_displays.py`, `tests-e2e/test_bridge.py`, `src/reachy_mini_bridge/testing/fixtures.py` once they exist.
- `specs/_index.md`, `plans/_index.md`, the seven specs' `**Status:**` lines, this file — statuses (Step 10).

## Steps

### Step 0 — Baseline

The check command is green on `main`.

### Step 1 — Move the camera overlay

A pure move, before anything new:
- `ViewerOverlay`, `overlay_rect`, `resample_nearest`, `_mujoco_version`, the `OVERLAY_*` constants and `_capture_viewer` go to `sim_displays.py`. `_capture_viewer` becomes the public `capture_viewer(displays, viewer_module)`, taking a sequence of displays (one today, the overlay).
- `sim_daemon.py` imports them. `sim_displays.py` never imports `sim_daemon.py`, so there is no cycle. Keep the names `sim_daemon` re-exports in its `__all__` only if something outside the module uses them (grep `examples/`, `tests-e2e/`, `docs/`).
- The overlay's tests move from `tests/test_sim_daemon.py` to `tests/test_sim_displays.py` unchanged. The wiring tests (the relay's `tee` branch, the renderer tap, the handle handed over in `run()`) stay in `tests/test_sim_daemon.py`.
- The full fast tier passes with no test edited beyond its imports.

### Step 2 — The inject routes

- `sim_scene.py`:
  - `_ROUTE_PREFIX = "/api/sim/inject"`;
  - the router's paths become `/bodies`, `/bodies/spawn`, `/bodies/clear`, `/bodies/{name}`, with the two fixed ones **registered before** `{name}`;
  - `FacePlane.__post_init__` (or wherever names are validated) refuses `spawn` / `clear` with a `ValueError`;
  - `SimSceneClient` requests the new paths.
- `fixtures.py` `_probe_faces` — `GET /api/sim/inject/bodies`.
- Tests:
  - the served-app tests on the new paths (`POST /api/sim/inject/bodies/spawn` spawns rather than hitting `{name}` with `404`);
  - a scene with a prop named `spawn` refused at write time;
  - the old `/api/sim-scene/bodies` answers `404` (clean break, no alias).
- `docs/running-the-sim-daemon.md`'s `curl` line, `AGENTS.md`'s `testing/` row.

### Step 3 — The config

- `SIM_DISPLAYS = ("camera_overlay", "robot_gaze", "face_markers")`; `SimDisplaySettings` gains both booleans.
- Validation, `enabled()` order and the headless `ConfigError` come for free. Tests:
  - both parse;
  - `enabled()` lists them in order;
  - each with `headless: true` is a `ConfigError` naming it;
  - `launch_command` emits `--sim-display robot_gaze --sim-display face_markers` after the camera flags (`tests/test_daemon.py`).
- `config.example.json`: both `false`, keeping the example's documented-every-field rule (`tests/test_config.py` already loads it).

### Step 4 — `frame_head_pose`

- A module function in `head_tracking.py` implementing the spec's rule:
  - fixed → `INIT_HEAD_POSE`;
  - a stamped `report.head_pose`;
  - else `history()` at `(report.ts if report.ts > 0 else now) − delay` by `nearest_index`.
- `_aim` calls it:
  - with `self._delay`, after `_head_at_frame`'s bookkeeping, which keeps adding the detection and refitting;
  - `_head_at_frame` keeps the side effects, and the pose lookup moves into the function;
  - behaviour is unchanged, and the existing tracker tests pin it.
- New tests:
  - each of the three branches;
  - calling it twice leaves `delay_s` unchanged.

### Step 5 — `face_marker` (pure)

- `FaceMarker` is a frozen dataclass: `pos`, `quat` (`[w, x, y, z]`), `size`, `followed`, `label`. It has `to_json()` for the route body.
- `face_marker(face, camera, head_pose, *, box_height_m=FACE_BOX_HEIGHT_M, followed=False)`:
  - the pixel (as `_aim`), then `undistort_points`, then `z = f_y · box_height_m / (face.size · camera.size[1])`;
  - the camera-frame point, then the face frame (normal toward the camera centre, up = camera −y orthogonalised, right completing), rotated by roll / yaw / pitch;
  - through `default_head_to_camera_transform()`, then `head_pose`.
- Signs are set by the reprojection tests, not by guessing:
  - project the marker's centre and its up axis back through `K`; the centre lands on the face's pixel, and the up axis's image tilt equals `roll`;
  - a positive `yaw` turns the normal's image-x component positive.
- `FACE_BOX_HEIGHT_M = 0.2` as a placeholder until Step 8.
- Tests (sim pinhole), as the spec's "Geometry" bullet:
  - round trip from a known point;
  - the reprojection;
  - roll sign, yaw sign;
  - fixed camera;
  - head-turned vs neutral giving the same world point for the same world face.

### Step 6 — The daemon side

- `sim_displays.py`, importing `mujoco` / `fastapi` inside functions:
  - **`SceneLayer(views, *, hz=SCENE_LAYER_HZ)`** — `attach(handle)` / `stop()`, with the overlay's shape (thread, degrade rule, warn-once draws). Each tick it collects `view.geoms(model, data)` specs, fills `handle.user_scn.geoms[i]` via `mjv_initGeom` / `mjv_connector` under `handle.lock()`, and sets `ngeom`. It caps at `maxgeom` with one warning. `stop()` sets `ngeom = 0` while the handle runs.
  - **`RobotGazeView(model)`** — resolves the `eye_camera` id once, and yields one line spec per tick.
  - **`FaceMarkersView(backend)`**:
    - measures the frame offset once, as `site_xpos[head] − backend.get_mj_present_head_pose()[:3, 3]` (after the backend's first `mj_forward`: measure lazily on the first draw);
    - holds the latest set and its arrival time under a lock;
    - `geoms()` yields nothing when the set is older than `FACE_MARKERS_STALE_S`.
  - **`build_router(view)`** — `PUT` / `GET /faces`, with validation per the spec's table.
- `sim_daemon.py`:
  - `_Displays(camera_overlay, robot_gaze, face_markers)` built from the parsed flags;
  - in `bridge_backend.__init__`, when `robot_gaze or face_markers`, a `SceneLayer` over the views that are on, with the `FaceMarkersView` also kept on the backend for the router;
  - `capture_viewer(displays, viewer_module)` gets the overlay and the layer, and binds `close` to stop them all first;
  - `run()` uses it when any display is on.
- `run_sim_daemon`'s `create_app` wrap mounts `build_router(...)` at `/api/sim/displays` when `face_markers` is on. The router needs the view, which exists only once the backend is built: hand it a getter (the backend class records its latest instance's view), and answer `503` while it is not there yet.
- Tests (sim extra):
  - the gaze line on a real `MjData` at `qpos0`, and turning with the head (joints set, `mj_forward`) under the `webcam` source as under `sim`;
  - the offset equal to 0.177 in z on the backend's model;
  - `world_pos` = `pos` + offset;
  - on a fake handle carrying a real `mujoco.MjvScene(model, maxgeom=…)` and a `lock()` context: geoms written, stale set dropped, stop clears, no-`user_scn` warns once, overflow warns once;
  - router validation, `age_s`, `404` without the display;
  - `capture_viewer` with overlay + layer stopping both before close, and restoring `launch_passive` on a raise.
- **Check by eye once** (viewer sim, `--sim-display robot_gaze --sim-display face_markers` with a hand `PUT`):
  - the line and an ellipsoid are drawn, and the line passes through the followed face's marker once tracking has settled;
  - the camera overlay does not show them;
  - the geom `label` is drawn. If MuJoCo draws no label on a user geom, drop `label` from the marker and the route, and say so in the spec.

### Step 7 — The bridge side

- **`FaceMarkerPublisher(faces, head_tracking, tracker, camera, history, url)`**:
  - an asyncio task polling at `FACE_POLL_HZ`;
  - a new `frame_id` builds the markers with `face_marker` and `frame_head_pose` (delay from `tracker.delay_s`, or `DELAY_PRIOR_S` without a tracker), with `followed` from `head_tracking.value.track_id`;
  - sends from `asyncio.to_thread` (`urllib.request`, `PUT`, JSON, 0.5 s timeout), one in flight with the newest pending;
  - an inactive report sends one empty set;
  - `404` → one `WARNING` naming `--sim-display face_markers`, then it returns;
  - other errors → `WARNING` once, then `DEBUG`.
- `bridge.py`:
  - on `sim` with `config.daemon.sim_displays.face_markers`, start it after the detection loop and the tracker, and push its cancel-and-await on the exit stack, so it stops before the detection loop;
  - the URL is the daemon's HTTP address (the host / port `start_daemon` and readiness use);
  - the camera model is the tracker's (`CameraModel.for_sim(config.daemon.camera)`).
- Tests (fake backend, a stub detector through `custom`, a `http.server` on a thread recording requests; the fake config uses `backend: "fake"`, so give the publisher a test seam or construct it directly with the bridge's observables — whichever keeps the test driving public behaviour):
  - one set per new `frame_id`;
  - the followed face marked;
  - the empty set on detection off;
  - a `404` server stopping it after one warning;
  - a cancel mid-request (the server sleeping) ending it with the session still stopping cleanly (AGENTS.md "Testing": cancellable).

### Step 8 — Calibrate `FACE_BOX_HEIGHT_M` (viewer sim)

- A throwaway script in the scratchpad, on a harness-style bridge over the viewer sim:
  - spawn one portrait at `(d, 0, 0.20)` for `d` in 0.35, 0.45, 0.60, with the head held neutral (`set_idle("hold")`, tracking off — memory: a still pose needs the hold);
  - average `face.size` over ~2 s;
  - compute `H = z · face.size · 720 / f_y`, where `z` is the portrait's distance from the eye camera along its axis (`d − 0.039`) and `f_y` is from the sim pinhole.
- Set `FACE_BOX_HEIGHT_M` to the mean, rounded to the cm. Record the three values and the spread in the spec's "From a face to a marker" and in this plan's Done note.

### Step 9 — Harness and e2e

- `_daemon.py` / `fixtures.py`: on the viewer sim, the spawned `DaemonConfig` and `live_bridge`'s config carry `sim_displays.face_markers = true`. Headless it stays off: it is a `ConfigError` there.
- `tests-e2e/test_bridge.py`, `requires_caps(live_bridge, "camera", "faces")`:
  - **lands on the portrait** — portraits at 0.35 / 0.45 / 0.60 m, ahead and ±0.15 m. `GET /api/sim/displays/face_markers` (a small helper beside `SimSceneClient`, or `urllib` in the test) gives a `world_pos` within 0.03 m across the portrait's ray and within 15 % along it;
  - **stays put** — portrait ahead, tracking on, an emotion played. Sample `world_pos` through it, assert the spread is within 0.05 m, and record the measured spread.
- Run the viewer tier twice. Both must pass. Report any skip with its reason.

### Step 10 — Docs, map, statuses

- `AGENTS.md` Project-map row for `sim_displays.py` (`tests/test_project_map.py` requires it once the module exists).
- `sim_displays.md` frontmatter completed (Scope).
- `docs/running-the-sim-daemon.md` "Sim displays".
- Specs back to `Implemented`: `sim_displays.md` (from `Stable`), and `sim_daemon.md`, `config.md`, `head_tracking.md`, `bridge.md`, `sim_scene.md`, `testing_support.md` (from `Updated`). Update both the files and `specs/_index.md`.
- This plan `Done` with a Done note (calibration, measured spread, any departures), here and in `plans/_index.md`.

## Verification

- Fast tier:
  - `tests/test_sim_displays.py` (geometry, layer, router, offset, publisher);
  - the edited `test_sim_daemon.py`, `test_config.py`, `test_daemon.py`, `test_head_tracking.py`, `test_bridge.py`, `test_sim_scene.py`, `test_testing_support.py`;
  - `tests/test_project_map.py`.
- Lint, format (code dirs only), `pyright`, full `uv run pytest`.
- Headless live tier: unchanged results (the displays are off headless).
- Viewer live tier: the two new marker checks pass and the existing attention / gaze tests are unaffected.
- The by-eye check of Step 6.
