---
code:
  - src/reachy_mini_bridge/head_tracking.py
  - src/reachy_mini_bridge/motion.py
  - src/reachy_mini_bridge/api.py
tests:
  - tests/test_motion.py
  - tests/test_api.py
  - tests-e2e/test_api.py
---

# Head tracking (`head_tracking.py`)

**Status:** Stable

## Purpose

The bridge's own head tracker: it takes the target face that user perception reports ([user_perception.md](user_perception.md)) and turns it into an **aim** — the head pose that looks at that person — which the motion loop composes into the idle move ([motion.md](motion.md) "The gaze layer"), so the robot looks at the person it is talking to while it keeps breathing, and idles in full again once nobody is there.

Upstream tracks daemon-side, with the client able only to switch it on and off: at full weight the daemon discards the client's head target, on a lost face it recentres and then keeps discarding, and in the sim its aim is wrong ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking"). Owning the aim in the bridge removes the attention loop, `play_emotion`'s weight dip and two sim-launcher corrections that worked around it, and is what lets a developer's own detector ([user_perception.md](user_perception.md) "Custom detectors") steer the head. The tracker sits between perception and motion: geometry on one side, gaze policy on the other, and no knowledge of where the faces come from.

## Core concepts / Decided

### Inputs and outputs

- **In:** the detection loop's per-poll observations — every `FaceReport`, not the debounced count events — through `observe(report)`; the motion loop's commanded-pose history through `head_pose_at(age_s)`; the camera model of the active camera (below); the caller's weight.
- **Out:** `motion.set_gaze(aim, weight)` — an aim (a 4×4 head pose) or `None` to withdraw it, and the blend weight. The tracker never touches the robot; the loop is the one writer.
- It runs whenever tracking is on (`motion.tracking` at entry, `start_head_tracking` / `stop_head_tracking` while entered), built once per session by the api.

### The aim

- **Geometry.** Upstream's, with the bridge's inputs: the normalised face becomes a pixel of a nominal frame, undistorted through the intrinsics into a ray in the camera frame, rotated into the world by the **head pose at the time of the observation**, offset by the head-to-camera transform, and turned into a look-at pose (`reachy_mini.vision.look_at.look_at_image_pose`, `default_head_to_camera_transform`). A `look_at_image_pose` that raises (a face at the frame's edge under strong distortion) is one `DEBUG` line and the previous aim stands.
- **Intrinsics — the camera model.** `CameraModel(K, D, size, fixed)`. On a robot, the SDK client's calibrated matrix and distortion (`robot.media.camera.camera_specs.K` / `D`, at that camera's calibrated resolution; the Lite's specs when the client runs without media). In the sim, upstream's matrix is wrong for the rendered camera ([sim_daemon.md](sim_daemon.md) open question 2), so the model is the bridge's own ideal pinhole — `pinhole_intrinsics` / `sim_hfov_deg`, moved out of the sim launcher into this module — of the scene's eye camera (`SIM_EYE_CAMERA_FOVY_DEG = 80` at 16:9, pinned against the MJCF by a sim-extra test) for a `sim` camera source, or of the config's `hfov_deg` for a `webcam`, chosen from `DaemonConfig.camera` when the backend is `sim`. A borrowed sim daemon of unknown camera is assumed to use the `sim` camera.
- **A fixed camera.** With a `webcam` source the camera does not turn with the head, so the ray is rotated by the **neutral** pose instead of the head's: the aim is an absolute direction from the rest pose, and the head eases to it and holds — step aside and it follows by the same angle, stand still and it stays. Head-mounted geometry would feed the head's own motion back into the aim and run it to its limit within a second. (This was the sim launcher's third correction; it belongs here.)
- **The head pose at the observation's time.** Aiming a stale detection with the present head pose is what gives upstream's tracker its single overshoot ([sim_scene.md](sim_scene.md) "Head tracking converges on the face"), and the bridge's detection is older still — a frame, the detector, up to a poll period. The motion loop keeps a short history of the head poses it commanded (`GAZE_HISTORY_S = 1.0`, [motion.md](motion.md)), and the tracker reads the pose at the observation's age. The age is `now − ts` when `ts` is on a clock the bridge can compare with — the daemon's `time.monotonic()`, system-wide on the same host (`robot.client.host` is loopback), and a custom detector's `ts`, which the bridge stamps itself — clamped to `[0, GAZE_HISTORY_S]`; on a daemon on another host (a wireless robot) the age is the fixed estimate `GAZE_LATENCY_S = 0.15`. The commanded pose stands in for the actual one (the real head lags it by the IK and the motors); the live tier's convergence tests are the check that this is close enough.

### Easing, loss, weight

- **Easing** is the loop's: the tracker publishes a new aim per observation with a face, and the loop eases toward it per tick ([motion.md](motion.md) `GAZE_ALPHA`), so an aim arriving at the detector's rate — 10 a second, upstream's camera-feed cap — never steps the head.
- **Loss.** When no face has been seen for `TRACKING_LOST_S = 2.0` s (upstream's own lost-face timeout), the tracker withdraws the aim (`None`) and the loop fades the gaze layer out — the head eases back onto the idle move, at or near neutral, and the robot idles in full. The next face publishes an aim again and the layer fades back in. The grace the attention loop used to keep (3 s, longer than the daemon's recentre) has no reason left: there is no daemon recentre to wait out.
- **Weight.** `start_head_tracking(weight)`'s weight, `[0, 1]`, is the layer's blend factor, held by the tracker and handed to the loop with every aim: at `1.0` the head is the aim with the idle move's own gaze-time motion composed on top (its `gaze_offsets`, [motion.md](motion.md) "The gaze layer" — breathing keeps its breath and antennas and tones its roaming down; a custom move that defines none is still on the aim); lower weights lean toward the face while showing more of the idle move; `stop_head_tracking()` withdraws the aim and the weight.
- **`attention` is derived**, no longer a loop of its own: `"engaged"` while the tracker holds an aim (a face seen within `TRACKING_LOST_S`), `"watching"` while tracking is on and nobody has been seen for longer, `None` when tracking is off ([api.md](api.md) "Attention / gaze").

### Configuration and verbs

- `motion.tracking` ([config.md](config.md)) — whether the tracker runs from session entry; `start_head_tracking(weight=1.0)` / `stop_head_tracking()` / `tracking` while entered ([api.md](api.md)). It lives in the `motion` block because the gaze is a layer of the motion loop.
- **Tracking implies detection.** The tracker is a client of the detection loop ([user_perception.md](user_perception.md) "The detection loop"), which runs while either `faces.detection` or tracking is on; starting tracking starts detection if it is not already running.
- **A mode, not a move.** Tracking moves the head only through the motion loop, which is paused without motors and emits nothing with presence off, so `start_head_tracking` is a mode switch like `set_presence`: it needs no motors and raises nothing about them, only `ValueError` for a weight outside `[0, 1]` ([api.md](api.md) "Motors" — this changes the verb's former contract).
- **Detection sources are not the tracker's concern.** In `daemon` mode the daemon's own tracker is armed at a negligible weight so that it keeps *detecting* ([user_perception.md](user_perception.md) "Detection sources"); the tracker here neither knows nor cares — the head is steered from the reports alone.

### Lifecycle

Built by `ReachyMiniApi.__aenter__` together with the detection loop, when `motion.tracking` is on — whether or not motors are enabled. The api constructs the `MotionSession` object before them (construction starts no thread), so the tracker holds the session's `set_gaze` and `head_pose_at` from the outset; the tracker then starts with the detection loop, before the session's `start()` — an aim it hands over meanwhile waits in the session's command queue and takes effect at the first tick ([motion.md](motion.md) "The gaze layer") — and the aim shows the moment the loop is commanding. Stopped with the detection loop right after the motion session on exit ([api.md](api.md) "Lifecycle"). `stop_head_tracking()` stops it mid-session and stops the detection loop too when `face_detection` is off.

### `fake` backend support

Nothing tracker-specific is needed on the fake beyond what perception and motion provide ([user_perception.md](user_perception.md) "`fake` backend support": `show_face` / `hide_face`; [motion.md](motion.md): recorded `targets`) and a `media.camera.camera_specs` stand-in carrying a Lite-like `K` / `D`, so `CameraModel.for_robot` runs offline. The whole pipeline then runs on the fake at real time: `tests/` observe the recorded head targets turning toward a face shown at `x = 0.5` and easing back once it is hidden for the loss timeout, an emotion playing through unchanged with a face shown and the head returning to it afterwards, and `attention` moving through `watching` → `engaged` → `watching` → `None`. The geometry itself is pinned offline with no daemon: a closed loop that projects the test scene's portrait through the sim camera's pinhole from the commanded head pose, feeds the tracker, and asserts the head converges within 3° of `atan2(y, x)` with at most one bounded overshoot — with a delayed observation, and with a fixed camera that must settle rather than run away.

## Relationship to the other specs

- **[user_perception.md](user_perception.md):** the reports the tracker consumes, every poll; tracking implies detection.
- **[motion.md](motion.md):** `set_gaze` and `head_pose_at`; the gaze layer composes, eases and fades what the tracker hands it.
- **[api.md](api.md):** the tracking verbs and `attention`, re-based on the tracker; the attention loop and `play_emotion`'s daemon-weight dip are gone.
- **[config.md](config.md):** `motion.tracking`; `daemon.camera` selects the sim's camera model.
- **[robot.md](robot.md):** `media.camera.camera_specs` joins the consumed slice.
- **[sim_daemon.md](sim_daemon.md):** corrections 2 and 3 (intrinsics, fixed camera) leave the launcher for this module; the pinhole helpers move with them.
- **[sim_scene.md](sim_scene.md) / [testing.md](testing.md):** the viewer-sim attention and gaze tests assert where the head ends up through the api and are the acceptance suite.

## Open questions

1. **The webcam's cropped field of view.** The sim launcher's relay narrows a wide camera's field of view when it crops to 16:9 and knows the negotiated width only inside the daemon; the tracker uses the config's `hfov_deg`. A daemon endpoint reporting the effective field of view would close the gap; deferred until a camera's aim error is measured to matter.
2. **Webcam calibration.** An ideal pinhole from `hfov_deg` follows a person by eye; a calibrated matrix and distortion (upstream ships a calibration tool for the robot's camera) would make the aim precise. Deferred until a manual test needs better than a few degrees.
3. **Numbers.** `TRACKING_LOST_S`, `GAZE_LATENCY_S` here and `GAZE_ALPHA` in the loop are starting values; to be tuned on the viewer sim and the robot within the implementation plan.
4. **A bearing for the report.** Once the tracker's camera model is shared with the face report, `Face` can carry yaw / pitch in degrees ([user_perception.md](user_perception.md) open question 2).
