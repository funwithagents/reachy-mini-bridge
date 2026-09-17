---
code:
  - src/reachy_mini_bridge/sim_daemon.py
  - src/reachy_mini_bridge/config.py
  - src/reachy_mini_bridge/daemon.py
tests:
  - tests/test_sim_daemon.py
  - tests/test_daemon.py
---

# Sim daemon launcher (`sim_daemon.py`)

**Status:** Implemented

## Purpose

Every MuJoCo daemon the bridge starts runs through the bridge's own launcher, `python -m reachy_mini_bridge.sim_daemon`: upstream's daemon, unchanged in everything but three corrections that make **face tracking work in the sim** — so the viewer sim is a robot stand-in a person can test against by hand, not only a motion and audio target — and one addition, a **host webcam as the sim's camera**, so a person in front of the computer is who the simulated robot sees and follows.

Upstream's MuJoCo backend (SDK 1.10) gets face tracking wrong in three independent ways, each of which alone makes the head miss the face (the measurements are in [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking" and below):

1. its control loop never steps tracking, so the head never follows at all;
2. the tracker's camera intrinsics are mis-scaled for the sim camera, so the head settles ~45° away from the face — and, re-engaged from a small weight, freezes on an unreachable aim;
3. the tracking geometry assumes the camera turns with the head, which a webcam on the host does not.

The launcher corrects all three inside the daemon process, so the bridge — the api, the control panel, the e2e tier — sees a sim whose tracking converges on the face, exactly as a robot's does. The corrections belong upstream; the launcher carries them until they land there, and each is a separate, removable piece.

## Core concepts / Decided

### The launcher

```
python -m reachy_mini_bridge.sim_daemon [--scene NAME] [--headless] [--[no-]preload-datasets]
    [--camera sim|webcam] [--webcam-device DEVICE] [--webcam-hfov DEGREES] [upstream flags…]
```

`run_sim_daemon(argv=None, *, extensions=())` substitutes the backend class upstream's daemon constructs (`reachy_mini.daemon.daemon.MujocoBackend`) with the bridge's subclass (`corrected_backend(...)`, below), rewrites `sys.argv` to `--sim [--scene NAME] [--headless] --[no-]preload-datasets` plus any unrecognised flags forwarded verbatim, and calls upstream's `main()`. Everything else — the FastAPI app, the media server, readiness, shutdown — is upstream's.

`extensions` is how a layer on top adds to the daemon without a second launcher: a `SimDaemonExtension` has two optional hooks, `on_backend(backend)` — called at the end of the backend's `__init__`, once the model is built — and `on_app(app)` — called on the FastAPI app upstream's `create_app` returned. The bridge's test scene is one ([sim_scene.md](sim_scene.md)): its launcher, `python -m reachy_mini_bridge.testing.sim_scene --scene-path FILE [same flags]`, resolves the file to an upstream scene name and runs `run_sim_daemon` with the extension that installs the scene director (`on_backend`) and mounts the `/api/sim-scene` router (`on_app`). The corrections below therefore apply identically to every sim the bridge starts, test scene or not.

It runs under `mjpython` for the viewer, under any interpreter headless ([daemon.md](daemon.md) "The launch command" builds both). A daemon started by hand with upstream's `reachy-mini-daemon --sim` has none of this; for manual work, start `python -m reachy_mini_bridge.sim_daemon` (or let a `ReachyMiniApi` config with `daemon.spawn` do it) and the bridge borrows it like any other.

### Correction 1 — tracking is stepped on every control tick

Upstream's daemon-side tracking has two halves: a detector thread that queues observations, and `step_head_tracking()` — which drains them, latches the aim and marks IK — that the backend loop must call. Only the real-robot loop calls it; the MuJoCo loop never does, so a stock sim detects faces while `get_tracked_face()` stays undetected and the head never moves. The subclass overrides `update_head_kinematics_model` — the method the MuJoCo loop calls once per 50 Hz control tick, at the point where the robot loop calls it — to step tracking right after it: kinematics update, tracking step, IK, the robot loop's order.

### Correction 2 — the tracker uses the true intrinsics of the camera it reads

The daemon's `FaceTracker` downscales the camera stream to 320 px wide and converts a face pixel into an aim with a camera matrix it derives through `intrinsics_for_size(camera_specs.K, crop_scale, frame_size)`. That function rescales every matrix from the real robot's 3840×2592 sensor calibration. The sim's `MujocoCameraSpecs.K` is a 1280×720 matrix — and itself assumes a ~53° field of view where the MJCF `eye_camera` has `fovy` 80° — so at 320×180 the tracker works with fx 106.7, fy 50, principal point (53.3, 25), where the rendered image has f 107.3 and principal point (160, 90). The tracking loop then converges on the face sitting near the image's top-left corner: the head settles ~45° off the face (yaw −34°, pitch +31° for a face dead ahead), and a re-engage from the attention loop's watch weight freezes on an aim IK cannot reach ([api.md](api.md) "Attention").

In the launched daemon, the tracker's intrinsics are those of an ideal pinhole camera for the **active camera source**, at whatever frame size the tracker reads: square pixels, principal point at the frame centre, no distortion —

```
fx = fy = (width / 2) / tan(hfov / 2)      cx = width / 2      cy = height / 2
```

with `hfov` the source's horizontal field of view: for `sim`, derived from the scene's own eye camera (`2·atan(tan(fovy/2) · width/height)` with `fovy = model.cam_fovy[eye_camera]`, 80° → ~112° at 16:9); for `webcam`, `--webcam-hfov`. The replacement is scoped to the tracker (the `intrinsics_for_size` name `reachy_mini.vision.face_tracking` resolves), installed by the launcher before the daemon starts; the camera specs the daemon reports to clients (`GET /api/camera/specs`, the SDK's `media.camera.K`) are left as upstream has them (open question 2).

Measured with this correction — offline in a closed-loop replica of the daemon's tick, and live on the viewer daemon with the test scene's portrait: a face dead ahead holds at yaw 0°, pitch ~6° (the tracker aims at the nose, below the portrait's centre); at ±0.15 m lateral, 0.45 m away, yaw ±17–19° (geometry: `atan2(0.15, 0.45)` = ±18.4° — the eye camera is on the head's forward axis); the tracked face sits at the image centre (normalised |x|, |y| < 0.01); a re-engage after the attention hand-back converges the same way; on the way the head swings once past the face by 3–9.5° and creeps back (upstream's aim adds a detection a few frames old to the present pose), never oscillating; a 200 ms detection latency still settles within ~3°.

### Camera sources

`--camera` selects what the daemon's camera stream carries. Both feed the same place — the RTP raw-video stream on UDP port 5005 that upstream's media server reads for a MuJoCo sim — so the media server, the tracker, and every client (`get_camera_frame()`) are unchanged whichever source runs.

| `--camera` | Frames | Needs |
|---|---|---|
| `sim` (default) | upstream's render thread: the scene rendered from the head-mounted `eye_camera`, 1280×720 RGB at 25 Hz | the viewer (upstream renders only when not headless) |
| `webcam` | a capture thread relaying a host camera: the source opened at 1280×720, converted to RGB, sent on the same stream | a host camera and, on macOS, camera permission for the app that launched the daemon; headless or viewer alike |

- **Device.** `--webcam-device` names the capture device: omitted, the platform's default camera (`autovideosrc`); on macOS a device index (`avfvideosrc device-index=N`); on Linux a device path (`v4l2src device=/dev/videoN`).
- **Format.** The source is asked for 1280×720, the sim camera's size, so the frame has the webcam's own aspect and square pixels; a camera that cannot deliver 1280×720 fails as a capture error (below). Frames are relayed as captured, not mirrored — what a camera looking at the person sees, which is what the robot's own camera would see.
- **No render in webcam mode.** Under the viewer, the render thread is not started, so exactly one sender feeds the stream; the viewer window still shows the robot.
- **Capture errors don't stop the daemon.** A source that cannot start, or delivers no frame within 5 s, logs one `ERROR` naming the device and — on macOS — the likely cause (camera permission for the terminal or editor that launched the daemon); the daemon keeps running with no camera (`get_camera_frame()` returns `None`, the tracker sees nothing), and the relay retries every 5 s so granting the permission or plugging the camera in recovers without a restart.
- `--webcam-device` and `--webcam-hfov` are accepted only with `--camera webcam` (an argument error otherwise). `--webcam-hfov` is a number strictly between 1 and 179, default **70** — typical of a laptop or USB webcam; aim precision follows how well it matches the real camera (open question 1).

### Correction 3 — a webcam is a fixed camera

Upstream's aim assumes a head-mounted camera: `set_tracking_face` turns the face pixel into a world direction through `T_world_head = get_current_head_pose()` at the moment the observation is processed. That is right for `sim` (the eye camera turns with the head, so the face moves toward the image centre as the head turns and the loop closes). A webcam does not move: as the head turns, the face stays at the same pixel, and each new observation adds the same offset to an already-turned head — positive feedback that drives the head to its limit within a second.

With `--camera webcam`, the aim is computed from the camera's **fixed** pose: `T_world_head` is the neutral head pose (`INIT_HEAD_POSE`) in that computation, so the webcam stands where the robot's eye camera is at rest, looking straight ahead. The observation then gives an absolute direction — where the person is, seen from the rest pose — and the head eases to it and holds, with no feedback through its own motion: step aside and the head follows by the same angle; stand still and it stays. Everything after the aim is unchanged: the per-tick easing, the tracking weights, the lost-face recentre, and the bridge's attention hand-back on top. The override is confined to that one pose read — the subclass's `set_tracking_face` presents the neutral pose to upstream's computation; every other reader of the head pose (the aim's starting point, IK, the moves) sees the real one. Head-mounted geometry stays in force for `sim`.

The parallax between the webcam and the robot's eye (a webcam above a screen, a person at arm's length) is not modelled: the head aims along the direction the webcam sees the face in, which is what a person watching the viewer expects.

### Testable without a daemon

The corrections and the argv handling are pure or in-process, so the deterministic `tests/` tier pins them (skipping without the `sim` extra, like the scene tests):

- **Tracking converges on the face (sim geometry).** The corrected backend, stepped through real `mj_step`s, with observations built by projecting the test scene's face through the MuJoCo eye camera (the true pixel — no render, no detector, no network), turns the head until the camera axis is within a few degrees of the face, for a face ahead and to either side; the same loop with upstream's intrinsics ends more than 30° off — the test fails on the bug it guards.
- **A fixed webcam neither drifts nor stalls.** The same loop with `--camera webcam` geometry and a constant observation (a face that does not move in the image, as a webcam sees a still person) settles at the angle that observation implies from the rest pose and stays there; with head-mounted geometry it runs away.
- **Intrinsics.** The tracker's matrix for `sim` equals the pinhole of the scene's `fovy` at 320×180 (and at 1280×720), for `webcam` of the given `hfov`.
- **Stepping and hooks.** A control tick steps tracking after the kinematics update; `on_backend` runs once the model exists, `on_app` on the built app.
- **Launcher argv.** Flag rewriting and passthrough; the webcam flags rejected without `--camera webcam`; the render thread not started and the relay started in webcam mode (the capture pipeline behind a seam).

The live check is manual and in [the plan](../plans/202609171842_sim-daemon-launcher-tracking-corrections-and-webcam.md): the viewer sim with the test scene (automated in the attention / gaze tests of `tests-e2e/test_api.py`, which check the head's path onto the face and where it settles), and the viewer sim with a webcam and a person in front of it.

## Relationship to the other specs

- **[daemon.md](daemon.md):** `launch_command` builds every `sim` recipe on this launcher (the test scene's for a `.xml` scene), forwarding the camera flags from `DaemonConfig.camera`.
- **[config.md](config.md):** `daemon.camera` (`source`, `device`, `hfov_deg`) configures the camera source.
- **[sim_scene.md](sim_scene.md):** the test scene is a `SimDaemonExtension`; its face tests rely on corrections 1 and 2.
- **[api.md](api.md):** "Attention" runs unchanged on top; the corrections are what make its hand-back and re-engage converge in the sim.
- **[testing.md](testing.md) / [testing_support.md](testing_support.md):** the harness spawns through `daemon.py`, so the e2e tier gets the corrected sim with no change of its own.

## Open questions

1. **Webcam calibration.** `hfov` with an ideal pinhole is good enough to follow a person by eye; a calibrated matrix and distortion (upstream ships a calibration tool for the robot's camera) would make the aim precise. Deferred until a manual test needs better than a few degrees.
2. **The intrinsics clients see.** `GET /api/camera/specs` and the SDK's `media.camera.K` still report upstream's `MujocoCameraSpecs.K`, which is wrong for the rendered camera (and for a webcam); a client computing geometry from frames — upstream's `look_at_image` — inherits the error. Correcting what the daemon reports is deferred until the bridge ships a gaze-from-pixels verb ([api.md](api.md) deferred `look_at_image`), and belongs in the same upstream fix.
3. **Other webcam formats.** A camera that cannot deliver 1280×720 (a 4:3-only device) is a capture error today; scaling with borders and deriving the focal length from the content width is the extension, deferred until such a camera is in use.
4. **Removing the corrections.** Each correction is its own piece so it can be dropped once upstream ships the fix; the fast convergence tests stay, as the check that upstream's fix actually converges.
