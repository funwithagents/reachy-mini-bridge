# Running a Reachy Mini daemon (for e2e / dev)

How to bring up a `reachy_mini` daemon — for the e2e tests, or for developing against a live daemon. Like [reachy-mini-api.md](reachy-mini-api.md), this is a reference note about the upstream SDK, not a spec. It's the operational companion to the e2e **strategy** in [../specs/testing.md](../specs/testing.md) ("E2E targets & capabilities"), which owns the capability matrix; this file records the concrete launch recipes and *why* they work.

## Launch modes

The bridge implements these recipes in `reachy_mini_bridge.daemon` (spec:
[../specs/daemon.md](../specs/daemon.md)): a `ReachyMiniConfig` with `"backend": "sim"`
and `"daemon": {"spawn": "auto"}` makes `ReachyMiniApi` spawn the headless daemon below
(or the viewer with `"headless": false`), wait for readiness, and stop it on exit — and the
e2e harness uses the same code. `"backend": "real"` with the same `daemon` block does the
same for a robot plugged into this machine over USB (see "Real robot" below). The commands
here are what it runs, for when you want to start a daemon by hand.

**Every sim the bridge starts runs through its own launcher**, `python -m
reachy_mini_bridge.sim_daemon` ([../specs/sim_daemon.md](../specs/sim_daemon.md)):
upstream's daemon plus the corrections that make face tracking work in the sim (upstream's
MuJoCo loop never steps tracking, and its tracker's intrinsics put the head ~45° off the
face — [upstream-sim-face-tracking.md](upstream-sim-face-tracking.md)), and a choice of
camera source. It takes upstream's flags. Start the sim through it when you start one by
hand for the bridge to borrow; upstream's `reachy-mini-daemon --sim` below works for motion
and audio but not for face tracking.

### Headless sim — CI (motion + audio, no camera)

Real MuJoCo physics, no viewer, no display — runs anywhere:

```
uv run python -m reachy_mini_bridge.sim_daemon --headless --preload-datasets
# upstream alone, without the tracking corrections:
reachy-mini-daemon --sim --headless --preload-datasets
```

- Serves `http://127.0.0.1:8000` in ~1s. Add `--no-media` for a pure **motion** daemon (no camera/audio) — the lightest option for motion-only work; the e2e harness spawns media-on so it can probe audio.
- **Media on** (omit `--no-media`) brings up **audio**: the daemon falls back to the host's default mic/speaker and enables **software AEC** (`No hardware AEC; enabled software echo cancellation`). The macOS `libgstpython.dylib` GStreamer warning is harmless.
- **The rendered camera does not work here**: upstream starts the eye-camera render only under the viewer (`get_frame()` returns `None`). Use the viewer mode for it — or a webcam (below), which works headless too.

### Headfull / viewer sim — local (adds camera, watchable)

Drop `--headless` to open the MuJoCo viewer. The viewer supplies a **GL context** (so `get_frame()` works) and lets you watch the sim as a robot stand-in. It needs an **interactive GUI session**; on **macOS** it must run under `mjpython`:

```
mjpython -m reachy_mini_bridge.sim_daemon --scene minimal --preload-datasets
# upstream alone, without the tracking corrections:
mjpython -m reachy_mini.daemon.app.main --sim --scene minimal --preload-datasets
```

From a real Terminal (your GUI session) this opens the window. From a **background/agent/CI** process tree it **segfaults (exit 139)** at window creation — the viewer only opens inside a GUI (Aqua) session.

> **The screen must be unlocked.** Even inside your GUI session, a **locked screen** (or a display asleep) denies the window server a GL context, so the viewer daemon either **hangs** (produces no output → the e2e harness times out) or **segfaults** (`exit -11` in the harness). This is the main source of "flaky" `REACHY_MINI_E2E_SIM_VIEWER=1` runs: unlock the screen and re-run. The headless target has no such requirement — it exercises the same api/audio/motion paths without a display (only the camera needs the viewer's GL context).

To launch it from a non-GUI shell while you're logged in graphically:

```
launchctl asuser $(id -u) \
  <venv>/bin/mjpython -m reachy_mini_bridge.sim_daemon --scene minimal --preload-datasets
```

### A face in the sim (viewer + scene file)

Upstream's scenes ship nothing to look at. The bridge's shipped testing package can write a **test scene** — hidden-by-default props, a portrait plane today — in front of the robot and run the daemon on it through its own launcher, which lets you show, place, move and hide the props while the daemon runs ([../specs/sim_scene.md](../specs/sim_scene.md)):

```python
from reachy_mini_bridge.testing.sim_scene import write_test_scene

write_test_scene("/tmp/scene")  # -> /tmp/scene/scene.xml (the face starts hidden)
```

```
mjpython -m reachy_mini_bridge.testing.sim_scene --scene-path /tmp/scene/scene.xml --preload-datasets
```

It is the sim daemon launcher with the scene added, so the head converges on the face. Then, from any process: `SimSceneClient().show("face")` to bring it into view, `.place("face", (0.45, 0.15, 0.20), duration=1.5)` to move it, `.hide("face")` to take it away again — or `curl -X POST localhost:8000/api/sim-scene/bodies/face -H 'Content-Type: application/json' -d '{"visible": true}'`. A `ReachyMiniConfig` whose `daemon.scene` is that `.xml` path does the launch for you (`"headless": false` — the face needs the viewer's camera to be seen). The e2e harness runs every sim it spawns on this scene.

### You in front of the sim (a webcam as the camera)

For manual tests of face-driven behaviour, the sim can see through the computer's webcam instead of its rendered eye camera ([../specs/sim_daemon.md](../specs/sim_daemon.md) "Camera sources"): the person in front of the screen is who the simulated robot detects and follows, with or without the viewer.

```
mjpython -m reachy_mini_bridge.sim_daemon --camera webcam [--webcam-device 1] [--webcam-hfov 70]
uv run python -m reachy_mini_bridge.sim_daemon --headless --camera webcam
```

or, from a config, `"daemon": {"spawn": "auto", "headless": false, "camera": {"source": "webcam"}}` (the control panel takes such a config). The webcam is treated as a fixed camera at the robot's resting eye: step aside and the head turns by the angle the webcam sees you at, stand still and it holds. `--webcam-hfov` is the camera's horizontal field of view (70° default, typical of a laptop camera) — set it to your camera's for an accurate aim. `--webcam-device` is an index on macOS, a `/dev/videoN` path on Linux; omitted, the default camera.

On macOS the app that launched the daemon (your terminal, or VS Code) needs **Camera** access in System Settings → Privacy & Security. Without it the daemon keeps running, logs `webcam relay (...): no frame ...` once with that hint, and retries every 5 s — granting the permission brings frames back without a restart. The test scene's portrait is invisible to a webcam; the automated face tests use the default rendered camera.

### Real robot

A wireless robot runs its own daemon — nothing to start. Point the client at it (`connection_mode="network"`, the robot's host); everything is available, including **hardware** AEC, camera, and DoA.

A robot plugged into this machine over USB (Reachy Mini Lite) needs the daemon running here:

```
reachy-mini-daemon --kinematics-engine Placo --preload-datasets
```

It finds the robot's serial port itself, wakes the robot on start and puts it to sleep on stop (about 8 s). Pass `--kinematics-engine Placo` only with `reachy-mini[placo_kinematics]` installed — gravity compensation needs it. A `ReachyMiniConfig` with `"backend": "real"` and `"daemon": {"spawn": "auto"}` runs this for you.

## Connecting the client

```python
from reachy_mini_bridge.robot import build_robot

with build_robot(
    "real", connection_mode="network", host="127.0.0.1", port=8000
) as robot:
    ...
```

(Through the api, these go in the config's `robot` block; when the bridge manages the
daemon itself it fills `connection_mode="network"`, `host`, `port`, and
`media_backend="local"` in for you — see [../specs/config.md](../specs/config.md).)

- **Connect over the network.** The default `auto`/`localhost` path uses an IPC transport an externally-started daemon doesn't serve.
- **For media (camera/audio), pass `media_backend="local"`** (same machine as the daemon). The default WebRTC path errors with `KeyError: 'Producer reachymini not found.'`.
- **Audio needs `media.start_recording()` first** — then `get_audio_sample()` yields **float32, stereo, 16 kHz** mic frames; the speaker is `push_audio_sample` / `play_sound`.

## Gotchas

- **Wait for *readiness*, not just the open port.** `/ws/sdk` returns **`403 Forbidden` ("Daemon not ready")** until the daemon **wakes** and creates its `ws_server` — keep the default autostart/wake (do *not* pass `--no-autostart` / `--no-wake-up-on-start`). For `--sim`, confirm `get_status().backend_status is not None`: the WebSocket is accepted even when the MuJoCo backend failed to start (e.g. no GL context).
- **A lighter `--mockup-sim` mode exists** (kinematic mock, no physics) and also runs headless, but the bridge's e2e tier uses the real MuJoCo backend — the in-process `FakeReachyMini` already covers the mock level.
- **Spawning the daemon from a process that already imported `reachy_mini`?** Scrub the GStreamer-bundle env vars from the child's environment first (`GST_PLUGIN_PATH_1_0`, `GST_PLUGIN_SYSTEM_PATH_1_0`, `GST_REGISTRY_1_0`, `GST_PLUGIN_SCANNER_1_0`, `GI_TYPELIB_PATH`, `PYGI_DLL_DIRS`, `XDG_DATA_DIRS`, `XDG_CONFIG_DIRS`). `reachy_mini`'s `gstreamer_bundle.pth` **prepends** to these at every Python startup, so if the parent already set them the child's `.pth` doubles them (`scanner:scanner`) into a value GStreamer can't exec — the external plugin scanner then fails and the in-process fallback **segfaults on `libgstpython.dylib`** (exit 255). Scrubbing lets the child set fresh, correct values; upstream's own app launcher (`reachy_mini/apps/manager.py`) does exactly this, and so does the e2e harness. (Launched from a plain shell that never imported `reachy_mini`, the vars are unset and the warning really is harmless — as above.)
