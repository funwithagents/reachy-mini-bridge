# Running a Reachy Mini daemon (for e2e / dev)

How to bring up a `reachy_mini` daemon — for the e2e tests, or for developing against a live daemon. Like [reachy-mini-api.md](reachy-mini-api.md), this is a reference note about the upstream SDK, not a spec. It's the operational companion to the e2e **strategy** in [../specs/testing.md](../specs/testing.md) ("E2E targets & capabilities"), which owns the capability matrix; this file records the concrete launch recipes and *why* they work.

## Launch modes

### Headless sim — CI (motion + audio, no camera)

Real MuJoCo physics, no viewer, no display — runs anywhere:

```
reachy-mini-daemon --sim --headless --no-preload-datasets
```

- Serves `http://127.0.0.1:8000` in ~1s. Add `--no-media` for a pure **motion** daemon (no camera/audio) — the lightest option, what today's motion fixture uses.
- **Media on** (omit `--no-media`) brings up **audio**: the daemon falls back to the host's default mic/speaker and enables **software AEC** (`No hardware AEC; enabled software echo cancellation`). The macOS `libgstpython.dylib` GStreamer warning is harmless.
- **Camera does not work here on macOS**: the virtual-camera offscreen render needs a GL context that headless plain-python lacks (`get_frame()` returns `None`). Use the viewer mode for camera.

### Headfull / viewer sim — local (adds camera, watchable)

Drop `--headless` to open the MuJoCo viewer. The viewer supplies a **GL context** (so `get_frame()` works) and lets you watch the sim as a robot stand-in. It needs an **interactive GUI session**; on **macOS** it must run under `mjpython`:

```
mjpython -m reachy_mini.daemon.app.main --sim --scene minimal --no-preload-datasets
```

From a real Terminal (your GUI session) this opens the window. From a **background/agent/CI** process tree it **segfaults (exit 139)** at window creation — the viewer only opens inside a GUI (Aqua) session. To launch it from a non-GUI shell while you're logged in graphically:

```
launchctl asuser $(id -u) \
  <venv>/bin/mjpython -m reachy_mini.daemon.app.main --sim --scene minimal --no-preload-datasets
```

### Real robot

Nothing to start — the robot runs its own daemon. Point the client at it (`connection_mode="network"`, the robot's host); everything is available, including **hardware** AEC, camera, and DoA.

## Connecting the client

```python
from reachy_mini_bridge.client import build_robot

with build_robot(
    "real", connection_mode="network", host="127.0.0.1", port=8000
) as robot:
    ...
```

- **Connect over the network.** The default `auto`/`localhost` path uses an IPC transport an externally-started daemon doesn't serve.
- **For media (camera/audio), pass `media_backend="local"`** (same machine as the daemon). The default WebRTC path errors with `KeyError: 'Producer reachymini not found.'`.
- **Audio needs `media.start_recording()` first** — then `get_audio_sample()` yields **float32, stereo, 16 kHz** mic frames; the speaker is `push_audio_sample` / `play_sound`.

## Gotchas

- **Wait for *readiness*, not just the open port.** `/ws/sdk` returns **`403 Forbidden` ("Daemon not ready")** until the daemon **wakes** and creates its `ws_server` — keep the default autostart/wake (do *not* pass `--no-autostart` / `--no-wake-up-on-start`). For `--sim`, confirm `get_status().backend_status is not None`: the WebSocket is accepted even when the MuJoCo backend failed to start (e.g. no GL context).
- **A lighter `--mockup-sim` mode exists** (kinematic mock, no physics) and also runs headless, but the bridge's e2e tier uses the real MuJoCo backend — the in-process `FakeReachyMini` already covers the mock level.
