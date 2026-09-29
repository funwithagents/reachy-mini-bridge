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

Every hardware daemon the bridge starts — a Reachy Mini Lite on this machine's USB, `backend: "real"` with `daemon.spawn` ([daemon.md](daemon.md)) — runs through the bridge's own launcher, `python -m reachy_mini_bridge.real_daemon`: upstream's `reachy-mini-daemon`, unchanged in everything but one correction that makes the **robot's camera open reliably on macOS**.

Upstream's media server (SDK 1.10 and 1.11) finds the robot's camera once, at daemon construction, as its position in GStreamer's device monitor list, and opens it with `avfvideosrc device-index=N`. The order that index reads is AVFoundation's, and it is not stable: measured on a Mac with the robot and the built-in camera attached, consecutive opens of the same index in one process alternated between the two (the measurements are in [../docs/upstream-macos-camera-device-index.md](../docs/upstream-macos-camera-device-index.md)). Each build of the media pipeline is therefore a draw, and the wrong pick opens the computer's camera under the robot camera's caps (1920×1080@60), which fail to negotiate: the daemon logs `Internal data stream error … GstAVFVideoSrc` / `streaming stopped, reason not-negotiated`, audio keeps working, the IPC socket exists, and no client ever gets a frame — `get_camera_frame()` stays `None` and face tracking is blind. A daemon that "sometimes does not start the camera" is this draw lost.

The launcher corrects it inside the daemon process, so the bridge — the api, the control panel, the e2e tier — sees a hardware daemon whose camera is the robot's. The correction belongs upstream ([../docs/upstream-macos-camera-device-index.md](../docs/upstream-macos-camera-device-index.md) is the draft); the launcher carries it until it lands there, and it is one removable piece, the same shape as the sim launcher's corrections ([sim_daemon.md](sim_daemon.md)).

## Core concepts / Decided

### The launcher

```
python -m reachy_mini_bridge.real_daemon [--[no-]preload-datasets] [upstream flags…]
```

`run_real_daemon(argv=None)` installs the camera check (below), rewrites `sys.argv` to `--[no-]preload-datasets` plus any unrecognised flags forwarded verbatim — `--kinematics-engine Placo`, which [daemon.md](daemon.md)'s launch command adds when `placo` is importable, travels this way — and calls upstream's `main()`. Everything else — serial-port detection, the wake-up on start and the sleep on stop, the FastAPI app, the media server, readiness, shutdown — is upstream's. No `--sim`: the launcher is the hardware recipe.

It runs in the bridge's own interpreter (`<this interpreter> -m reachy_mini_bridge.real_daemon`, [daemon.md](daemon.md) "The launch command"), so unlike the sim recipes it needs no launcher on `PATH`: `reachy_mini` is a base dependency. A daemon started by hand with upstream's `reachy-mini-daemon` has none of this; for manual work, start `python -m reachy_mini_bridge.real_daemon` (or let a `ReachyMiniApi` config with `daemon.spawn` do it) and the bridge borrows it like any other.

### The camera check

`install_macos_camera_check()` wraps upstream's `GstMediaServer.start` (idempotent: a second install is a no-op). The wrapped start runs upstream's start — which builds the pipeline from scratch and sets it `PLAYING` — and then, on macOS and only when the media server's camera path is a device index (the sim's `use_sim` and a missing camera's `""` are left alone), reads which device the pipeline's `avfvideosrc` actually opened: its read-only `device-name` property, polled for up to 3 s. The check applies to every start, the daemon's first and each `acquire_media` restart alike.

`select_camera(opened, restart, *, first, count, expected, attempts)` is the pure decision, tested with scripted names:

- a device whose name contains one of the robot camera names (`ROBOT_CAMERA_NAMES` = `("Reachy", "Arducam_12MP", "imx708")`, the names upstream's detection matches — a test pins the parity with `device_detection.DEFAULT_CAM_NAMES`) is kept: the check returns `True` and the pipeline runs on;
- a device that reports no name within the timeout is accepted too, so a camera that names nothing is never retried into the ground;
- any other device (`Caméra du MacBook Pro`, say) is the wrong pick: the pipeline is set to `NULL`, the media server's camera path moves to the next index — round-robin over the video sources the device monitor lists (at least `first + 1`) — and upstream's start runs again, at most 6 builds in all, the first included. The wrong pick is a warning naming both indices and the device; a success after a rebuild is an info line with the build count.
- six wrong picks leave the last build running (audio, WebRTC, the IPC socket all work; only video is missing) and log an error saying so.

Round-robin rather than a retry on the same index: a retry alone is enough for an order that moves between opens, and the rotation also covers an order that is stable but offset from the monitor's. The check adds nothing to a build that opened the right camera, which is the common case on a Mac with no second camera. Off macOS (`avfvideosrc` is macOS-only), or with a non-numeric camera path, the wrapped start is upstream's start and nothing more.

### What it is not

The launcher does not touch readiness, which is the bridge's side of the same failure: [daemon.md](daemon.md)'s probe reads `GET /api/daemon/status` precisely so that it never makes the daemon rebuild its media pipeline — an SDK client built with `media_backend="no_media"` (upstream 1.10 / 1.11) does, and every rebuild was one more draw.

## Relationship to the other specs

- **[daemon.md](daemon.md):** the `real` launch recipe runs this launcher; the readiness probe is side-effect-free for the reason above.
- **[sim_daemon.md](sim_daemon.md):** the same pattern — a module of ours that patches upstream in-process and calls its `main()` — for the MuJoCo daemon; the two launchers share nothing but the shape, because their corrections do not overlap.
- **[api.md](api.md):** the camera feed (`api.camera`, [camera.md](camera.md)) on a real robot depends on the daemon having opened the robot's camera; with the check it does.

## Open questions

1. **A stable selector instead of a check.** GStreamer's main branch gives `avfvideosrc` a `unique-id` property (the provider already reports `avf.unique_id`) and deprecates `device-index`; the bundled GStreamer (1.28.6) has only the index. When the bundle ships `unique-id`, the correct fix — upstream's, ideally — is to open the camera by its unique id and drop the check. Until then the check stands.
2. **The sim's webcam source.** `--camera webcam` on the sim launcher also selects a macOS camera by `avfvideosrc device-index` ([sim_daemon.md](sim_daemon.md)), in the bridge's own relay pipeline rather than upstream's media server. A user who names a device index there is exposed to the same draw; the relay's device-name check would be the same idea in a different pipeline. Deferred until it bites.
