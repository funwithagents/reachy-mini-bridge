---
code:
  - src/reachy_mini_bridge/sim_daemon.py
  - src/reachy_mini_bridge/config.py
  - src/reachy_mini_bridge/daemon.py
  - src/reachy_mini_bridge/audio_devices.py
  - src/reachy_mini_bridge/testing/fixtures.py
tests:
  - tests/test_sim_daemon.py
  - tests/test_config.py
  - tests/test_daemon.py
---

# Sim world: several robots in one simulation

**Status:** Draft

## Purpose

A **sim world** is one MuJoCo simulation holding several Reachy Minis that share physics. Each robot is served as a daemon of its own, so a `ReachyMiniBridge` connects to one robot as it connects to any sim daemon and knows nothing of the others.

It exists for applications where robots meet: two conversational apps talking to each other, one per robot, each robot's camera seeing the other. The first user is WICA Interaction's interaction tester, where one robot plays the person in front of the other, and later a robot detector running on those camera frames.

Upstream's sim is one robot per daemon and one daemon per simulation ([sim_daemon.md](sim_daemon.md)). Two sim daemons side by side are two separate worlds: neither robot can see the other.

A spike proved the design below on macOS (branch `spike/two-robot-sim`, `spikes/two_robot_sim/README.md`, 2026-11-11): two bridges, each moving and seeing through its own robot, in one model. The frontmatter names the existing files this design changes; the plan that builds it adds the host's module (`src/reachy_mini_bridge/sim_world.py`) and its tests there.

## Core concepts / Decided

### One process, one physics host, one daemon per robot

```
                    one process (python, or mjpython for the viewer)
  ┌────────────────────────────────────────────────────────────────────────┐
  │ physics host: the model, the only mj_step, the viewer, the renderer    │
  │                                                                        │
  │ robot "a": upstream's daemon app + a robot view + its media server     │
  │ robot "b": the same, on its own port                                   │
  └────────────────────────────────────────────────────────────────────────┘
        ▲ REST + WebSocket, port A            ▲ REST + WebSocket, port B
   app process (bridge A)                 app process (bridge B)
```

- **The physics host** owns the composed model and its data, the only stepping loop (500 Hz), the one viewer and the one render thread.
- **A robot view** is the backend upstream's daemon talks to: a subclass of the bridge's sim backend bound to one robot of the model. It never steps the physics. The host calls its 50 Hz control tick on the physics thread, between two steps, so every robot's controller sees the same state.
- **One upstream daemon app per robot**, each on its own REST port. Upstream's app is instance-scoped where it matters (its daemon, its WebSocket server, its routers), so several run in one process.
- **Why one process.** A host process with one daemon process per robot would need a 50 Hz protocol for targets, states and head poses, and would separate the viewer from the daemons that draw on it. The cost of one process: a crash takes every robot down, and the daemons share one interpreter lock (no starvation measured with two robots).

### The model

- **Composed at start**, not written as a scene file: upstream's robot description is attached once per robot under a name prefix (`a_`, `b_`), at the robot's pose, on a world that restates upstream's `empty` scene (floor, light, skybox).
- **Robots are fixed to the world**, as upstream's single robot is. They do not move across the floor.
- **Each robot's API is in its own frame.** A view reports the head pose relative to its robot's base, so a client reads the same numbers whichever way its robot faces.

### The launcher: one more argument of the sim daemon

`python -m reachy_mini_bridge.sim_daemon --world FILE [--headless] [--[no-]preload-datasets] [--kinematics-engine ENGINE]`

- **One launcher for every sim.** Without `--world`, the sim daemon launcher runs as today ([sim_daemon.md](sim_daemon.md)). With it, the same command starts the world: the physics host, and a daemon per robot of the file.
- **Two paths behind one command.** The single sim calls upstream's `main()`, which builds one daemon. A world cannot, so `--world` runs the host instead. Both install the same backend subclass and the same extensions.
- **`--world` excludes the per-robot flags**: `--scene`, `--camera`, `--webcam-*`, `--audio-*`, `--discard-audio-output` and `--sim-display` are an argument error beside it. What they set is per robot, and the file says it.
- **Under `mjpython` for the viewer on macOS**, as the single sim is.

### The world file and `WorldConfig`

The world file is JSON, loaded into a `WorldConfig` (`config.py`, `from_dict` / `from_json` / `from_json_file` like the other configs):

```json
{
  "robots": [
    {"name": "a", "port": 8011, "position": [0.0, 0.0, 0.0], "yaw_deg": 0,
     "audio": {"input_device": "cable-b-to-a", "output_device": "cable-a-to-b"}},
    {"name": "b", "port": 8012, "position": [0.6, 0.0, 0.0], "yaw_deg": 180,
     "audio": {"input_device": "cable-a-to-b", "discard_output": true}}
  ]
}
```

- **A robot's entry is its place plus the per-robot part of a `daemon` block.** `name`, `port`, `position` and `yaw_deg` are the world's own. `audio` is `DaemonConfig`'s `audio` block, the same `SimAudioSettings`, parsed by the same code. `sim_displays` joins it once displays in a world are decided (open question 3).
- **The file holds nothing about how the process runs.** Headless or viewer, the kinematics engine and dataset preloading are launch flags, as for the single sim, so one world file serves a viewer run on a desk and a headless run in CI.
- **A robot is addressed by `host:port` alone**, like any daemon. Its name is for the model's prefixes, the logs and the report.
- **Audio is per robot.** The daemon-side device substitution of [audio.md](../audio/audio.md) is one set of devices per process and cannot serve two robots, so each robot's media server resolves its own. The client needs nothing new: a local bridge plays and records in its own process, on the devices its robot's report names. The spike proved it with two bridges: robot a's `say` came out of its named cable, robot b's was discarded in real time, and robot b's mic feed carried robot a's voice.
- **The camera source is the rendered one.** A host webcam as a robot's camera is not offered in a world: `camera` is not a field of a robot's entry.
- **Rejected at load**: fewer than one robot, a duplicate name or port, a name that is not a valid prefix, an unknown key, and one device named as both the input and the output of the same robot (a robot hearing its own voice through a cable).

### In a bridge's config: `daemon.world`

A `ReachyMiniConfig` stays one robot. It says which world its robot lives in, and which robot it is, with what it already has:

```json
{
  "backend": "sim",
  "robot":  { "port": 8012 },
  "daemon": { "spawn": "auto", "world": "worlds/face-to-face.json", "headless": false }
}
```

- **`daemon.world`** (a path, `null` by default) names the world file. Like every field of the block, it applies only to a daemon the bridge spawns ([config.md](../core/config.md)).
- **`robot.port` says which robot.** It must be the port of one of the file's robots, checked when the config is loaded.
- **`daemon.world` excludes the block's per-robot fields**: `scene`, `camera`, `audio` and `sim_displays` beside it are a `ConfigError`, as their flags are beside `--world`. `spawn`, `headless`, `kinematics_engine`, `preload_datasets` and `startup_timeout` keep their meaning, for the whole world.
- **`launch_command`** ([daemon.md](daemon.md)) builds `sim_daemon --world <absolute path>` with the launch flags from the block. Readiness is probed on the bridge's own port, as ever.
- **The session follows the report, not its config**, for what the world gave its robot: the audio devices and the camera socket ("The report and the client"). That is already the rule for a borrowed daemon.

### What each robot's daemon needs of its own

Upstream fixes four endpoints per machine. The world gives each robot its own:

| Endpoint | Upstream | In a world |
|---|---|---|
| REST and WebSocket port | configurable, default 8000 | the robot's `port` |
| Camera stream into the media server (UDP) | 5005 | a free port the host picks; internal to the process |
| WebRTC signalling | 8443 | a free port the host picks |
| Camera socket local clients read | `/tmp/reachymini_camera_socket` | `/tmp/reachymini_camera_socket_<port>` |

Only the socket is something a client must know, and the report gives it.

### The report and the client

- **`GET /api/bridge/daemon` on each robot's port** answers as the sim launcher does ([daemon.md](daemon.md), "What the daemon reports"), with that robot's audio devices, plus:

| Field | Value |
|---|---|
| `camera.socket` | the path of this robot's camera socket |
| `world.robot` | this robot's name |
| `world.robots` | every robot of the world: name, port, position and yaw |

- **The bridge follows `camera.socket`.** Before it builds its client, a session points upstream's local camera reader at the reported path, as it already follows the reported audio devices. Absent (a single sim, a stock daemon), upstream's path stands.
- **One robot's camera per process.** The path is a module constant upstream, so a process reads one robot's camera. One bridge per process already holds for the audio devices.
- **`world.robots` tells a robot where the others stand.** A test of a robot detector reads its ground truth there.

### Lifecycle

- **Whoever starts the world owns all of it.** That is a test fixture, a script, a person, or the first bridge to start with `spawn: "auto"` and `daemon.world`.
- **Every other bridge borrows its robot's port**: `spawn: "auto"` finding it ready, or `spawn: "never"`. Two apps with the same `daemon.world` and `spawn: "auto"` therefore need no third process: the first starts the world, the second joins it.
- **A bridge that owns the world stops it with its session**, every robot included, as it stops any daemon it spawned ([daemon.md](daemon.md), "Teardown stops what the bridge started"). The other bridges then lose their connection, as with any borrowed daemon whose owner leaves. Apps that must outlive one another start the world themselves and use `spawn: "never"`.
- **`spawn: "always"`** spawns the world and fails if the bridge's own port is taken, as for a single sim.
- **Stopping the launcher** (SIGINT or SIGTERM) puts every robot to sleep, stops every daemon, removes every socket and exits.

### Rendering

- **One render thread draws every eye camera**, in turn, each sent to its robot's media server.
- **The trimmed render, on every platform.** No shadows or reflections, and each camera's own robot left out. Upstream's full render cannot hold the rate with two cameras: measured on an Apple M5 Pro, 8 to 10 frames a second per robot against about 20 trimmed.
- **A camera's own robot is hidden through a geom group**, not by writing into the model. The single sim's headless trim writes alpha 0 into the model ([sim_daemon.md](sim_daemon.md), "The headless camera"); in a shared model that would blank the robot in the viewer and in the other robot's camera.
- **The viewer shows every robot whole**, in one window.

### Kept in step with upstream

The view restates what upstream's backend does inline, so three couplings are pinned by tests rather than trusted:

- **The 50 Hz control tick** copies the body of upstream's loop (about 30 lines). A fast test drives a stock backend and a view with the same targets and compares what each publishes.
- **The view does not run upstream's backend constructor**, which loads a scene of its own. The same fast test fails on an attribute upstream adds there.
- **Five private builders of upstream's media server are overridden** per robot (the UDP source, the camera socket, the signalling port, the audio source, the audio sink). A renamed builder fails at start.
- **A live test asserts the two cameras differ.** If upstream came to read the socket path once at import, both clients would silently read the first robot's camera.

The spike's README lists every upstream assumption with its file and line.

### Testing

- **Fast tier**: the composed model (prefixes, poses, each robot's actuators and camera); the view against a stock backend; `WorldConfig`'s validation; `daemon.world` in a bridge's config (the port among the world's, the excluded fields); the launch command built for it; `--world` beside a per-robot flag; the report's fields; the endpoints given to each robot.
- **Live tier**: a `live_world` fixture beside `live_daemon` ([testing_support.md](../testing/testing_support.md)), yielding each robot's `host:port`. Its tests are the spike's proofs: each bridge moves only its own robot; each camera shows the other robot; each robot opens its own audio devices. A fixture that spawns bridge processes passes them `scrubbed_env()` ([daemon.md](daemon.md), "The child's environment is scrubbed"): a Python child that inherits the parent's GStreamer paths sets them again and crashes scanning plugins, as the spike found.

### Platforms

| | State |
|---|---|
| macOS, viewer and headless | proved by the spike |
| Linux headless (EGL), CI | not run yet; to prove before the spec is `Stable` |
| Windows | not considered |

## Relationship to the other specs

- **[sim_daemon.md](sim_daemon.md)**: the launcher gains `--world`. The view wraps its backend subclass; the render trim through geom groups may replace its alpha trim (open question 6).
- **[config.md](../core/config.md)**: `WorldConfig`, and the `daemon` block's `world` field with what it excludes.
- **[daemon.md](daemon.md)**: the launch command for a world; the report gains `camera.socket` and `world`; borrowing a world's port is ordinary borrowing.
- **[camera.md](../vision/camera.md)**: the session points the camera reader at the reported socket before the feed starts.
- **[audio.md](../audio/audio.md)**: the devices a session follows from the report, unchanged; per-robot resolution on the daemon side is this spec's.
- **[sim_scene.md](../testing/sim_scene.md)**, **[sim_displays.md](sim_displays.md)**: props and displays in a world are open (questions 2 and 3).
- **[testing_support.md](../testing/testing_support.md)**: the `live_world` fixture.

## Open questions

1. **A robot's own echo, and two cables.** The spike wired one direction, robot a's voice into robot b's microphone, on the one virtual cable of its machine. Both directions at once need two cables, and nothing yet checked what a robot hears of its own voice, which the real robot cancels in hardware and the sim does not. Whether a world offers an echo canceller per robot, or leaves it to the application, is to decide.
2. **Props in a world.** The scene director drives props from inside the physics loop through one callback per process ([sim_scene.md](../testing/sim_scene.md)), which suits one host. Whether props are declared in the world file, and on which robot's port `/api/sim/inject` is served (each, or the first), is to decide. The interaction tester needs hand props in front of one robot.
3. **Sim displays in a world.** `camera_overlay`, `robot_gaze` and `face_markers` each belong to one robot. Whether a world shows them for one named robot, for all, or not at first.
4. **Two robots on the default microphone.** In the spike, one start in eight with both robots on the default input logged `No caps found for stream audio_0`, and its clients got no camera frames after a media release. With a named device per robot it did not happen. The cause is not known, nor whether a single stock daemon shows it. Until it is, a world may require a named input (or none) per robot.
5. **WebRTC clients.** Upstream's client dials signalling port 8443, so a remote client cannot reach a robot on another port. Local clients, the bridge's path on one machine, do not use it. Left unsupported.
6. **One path or two behind the launcher.** A single sim could run as a world of one robot, which would leave one backend and one render path. It would also put the copy of upstream's loop under every sim. Decided for now: without `--world` the launcher runs as today, and the question is reopened once the world has run in CI.
7. **Upstream options.** Three options upstream (the UDP port, the socket path, the signalling port) would turn most of the overrides into configuration, and a control tick factored into a method would remove the copy. To propose.
8. **Headless camera on macOS.** The spike rendered offscreen on macOS with no window, which [sim_daemon.md](sim_daemon.md) says a Mac cannot do. It ran inside a logged-in session; over SSH is untested. To re-check there.
9. **More than two robots, and robots that touch.** Nothing limits the count, but two is what was measured; the camera rate falls with each robot. Collisions between robots were not exercised.
10. **Two bridges starting at once.** Both find the world's ports closed and both spawn it; the second launcher fails on a taken port. Whether `auto` then falls back to borrowing (the port it wanted is ready a moment later), or the apps are told to start one after the other, is to decide.
