---
code:
  - src/reachy_mini_bridge/config.py
  - src/reachy_mini_bridge/errors.py
  - config.example.json
tests:
  - tests/test_config.py
---

# Configuration (`ReachyMiniConfig`)

**Status:** Updated

## Purpose

`ReachyMiniConfig` is the one declarative object that describes everything needed to bring up a `ReachyMiniBridge` ([bridge.md](bridge.md)): which backend, how to reach — or spawn — its daemon, the default speech synthesizer, and the audio profile. It is buildable from a dict, a JSON string, or a JSON file, so a host application keeps its robot settings in its own configuration next to everything else and constructs a talking robot in one call. The same file switches between the real robot and the simulator by changing one string, and runs on the offline fake once `daemon.spawn` is `never` or the block is absent (the fake has no daemon).

The shape and the constructor trio mirror [`tts-engine`'s configuration](https://github.com/funwithagents/tts-engine/blob/main/specs/configuration.md) (`TTSEngineConfig`), so the two first-party libraries read the same way — and the bridge's `tts` block *is* a tts-engine `engine` block, carried through verbatim.

## Core concepts / Decided

### Top-level structure

```json
{
  "backend": "sim",
  "robot":  { "host": "127.0.0.1", "port": 8000 },
  "daemon": { "spawn": "auto", "headless": false, "camera": { "source": "webcam" } },
  "tts":    { "module": { "type": "elevenlabs", "api_key_env": "ELEVENLABS_API_KEY", "voice_id": "..." } },
  "audio":  { "xvf3800": null },
  "face_detection": { "detector": "yunet", "enabled": true, "width": 320, "target_fps": null },
  "motion": { "presence": true, "idle": "breathing", "wobbling": true, "tracking": true }
}
```

Every block is optional: `ReachyMiniConfig()` is a valid config — the `real` backend, upstream's connection defaults, no daemon management, no default synthesizer, firmware audio defaults, the `face_detection` defaults (no detector, detection off, a 320 px detection width, no rate ceiling) and the `motion` defaults (presence and wobbling on, tracking off — it needs a detector — the idle mode `"breathing"`). `config.example.json` in the repo root documents every field with placeholder values and is kept in sync with this spec. It describes the **sim viewer seeing through the host webcam** (`backend: "sim"`, `daemon.spawn: "auto"`, `daemon.headless: false`, `daemon.camera.source: "webcam"`): the configuration that shows the most — the robot moving in the MuJoCo window, and its camera on the person in front of the computer, whom it follows (`face_detection.detector: "yunet"`, detection and tracking on, detecting at `width: 640`, twice the default, for a finer roll) — so it is what the README and the [control panel](../examples/control_panel.md) start from — with every sim display on (`daemon.sim_displays`: `camera_overlay`, `robot_gaze`, `face_markers`), so the viewer window also shows what the camera sees, where the robot looks and where the bridge places each face. Set `source` to `"sim"` for the rendered eye camera instead.

```python
@dataclass
class ReachyMiniConfig:
    # "real" | "sim" | "fake"
    backend: str = "real"
    # upstream ReachyMini(...) kwargs, verbatim
    robot: dict[str, Any] = field(default_factory=dict)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    # a tts-engine `engine` block, verbatim
    tts: dict[str, Any] | None = None
    audio: AudioSettings = field(default_factory=AudioSettings)
    # face detection: the detector (none / yunet / custom), whether it runs from entry, its cost knobs
    face_detection: FaceDetectionSettings = field(default_factory=FaceDetectionSettings)
    # everything that shapes the robot's behaviour at rest: the loop's own modes
    # (presence, idle, idle_move, tracking) and the daemon-side mode the bridge arms (wobbling)
    motion: MotionSettings = field(default_factory=MotionSettings)
```

The config module (`config.py`) imports neither `tts_engine` nor `reachy_mini` at module load: the raw blocks it carries are consumed by the layer that needs them (`tts` by [audio.md](../audio/audio.md)'s adapter, `robot` by [robot.md](robot.md)'s factory). The one upstream lookup — the `robot` key check below — imports `reachy_mini` lazily inside `from_dict`.

### Constructors

Both config classes that a caller builds directly (`ReachyMiniConfig`, and `DaemonConfig` / `SimCameraSettings` / `AudioSettings` / `FaceDetectionSettings` / `MotionSettings` for the nested blocks) expose the same symmetric trio as tts-engine, layered file → json → dict so all three share one validation path:

| Constructor | Input | Notes |
|---|---|---|
| `from_dict(data)` | a parsed dict | validates and builds |
| `from_json(text)` | a JSON string | parses, then delegates to `from_dict` |
| `from_json_file(path)` | a file path | reads the file, then parses — an invalid-JSON error names the path |

`ReachyMiniBridge` mirrors the trio (`ReachyMiniBridge.from_dict` / `from_json` / `from_json_file`), each building the config and then the bridge — see [bridge.md](bridge.md) "Constructed from a config". The bridge has no free `load_config` function.

### `backend`

One of `"real"`, `"sim"`, `"fake"` (the three backends in [robot.md](robot.md)); default `"real"`. Any other value is a `ConfigError`.

### `robot` block — upstream kwargs, verbatim

The keyword arguments for upstream's `reachy_mini.ReachyMini(...)` constructor (`robot_name`, `host`, `port`, `connection_mode`, `media_backend`, `timeout`, `automatic_body_yaw`, `log_level`, …), carried as a raw `dict` and forwarded verbatim through `build_robot(backend, **robot)` ([robot.md](robot.md)). The bridge does not re-model these as typed fields: their names, defaults, and deprecations are upstream's, and forwarding them keeps the bridge in step with each upstream release without a mirror to maintain. Upstream's defaults stand for anything not given.

- **Keys are validated against upstream's signature.** `from_dict` checks each key against `inspect.signature(reachy_mini.ReachyMini)` and raises `ConfigError` naming an unknown key — so a typo fails at config time with the key's name rather than as a `TypeError` at connect time. Values are not validated here; upstream checks them when it connects.
- **Two keys are reserved and rejected:** `use_sim` (the bridge derives it from `backend`) and `spawn_daemon` (the bridge's own `daemon` block manages the daemon — upstream's flag launches the viewer variant with no readiness wait and no environment scrub, which does not work from a process that has imported `reachy_mini`; see [daemon.md](../daemon/daemon.md)). Either key is a `ConfigError` that points at `backend` / `daemon.spawn`.
- **On `fake`, `robot` is validated but not applied** — `FakeReachyMini()` takes no options — so a config written for `sim` or `real` runs offline by flipping `backend` alone.
- **When the `daemon` block spawns or borrows a daemon**, the effective options fill in what a locally managed daemon needs unless the caller set them: `host="127.0.0.1"`, `port=8000`, `connection_mode="network"`, `media_backend="local"` (an externally started daemon serves neither the IPC transport nor the WebRTC media path — see [../docs/guides/running-daemons.md](../../docs/guides/running-daemons.md)). `host` must then be a loopback address — `127.0.0.1` or `localhost`; IPv6's `::1` is refused too, since upstream's SDK client forms its URLs from the raw host and could never reach it — anything else is a `ConfigError`.

### `daemon` block → `DaemonConfig`

How the bridge brings up the daemon the robot client talks to — the MuJoCo daemon for `sim`, the hardware daemon of a robot attached to this machine over USB for `real`. Behavior is specified in [daemon.md](../daemon/daemon.md); this block is its configuration.

| Field | Type | Default | Description |
|---|---|---|---|
| `spawn` | `"never"` \| `"auto"` \| `"always"` | `"never"` | `never`: connect only, to a daemon someone else runs. `auto`: reuse a daemon already ready at `robot.host:port`, else spawn one and own its teardown. `always`: spawn and own one; the port already in use is an error. |
| `headless` | bool | `true` | `sim` only. `true` launches the headless MuJoCo daemon (motion + audio; the rendered camera on Linux, where it draws offscreen, none on macOS — [sim_daemon.md](../daemon/sim_daemon.md) "The headless camera"; a `webcam` camera works on either); `false` launches the viewer (under `mjpython` on macOS; adds the window's GL context, so the `sim` camera works on every platform; needs an unlocked GUI session). |
| `scene` | string \| null | `null` | `sim` only. An upstream MuJoCo scene *name* (`empty`, `minimal`), passed as `--scene` when set — or, when it ends in `.xml`, the path of a scene *file* the bridge's own launcher loads (hidden-by-default props — a face today — a test shows/moves from tests: [sim_scene.md](../testing/sim_scene.md), written by `write_test_scene`). |
| `kinematics_engine` | `"analytical"` \| `"placo"` \| `"nn"` | `"analytical"` | The kinematics engine the spawned daemon solves every head target through — `sim` and `real` alike: upstream's MuJoCo backend takes the same engine as its hardware backend, and [daemon.md](../daemon/daemon.md) "The launch command" passes `--kinematics-engine` on every recipe. `analytical` is upstream's default, the closed-form solver of base `reachy-mini`; `placo` the full-URDF QP solver, the one engine with gravity compensation ([bridge.md](bridge.md) "Motors"), which needs the `placo` extra (`reachy-mini-bridge[placo]`, [../project.md](../project.md)) — a spawn without it is a `DaemonError` naming the extra; `nn` upstream's ONNX networks fitted from Placo data, in base `reachy-mini`. See "Kinematics engines" below. Like `scene` or `camera`, it applies only to a daemon the bridge spawns: a borrowed daemon — a wireless robot's included — runs whatever it was started with, and the bridge reads the engine where it matters (the gravity-compensation guard). |
| `camera` | object → `SimCameraSettings` | `{"source": "sim"}` | `sim` only. What the sim daemon's camera shows ([sim_daemon.md](../daemon/sim_daemon.md) "Camera sources"): `source` `"sim"` renders the scene from the robot's eye camera (under the viewer, and headless on Linux where it renders offscreen — a macOS headless sim has none); `"webcam"` relays a host camera instead — the person in front of the computer is who the simulated robot sees and follows, headless or viewer. See the table below. |
| `sim_displays` | object → `SimDisplaySettings` | `{}` | `sim` only, viewer only. What the MuJoCo viewer window shows besides the scene: one boolean per display, all off by default — `camera_overlay`, `robot_gaze`, `face_markers` ([sim_displays.md](../daemon/sim_displays.md)). A display on with `headless: true` is a `ConfigError`. See the table below. |
| `preload_datasets` | bool | `true` | `true` passes `--preload-datasets`: the daemon downloads the recorded-move datasets (emotions, dances) in the background after it starts, so the first `play_emotion` does not wait on a download; readiness is not delayed. `false` passes `--no-preload-datasets` (the datasets then load on first use). |
| `startup_timeout` | number | `45.0` | Seconds to wait for a spawned or booting daemon to become ready — a positive, finite number (`Infinity` / `NaN`, which JSON parsers may accept, would make the wait unbounded). |

```python
@dataclass
class DaemonConfig:
    spawn: str = "never"
    headless: bool = True
    scene: str | None = None
    camera: SimCameraSettings = field(default_factory=SimCameraSettings)
    sim_displays: SimDisplaySettings = field(default_factory=SimDisplaySettings)
    preload_datasets: bool = True
    startup_timeout: float = 45.0
```

The `camera` object (`SimCameraSettings`, with the same `from_dict` / `from_json` / `from_json_file` trio):

| Field | Type | Default | Description |
|---|---|---|---|
| `source` | `"sim"` \| `"webcam"` | `"sim"` | `sim`: the rendered eye camera. `webcam`: a host camera, treated as fixed at the robot's rest eye pose for tracking. |
| `device` | string \| integer \| null | `null` | `webcam` only. The capture device: `null` the default camera, an integer a macOS device index, a string a Linux device path (`/dev/video0`). |
| `hfov_deg` | number | `70.0` | `webcam` only. The webcam's horizontal field of view in degrees, from which the tracker's intrinsics are derived — match it to the camera for an accurate aim. |

```python
@dataclass
class SimCameraSettings:
    source: str = "sim"
    device: str | int | None = None
    hfov_deg: float = 70.0
```

`device` and `hfov_deg` are accepted with `source: "sim"` and play no part there, so a file switches sources by changing `source` alone. The launch flags they produce are [daemon.md](../daemon/daemon.md)'s.

The `sim_displays` object (`SimDisplaySettings`, with the same trio) is the home of the viewer's displays: one boolean per display, every one off by default, and `enabled()` lists the names of those that are on. The names are `SIM_DISPLAYS`, the values `--sim-display` takes ([sim_daemon.md](../daemon/sim_daemon.md)); a new display is one more field and one more name there, nowhere else.

| Field | Type | Default | Description |
|---|---|---|---|
| `camera_overlay` | bool | `false` | Draw the sim daemon's camera stream — the webcam, or the rendered eye camera — as a picture in the top-right corner of the MuJoCo viewer window ([sim_displays.md](../daemon/sim_displays.md) "Camera overlay"). |
| `robot_gaze` | bool | `false` | Draw the robot's gaze — the eye camera's optical axis, the one the head tracker aligns with the followed face — as a line in the viewer's 3D scene ([sim_displays.md](../daemon/sim_displays.md) "The robot's gaze"). |
| `face_markers` | bool | `false` | Draw an ellipsoid per detected face where the bridge places it in the viewer's 3D scene; the bridge sends them while its detection loop runs ([sim_displays.md](../daemon/sim_displays.md) "The face markers"). Read by both sides: the launch flag, and the bridge's publisher. With no detector configured there is nothing to draw, which is not an error. |

```python
@dataclass
class SimDisplaySettings:
    camera_overlay: bool = False
    robot_gaze: bool = False
    face_markers: bool = False
```

A display is drawn in the viewer window, so any display on needs `headless: false`; with `headless` at its default `true` the block is a `ConfigError` naming the display and `daemon.headless`, rather than a daemon that silently shows nothing.

#### Kinematics engines

The daemon solves every head target through one engine, chosen at its start (upstream `reachy_mini` 1.10: `--kinematics-engine`, reported at `GET /api/kinematics/info`). `daemon.kinematics_engine` names it in the bridge's words; `KINEMATICS_ENGINES` is the tuple of those words and `UPSTREAM_KINEMATICS_ENGINES` maps each to the name upstream's flag takes — the launch command passes the latter, the gravity-compensation guard compares against it ([bridge.md](bridge.md) "Motors"):

| `kinematics_engine` | Upstream name | What it is | Install | Head limits it applies | Gravity compensation |
|---|---|---|---|---|---|
| `analytical` (default) | `AnalyticalKinematics` | closed-form inverse kinematics over the arm and rod lengths, in Rust; never fails to solve | base `reachy-mini` | relative head/body yaw 65°, body yaw 160° | no — the robot daemon rejects the mode |
| `placo` | `Placo` | Placo (on Pinocchio) over the robot's full URDF: an iterative QP per target, warm-started from the last solution; an unreachable pose is rejected by the daemon ("head pose not achievable") | the `placo` extra — `reachy-mini-bridge[placo]` → `reachy-mini[placo_kinematics]` (wheels for macOS and Linux, none for Windows) | tilt cone 35°, relative yaw 55°, absolute yaw 179° | yes, on hardware — the one engine that computes the compensating torques |
| `nn` | `NN` | two ONNX networks (forward and inverse) fitted from Placo's data; its fit, measured on the sim: the neutral it reaches sits 8.5 mm forward and 1.45° pitched from the origin, and a head aimed 18° to the side over-rotates by 3 to 4° — enough to put a followed face 6% off the image centre ([open question 3](#open-questions)) | base `reachy-mini` (`onnxruntime` and the models ship with it) | not characterised | no |

The engine is a **config choice, never an install side effect**: installing the `placo` extra changes nothing until a config asks for `placo`. Whether the package is installed is checked when the daemon is launched, not when the config loads, so a config for a borrowed daemon (`spawn: "never"`) loads without it. The sim honours the choice as the robot does — its targets are solved through the engine named — so the live tier runs the same suite on each ([../testing/testing.md](../testing/testing.md) "One engine per run"); gravity compensation itself is hardware-only ([bridge.md](bridge.md) "Motors").

`spawn` other than `"never"` is valid with `backend` `"sim"` or `"real"`; with `fake`, which has no daemon, it is a `ConfigError`. For `real` the bridge spawns the daemon of a robot plugged into this machine (a Lite over USB); a wireless robot runs its own daemon on the robot, so a config for one leaves `spawn` at `"never"` and points `robot.host` at it.

The fields that apply depend on the backend: `spawn`, `preload_datasets` and `startup_timeout` apply to both; `headless`, `scene`, `camera` and `sim_displays` are MuJoCo knobs that play no part on `real`. They are accepted there, so one file switches `sim` ↔ `real` by changing `backend` alone.

### `tts` block — a tts-engine `engine` block, verbatim

The default synthesizer for `say`. When present, it is exactly a tts-engine **`engine` block** (`module` + optional `player`, *not* wrapped under an `"engine"` key), carried as a raw `dict` and handed to `TTSEngineSynthesizer` ([audio.md](../audio/audio.md)) at `ReachyMiniBridge` construction — which runs it through `TTSEngineConfig.from_dict`, tts-engine's own validation. The config layer checks only the shape it can without importing tts-engine: the block is an object whose `module` is an object with a non-empty string `type`.

- The key is `tts`, not `synthesizer`: it configures the shipped tts-engine adapter specifically. A custom `SpeechSynthesizer` is code, passed as `ReachyMiniBridge(config, synthesizer=...)`, and an explicit synthesizer wins over the block (the block is then not consumed, and tts-engine is not imported).
- The extra for the provider `module.type` names must be installed (`reachy-mini-bridge[tts-pocket]` / `[tts-elevenlabs]` / `[tts-gradium]`, see [project.md](../project.md)); otherwise the adapter build fails with tts-engine's `ConfigError`, which — like any other adapter-build failure, typically the module's `api_key_env` unset — degrades to no voice, see [bridge.md](bridge.md) "Constructed from a config".
- `player` is accepted for symmetry with a tts-engine file and has no effect: the bridge feeds tts-engine a robot-speaker sink in place of its local player.
- No environment variables are read at config time; a module's `api_key_env` is resolved by the module at engine construction, as in tts-engine.

### `audio` block → `AudioSettings`

| Field | Type | Default | Description |
|---|---|---|---|
| `xvf3800` | list of `[name, [values…]]` pairs \| null | `null` | The XVF3800 audio-processor profile applied on session start ([audio.md](../audio/audio.md) "XVF3800 config applied on session start"). `null` keeps the firmware defaults. |

```python
@dataclass
class AudioSettings:
    xvf3800: list[Any] | None = None
```

The value is carried verbatim to `MediaSession(robot, audio_config=...)`, whose upstream target is `apply_audio_config(config: Sequence[tuple[str, Sequence[AudioControlValue]]])` — a JSON list of two-item lists satisfies that `Sequence` shape directly. The config layer checks the shape: a list whose items are two-item lists with a string first item.

### `face_detection` block → `FaceDetectionSettings`

Face detection ([user_perception.md](../vision/user_perception.md)): which detector finds the faces, whether the detection loop runs from session entry, and what the detector may cost. Detection is opt-in: with no detector named, nothing is detected and nothing tracks. The block is named for the module and the verb (`face_detection.py`, `set_face_detection`); `bridge.faces` is its output. Each switch has a runtime counterpart — `set_face_detection(enabled)` for the switch, `set_face_detector(factory)` for the custom detector ([bridge.md](bridge.md) "Faces"); `width` and `target_fps` are read when the loop starts.

| Field | Type | Default | Description |
|---|---|---|---|
| `detector` | `null` \| `"yunet"` \| `"custom"` | `null` | The face detector the bridge runs on the camera feed's frames ([camera.md](../vision/camera.md)). `null`: none — no detection, no tracking; `enabled: true` or `motion.tracking: true` is then a `ConfigError`. `yunet`: the shipped default, upstream's YuNet model run by the bridge ([user_perception.md](../vision/user_perception.md) "The shipped detector — `yunet.py`"); nothing to install, the weights downloaded into the Hugging Face cache on first use. `custom`: the caller's detector — any other model, a vision library's included; needs `face_detector`. |
| `enabled` | bool | `false` | Run the detection loop from session entry, so `bridge.faces` reports who is there. Needs no motors, needs a `detector`. The loop also runs whenever head tracking is on ([user_perception.md](../vision/user_perception.md) "Configuration"), whatever this says. |
| `width` | positive integer \| null | `320` | The width in pixels the shipped detector works at: the frame is subsampled by an integer stride to about this width before detection, every point scaled back. 320 is upstream's own detection width. `null`: the full frame, no subsampling. Cost per frame against precision — 640 quadruples YuNet's cost and halves its landmark error. A custom detector's working width is its author's, so the field plays no part with `custom`. |
| `target_fps` | positive number \| null | `null` | A ceiling on detections per second, enforced by the loop skipping frames, for every detector. `null`: once per new camera frame. A ceiling, not a guarantee: the loop never detects faster than the feed delivers frames or the detector returns. The lever for the wireless robot, whose camera streams at a nominal 30 fps. |
| `face_detector` | a zero-argument callable returning a `FaceDetector`, or `None` — **Python only** | `None` | The custom detector's factory, used when `detector` is `"custom"` (set on the dataclass, as `motion.idle_move` is; a JSON file names the source and code supplies the detector). Checked at session entry: `"custom"` with none registered fails bring-up with `ValueError`. |

```python
@dataclass
class FaceDetectionSettings:
    # None | "yunet" | "custom"
    detector: str | None = None
    enabled: bool = False
    # the width the shipped detector works at; None = the full frame
    width: int | None = 320
    # a ceiling on detections per second; None = once per new frame
    target_fps: float | None = None
    # Python only: the custom detector's factory
    face_detector: Callable[[], FaceDetector] | None = None
```

`FaceDetector` is [user_perception.md](../vision/user_perception.md)'s protocol, imported for type checking only. The valid detector names are the module constant `FACE_DETECTORS = ("yunet", "custom")`; `None` is the absence of one.

### `motion` block → `MotionSettings`

The initial values of everything that shapes the robot's behaviour at rest: the loop's own modes (`presence`, `idle`, `idle_move`, [motion.md](../motion/motion.md) "Presence and the idle mode"; `tracking`, the gaze layer — [motion.md](../motion/motion.md) "The gaze layer") and the one daemon-side mode the bridge arms around them (`wobbling`). Applied when the session starts; each has a runtime verb — `set_presence(enabled)` / `set_idle(mode)` / `set_idle_move(factory)` / `set_wobbling(enabled)` / `start_head_tracking(...)` / `stop_head_tracking()` — that changes it while entered ([bridge.md](bridge.md)).

| Field | Type | Default | Description |
|---|---|---|---|
| `presence` | bool | `true` | The background behaviour: while on, the loop fills every idle moment with the idle move so the robot never goes dead between verbs. `false` makes the bridge command the head only while a verb runs — for a caller driving the head through the raw robot. Emotions play either way. |
| `idle` | `"breathing"` \| `"hold"` \| `"custom"` | `"breathing"` | Which idle move presence plays: `breathing` is the built-in animation (slow breaths with random rests between them, the head roaming in roll/pitch/yaw, the antennas roaming and flicking independently), `hold` a still neutral, `custom` the caller's own idle move — the one in `idle_move`, or registered later with `set_idle_move`; with none registered `custom` plays the hold ([motion.md](../motion/motion.md) "Custom idle moves"). Ignored while `presence` is off. |
| `idle_move` | a zero-argument callable returning an `IdleMove`, or `None` — **Python only** | `None` | The factory of the custom idle move, stored whatever `idle` is and played whenever `idle` is `"custom"`. It is code, so it is set on the dataclass (`MotionSettings(idle="custom", idle_move=SlowNod)`); a JSON file names the mode and code supplies the move. Checked when the session starts ([motion.md](../motion/motion.md) "Custom idle moves"). |
| `wobbling` | bool | `true` | A robot that talks sways its head while it talks: `ReachyMiniBridge` enables upstream's audio-reactive head wobbling on entry and switches it off again on exit whenever it is still on ([bridge.md](bridge.md) "Audio-reactive motion (head wobbling)"; mechanism in [audio.md](../audio/audio.md) "Head wobbling"). `false` keeps the head still while audio plays (a caller driving the head precisely, or a quiet demo). |
| `tracking` | bool | `false` | Whether the bridge's head tracker runs from session entry ([bridge.md](bridge.md) "Attention / gaze (autonomous)", [head_tracking.md](../motion/head_tracking.md)): the robot keeps the tracked face in view, the aim composed into the idle move. It implies detection — the detection loop runs while either `face_detection.enabled` or this is on — and so needs a `face_detection.detector` (`true` with none is a `ConfigError`). A mode like `presence`: it needs no motors (its aim takes effect once the motion loop runs) and holds until `stop_head_tracking()`. `false` leaves tracking off until `start_head_tracking(...)` is called explicitly. |

```python
@dataclass
class MotionSettings:
    presence: bool = True
    # "breathing" | "hold" | "custom"
    idle: str = "breathing"
    # Python only: the custom idle move's factory
    idle_move: Callable[[], IdleMove] | None = None
    wobbling: bool = True
    tracking: bool = False
```

`IdleMove` is [motion.md](../motion/motion.md)'s base class, imported for type checking only, so the config module still loads without `reachy_mini`. The valid modes are the module constant `IDLE_MODES = ("breathing", "hold", "custom")`.

It is one block, not several top-level keys, because every field configures the same thing from a caller's perspective — what the robot looks like when nothing else is commanding it — even though four live in the bridge's own loop (`presence`, `idle`, `idle_move`, `tracking`) and one is a daemon-side mode the bridge merely arms (`wobbling`); more knobs in either category (a listening cue, a tracking weight) would land beside them. Face *detection* is perception, not behaviour at rest, hence its own `face_detection` block.

### `ConfigError`

`ConfigError(ValueError)`, in `errors.py` — a malformed config is invalid input data, the same taxonomy tts-engine uses and the one [robot.md](robot.md) reserves `ValueError` for (as opposed to `BridgeError`'s runtime failures). A caller catches `ConfigError` for the specific type or `ValueError` for any bad-config surface, including the tts-engine `ConfigError` raised when the `tts` block is consumed.

### Validation rules

All enforced by `ReachyMiniConfig.from_dict` (delegating to `DaemonConfig.from_dict` / `AudioSettings.from_dict`), so every constructor path validates identically:

- Invalid JSON raises `ConfigError` (with the file path from `from_json_file`).
- The top-level value and the `robot`, `daemon`, `tts`, `audio`, `face_detection`, and `motion` blocks must be JSON objects (`tts` may be `null`); `backend` is the one top-level scalar. Shape failures raise `ConfigError`, never a raw `AttributeError` / `TypeError`.
- Unknown top-level keys, and unknown keys inside `daemon` / `daemon.camera` / `daemon.sim_displays` / `audio` / `face_detection` / `motion`, raise `ConfigError` naming the key (the blocks are ours, so a typo is caught). Unknown keys inside `robot` raise `ConfigError` per the upstream-signature check above; `tts.module` is left to tts-engine.
- `face_detection.detector` is `null` or one of {`yunet`, `custom`}; `face_detection.enabled` is a boolean; `face_detection.width` is `null` or a positive integer other than `bool`; `face_detection.target_fps` is `null` or a positive finite number other than `bool`; `face_detection.face_detector` in a dict / JSON config is a `ConfigError` (it is set from code, as `motion.idle_move` is).
- `face_detection.enabled: true` or `motion.tracking: true` with `face_detection.detector` `null` is a `ConfigError` naming both fields — the one cross-block rule, checked by `ReachyMiniConfig.from_dict` once both blocks are built. (A `ReachyMiniConfig` assembled directly in code is not validated; the bridge raises `ValueError` for the same contradiction at session entry, [user_perception.md](../vision/user_perception.md) "Configuration".)
- `backend` ∈ {`real`, `sim`, `fake`}; `daemon.spawn` ∈ {`never`, `auto`, `always`}; `daemon.spawn != "never"` requires `backend` `sim` or `real`.
- `daemon.kinematics_engine` ∈ {`analytical`, `placo`, `nn`} (a string; the `placo` package's presence is [daemon.md](../daemon/daemon.md)'s check, at launch); `daemon.headless` / `daemon.preload_datasets` are booleans; `daemon.scene` a non-empty string or `null`; `daemon.startup_timeout` a positive finite number other than `bool` (`inf` / `nan` are rejected: the readiness deadline must be reachable, [../daemon/daemon.md](../daemon/daemon.md)).
- `daemon.camera` is an object with no unknown keys; `source` ∈ {`sim`, `webcam`}; `device` is `null`, a non-empty string, or a non-negative integer other than `bool`; `hfov_deg` a number other than `bool`, strictly between 1 and 179.
- `daemon.sim_displays` is an object with no unknown keys, every value a boolean; a display `true` while `daemon.headless` is `true` is a `ConfigError` naming both.
- `robot` must not contain `use_sim` or `spawn_daemon`; with `daemon.spawn != "never"`, `robot.host` (if given) must be a loopback address.
- `tts`, when not `null`, is an object with a `module` object whose `type` is a non-empty string.
- `audio.xvf3800`, when not `null`, is a list of two-item lists with a string first item.
- `motion.presence`, `motion.wobbling` and `motion.tracking` are booleans; `motion.idle` ∈ {`breathing`, `hold`, `custom`}.
- `motion.idle_move` in a dict / JSON config is a `ConfigError` whose message points at `MotionSettings(idle_move=...)`: the field is set from code. The factory itself is checked when the session starts, not by the config layer.

## Relationship to the other specs

- **[bridge.md](bridge.md):** `ReachyMiniBridge` is constructed from a `ReachyMiniConfig` (or a backend-string shorthand for one), mirrors the `from_*` trio, applies `motion.wobbling` on entry, starts the detection loop and the head tracker from `face_detection` and `motion.tracking`, and starts the motion loop with `motion.presence`, `motion.idle` and `motion.idle_move`.
- **[user_perception.md](../vision/user_perception.md):** the `face_detection` block selects the detector, the detection switch and the detector's cost knobs; `motion.tracking` the head tracker; both switches need a detector.
- **[motion.md](../motion/motion.md):** the `motion` block is the initial state of the loop's presence, idle mode, custom idle move and gaze layer (and, for `wobbling`, of the daemon-side mode the bridge arms around it).
- **[robot.md](robot.md):** the `robot` block is what `build_robot(backend, **robot)` forwards.
- **[daemon.md](../daemon/daemon.md):** the `daemon` block configures the bridge-owned daemon lifecycle; `backend` selects its launch recipe; `daemon.kinematics_engine` the `--kinematics-engine` every recipe passes.
- **[sim_daemon.md](../daemon/sim_daemon.md):** `daemon.camera` selects the sim daemon's camera source; `daemon.sim_displays` turns its sim displays on, specified in [sim_displays.md](../daemon/sim_displays.md).
- **[audio.md](../audio/audio.md):** the `tts` block builds the default `TTSEngineSynthesizer`; `audio.xvf3800` is the session's `audio_config`; `motion.wobbling` arms the head wobbler on the speaker path.
- **[testing_support.md](../testing/testing_support.md):** the `live_bridge` fixture builds its bridge from a `ReachyMiniConfig` whose `robot` block carries the harness's connection options.

## Open questions

1. **Environment-variable interpolation in config files.** Whether string values in the file may reference environment variables (e.g. a `${REACHY_MINI_HOST}` for `robot.host`) is deferred until a deployment needs it; tts-engine's modules already resolve their own `*_env` keys.
2. **Named XVF3800 profiles.** Whether `audio.xvf3800` also accepts a profile *name* (e.g. `"conversation"`) resolving to a bridge-shipped tuned profile is deferred with [audio.md](../audio/audio.md) open question 2 — it needs hardware to tune against.
3. **The engines against the bridge's own moves.** Whether every pose the bridge sends — the breaths and the idle roaming, the gaze aim, the head tracker's reach, the recorded emotions — fits Placo's 35° tilt cone and 55° relative yaw, what a moving target costs Placo per solve (a QP per update; more on the wireless robot's Pi than on a Mac), and NN's accuracy and failure mode on those moves are measured, not assumed: the live tier run on each engine ([../testing/testing.md](../testing/testing.md) "One engine per run") is the measurement, and its findings land here — a rejected move or a missed threshold is recorded with the engine and the move. Measured on the headless sim (2026-10-07, `reachy_mini` 1.10.0, an M-series Mac): Placo rejects none of the tier's moves and solves a moving target in about 0.2 ms; the still neutral rests within 0.1 mm of the origin on analytical and Placo, and **8.5 mm forward, 1.45° pitched on NN** — the networks' fit, a steady bias rather than a failure, which is why the tier's "back to neutral" check measures the return against the engine's own resting pose. On the viewer sim Placo passes the whole tier — every tracking test settles on its expected yaw — and NN over-rotates by 3 to 4° at 18° of yaw (the tracker aims from the head pose the daemon reports, NN's own forward kinematics), so two tracking tests fail on the image-centring check; NN is therefore an accepted value with a documented error, outside CI ([../testing/ci.md](../testing/ci.md)), until the networks improve or the tracker learns to correct for it.
4. **`check_collision`.** Upstream's `--check-collision` (Placo only) has no field; one more boolean, valid with `placo` alone, when someone asks for it.
