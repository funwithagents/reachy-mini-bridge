# Spike: two Reachy Minis in one MuJoCo simulation

**Status:** spike, 2026-11-11. Prototype code, not production, no spec changed. Measured on
macOS arm64 (Apple M5 Pro), `reachy_mini` 1.11.0, `mujoco` 3.3.7.

## Question

Can one MuJoCo simulation hold two Reachy Minis that share physics, each exposed as its own
daemon (REST port, camera stream, audio devices), so that two independent `ReachyMiniBridge`
instances each connect to one robot as to a normal sim daemon, each camera seeing the other
robot?

**Yes.** Everything in "What was proved" below was run, not inferred.

## Layout chosen: one process, two upstream daemons, one physics host

```
                      one process (python, or mjpython for the viewer)
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │ PhysicsHost: one MjModel/MjData, the only mj_step, the viewer, the renderer  │
  │   physics thread 500 Hz ── every 10th step: view_a.control_tick(), view_b…   │
  │   render thread ── a_eye_camera → UDP 5011, b_eye_camera → UDP 5012          │
  │                                                                              │
  │ robot a: upstream create_app() + Daemon + RobotView(a_) + RobotMediaServer   │
  │   REST/WS 8011, udpsrc 5011, IPC /tmp/reachymini_camera_socket_a, sig 8451   │
  │ robot b: the same on 8012, 5012, …_socket_b, 8452                            │
  └──────────────────────────────────────────────────────────────────────────────┘
        ▲ ws + REST 8011, socket _a            ▲ ws + REST 8012, socket _b
   app process A (bridge A)                app process B (bridge B)
```

Why this and not a physics host process plus two thin daemon processes:

- Upstream's daemon is instance-scoped where it matters: `create_app(args)` builds its own
  `Daemon`, `AppManager`, `WSServer` and routers reading `request.app.state.daemon`. Two
  apps on two uvicorn servers in one event loop run side by side; nothing in the REST or
  WebSocket path is a module global.
- What *is* fixed per process is a short list of constants and one singleton, each of which
  has a per-instance seam (table below). None needed an edit of site-packages.
- The alternative needs an IPC protocol for joint targets, joint states and the head pose at
  50 Hz, a second one for display wiring (viewer overlays and face markers live with the
  viewer, the daemons would not), and three processes to supervise. It buys nothing the
  in-process layout lacks, except that the bridge's existing per-process substitutions
  (`audio_devices.substitute`, `daemon.MujocoBackend`) would apply unchanged.
- In one process the control ticks run on the physics thread between two steps, so both
  robots' controllers see the same state with no locking protocol, and the render thread
  needs a single lock.

The cost of the choice: a crash takes both robots down, the bridge's launcher
(`run_sim_daemon`, which calls upstream's `main()` once) cannot be reused as is, and the two
daemons share one GIL (measured below: no starvation at two robots).

## What was proved

Run from the worktree root. Ports 8011/8012 so a daemon on 8000 is left alone.

```
uv run python  -m spikes.two_robot_sim.host --headless --discard-audio-output   # no window
uv run mjpython -m spikes.two_robot_sim.host --discard-audio-output             # viewer, macOS
```

| Proof | Command | Result |
|---|---|---|
| Model | `python -m spikes.two_robot_sim.check_world --render` | 18 actuators, 2 eye cameras, 10 equalities, 98 meshes (upstream's `meshdir` resolves through `MjSpec.attach`); robots welded to the world (no free joint), `a_` camera at (0.039, 0, 0.202) looking +x, `b_` at (0.561, 0, 0.202) looking −x; 0.07 ms per step against 0.03 ms for one robot |
| Each client moves only its robot | `python -m spikes.two_robot_sim.prove_isolation` (two upstream `ReachyMini` clients, one per port; exit 0) | client a commands yaw +20°: robot a head joints move 0.122 rad, antennas 0.600 rad, head yaw reads +20.2° in its own frame, robot b joints move 0.00007 rad. Client b commands −20°: robot b 0.122 / 0.600 rad, −20.2°, robot a 0.00019 rad. Same under the viewer (0.00004 / 0.00024 rad) |
| Each client's `get_frame()` is its robot's camera, the other robot in it | `python -m spikes.two_robot_sim.grab_frame --port 8012 --yaw 25 --hold 12 --out …/frame_b_head_turned.png &` then `… --port 8011 --out …/frame_a_sees_b_turned.png` | `frame_a_sees_b_turned.png`: robot B centred (white-pixel centroid x=639 of 1280), its head visibly turned. `frame_b_head_turned.png`: robot A at x=873, pushed right by B's own 25° turn. Headless and under the viewer (`viewer_frame_*.png`) |
| Two real `ReachyMiniBridge` sessions at once, one process each | `python -m spikes.two_robot_sim.prove_bridge --port 8011 --other-port 8012 --out …/bridge_frame_a.png` and the mirror | each session: camera feed frames (`bridge_frame_*.png`), mic chunks flowing (686 in 6 s, 2084 in 20 s), motion loop breathing. Alone, bridge A moves robot A (0.505 rad range) and robot B reads 0.0000 |
| Audio devices per robot | host with `--audio-input-device-a "Micro MacBook Pro" --audio-input-device-b "BlackHole 2ch" --discard-audio-output` | daemon log: `Using CoreAudio device BuiltInMicrophoneDevice for capture` for a, `BlackHole2ch_UID` for b; each bridge's `bridge.daemon.audio` names its own robot's input and the discard, and opens them |
| One viewer window, both robots | `mjpython -m spikes.two_robot_sim.host` | one window titled `two_robot_scene`, both robots side on, facing each other; seen in a screenshot (not kept, see below). `side_view_offscreen.png` is the same view rendered offscreen |
| Clean stop | SIGINT to the headless host | both daemons go to sleep and stop, both sockets removed, exit |

Rates, both robots awake, both cameras streaming:

| Run | Physics | Camera, per robot | Draw per frame |
|---|---|---|---|
| headless, trimmed render (default) | 502 Hz | 20.9 fps | 18.8 ms |
| viewer, trimmed render | 491 Hz | 19.1 fps | 18.8 ms |
| headless, `--full-render` (upstream's) | 506 Hz | 9.8 fps | 50.5 ms |
| viewer, `--full-render` | 466 Hz | 7.8 fps | 67.8 ms |

Upstream's target is 25 fps. One robot rendered upstream's way took 35 to 49 ms a frame
standalone on this machine, so a second robot only makes an existing cost visible: 699 000
triangles drawn twice per frame, with a 4096-texel shadow map, through macOS's GL-on-Metal.
The trimmed render drops shadows and reflections and leaves each camera's own robot out
(its visual meshes sit in a geom group of their own, hidden by a per-camera `MjvOption`, so
the model the viewer draws is never touched).

## Not verified

- **Linux, EGL, CI.** Nothing ran off this Mac. The bridge's headless trim hides the eye
  camera's own tree by writing alpha 0 into the shared model; with two robots and a viewer
  that would blank a robot in the window, which is why the spike uses geom groups. Inferred
  to work under EGL, not run.
- **macOS headless camera, in general.** `mujoco.Renderer` built a CGL context and drew in a
  plain `python` process with no window here, in a thread, which the bridge's spec says a
  Mac cannot do. This shell runs inside a logged-in GUI session; whether it holds over SSH or
  from a LaunchDaemon was not tested.
- **A robot detector on the frames.** The other robot is plainly visible, 0.52 m from the
  camera, about 240 px tall without its antennas. No detector was run.
- **Sound through a speaker.** Every run discarded the output (`fakesink`), to stay off the
  owner's speakers. A named output device per robot goes through the same per-instance seam
  as the input and was not exercised. `say` was not called.
- **Two robots on one microphone.** With both daemons on the default microphone, one start
  in eight logged webrtcsink's `No caps found for stream audio_0` and that run's clients got
  no frames after a media release / re-acquire. With a named device per robot the release /
  re-acquire cycle worked. Cause not established; whether a stock single daemon shows it was
  not checked.
- **WebRTC clients.** Each robot's signalling server listens on its own port (8451, 8452),
  but upstream's client hardcodes 8443 (`media_manager.py:174`), so a remote / WebRTC client
  of the second robot cannot find it. Local IPC clients, the bridge's path on one machine,
  do not use it.
- **Placo and NN engines**, `check_collision`, the scene director (`mujoco.set_mjcb_control`
  is one callback per process, which suits one host), the sim displays, a webcam source.
- **Collisions between the robots.** They stand 0.6 m apart and never touch.
- **The viewer screenshot.** `screencapture` raised a macOS screen-recording permission
  prompt for the terminal's host app. One capture showed the viewer window; no more were
  taken and none is committed.

## Upstream assumptions worked around

Paths under `site-packages/reachy_mini/`, version 1.11.0. Nothing there was edited.

| Where | Assumption | Handling |
|---|---|---|
| `daemon/backend/mujoco/backend.py:86` | the backend loads `scenes/{scene}.xml` itself | `RobotView.__init__` skips `MujocoBackend.__init__`, calls `Backend.__init__` and sets the same attributes from the host's model |
| `backend.py:95` | the head site is named `head` | looked up as `<prefix>head` |
| `backend.py:103-110`, `utils.py:57` | every actuator of the model is this robot's | the nine names, prefixed; joint and actuator ids from them |
| `backend.py:112-122`, `:198-207` | the backend toggles collisions and settles the world | done once by the host for all robots |
| `backend.py:171` | the backend opens the viewer | the host opens the one viewer; views are `headless` |
| `backend.py:188-193` | `data.ctrl[:]` is one robot's | the host writes each robot's sleep pose through its actuator ids |
| `backend.py:214-217`, `:137-160` | the backend starts a render thread on UDP 5005, reading `data` unlocked | the host renders every eye camera on one thread, one port per robot, under its lock |
| `backend.py:227-288` | the backend owns the stepping loop; the 50 Hz body is inline, not a method | `control_tick()` restates that body (about 30 lines); the host calls it on the physics thread |
| `backend.py:258-260` | `ctrl[:7]` and `ctrl[-2:]` | indexed by the robot's actuator ids |
| `backend.py:303-307` | the head site's world pose is the pose in the robot's frame | multiplied by the inverse of the robot's base pose (robot B reads yaw −20.2° for a −20° command) |
| `daemon/daemon.py:862` | `MujocoBackend` is a module attribute, one per process | `daemon._setup_backend` replaced on the instance |
| `daemon/daemon.py:113-116` | `GstMediaServer` imported at construction | the module attribute is swapped for the robot's subclass around `create_app` |
| `daemon/daemon.py:427` | the backend is ready within 2 s of starting | the host settles the world before any daemon starts |
| `media/media_server.py:879` | `udpsrc` port 5005 | `_build_sim_source` overridden, port set on the element |
| `media/media_server.py:371` | webrtcsink's signalling server on its default 8443 | `_configure_webrtc` overridden: `signalling-server-port` and the signaller's `uri` |
| `daemon/utils.py:22`, `media_server.py:1087-1090`, `daemon/utils.py:69` | one camera socket, `/tmp/reachymini_camera_socket`, removed if present | `_build_ipc_branch` runs with the module's path and availability check pointed at the robot's socket |
| `media/camera_gstreamer.py:184`, `daemon/utils.py:69` | the **client** reads that same fixed path | `client.bind_camera_to(port)` sets both module constants in the client process from the daemon's `/api/spike/robot`; one process reads one robot's camera (two in one process work when each is bound just before its client is built) |
| `media/media_server.py:1298`, `:1420` | audio devices from the module's `get_audio_device` | `_build_audio_source` / `_build_audiosink_element` overridden per instance. The bridge's own `audio_devices.substitute` is one set of devices per process by design (`audio_devices.py:227`) and is not used |
| `media/central_signaling_relay.py:51`, `:1645`, `daemon/daemon.py:37` | one relay to Hugging Face's signalling per process, dialling local port 8443, started whenever an HF token exists | `daemon._start_central_signaling_relay` replaced by a no-op on each instance |
| `reachy_mini.py:349-357` | a `no_media` client makes its daemon release its media | not worked around: it is per daemon, so one robot's client cannot blind the other; it does stop that robot's camera for everyone until re-acquired |
| `daemon/app/main.py:98` | `robot_name` defaults to `reachy_mini` (mDNS record) | `reachy_mini_a`, `reachy_mini_b` |
| `daemon/app/routers/volume_control.py:100` | one volume control per process | left shared; not exercised |

`/tmp/reachy_mini_sounds` (`routers/media.py:27`) is shared and harmless.

## Risks against upstream releases

- **The loop body copy** is the fragile part. A change to upstream's `run()` loop (a new
  publisher, a tracking step, an IMU message) silently goes missing from `control_tick()`.
  A fast test comparing what a stock backend and a view publish for the same targets would
  catch it. Asking upstream to factor the 50 Hz body into a method removes the copy.
- **Skipping `MujocoBackend.__init__`** means any attribute upstream adds there is missing.
- **Private media-server methods** (`_build_sim_source`, `_build_ipc_branch`,
  `_configure_webrtc`, `_build_audio_source`, `_build_audiosink_element`): the bridge
  already leans on the last one. A renamed builder fails loudly at start, which is the
  right failure.
- **Module constants on the client** (`CAMERA_SOCKET_PATH` in two modules): a refactor that
  reads the path once at import would break the second robot's camera silently, both
  clients reading the first socket. Worth a live assertion that the two cameras differ.
- All of it would shrink to configuration if upstream took three options: the UDP port, the
  IPC socket path and the signalling port. Worth proposing.

## Recommended design for the bridge

A second launcher beside `sim_daemon.py`, not a mode of it: `python -m
reachy_mini_bridge.sim_world` (name open), taking a list of robots, each with a name, a
pose, a REST port and the per-robot flags the sim launcher already has (audio devices,
discard). It owns the physics host and serves one upstream app per robot, as here.

- **Derived endpoints.** UDP port, signalling port and socket path derive from the REST port
  (`/tmp/reachymini_camera_socket_<port>`), so a robot is still named by `host:port` alone
  and two worlds on one machine do not collide.
- **The report carries the socket.** `GET /api/bridge/daemon` gains `camera.socket`. The
  bridge points upstream's local camera reader at it before building its client, the way it
  already follows `audio` from the same report. Absent, upstream's path: stock daemons are
  untouched. One bridge per process for the camera, which the audio substitution already
  imposes.
- **Lifecycle.** `daemon.spawn` stays one process per bridge. A world is started by whoever
  owns the scene (a harness fixture, a script) and both bridges borrow their port; a bridge
  spawning "the other robot's" daemon as a side effect is not worth designing.
- **Backend.** The view should wrap the bridge's backend subclass, so sim displays, the
  webcam source and extensions keep one implementation; the render thread moves to the host
  and the headless trim switches from alpha-in-the-model to geom groups.
- **Testing.** A `live_world` fixture beside `live_daemon`, two `live_bridge`s, and the
  isolation and camera proofs of this spike as live tests; the view-against-stock
  comparison as a fast test.

Specs touched: a new `specs/daemon/sim_world.md`; `sim_daemon.md` (the shared backend, the
render trim), `daemon.md` (the report's `camera.socket`, borrowing a world's port),
`camera.md` or `robot.md` (the client-side socket binding), `config.md` if a config names a
world, `testing_support.md` and `sim_scene.md` (the fixture; props in a shared scene),
`sim_displays.md` (which robot a display belongs to). Rough size: the launcher and host
about 500 lines, the client binding and report about 80, tests about 400, plus the specs;
one plan, several days.

## Files

| File | What |
|---|---|
| `world.py` | the composed model: upstream's `empty` scene restated on an `MjSpec`, two robots attached with prefixes `a_` / `b_` |
| `host.py` | `PhysicsHost`, `robot_view`, `robot_media_server`, `robot_app`, the launcher |
| `client.py` | `bind_camera_to(port)`: the client-side socket binding |
| `check_world.py` | the model on its own: names, stability, facing, an offscreen render |
| `prove_isolation.py` | two upstream clients, joint-state assertions |
| `grab_frame.py` | one upstream client, one camera frame to a PNG |
| `prove_bridge.py` | one `ReachyMiniBridge` session on one robot |
| `frame_*.png`, `viewer_frame_*.png`, `bridge_frame_*.png` | frames read through `get_frame()` / `bridge.camera`, headless, under the viewer, and from the bridge |
| `side_view_offscreen.png` | both robots awake, from the viewer's camera, rendered offscreen |
