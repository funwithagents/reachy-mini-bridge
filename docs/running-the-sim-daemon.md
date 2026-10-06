# Running a Reachy Mini daemon (for e2e / dev)

How to bring up a `reachy_mini` daemon — for the e2e tests, or for developing against a live daemon. Like [reachy-mini-api.md](reachy-mini-api.md), this is a reference note about the upstream SDK, not a spec. It's the operational companion to the e2e **strategy** in [../specs/testing/testing.md](../specs/testing/testing.md) ("E2E targets & capabilities"), which owns the capability matrix; this file records the concrete launch recipes and *why* they work.

## Launch modes

The bridge implements these recipes in `reachy_mini_bridge.daemon` (spec:
[../specs/daemon/daemon.md](../specs/daemon/daemon.md)): a `ReachyMiniConfig` with `"backend": "sim"`
and `"daemon": {"spawn": "auto"}` makes `ReachyMiniBridge` spawn the headless daemon below
(or the viewer with `"headless": false`), wait for readiness, and stop it on exit — and the
e2e harness uses the same code. `"backend": "real"` with the same `daemon` block does the
same for a robot plugged into this machine over USB (see "Real robot" below). The commands
here are what it runs, for when you want to start a daemon by hand.

**Every sim the bridge starts runs through its own launcher**, `python -m
reachy_mini_bridge.sim_daemon` ([../specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md)):
upstream's daemon plus a choice of camera source (the rendered eye camera, or your webcam)
and the viewer's camera overlay. It takes upstream's flags. Faces are detected by the
bridge itself, on the host, from the camera stream the daemon serves — the daemon's own
face tracking is left as upstream ships it and never armed — so upstream's
`reachy-mini-daemon --sim` below works for the bridge too; the launcher is what adds the
webcam and the overlay.

### Headless sim — CI (motion + audio; the camera too on Linux)

Real MuJoCo physics, no viewer, no display — runs anywhere:

```
uv run python -m reachy_mini_bridge.sim_daemon --headless --preload-datasets
# upstream alone, without the webcam source and the overlay:
reachy-mini-daemon --sim --headless --preload-datasets
```

- Serves `http://127.0.0.1:8000` in ~1s (`--fastapi-port` moves it; a daemon the bridge spawns is bound to the config's `robot.host:port` the same way, so two sims on one machine take two ports). Add `--no-media` for a pure **motion** daemon (no camera/audio) — the lightest option for motion-only work; the e2e harness spawns media-on so it can probe audio.
- **Media on** (omit `--no-media`) brings up **audio**: the daemon falls back to the host's default mic/speaker and enables **software AEC** (`No hardware AEC; enabled software echo cancellation`). The macOS `libgstpython.dylib` GStreamer warning is harmless.
- **The rendered camera works headless on Linux, not on macOS.** Upstream starts the eye-camera render only under the viewer; the bridge's launcher starts it itself for a headless run wherever MuJoCo can draw without a display — on Linux, through Mesa's EGL (`MUJOCO_GL` defaulted to `egl`; `osmesa` works too if the environment names it; the `libegl1` / `libgl1-mesa-dri` packages, or `libosmesa6`), so a Linux headless sim serves frames and the face tests run on a CI runner ([specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md) "The headless camera"). On macOS the only GL context is the window server's, so `get_frame()` returns `None` headless: use the viewer mode for the camera there — or a webcam (below), which works headless on either. A render context that cannot be created logs one `ERROR` and the daemon runs on without a camera.

### Headfull / viewer sim — local (adds camera, watchable)

Drop `--headless` to open the MuJoCo viewer. The viewer supplies a **GL context** (so `get_frame()` works on every platform) and lets you watch the sim as a robot stand-in. It needs an **interactive GUI session**; on **macOS** it must run under `mjpython` (on Linux the plain interpreter opens it, and the bridge's launch command uses that):

```
mjpython -m reachy_mini_bridge.sim_daemon --scene minimal --preload-datasets
# upstream alone, without the webcam source and the overlay:
mjpython -m reachy_mini.daemon.app.main --sim --scene minimal --preload-datasets
```

From a real Terminal (your GUI session) this opens the window. From a **background/agent/CI** process tree it **segfaults (exit 139)** at window creation — the viewer only opens inside a GUI (Aqua) session.

> **The screen must be unlocked.** Even inside your GUI session, a **locked screen** (or a display asleep) denies the window server a GL context, so the viewer daemon either **hangs** (produces no output → the e2e harness times out) or **segfaults** (`exit -11` in the harness). This is the main source of "flaky" `REACHY_MINI_E2E_SIM_VIEWER=1` runs: unlock the screen and re-run. The headless target has no such requirement — it exercises the same bridge/audio/motion paths without a display (only the camera needs the viewer's GL context).

### The MuJoCo version

Upstream's `mujoco` extra is one requirement, `mujoco==3.3.0` (February 2025). The bridge's `sim` extra does not go through it: it requires `mujoco>=3.3.1,<3.4` itself, so anything that installs `reachy-mini-bridge[sim]` gets the newest 3.3.x patch release with nothing to add on its side, and upstream's pin never enters the resolution. 3.3.1 is the floor because it gave the passive viewer image and text overlays (`set_images` / `set_texts`), which the launcher needs to draw the camera stream over the scene; `<3.4` keeps to patch releases of the version upstream tests on (the daemon also runs unchanged on 3.14.0 — measured for a report asking upstream for a range, drafted and to be filed).

Do not install `reachy-mini[mujoco]` (or `[all]`) next to the bridge: that brings the pin back and the resolver fails on the conflict. (Unrelated to MuJoCo: a project's lock on macOS also needs the `dependency-metadata` entries for `pygobject` / `pycairo` that the bridge's `pyproject.toml` carries, or uv tries to build them from source.)

To launch it from a non-GUI shell while you're logged in graphically:

```
launchctl asuser $(id -u) \
  <venv>/bin/mjpython -m reachy_mini_bridge.sim_daemon --scene minimal --preload-datasets
```

**See what the robot sees.** `--sim-display camera_overlay` draws the camera stream in the top-right corner of the viewer window — the rendered eye camera, or the webcam with `--camera webcam`, mirrored so you see yourself as in a mirror (the stream itself stays as the camera sees) — with the camera's name at the top left. In a config: `"daemon": {"headless": false, "sim_displays": {"camera_overlay": true}}` (the example config has it on). Viewer only: with `--headless` it is an argument error, and the config refuses it with `headless: true`. It needs the MuJoCo the `sim` extra installs (3.3.1 or later, "The MuJoCo version" above); an older one gets one warning and no picture.

**See where it looks and where it places the faces.** Two more sim displays draw in the viewer's 3D scene (spec: [specs/daemon/sim_displays.md](../specs/daemon/sim_displays.md)):

- `--sim-display robot_gaze` draws a blue line along the eye camera's optical axis: where the robot is looking. It is the axis the head tracker aligns with the face it follows, and it turns with the simulated head whatever the camera source.
- `--sim-display face_markers` draws a flat ellipsoid for each face the bridge detects, where the bridge places it in space: green for the face the head follows, yellow for the others, labelled with the face's track id. A bridge session whose config has `sim_displays.face_markers` on sends them to the daemon (`PUT /api/sim/displays/face_markers`); `curl localhost:8000/api/sim/displays/face_markers` shows what the daemon holds. A marker's direction is the face's pixel; its distance is estimated from the face's size, so it is approximate: one hard-coded face height, calibrated on the test scene's portrait, places every face, and a person in front of a webcam is drawn about a third nearer than they stand.

In a config: `"sim_displays": {"camera_overlay": true, "robot_gaze": true, "face_markers": true}`. The flags repeat: `--sim-display robot_gaze --sim-display face_markers`. Both are viewer only, like the overlay. They are drawn for the viewer alone: the robot's camera, the detector and the camera overlay never see them. Once tracking has settled, the gaze line passes through the followed face's marker.

### A face in the sim (viewer + scene file)

Upstream's scenes ship nothing to look at. The bridge's shipped testing package can write a **test scene** — hidden-by-default props, a portrait plane today — in front of the robot and run the daemon on it through its own launcher, which lets you show, place, move and hide the props while the daemon runs ([../specs/testing/sim_scene.md](../specs/testing/sim_scene.md)):

```python
from reachy_mini_bridge.testing.sim_scene import write_test_scene

write_test_scene("/tmp/scene")  # -> /tmp/scene/scene.xml (the face starts hidden)
```

```
mjpython -m reachy_mini_bridge.testing.sim_scene --scene-path /tmp/scene/scene.xml --preload-datasets
```

It is the sim daemon launcher with the scene added; a bridge configured with the `yunet` detector then finds the face in the rendered camera and the head converges on it. Then, from any process: `SimSceneClient().show("face_1")` to bring it into view, `.place("face_1", (0.45, 0.15, 0.20), duration=1.5)` to move it, `.hide("face_1")` to take it away again — or `curl -X POST localhost:8000/api/sim/inject/bodies/face_1 -H 'Content-Type: application/json' -d '{"visible": true}'`. A `ReachyMiniConfig` whose `daemon.scene` is that `.xml` path does the launch for you (`"headless": false` on macOS, where only the viewer has a camera to see the face with; on Linux a headless daemon renders it offscreen). The e2e harness runs every sim it spawns on this scene.

### You in front of the sim (a webcam as the camera)

For manual tests of face-driven behaviour, the sim can see through the computer's webcam instead of its rendered eye camera ([../specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md) "Camera sources"): the person in front of the screen is who the simulated robot detects and follows, with or without the viewer.

```
mjpython -m reachy_mini_bridge.sim_daemon --camera webcam [--webcam-device 1] [--webcam-hfov 70] [--sim-display camera_overlay]
uv run python -m reachy_mini_bridge.sim_daemon --headless --camera webcam
```

or, from a config, `"daemon": {"spawn": "auto", "headless": false, "camera": {"source": "webcam"}}` (the control panel takes such a config). The webcam is treated as a fixed camera at the robot's resting eye: step aside and the head turns by the angle the webcam sees you at, stand still and it holds. `--webcam-hfov` is the camera's horizontal field of view (70° default, typical of a laptop camera) — set it to your camera's for an accurate aim. `--webcam-device` is an index on macOS, a `/dev/videoN` path on Linux; omitted, the default camera.

On macOS the app that launched the daemon (your terminal, or VS Code) needs **Camera** access in System Settings → Privacy & Security. Without it the daemon keeps running, logs `webcam relay (...): no frame ...` once with that hint, and retries every 5 s — granting the permission brings frames back without a restart. The test scene's portrait is invisible to a webcam; the automated face tests use the default rendered camera.

### Real robot

A wireless robot runs its own daemon — nothing to start. Point the client at it (`connection_mode="network"`, the robot's host, `media_backend` left at upstream's default: a network client streams the camera and the audio over WebRTC). Upstream serves everything there — **hardware** AEC, camera and DoA included; the bridge itself is not validated on a wireless robot yet (the README's support status).

A robot plugged into this machine over USB (Reachy Mini Lite) needs the daemon running here — the bridge's launcher:

```
uv run python -m reachy_mini_bridge.real_daemon --kinematics-engine Placo --preload-datasets
```

It is upstream's `reachy-mini-daemon` run in-process with the bridge's macOS camera check ([../specs/daemon/real_daemon.md](../specs/daemon/real_daemon.md)): upstream opens the camera by a device index that moves between runs, and the launcher reads back which device opened and rebuilds the pipeline until it is the robot's camera; flags it does not know go to upstream unchanged. It finds the robot's serial port itself, wakes the robot on start and puts it to sleep on stop (about 8 s). Pass `--kinematics-engine Placo` only with `reachy-mini[placo_kinematics]` installed — gravity compensation needs it. A `ReachyMiniConfig` with `"backend": "real"` and `"daemon": {"spawn": "auto"}` runs exactly this for you. Running `reachy-mini-daemon` directly is a different route: the stock daemon, without the camera check.

## Linux

A daemon on a Linux machine — this sim, or a Lite plugged in over USB — needs the system GStreamer, the Rust webrtc plugin and, for the headless camera, Mesa: [linux.md](linux.md).

## Connecting the client

```python
from reachy_mini_bridge.robot import build_robot

with build_robot(
    "real",
    connection_mode="network",
    host="127.0.0.1",
    port=8000,
    media_backend="local",  # a daemon on this machine serves the IPC media path
) as robot:
    ...
```

(Through the bridge, these go in the config's `robot` block; when the bridge manages the
daemon itself it fills `connection_mode="network"`, `host`, `port`, and
`media_backend="local"` in for you — see [../specs/core/config.md](../specs/core/config.md).)

- **Connect over the network.** The default `auto`/`localhost` path uses an IPC transport an externally-started daemon doesn't serve.
- **For media (camera/audio), pass `media_backend="local"`** (same machine as the daemon). The default WebRTC path errors with `KeyError: 'Producer reachymini not found.'`.
- **Audio needs `media.start_recording()` first** — then `get_audio_sample()` yields **float32, stereo, 16 kHz** mic frames; the speaker is `push_audio_sample` / `play_sound`.

## Gotchas

- **Wait for *readiness*, not just the open port.** The daemon serves nothing until it has **woken** — keep the default autostart/wake (do *not* pass `--no-autostart` / `--no-wake-up-on-start`) — and once it serves, `GET /api/daemon/status` says whether the backend runs: confirm its `backend_status` is not `null`, since the endpoint (and `/ws/sdk`) answer even when the MuJoCo backend failed to start (e.g. no GL context). Probe with that plain GET, not with an SDK client built with `media_backend="no_media"`: in 1.10 / 1.11 such a client makes the daemon release and re-acquire its whole media pipeline (see [reachy-mini-api.md](reachy-mini-api.md) "Media release").
- **A lighter `--mockup-sim` mode exists** (kinematic mock, no physics) and also runs headless, but the bridge's e2e tier uses the real MuJoCo backend — the in-process `FakeReachyMini` already covers the mock level.
- **Spawning the daemon from a process that already imported `reachy_mini`?** Scrub the GStreamer-bundle env vars from the child's environment first (`GST_PLUGIN_PATH_1_0`, `GST_PLUGIN_SYSTEM_PATH_1_0`, `GST_REGISTRY_1_0`, `GST_PLUGIN_SCANNER_1_0`, `GI_TYPELIB_PATH`, `PYGI_DLL_DIRS`, `XDG_DATA_DIRS`, `XDG_CONFIG_DIRS`). `reachy_mini`'s `gstreamer_bundle.pth` **prepends** to these at every Python startup, so if the parent already set them the child's `.pth` doubles them (`scanner:scanner`) into a value GStreamer can't exec — the external plugin scanner then fails and the in-process fallback **segfaults on `libgstpython.dylib`** (exit 255). Scrubbing lets the child set fresh, correct values; upstream's own app launcher (`reachy_mini/apps/manager.py`) does exactly this, and so does the e2e harness. (Launched from a plain shell that never imported `reachy_mini`, the vars are unset and the warning really is harmless — as above.)
