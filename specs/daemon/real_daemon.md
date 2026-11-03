---
code:
  - src/reachy_mini_bridge/real_daemon.py
  - src/reachy_mini_bridge/daemon.py
tests:
  - tests/test_real_daemon.py
  - tests/test_daemon.py
---

# Real daemon launcher (`real_daemon.py`)

**Status:** Implemented

## Purpose

Every hardware daemon the bridge starts — a Reachy Mini Lite on this machine's USB, `backend: "real"` with `daemon.spawn` ([daemon.md](daemon.md)) — runs through the bridge's own launcher, `python -m reachy_mini_bridge.real_daemon`: upstream's `reachy-mini-daemon`, unchanged in everything but two corrections that make the **robot's camera open reliably and expose properly on macOS**.

Upstream's media server (SDK 1.10 and 1.11) finds the robot's camera once, at daemon construction, as its position in GStreamer's device monitor list, and opens it with `avfvideosrc device-index=N`. The order that index reads is AVFoundation's, and it is not stable: measured on a Mac with the robot and the built-in camera attached, consecutive opens of the same index in one process alternated between the two. Each build of the media pipeline is therefore a draw, and the wrong pick opens the computer's camera under the robot camera's caps (1920×1080@60), which fail to negotiate: the daemon logs `Internal data stream error … GstAVFVideoSrc` / `streaming stopped, reason not-negotiated`, audio keeps working, the IPC socket exists, and no client ever gets a frame — upstream's `get_frame()` returns nothing, `bridge.camera.latest()` stays `None`, the bridge's detector reads inactive and face tracking is blind. A daemon that "sometimes does not start the camera" is this draw lost.

And the camera it opens is dark. Upstream pins the Lite camera to its 1920×1080@60 mode, the one mode of that camera where the firmware's auto-exposure cannot reach its target in room light: the camera holds the frame rate the host asked for, so exposure is capped at one frame time, 16.6 ms — the value the camera reports for its exposure control — and the image comes out three to four times darker than the same scene in any of its 30 fps modes (measured on a Lite: a mean of 23 to 30 on 255 against 100 to 111, with auto-exposure visibly converging in the 30 fps modes and pinned from the first frame at 60). Upstream's troubleshooting page knows the image as "dark on the Lite version" and names the cure — the camera's UVC *auto-exposure priority* control set to 1, which lets the camera lower its frame rate to expose longer — through a camera-control application, because nothing in the daemon's GStreamer path reaches that control on macOS. Measured with the daemon's own pipeline streaming, the flag takes the same scene from 23 to 101 within three seconds, the frame rate easing from 55 to 49; the camera keeps the setting across pipeline rebuilds, until it is power-cycled.

The launcher corrects both inside the daemon process, so the bridge — the bridge, the control panel, the e2e tier — sees a hardware daemon whose camera is the robot's, exposed as the camera's own auto-exposure wants it. The corrections belong upstream (a report with the measurements is drafted, to be filed); the launcher carries them until they land there, and each is one removable piece, the same shape as the sim launcher's corrections ([sim_daemon.md](sim_daemon.md)).

## Core concepts / Decided

### The launcher

```
python -m reachy_mini_bridge.real_daemon [--[no-]preload-datasets] [upstream flags…]
```

`run_real_daemon(argv=None)` installs the camera check and the camera exposure (below), rewrites `sys.argv` to `--[no-]preload-datasets` plus any unrecognised flags forwarded verbatim — `--kinematics-engine <engine>`, which [daemon.md](daemon.md)'s launch command adds from the config's `daemon.kinematics_engine`, travels this way — and calls upstream's `main()`. Everything else — serial-port detection, the wake-up on start and the sleep on stop, the FastAPI app, the media server, readiness, shutdown — is upstream's. No `--sim`: the launcher is the hardware recipe.

It runs in the bridge's own interpreter (`<this interpreter> -m reachy_mini_bridge.real_daemon`, [daemon.md](daemon.md) "The launch command"), so unlike the sim recipes it needs no launcher on `PATH`: `reachy_mini` is a base dependency. A daemon started by hand with upstream's `reachy-mini-daemon` has none of this; for manual work, start `python -m reachy_mini_bridge.real_daemon` (or let a `ReachyMiniBridge` config with `daemon.spawn` do it) and the bridge borrows it like any other.

### The camera check

`install_macos_camera_check()` wraps upstream's `GstMediaServer.start` (idempotent: a second install is a no-op) with both corrections. The wrapped start runs upstream's start — which builds the pipeline from scratch and sets it `PLAYING` — and then, on macOS and only when the media server's camera path is a device index (the sim's `use_sim` and a missing camera's `""` are left alone), reads which device the pipeline's `avfvideosrc` actually opened: its read-only `device-name` property, polled for up to 3 s. The check applies to every start, the daemon's first and each `acquire_media` restart alike; the camera exposure (below) follows it on the same starts.

`select_camera(opened, restart, *, first, count, expected, attempts)` is the pure decision, tested with scripted names:

- a device whose name contains one of the robot camera names (`ROBOT_CAMERA_NAMES` = `("Reachy", "Arducam_12MP", "imx708")`, the names upstream's detection matches — a test pins the parity with `device_detection.DEFAULT_CAM_NAMES`) is kept: the check returns `True` and the pipeline runs on;
- a device that reports no name within the timeout is accepted too, so a camera that names nothing is never retried into the ground;
- any other device (`Caméra du MacBook Pro`, say) is the wrong pick: the pipeline is set to `NULL`, the media server's camera path moves to the next index — round-robin over the video sources the device monitor lists (at least `first + 1`) — and upstream's start runs again, at most 6 builds in all, the first included. The wrong pick is a warning naming both indices and the device; a success after a rebuild is an info line with the build count.
- six wrong picks leave the last build running (audio, WebRTC, the IPC socket all work; only video is missing) and log an error saying so.

Round-robin rather than a retry on the same index: a retry alone is enough for an order that moves between opens, and the rotation also covers an order that is stable but offset from the monitor's. The check adds nothing to a build that opened the right camera, which is the common case on a Mac with no second camera. Off macOS (`avfvideosrc` is macOS-only), or with a non-numeric camera path, the wrapped start is upstream's start and nothing more.

### The camera exposure

After the check, on the same macOS starts, the wrapped start sets the Lite camera's UVC auto-exposure priority to 1 — one USB control request to the camera, beside the pipeline rather than through it, since GStreamer's `avfvideosrc` exposes no camera control. The request goes over the device's endpoint zero and needs no claim on the camera's USB interface, which macOS's camera driver holds while it streams: that is what makes the request possible from the daemon process with the camera open.

- **The camera, by its USB identity.** The Lite camera is found by its vendor and product ids (`LITE_CAMERA_USB_ID` = `(0x38FB, 0x1002)`, upstream's `ReachyMiniLiteCamSpecs.vid` / `.pid` — a test pins the parity), through pyusb on the libusb that `libusb_package` bundles, both dependencies of upstream's own (its audio control utilities drive the sound card the same way): no dependency of the bridge's. A Mac with no Lite camera attached — nothing happens. The correction is therefore the Lite's alone by construction, and macOS's by the platform guard it shares with the check.
- **The terminal, by its descriptors.** `camera_terminal(descriptors)` is the pure parse of the camera's VideoControl interface's class-specific descriptors: the camera input terminal (`CS_INTERFACE` / `VC_INPUT_TERMINAL` of type `ITT_CAMERA`) gives its id, which addresses the request, and its `bmControls` bitmap whether it advertises the priority control (`CameraTerminal(terminal_id, ae_priority)`); a malformed block, or no camera terminal, is `None`.
- **The decision.** `enable_ae_priority(control, terminal)` reads the control (`GET_CUR`) and acts on what it finds, with one log line each — tested with a scripted control: a terminal that does not advertise the control is left alone, a warning naming the dark default (`unsupported`); a flag already at 1 — a camera kept from an earlier daemon, or set by hand — is one `DEBUG` line and no write (`already`); a flag at 0 is set to 1 (`SET_CUR`) and read back, an `INFO` line saying the camera may now lower its frame rate to expose longer (`enabled`); a request the camera or the bus refuses, or a read-back that is not 1, is a warning and the daemon runs on with the image as upstream leaves it (`refused`).
- **Nothing stops the daemon.** A missing USB backend, a camera that cannot be opened over USB, any error on the way is one warning and the dark image: video, audio, the IPC socket all work as before. The flag is not restored when the daemon stops — it is the camera's auto-exposure doing what every other application lets it do, and the next daemon reads it as `already`.
- **What it changes for clients.** Nothing in shape: the stream stays 1920×1080 in the 60 fps mode upstream chose, so the frame size, the IPC branch, the calibration the bridge's tracker scales from the default resolution ([head_tracking.md](../motion/head_tracking.md)) are all untouched. The frame rate may ease below 60 when the light asks for it, down to the 5 fps interval the camera advertises for its 1080p mode in a dark room; the IPC branch every local client reads is capped at 10 fps anyway, so the bridge's camera feed and detector see the difference only in a room that dark. A WebRTC viewer sees a variable rate.

Off macOS the wrapped start is upstream's start: a Linux daemon opens the same 1080p60 mode and its users have `v4l2-ctl` for the control (open question 3).

### The report

The launcher serves `GET /api/bridge/daemon` on every run, through a `create_app` wrap: `{"launcher": "real"}` ([daemon.md](daemon.md) "What the daemon reports"). The engine a session needs comes from upstream's own route.

### What it is not

The launcher does not touch readiness, which is the bridge's side of the same failure: [daemon.md](daemon.md)'s probe reads `GET /api/daemon/status` precisely so that it never makes the daemon rebuild its media pipeline — an SDK client built with `media_backend="no_media"` (upstream 1.10 / 1.11) does, and every rebuild was one more draw.

Nor does it choose the camera's mode. Capturing a 30 fps mode and scaling it into the stream would brighten the image as much, at the cost of a pipeline change, a frame-rate change for every client and an intrinsics question; the priority flag is upstream's own documented cure and leaves the mode as upstream chose it.

### Testable without a daemon

The deterministic `tests/` tier pins, with no robot, no USB and no pipeline:

- **The parse.** `camera_terminal` on hand-built descriptor bytes: a camera terminal advertising the control, one not advertising it, a VideoControl interface whose only input terminal is not a camera, a block of zero length — and the USB ids equal to upstream's Lite specs.
- **The decision.** `enable_ae_priority` with a scripted control: each outcome, the requests made (none for `unsupported` and `already`, a `SET_CUR` of one byte 1 then a `GET_CUR` for `enabled`), the levels logged, and a refusal — a raised `OSError`, a read-back that stays 0 — ending in a warning with the control left as it is.
- **The wiring.** The wrapped start runs the exposure step after the selection on a macOS start that opened a camera by index, with the camera the finder returns; not at all off macOS or for a non-numeric path; and a finder that raises, or finds no Lite camera, leaves one warning or nothing and the start complete.

## Relationship to the other specs

- **[daemon.md](daemon.md):** the `real` launch recipe runs this launcher; the readiness probe is side-effect-free for the reason above.
- **[sim_daemon.md](sim_daemon.md):** the same pattern — a module of ours that patches upstream in-process and calls its `main()` — for the MuJoCo daemon; the two launchers share nothing but the shape, because their corrections do not overlap.
- **[bridge.md](../core/bridge.md):** the camera feed (`bridge.camera`, [camera.md](../vision/camera.md)) on a real robot depends on the daemon having opened the robot's camera; with the check it does, and with the exposure correction its frames are lit as the camera's auto-exposure wants them — the detector's business ([user_perception.md](../vision/user_perception.md)) as much as a viewer's.
- **[config.md](../core/config.md):** no field. Both corrections make the camera do what every user expects of it; the one trade-off, a frame rate that can ease in a dark room, is invisible to the bridge's own consumers. An opt-out is the moment a WebRTC viewer needs a constant 60 fps (open question 4).

## Open questions

1. **A stable selector instead of a check.** GStreamer's main branch gives `avfvideosrc` a `unique-id` property (the provider already reports `avf.unique_id`) and deprecates `device-index`; the bundled GStreamer (1.28.6) has only the index. When the bundle ships `unique-id`, the correct fix — upstream's, ideally — is to open the camera by its unique id and drop the check. Until then the check stands.
2. **The sim's webcam source.** `--camera webcam` on the sim launcher also selects a macOS camera by `avfvideosrc device-index` ([sim_daemon.md](sim_daemon.md)), in the bridge's own relay pipeline rather than upstream's media server. A user who names a device index there is exposed to the same draw; the relay's device-name check would be the same idea in a different pipeline. Deferred until it bites.
3. **The exposure on Linux and Windows.** Both open the same 1080p60 mode, so the same darkness; Linux's `uvcvideo` driver exposes the control to `v4l2-ctl` (upstream's documented fix there) and to the same USB request through libusb, given udev permissions the bridge cannot assume. Deferred until a Linux user of a Lite asks.
4. **An opt-out.** A `daemon` field turning the exposure correction off, for a WebRTC viewer that wants a constant 60 fps over a bright image. Deferred until asked for; the correction is one call behind the platform guard, so the switch is cheap to add.
