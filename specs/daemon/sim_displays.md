---
code:
  - src/reachy_mini_bridge/sim_displays.py
  - src/reachy_mini_bridge/sim_daemon.py
  - src/reachy_mini_bridge/config.py
  - src/reachy_mini_bridge/daemon.py
  - src/reachy_mini_bridge/head_tracking.py
  - src/reachy_mini_bridge/bridge.py
  - src/reachy_mini_bridge/testing/_daemon.py
  - src/reachy_mini_bridge/testing/fixtures.py
tests:
  - tests/test_sim_displays.py
  - tests/test_sim_daemon.py
  - tests/test_config.py
  - tests/test_daemon.py
  - tests/test_head_tracking.py
  - tests/test_bridge.py
  - tests/test_testing_support.py
  - tests-e2e/test_bridge.py
---

# Sim displays — what the MuJoCo viewer shows besides the scene (`sim_displays.py`)

**Status:** Implemented

## Purpose

The viewer window of a bridge-launched sim ([sim_daemon.md](sim_daemon.md)) can show more than the scene. Three **sim displays** let a person watching the sim see what the robot sees, where it looks, and where the bridge believes each face is:

| Name | What it draws | Section |
|---|---|---|
| `camera_overlay` | the camera stream (the webcam, or the rendered eye camera) as a picture in the view's top-right corner | "Camera overlay" |
| `robot_gaze` | the eye camera's optical axis, the one the head tracker aligns, as a line in the 3D scene | "The robot's gaze" |
| `face_markers` | an ellipsoid per face the bridge detects, at the pose the bridge estimates, in the 3D scene | "The face markers" |

The face markers show the bridge's geometry, not only its detections. A marker is placed in the world with the head pose of its frame, so it stays put while the head turns when the tracker's pose and delay handling are right, and it slides when they are wrong ([head_tracking.md](../motion/head_tracking.md) "The aim"). On the test scene it lands on the portrait whose true pose is known ([sim_scene.md](../testing/sim_scene.md)), which shows the depth estimate's error. Both views are sim displays, siblings of the camera overlay: `daemon.sim_displays.robot_gaze` and `daemon.sim_displays.face_markers` in a config ([config.md](../core/config.md)), `--sim-display robot_gaze` and `--sim-display face_markers` on the launcher ("One viewer handle, every display" above).

## Core concepts / Decided

### Turning a display on

`--sim-display <name>` on the sim daemon launcher, repeatable, turns a display on; `daemon.sim_displays.<name>` is its config side ([config.md](../core/config.md)), and `launch_command` forwards one flag per display that is on ([daemon.md](daemon.md)). The names are `SIM_DISPLAYS`, in the table's order. Every display draws in the viewer window, so each is an argument error with `--headless` and a `ConfigError` with `headless: true`. A display is not a camera: the stream clients and the tracker read is untouched by all three.

### One viewer handle, every display

Upstream's `run()` keeps the handle it launches as a local and closes it itself at the end. While `run()` runs with any display on, `mujoco.viewer.launch_passive` (looked up on the module at call time) is substituted by a wrapper that hands the handle to every display that is on and binds the handle's `close` to stop them all first — a draw issued after the close could wait on a render thread that is gone. The original is restored when `run()` returns, also on an exception; a process-local substitution, gone the day upstream keeps the handle on `self`.

The camera overlay draws through the handle's `set_images` / `set_texts`. `robot_gaze` and `face_markers` draw through the scene layer, the one owner of the handle's `user_scn` ("The scene layer" below).

A display that takes data from a client mounts its router under **`/api/sim/displays`** on upstream's app, through the same `create_app` wrap as the extensions' `on_app` ([sim_daemon.md](sim_daemon.md) "The launcher"). Today that is `face_markers`, with `PUT` / `GET /api/sim/displays/face_markers` ("Bridge → daemon: the displays route" below). The test scene's props sit beside it under `/api/sim/inject` ([sim_scene.md](../testing/sim_scene.md) "The router"), so a path says whether it puts something in the scene the camera sees or only draws for the person watching.

### Camera overlay

`--sim-display camera_overlay` — `daemon.sim_displays.camera_overlay` in a config ([config.md](../core/config.md)) — draws the camera stream as a picture in the top-right corner of the MuJoCo viewer window: the webcam's frames with `--camera webcam`, the rendered eye camera's with `sim`. It is a display, not a camera: the stream clients and the tracker read is untouched; and headless there is no window to draw on, so the flag is an argument error with `--headless` (the config rejects the field with `headless: true` the same way).

- **How it is drawn.** MuJoCo's passive viewer draws images over its 3D view on every frame through `viewer.Handle.set_images` (MuJoCo 3.3.1+ — why the bridge's `sim` extra requires 3.3.x itself, [project.md](../project.md)): an RGB `uint8` image of exactly a rectangle's size, top-down (the binding flips it for OpenGL), copied on the call, drawn until replaced or cleared. The rectangle (`overlay_rect`) is 16:9, a quarter of the 3D view's width (`OVERLAY_FRACTION`), both sides even, inset by 2 % of the view's width (`OVERLAY_MARGIN`) from its top-right corner. It is recomputed from the handle's `viewport` — framebuffer pixels, twice the window's points on a Retina display — for every frame, so a resize keeps the picture in its corner; a view whose picture would have a side under 32 px draws nothing. The picture carries a 2 px light frame, so it stands out from a scene of its own colours (the eye camera's view of the empty scene is the viewer's own skybox and floor). One line of text at the view's top left (`set_texts`) names the camera — the webcam's name and negotiated size, the words the relay logs, or `eye camera 1280x720`. The overlay says what it does at `INFO` in the daemon's log — attached to the viewer, fed by the eye camera render, the first frame drawn and where — and a draw that raises is one `WARNING` with the traceback, then quiet.
- **Threads.** `set_images` waits for the viewer's render thread (up to one UI frame), so it never runs on the physics loop or a feed thread: frames go through `ViewerOverlay.show` into a latest-frame slot, and the overlay's own thread resamples the latest to the rectangle (`resample_nearest`, nearest neighbour — the daemon has no OpenCV) and draws it. A feed faster than the viewer only ever loses intermediate frames.
- **Where the frames come from.** *Webcam:* a `tee` after the relay pipeline's RGB 1280×720 caps adds a second branch — a leaky one-buffer queue, `videoscale` to 640×360 (`OVERLAY_SOURCE_SIZE`), a horizontal `videoflip`, an `appsink` named `overlay` — whose samples are copied into arrays and shown; the stream branch is the same as without the overlay and never waits on it. The picture is **mirrored**, as a person in front of a camera expects to see themselves; the flip lives in the overlay branch alone, so the stream and the tracker keep what the camera sees (the "not mirrored" of [sim_daemon.md](sim_daemon.md) "Camera sources" still holds for them). The rendered eye camera's picture is not mirrored: it is the scene, which the viewer shows beside it. *Rendered eye camera:* upstream's `rendering_loop` gets its offscreen renderer from `_get_renderer`, which the launcher's backend subclass wraps so every rendered frame is also shown (resampled to 640×360) on the render thread, before it goes to the stream.
- **Degrade rule.** A handle without `set_images` — a MuJoCo before 3.3.1, which a downstream installing `reachy-mini[mujoco]` alongside the bridge would get — is one `WARNING` naming the installed version, and no picture; nothing else changes. Nothing in the bridge depends on the MuJoCo version at runtime.

### Drawn for the viewer alone: the viewer's user scene

`robot_gaze` and `face_markers` are geoms in the passive viewer handle's **`user_scn`**, an `mjvScene` the viewer draws on top of the model's own geoms on every frame. The views fill it with `mujoco.mjv_initGeom` / `mujoco.mjv_connector` under `handle.lock()`, and upstream's physics loop, which calls `viewer.sync()`, carries it to the window.

The eye camera's frames come from upstream's offscreen `mujoco.Renderer`, whose scene is built from the model and data alone. **These geoms are therefore never in the camera stream**: the detector never sees a marker, and the camera overlay, which shows the stream, never shows one either. That is why a display in the 3D scene is a `user_scn` geom and never a body of the model, which the eye camera would render like a portrait.

### The scene layer

`SceneLayer` owns `user_scn` for every display that draws in the 3D scene. It is one daemon thread, started when the viewer handle is handed over ("One viewer handle, every display" above) and stopped before the viewer closes. At `SCENE_LAYER_HZ = 30` it:

- asks each view that is on for its geoms (`RobotGazeView`, `FaceMarkersView`);
- writes them into `user_scn` in one pass under `handle.lock()`, then sets `user_scn.ngeom`;
- clears `user_scn` (`ngeom = 0`) on stop, while the viewer is still up.

The layer runs on its own thread, never the physics loop's or a request handler's, because `handle.lock()` waits for the viewer. Geoms beyond `user_scn.maxgeom` are dropped (one `WARNING`). A handle without `user_scn` is one `WARNING` naming the installed MuJoCo version and no drawing, like the overlay's degrade rule. A draw that raises is one `WARNING` with the traceback, then `DEBUG`.

The views read `MjData` (site and camera frames) from the layer thread without the physics loop's cooperation. A read torn by a concurrent `mj_step` is off by one step for one frame, which a line drawn for a person watching absorbs.

### The robot's gaze

`RobotGazeView` draws one line: the **eye camera's optical axis**, from the `eye_camera`'s position (`data.cam_xpos`) along the camera frame's −z (`data.cam_xmat`, third column negated). It is `ROBOT_GAZE_LENGTH_M = 1.0` long (past the portraits' 0.30–0.70 m), drawn with `mjv_connector` as an `mjGEOM_LINE` `ROBOT_GAZE_WIDTH_PX = 3` wide in `ROBOT_GAZE_RGBA` (blue).

This is the axis the head tracker aligns. Its aim (`look_at_image_pose` with the head-to-camera transform, [head_tracking.md](../motion/head_tracking.md) "The aim") turns the head until the followed face sits at the image centre, which is on this line. Once tracking has converged, the line passes through the followed face's marker, and the gap between them while it converges is the aim still in flight.

The line always belongs to the **simulated robot's head**: it is read from the `eye_camera` frame in `MjData`, which rides the head about 0.04 m forward of and 0.05 m above the `head` site, so it turns and moves with the head whatever the camera source.

Under a `webcam` source the line still is the robot's head-mounted camera, never the webcam's fixed position. The webcam only changes where the faces come from: the tracker models the webcam as a fixed camera at the neutral eye camera ([head_tracking.md](../motion/head_tracking.md) "A fixed camera"), so the face markers are placed from that fixed viewpoint, while the gaze line keeps following the head the tracker turns toward them. Converged, the line points at the followed face's marker to within the parallax between the fixed viewpoint and the turned eye camera: a few centimetres at desk range, since the eye camera moves a few centimetres as the head turns.

### The face markers

#### What a marker is

A `FaceMarker(pos, quat, size, followed, label)` is one face as the bridge places it:

- `pos` is in metres and `quat` is `[w, x, y, z]`, both in the **head-pose frame**: the frame of the poses the SDK reports and commands, identity at the neutral head.
- `size` is `(width, height)` in metres, the same for every marker of a session.
- `followed` is true for the face the head tracker follows (`bridge.head_tracking.value.track_id`).
- `label` is the face's `track_id` as text.

The daemon draws it as an `mjGEOM_ELLIPSOID` with semi-axes `(width/2, height/2, FACE_MARKER_THICKNESS_M/2)` (`0.01` m thick) along the face's right, up and normal. It is coloured `FOLLOWED_RGBA` (green) or `FACE_RGBA` (yellow), both at alpha 0.6, and carries its label as the geom's text.

#### From a face to a marker

`face_marker(face, camera, head_pose, *, box_height_m=FACE_BOX_HEIGHT_M)` turns one `Face` of a report into a `FaceMarker`. It is a pure function in the bridge, which holds every input:

1. **The pixel.** The face's normalised `(x, y)` (its nose when known, else the bbox centre) becomes a pixel of the camera model's frame, exactly as the tracker's aim does, then an undistorted normalised point `(x_n, y_n)` through `K` and `D`.
2. **The depth.** A camera gives a direction, not a distance, so the distance comes from the face's apparent size by the pinhole's similar triangles. With `h_px = face.size · camera.size[1]` (the bbox height in the model's pixels), the depth along the optical axis is `z = f_y · box_height_m / h_px`, and the point in the camera frame is `z · (x_n, y_n, 1)`. The direction is exact up to the camera model. The distance is as good as `box_height_m` is for the face in view.
3. **The size.** A marker is always the same object: `box_height_m` tall and `FACE_MARKER_ASPECT = 0.8` of that wide, whatever the detector's box looks like. The box's own proportions are not used: an upright box grows and squares up around a tilted face, which would deform the marker. Only the marker's place and orientation change from one report to the next. A face with no size has no distance and gets no marker.
4. **The orientation, in the camera frame.** The face's normal points from the face back to the camera centre. Its up is the camera's up (−y) made orthogonal to the normal, and its right completes a right-handed frame. That frame is then turned in its own axes: `yaw` about its up, `pitch` about its right, then `roll` about the line of sight, with signs such that projecting the marker back through the pinhole reproduces the report's convention ([user_perception.md](../vision/user_perception.md) "The face report"). The tilt of the marker's width axis in the image equals `roll`. A positive `yaw` turns the normal toward the image's right. A positive `pitch` tilts the face down. A `None` angle is zero. With the shipped `yunet`, which gives roll only, a marker faces the camera and leans with the eye line. A detector that fits a head also turns it.
5. **Into the head-pose frame.** Camera frame → head frame through `default_head_to_camera_transform()` (the tracker's), → the head-pose frame through `head_pose`.

`FACE_BOX_HEIGHT_M = 0.125` is the one ratio that turns a face's size into a distance, for every camera. It is a heuristic for a debug view, calibrated on the test scene's portrait, whose distance is known: YuNet's box measured 0.124, 0.124 and 0.129 m tall at 0.35, 0.45 and 0.60 m on the viewer sim (2026-10-01). The portrait is smaller than life (its face fills half of a 0.25 m plane), so a person in front of a webcam, whose face is bigger, is drawn nearer than they stand, by about a third. The direction stays right.

#### The head pose of a frame

A marker is placed with the head pose its frame was taken from, the pose the head tracker aims that report against. The rule lives in one function the tracker and the markers share, `frame_head_pose(report, camera, history, delay, now)` ([head_tracking.md](../motion/head_tracking.md) "The aim"):

- a fixed camera (`webcam`) gives the neutral pose;
- a report stamped with its frame's `head_pose` gives that pose;
- otherwise it is the reported head pose at the report's time (`report.ts`, or `now` for an unstamped report) minus `delay`.

The markers take `delay` from the bridge's tracker (`HeadTracker.delay_s`): its online estimate, which is `DELAY_PRIOR_S` until a tracked turn has taught it better and keeps its last value while tracking is off. The pose is read without side effects: computing a marker never feeds the tracker's delay estimate.

Under `webcam` the camera is fixed at the neutral head, so the markers show where the tracker believes the person is: in front of the robot, as if they stood where the webcam sees them.

### Bridge → daemon: the displays route

The faces are in the bridge and the viewer is in the daemon, so the markers cross over HTTP on the daemon's own port.

**Daemon side.** With `face_markers` on, the sim daemon launcher mounts the displays router at **`/api/sim/displays`** on upstream's app ("One viewer handle, every display" above). Without it the routes do not exist (`404`).

| Route | Body | Result |
|---|---|---|
| `PUT /api/sim/displays/face_markers` | `{"markers": [{"pos": [x,y,z], "quat": [w,x,y,z], "size": [w,h], "followed": bool, "label": str \| null}]}` | `{"markers": n}`; `400` on an unknown field or a malformed value (a quaternion is normalised, all-zero refused) |
| `GET /api/sim/displays/face_markers` | — | `{"age_s": float \| null, "markers": [...]}`: the stored set, each marker also carrying `world_pos` (MuJoCo world coordinates, below); `age_s` is the time since it arrived, `null` before the first |

The router is on the app before the backend exists, so both routes answer `503` until the backend is built.

- **The frame offset is the daemon's.** The route takes poses in the head-pose frame, the one every SDK client speaks. The MuJoCo world differs from it by a translation: upstream's MuJoCo backend reports the `head` site shifted down 0.177 m (`get_mj_present_head_pose`). `FaceMarkersView` measures that offset once the model exists, as `site_xpos[head] − get_mj_present_head_pose()[:3, 3]`, rather than hard-coding upstream's constant, and adds it to every marker's position.
- **Stale markers vanish.** A set older than `FACE_MARKERS_STALE_S = 0.5` s on the daemon's clock is not drawn, so a bridge that stops or crashes leaves no frozen ellipsoids. The bridge sends an empty set when detection stops.

**Bridge side.** `FaceMarkerPublisher` is an asyncio task on the bridge's event loop, started and stopped through an async `start()` / `stop()` pair like the detection loop. It runs for the whole session when the backend is `sim` and `daemon.sim_displays.face_markers` is on ([bridge.md](../core/bridge.md) "Lifecycle"), whether or not detection is switched on: an inactive report is what it sees while detection is off. It reads the face report, the tracker's delay and the pose history on the loop, and hands each request to a worker thread (`asyncio.to_thread`), so a slow daemon never holds the loop.

- It **polls** `bridge.faces.value` 30 times a second (the detection loop's own rate). `changes()` wakes only when the face count changes, and a marker must follow a face that merely moves.
- A report it has not sent (a new `frame_id` or `ts`: one per detection, about 10/s) becomes a set of markers, one per face. It is `PUT` from a worker thread, with `urllib` and a 0.5 s timeout, to the daemon's address: the robot options' host and port, else the local daemon's defaults ([daemon.md](daemon.md)).
- One request is in flight at a time. The report it reads next is the newest, so reports that arrived meanwhile are skipped.
- An inactive report sends one empty set.
- **A daemon without the route** (upstream's `reachy-mini-daemon --sim`, or the launcher without `--sim-display face_markers`) answers `404`. That is one `WARNING` naming `--sim-display face_markers`, and the publisher stops for the session.
- Any other failure is one `WARNING`, then `DEBUG`, and the publisher keeps going: a daemon restarting under a borrowed session recovers.
- Nothing it does raises into the session. `stop()` cancels the task and returns without waiting for a request in flight: the request ends on its own within its timeout, on its worker thread, and the session's teardown carries on.

## Module

`src/reachy_mini_bridge/sim_displays.py` holds the three displays, daemon side and bridge side, like `testing/sim_scene.py` holds its director and its client:

- **pure:** `overlay_rect`, `resample_nearest`, `FaceMarker`, `face_marker`, and the constants (`OVERLAY_*`, `SCENE_LAYER_HZ`, `ROBOT_GAZE_*`, `FACE_*`);
- **daemon side:** `ViewerOverlay`, `SceneLayer`, `RobotGazeView`, `FaceMarkersView`, `capture_viewer` (the `launch_passive` substitution), and `build_router`, which import `mujoco` and `fastapi` when they run;
- **bridge side:** `FaceMarkerPublisher`, `face_markers_url`, and `fetch_face_markers` (what the daemon holds, or `None` when it has no such display).

The sim daemon launcher keeps what feeds the overlay, because it is camera-source wiring: the webcam relay's overlay branch and the tap on the eye camera's renderer ([sim_daemon.md](sim_daemon.md) "Camera sources"). It imports the displays from here and builds the ones that are on. `frame_head_pose` lives in `head_tracking.py` beside the aim it serves. The module imports neither `mujoco` nor `fastapi` at import time, so the bridge process never needs the `sim` extra to import it.

## Testing

**Fast tier** (`tests/test_sim_displays.py`; the daemon-side tests skip without the `sim` extra):

- **The camera overlay.** `overlay_rect` places the picture (16:9, even sides, the corner, the margin, `None` on a view too small); `resample_nearest` keeps the size and the corners; on a fake viewer handle the overlay draws the latest frame at the rectangle's size, skips to the latest frame while the viewer is busy, follows a resized viewport, labels, clears on stop, and warns once on a handle without `set_images`.
- **The viewer handle.** `capture_viewer` hands the handle to every display that is on (the overlay, the scene layer) and stops them before the close, restoring the launch function even when the run raises.
- **Geometry, on the sim pinhole.** A face projected from a known point comes back on the same ray, at the depth its size implies (exact when the point's box is `FACE_BOX_HEIGHT_M` tall). The marker reprojects onto the face's pixel. Its major axis's tilt in the image equals `roll`, and a positive `yaw` turns its normal toward the image's right. A fixed camera places it from the neutral pose. A face seen with the head turned lands where the same face seen from neutral does.
- **`frame_head_pose`.** A fixed camera gives the neutral pose. A stamped report gives its own pose. An unstamped one gives the history's pose at the report's time minus the delay. Calling it leaves the tracker's delay estimate unchanged (`tests/test_head_tracking.py`).
- **The robot's gaze.** On a real `MjData` at `qpos0`: the line starts at `eye_camera` and runs along its optical axis (world +x at the neutral head). With the head's joints turned and `mj_forward` run, the line turns with the head, in a backend built with the `webcam` source as with `sim`.
- **The layer, on a fake viewer handle carrying a real `mjvScene`.** Geoms are written under the lock. A stale marker set is dropped. Stop clears `ngeom`. A handle without `user_scn` warns once. Geoms past `maxgeom` are dropped with one warning.
- **The frame offset.** Measured on the backend's model, it equals upstream's 0.177 m in z; a marker's `world_pos` is its `pos` plus it.
- **The router.** Validation, the stored set, `age_s`, a `404` without the display.
- **The publisher.** Fed reports directly, with a recording stand-in for the route: one set per new report, the followed face marked; one empty set for an inactive report; the same marker for the same face whatever the camera; stopping after one warning on a `404`; one warning then recovery through a failing daemon; the loop staying responsive while a request is held; a stop mid-request returning at once with the task ended, and a fresh start sending again.
- **Through the bridge** (`tests/test_bridge.py`), on a `sim` session whose robot is the fake, with a stub detector ([user_perception.md](../vision/user_perception.md) "`fake` backend support") and a local HTTP server as the daemon's port: the face's marker arrives at `/api/sim/displays/face_markers`, marked as followed once the head follows it; an empty set once nobody is there; nothing after `stop()`; nothing at all with the display off; and a session stopped while the daemon holds a request starts and sends again.

**Viewer e2e** (`tests-e2e/test_bridge.py`, viewer sim only: `requires_caps(live_bridge, "camera", "faces", "face_markers")`. The harness's viewer sim runs with `face_markers` on, and `face_markers` is the capability that the daemon answers the route, [testing.md](../testing/testing.md)):

- **The marker lands on the portrait.** For a portrait at 0.35 m and 0.60 m ahead and at 0.45 m to either side, the `GET` route's `world_pos` is within 0.03 m of the portrait sideways, up to 0.09 m under its centre (the marker is on the nose), and within 15 % of its distance. Measured on the viewer sim: 0.008 m sideways, 0.02 m under the centre, 3 % in distance at worst.
- **The marker stays put while the head turns.** With the portrait still and tracking on, an emotion plays: the marker's `world_pos` stays within 0.05 m of its mean while the head moves. Measured over nine runs: the head moved 32°, the marker 0.023 to 0.038 m at most (about 0.02 m at the 90th percentile).

## Relationship to the other specs

- **[sim_daemon.md](sim_daemon.md):** the launcher that parses `--sim-display`, builds the displays that are on, feeds the overlay from its camera source, and mounts the displays router.
- **[config.md](../core/config.md):** `daemon.sim_displays` (`camera_overlay`, `robot_gaze`, `face_markers`), viewer only.
- **[daemon.md](daemon.md):** `launch_command` forwards a `--sim-display` per display that is on.
- **[head_tracking.md](../motion/head_tracking.md):** `frame_head_pose` and the tracker's `delay_s`, the camera model, `default_head_to_camera_transform`; the axis `robot_gaze` draws is the one its aim aligns.
- **[user_perception.md](../vision/user_perception.md):** the `Face` a marker is built from and its orientation convention; `bridge.faces`, polled.
- **[bridge.md](../core/bridge.md):** the face marker publisher's place in the session's lifecycle.
- **[sim_scene.md](../testing/sim_scene.md):** the portraits the e2e checks land the markers on, and the calibration of `FACE_BOX_HEIGHT_M`; its props are injected under `/api/sim/inject/bodies`, beside the displays route.
- **[project.md](../project.md):** the `sim` extra's MuJoCo 3.3.x, which `set_images` needs.

## Open questions

1. **A distance that fits a person.** One hard-coded height places every face, and it is the portrait's. A person is drawn about a third too near, and a tilted head a little nearer still (the detector's upright box grows around a tilted face). A height per camera source, a config knob, or a correction for the roll would tighten it. Deferred while the markers are a debug view.
2. **The tracker's aim.** A sphere where the gaze layer aims, beside the robot's gaze line, would show the easing and the delay converging. It needs the aim pushed like the faces (a second list on the same route). Deferred until the head axis and the markers are in use.
3. **The faces on the camera overlay.** Each face's box drawn on the overlay picture shows whether detection works, but not where the face is. The markers' pixel boxes could travel with them. Deferred: the overlay shows the newest frame and the faces are a frame or more older, so the boxes would lag.

