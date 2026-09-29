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

Every MuJoCo daemon the bridge starts runs through the bridge's own launcher, `python -m reachy_mini_bridge.sim_daemon`: upstream's daemon with two additions that make **the viewer sim a robot stand-in a person can test against by hand**, not only a motion and audio target — a **host webcam as the sim's camera**, so a person in front of the computer is who the simulated robot sees and follows, and a **camera overlay** on the viewer window, so the person watching the sim also sees what the robot sees — and an extension hook a layer on top (the test scene) adds to the daemon through.

The daemon's own face tracking plays no part in any of it. The bridge detects faces on the host, from the camera stream the daemon serves, and aims the head with its own tracker ([user_perception.md](../vision/user_perception.md), [head_tracking.md](../motion/head_tracking.md)); upstream's daemon-side tracking — which in the MuJoCo backend (SDK 1.10) is never stepped by the control loop and aims with intrinsics mis-scaled for the sim camera ([../docs/reachy-mini-api.md](../../docs/reachy-mini-api.md) "Face tracking") — is left as upstream ships it, untouched by the launcher and never armed by the bridge. What the launcher changes is what the camera stream *carries* and what the viewer *shows*.

## Core concepts / Decided

### The launcher

```
python -m reachy_mini_bridge.sim_daemon [--scene NAME] [--headless] [--[no-]preload-datasets]
    [--camera sim|webcam] [--webcam-device DEVICE] [--webcam-hfov DEGREES]
    [--sim-display camera_overlay] [upstream flags…]
```

`run_sim_daemon(argv=None, *, extensions=())` substitutes the backend class upstream's daemon constructs (`reachy_mini.daemon.daemon.MujocoBackend`) with the bridge's subclass (`bridge_backend(...)`, below), rewrites `sys.argv` to `--sim [--scene NAME] [--headless] --[no-]preload-datasets` plus any unrecognised flags forwarded verbatim, and calls upstream's `main()`. Everything else — the FastAPI app, the media server, readiness, shutdown — is upstream's.

`extensions` is how a layer on top adds to the daemon without a second launcher: a `SimDaemonExtension` has two optional hooks, `on_backend(backend)` — called at the end of the backend's `__init__`, once the model is built — and `on_app(app)` — called on the FastAPI app upstream's `create_app` returned. The bridge's test scene is one ([sim_scene.md](../testing/sim_scene.md)): its launcher, `python -m reachy_mini_bridge.testing.sim_scene --scene-path FILE [same flags]`, resolves the file to an upstream scene name and runs `run_sim_daemon` with the extension that installs the scene director (`on_backend`) and mounts the `/api/sim-scene` router (`on_app`). The camera sources and the overlay below therefore apply identically to every sim the bridge starts, test scene or not.

It runs under `mjpython` for the viewer, under any interpreter headless ([daemon.md](daemon.md) "The launch command" builds both). A daemon started by hand with upstream's `reachy-mini-daemon --sim` has none of this; for manual work, start `python -m reachy_mini_bridge.sim_daemon` (or let a `ReachyMiniBridge` config with `daemon.spawn` do it) and the bridge borrows it like any other.

### The backend subclass

`bridge_backend(...)` returns a subclass of upstream's `MujocoBackend` that wires the launcher's additions into the daemon process: it selects the camera source (starting the webcam relay instead of the render thread in `webcam` mode, below), attaches the viewer overlay when a display asks for it, and calls every extension's `on_backend` at the end of `__init__`. It overrides nothing of the daemon's control loop, kinematics or tracking: the sim's physics, its IK and its face tracking run as upstream ships them. The camera specs the daemon reports to clients (`GET /api/camera/specs`, the SDK's `media.camera.K`) are left as upstream has them too (open question 2); the bridge's tracker uses its own pinhole of the sim camera ([head_tracking.md](../motion/head_tracking.md)).

### Camera sources

`--camera` selects what the daemon's camera stream carries. Both feed the same place — the RTP raw-video stream on UDP port 5005 that upstream's media server reads for a MuJoCo sim — so the media server, the tracker, and every client (`get_camera_frame()`) are unchanged whichever source runs.

| `--camera` | Frames | Needs |
|---|---|---|
| `sim` (default) | upstream's render thread: the scene rendered from the head-mounted `eye_camera`, 1280×720 RGB at 25 Hz | the viewer (upstream renders only when not headless) |
| `webcam` | a capture thread relaying a host camera: the source at 1280×720 where it offers it, else at whatever mode it prefers, centre-cropped to the stream's aspect and scaled to 1280×720; converted to RGB and sent on the same stream | a host camera and, on macOS, camera permission for the app that launched the daemon; headless or viewer alike |

- **Device.** `--webcam-device` names the capture device: omitted, the platform's default camera (`autovideosrc`); on macOS a device index (`avfvideosrc device-index=N`); on Linux a device path (`v4l2src device=/dev/videoN`). The relay logs the camera it opened by name, because which one the platform default turns out to be is not otherwise visible — on a machine with a Reachy Mini plugged in it is quite likely the robot's own camera (a 3840×2592 sensor of ~88° horizontal field of view, where `--webcam-hfov` defaults to a laptop's 70), and a person watching the sim follow them has no way to tell from the frames alone which camera is watching.
- **Format.** The camera is asked for the stream's own 1280×720 first, and takes it when it has it: no crop, no scale, its whole landscape view. A camera that does not offer that mode never negotiates it, so a second pipeline asks only that the frames live in system memory (a bare `video/x-raw`, which rules out the GPU-memory frames a macOS camera offers first — the crop cannot take those, and with an explicit device the pipeline will not even link) and lets the camera pick; that mode is then centre-cropped to the stream's 16:9 and scaled to 1280×720 with square pixels (`aspectratiocrop` → `videoscale`, the conversion to RGB last so it runs at the smallest size). The two pipelines are otherwise identical — the crop and the scale are no-ops at the stream's size — and a camera falling back to the second is ordinary, not a fault to report. Every camera therefore feeds the sim: a 720p-capable one exactly as it sees, a 4:3, 3:2 or 3840×2592 sensor keeping its full width and losing the top and bottom of its view, one wider than 16:9 keeping its full height. Frames are relayed as captured, not mirrored — what a camera looking at the person sees, which is what the robot's own camera would see.
- **Preference cannot be a caps list.** Negotiation ignores an ordered set of caps and a bounded range alike (a camera offering 3840×2592 and 1920×1080 picks the larger either way), so "this size if you have it" is two pipelines tried in order, not one filter.
- **Field of view.** `--webcam-hfov` is the camera's own horizontal field of view; the frame's is narrower only for a source wider than 16:9 (the crop keeps the fraction `r = min(1, (16/9) / (width/height))` of the source's width, giving `2·atan(tan(hfov/2)·r)`). The relay reads the resolution its pipeline negotiated and logs it as soon as frames flow. The bridge's tracker aims with the configured `hfov_deg` ([user_perception.md](../vision/user_perception.md) open question 3 covers the cropped case).
- **No render in webcam mode.** Under the viewer, the render thread is not started, so exactly one sender feeds the stream; the viewer window still shows the robot.
- **Capture errors don't stop the daemon.** A source that cannot start, or delivers no frame within 5 s, logs one `ERROR` naming the device and — on macOS — the likely cause (camera permission for the terminal or editor that launched the daemon); the daemon keeps running with no camera (`get_camera_frame()` returns `None`, the tracker sees nothing), and the relay retries every 5 s so granting the permission or plugging the camera in recovers without a restart.
- `--webcam-device` and `--webcam-hfov` are accepted only with `--camera webcam` (an argument error otherwise). `--webcam-hfov` is a number strictly between 1 and 179, default **70** — typical of a laptop or USB webcam; aim precision follows how well it matches the real camera (open question 1). A webcam is a **fixed** camera — it does not turn with the head — which is the bridge tracker's business ([head_tracking.md](../motion/head_tracking.md): the ray is rotated by the neutral pose, so the head eases to where the person is and holds), not the daemon's.

### Viewer overlay

`--sim-display camera_overlay` — `daemon.sim_displays.camera_overlay` in a config ([config.md](../core/config.md)) — draws the camera stream as a picture in the top-right corner of the MuJoCo viewer window: the webcam's frames with `--camera webcam`, the rendered eye camera's with `sim`. It is a display, not a camera: the stream clients and the tracker read is untouched; and headless there is no window to draw on, so the flag is an argument error with `--headless` (the config rejects the field with `headless: true` the same way). `sim_displays` is the home of every viewer display, `--sim-display <name>` its flag, repeatable; the camera overlay is the first.

- **How it is drawn.** MuJoCo's passive viewer draws images over its 3D view on every frame through `viewer.Handle.set_images` (MuJoCo 3.3.1+ — why the bridge's `sim` extra requires 3.3.x itself, [project.md](../project.md)): an RGB `uint8` image of exactly a rectangle's size, top-down (the binding flips it for OpenGL), copied on the call, drawn until replaced or cleared. The rectangle (`overlay_rect`) is 16:9, a quarter of the 3D view's width (`OVERLAY_FRACTION`), both sides even, inset by 2 % of the view's width (`OVERLAY_MARGIN`) from its top-right corner. It is recomputed from the handle's `viewport` — framebuffer pixels, twice the window's points on a Retina display — for every frame, so a resize keeps the picture in its corner; a view whose picture would have a side under 32 px draws nothing. The picture carries a 2 px light frame, so it stands out from a scene of its own colours (the eye camera's view of the empty scene is the viewer's own skybox and floor). One line of text at the view's top left (`set_texts`) names the camera — the webcam's name and negotiated size, the words the relay logs, or `eye camera 1280x720`. The overlay says what it does at `INFO` in the daemon's log — attached to the viewer, fed by the eye camera render, the first frame drawn and where — and a draw that raises is one `WARNING` with the traceback, then quiet.
- **Threads.** `set_images` waits for the viewer's render thread (up to one UI frame), so it never runs on the physics loop or a feed thread: frames go through `ViewerOverlay.show` into a latest-frame slot, and the overlay's own thread resamples the latest to the rectangle (`resample_nearest`, nearest neighbour — the daemon has no OpenCV) and draws it. A feed faster than the viewer only ever loses intermediate frames.
- **Where the frames come from.** *Webcam:* a `tee` after the relay pipeline's RGB 1280×720 caps adds a second branch — a leaky one-buffer queue, `videoscale` to 640×360 (`OVERLAY_SOURCE_SIZE`), a horizontal `videoflip`, an `appsink` named `overlay` — whose samples are copied into arrays and shown; the stream branch is the same as without the overlay and never waits on it. The picture is **mirrored**, as a person in front of a camera expects to see themselves; the flip lives in the overlay branch alone, so the stream and the tracker keep what the camera sees (the "not mirrored" of "Camera sources" still holds for them). The rendered eye camera's picture is not mirrored: it is the scene, which the viewer shows beside it. *Rendered eye camera:* upstream's `rendering_loop` gets its offscreen renderer from `_get_renderer`, which the corrected backend wraps so every rendered frame is also shown (resampled to 640×360) on the render thread, before it goes to the stream.
- **The viewer handle.** Upstream's `run()` keeps the handle it launches as a local and closes it itself at the end. While `run()` runs with the overlay on, `mujoco.viewer.launch_passive` (looked up on the module at call time) is substituted by a wrapper that hands the handle to the overlay and binds the handle's `close` to stop the overlay first — a draw issued after the close could wait on a render thread that is gone. The original is restored when `run()` returns, also on an exception; a process-local substitution, gone the day upstream keeps the handle on `self`.
- **Degrade rule.** A handle without `set_images` — a MuJoCo before 3.3.1, which a downstream installing `reachy-mini[mujoco]` alongside the bridge would get — is one `WARNING` naming the installed version, and no picture; nothing else changes. Nothing in the bridge depends on the MuJoCo version at runtime.

### Testable without a daemon

The backend subclass, the argv handling and the overlay are pure or in-process, so the deterministic `tests/` tier pins them (skipping without the `sim` extra, like the scene tests):

- **Hooks.** `on_backend` runs once the model exists, `on_app` on the built app; the subclass leaves the control loop's methods as upstream's.
- **Any camera feeds the stream.** The capture pipeline crops and scales into 1280×720 rather than constraining the source; the relay reports the resolution it negotiated.
- **Launcher argv.** Flag rewriting and passthrough; the webcam flags rejected without `--camera webcam`; `--sim-display` rejected with `--headless` and reaching the backend as its displays; the render thread not started and the relay started in webcam mode (the capture pipeline behind a seam).
- **The viewer overlay.** `overlay_rect` places the picture (16:9, even sides, the corner, the margin, `None` on a view too small); `resample_nearest` keeps the size and the corners; the relay description with the overlay has the `tee` branch and an unchanged stream branch, and without it is today's string; on a fake viewer handle the overlay draws the latest frame at the rectangle's size, skips to the latest frame while the viewer is busy, follows a resized viewport, labels, clears on stop, and warns once on a handle without `set_images`; the backend subclass taps the renderer in `sim` mode and hands the overlay to the relay in `webcam` mode; the run hands the viewer to the overlay and stops it before the close, restoring the launch function even when the run raises.

The sim's faces are the bridge's to detect and the head's convergence on them the bridge tracker's to prove ([user_perception.md](../vision/user_perception.md), [head_tracking.md](../motion/head_tracking.md)): the tracker's fast tests project the test scene's face through the sim camera's pinhole and step the tracker and the gaze layer offline, and the live check is the attention / gaze tests of `tests-e2e/test_bridge.py` on the viewer sim — the shipped detector on the rendered eye camera — plus the manual check of the viewer sim with a webcam and a person in front of it.

## Relationship to the other specs

- **[daemon.md](daemon.md):** `launch_command` builds every `sim` recipe on this launcher (the test scene's for a `.xml` scene), forwarding the camera flags from `DaemonConfig.camera` and a `--sim-display` per display on in `DaemonConfig.sim_displays`.
- **[config.md](../core/config.md):** `daemon.camera` (`source`, `device`, `hfov_deg`) configures the camera source; `daemon.sim_displays` (`camera_overlay`) the viewer displays.
- **[sim_scene.md](../testing/sim_scene.md):** the test scene is a `SimDaemonExtension`; its face tests rely on the rendered camera stream for detection and on the bridge's tracker for the aim.
- **[user_perception.md](../vision/user_perception.md):** the bridge's detector runs on the camera stream the launcher feeds — rendered under the viewer, or a host webcam.
- **[head_tracking.md](../motion/head_tracking.md):** the tracker owns the sim camera's intrinsics and the fixed-camera geometry for a webcam, and the pinhole helpers.
- **[testing.md](../testing/testing.md) / [testing_support.md](../testing/testing_support.md):** the harness spawns through `daemon.py`, so the e2e tier gets the corrected sim with no change of its own.

## Open questions

1. **Webcam calibration.** `hfov` with an ideal pinhole is good enough to follow a person by eye; a calibrated matrix and distortion (upstream ships a calibration tool for the robot's camera) would make the aim precise. Deferred until a manual test needs better than a few degrees.
2. **The intrinsics clients see.** `GET /api/camera/specs` and the SDK's `media.camera.K` still report upstream's `MujocoCameraSpecs.K`, which is wrong for the rendered camera (and for a webcam); a client computing geometry from frames — upstream's `look_at_image` — inherits the error, which is why the bridge's tracker carries its own pinhole for the sim ([head_tracking.md](../motion/head_tracking.md)). Correcting what the daemon reports is deferred until the bridge ships a gaze-from-pixels verb ([bridge.md](../core/bridge.md) deferred `look_at_image`), and belongs in the same upstream fix.
3. **The cropped-away view.** A source narrower than 16:9 loses the top and bottom of its view to the crop (17% of a 3840×2592 sensor's height, 25% of a 4:3 one's), which costs the tracker faces high or low in the camera's field. Keeping them would mean letterboxing into the frame, which GStreamer does not do at a fixed output size — `videoscale add-borders` stretches and flags a non-square pixel aspect instead of padding, and `videobox` with offsets computed from the negotiated caps is dynamic pipeline surgery ([the plan](../../plans/202609172245_webcam-relay-any-camera-and-daemon-output.md) measures both). Deferred until a camera's lost band actually costs a detection.
