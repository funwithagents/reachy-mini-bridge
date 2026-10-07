# Configuration reference — `ReachyMiniConfig`

Every field of the bridge's declarative config: its default, what it does, and the verb that changes it at run time. This is the consumer's home for the config; the design and the validation rules behind each block are in [specs/core/config.md](../../specs/core/config.md). [config.example.json](../../config.example.json) is the inventory with placeholder values, and [examples/configs/](../../examples/configs/) holds a short profile per setup.

`ReachyMiniConfig` is one declarative object, buildable from a dict, a JSON string or a JSON file — `ReachyMiniBridge.from_dict(...)`, `.from_json(...)`, `.from_json_file(...)` take the same input; `ReachyMiniBridge("fake")` is the backend-string shorthand for a config with everything else at its default. **Every block is optional** — `ReachyMiniConfig()` is a valid config: the real robot, upstream's connection defaults, no daemon management, no voice, everything at rest switched on, no detector. Unknown keys in a bridge-owned block are a `ConfigError`, so a typo is caught at load time.

```json
{
  "backend": "sim",

  "robot": {
    "host": "127.0.0.1",
    "port": 8000
  },

  "daemon": {
    "spawn": "auto",
    "headless": false,
    "camera": {
      "source": "webcam"
    }
  },

  "tts": {
    "module": {
      "type": "pocket",
      "voice": "george"
    }
  },

  "face_detection": {
    "detector": "yunet",
    "enabled": true,
    "width": 320,
    "target_fps": null
  },

  "motion": {
    "presence": true,
    "idle": "breathing",
    "wobbling": true,
    "tracking": true
  }
}
```

That is the example config in brief — the MuJoCo viewer, started for you, seeing through your webcam, with a local voice. It is the maximal setup, a demo and the inventory; a minimal one is a profile in [examples/configs/](../../examples/configs/).

## `backend`

`"real"` (default), `"sim"` or `"fake"` — see [backends-and-capabilities.md](backends-and-capabilities.md). Changing this one string moves a config between the robot and the simulator; every other block is accepted on both, even where it does nothing (the MuJoCo knobs on `real`). The offline `fake` has no daemon, so a `daemon.spawn` other than `"never"` is a `ConfigError` there — set it to `"never"` (or drop the `daemon` block) and the same file runs on the fake.

## `robot` — how to reach the robot

Keyword arguments forwarded verbatim to upstream's `ReachyMini(...)`, so their names and defaults are upstream's, not the bridge's. Unknown keys are rejected at config time (checked against the upstream signature) rather than failing at connect time. The ones you are likely to set:

| Field | Default | What it does |
|---|---|---|
| `host` | `"reachy-mini.local"` | Where the daemon listens. A wireless robot's IP or hostname; `"127.0.0.1"` for a local daemon |
| `port` | `8000` | The daemon's port |
| `connection_mode` | `"auto"` | Transport to the daemon: `"auto"`, `"localhost_only"` or `"network"` (upstream 1.10's values; the bridge checks the key, upstream the value) |
| `media_backend` | `"default"` | How audio/video are carried; `"local"` is what a local daemon serves, the default is the WebRTC stream a wireless robot serves |
| `timeout` | `5.0` | Seconds to wait on the connection |
| `robot_name` | `"reachy_mini"` | The robot's name on the daemon |
| `automatic_body_yaw` | `true` | Upstream's automatic body-yaw following |
| `log_level` | `"INFO"` | Upstream client log level |

Two keys are **reserved**: `use_sim` (derived from `backend`) and `spawn_daemon` (use `daemon.spawn`) — either one is a config error pointing you at the right field. On `fake` the block is validated but unused. When the bridge manages the daemon (`daemon.spawn` other than `"never"`), it fills in what a local daemon needs for anything you left unset — `host` `127.0.0.1`, `port` `8000`, `connection_mode` `"network"`, `media_backend` `"local"` — and `host`, if you do set it, must be `127.0.0.1` or `localhost` (IPv4 loopback: upstream's client forms its URL from the raw host and cannot reach `::1`).

## `daemon` — whether the bridge starts one

| Field | Default | What it does |
|---|---|---|
| `spawn` | `"never"` | `"never"`: only connect, to a daemon you run (what a wireless robot needs). `"auto"`: reuse one already listening at `host:port`, else start one and stop it on exit. `"always"`: insist on starting one — a port already in use is an error |
| `headless` | `true` | *sim only.* `true` runs MuJoCo with no window: motion and audio, and on Linux the rendered camera too (offscreen through EGL); on macOS no camera. `false` opens the **viewer** (under `mjpython` on macOS), so you watch the robot and the `sim` camera works everywhere; needs an unlocked GUI session |
| `scene` | `null` | *sim only.* An upstream scene name (`"empty"`, `"minimal"`), or the path of a scene `.xml` for the bridge's launcher — how the test scene's portrait gets loaded ([specs/testing/sim_scene.md](../../specs/testing/sim_scene.md)) |
| `kinematics_engine` | `"analytical"` | The kinematics engine the daemon solves every head target through — `"analytical"`, `"placo"` or `"nn"`, sim and robot alike; see [Kinematics engines](#kinematics-engines) below. `"placo"` needs the `placo` extra, checked when the daemon starts |
| `camera` | `{"source": "sim"}` | *sim only.* What the sim's camera shows — see the table below |
| `sim_displays` | all `false` | *sim viewer only.* What the viewer window draws besides the scene — see the table below |
| `preload_datasets` | `true` | Downloads the recorded-move datasets in the background at startup, so the first `play_emotion` doesn't wait on a download. Readiness isn't delayed either way |
| `startup_timeout` | `45.0` | Seconds to wait for a spawned daemon to become ready (a positive, finite number) |

`spawn` other than `"never"` needs `backend` `"sim"` or `"real"` (`fake` has no daemon). On `real` it starts the hardware daemon of a robot plugged into **this machine** over USB — it wakes the robot, and puts it to sleep on exit. `headless`, `scene`, `camera` and `sim_displays` are MuJoCo knobs and play no part on `real`; `kinematics_engine` applies to both. Every field here describes a daemon the bridge starts: a daemon you run yourself, a wireless robot's included, keeps whatever it was started with. The commands the bridge runs, for starting a daemon by hand: [../guides/running-daemons.md](../guides/running-daemons.md).

### Kinematics engines

The daemon turns every head pose into motor angles through one kinematics engine, chosen when it starts. The bridge passes the one the config names to every daemon it spawns, the simulator's included, so the sim rejects and accepts the same poses as a robot on that engine would.

| `kinematics_engine` | What it is | Needs | Head limits | Gravity compensation |
|---|---|---|---|---|
| `"analytical"` (default) | upstream's default: a closed-form solver, never fails to solve | nothing beyond the base package | relative head/body yaw 65°, body yaw 160° | no — `set_motors_state("gravity_compensation")` raises |
| `"placo"` | a whole-body solver over the robot's URDF; a pose it cannot reach is rejected by the daemon | the `placo` extra (`reachy-mini-bridge[placo]`); a spawn without it fails naming the extra | tilt cone 35°, relative yaw 55° | yes, on a robot (the only engine that computes the compensating currents); the sim accepts the mode and does nothing |
| `"nn"` | two neural networks fitted from the Placo solver's data — less accurate: the head rests 8.5 mm forward of neutral and over-rotates by 3 to 4° at 18° of yaw, so a followed face sits a little off centre | nothing beyond the base package | not characterised | no |

Installing the `placo` extra changes nothing by itself: the engine is this field's choice. A stock wireless robot's own daemon runs the analytical engine ([backends-and-capabilities.md](backends-and-capabilities.md)); to run on another, start that daemon yourself with upstream's `--kinematics-engine` flag. What the bridge learned about the three engines is in [../internals/upstream-sdk-notes.md](../internals/upstream-sdk-notes.md).

**`daemon.camera`** — the sim's eyes ([specs/daemon/sim_daemon.md](../../specs/daemon/sim_daemon.md)):

| Field | Default | What it does |
|---|---|---|
| `source` | `"sim"` | `"sim"` renders the scene from the robot's eye camera (the viewer, or headless on Linux). `"webcam"` relays your computer's camera instead, so the person in front of the screen is who the simulated robot sees and follows — headless or viewer |
| `device` | `null` | *webcam only.* `null` is the default camera; an integer is a macOS device index, a string a Linux device path (`/dev/video0`) |
| `hfov_deg` | `70.0` | *webcam only.* The camera's horizontal field of view in degrees, which the tracker's intrinsics derive from — match it to your camera for an accurate aim |

**`daemon.sim_displays`** — what the viewer shows ([specs/daemon/sim_displays.md](../../specs/daemon/sim_displays.md)); every one is viewer only, so any of them `true` with `headless: true` is a `ConfigError`:

| Field | Default | What it does |
|---|---|---|
| `camera_overlay` | `false` | The camera stream as a picture in the top-right corner of the viewer window — the rendered eye camera, or the webcam mirrored |
| `robot_gaze` | `false` | A blue line along the eye camera's optical axis in the 3D scene: where the robot looks |
| `face_markers` | `false` | An ellipsoid per face the bridge detects, where the bridge places it (green for the face the head follows, labelled with its `track_id`); the bridge sends them to the daemon while the session runs |

## `tts` — the default voice for `say`

A [tts-engine](https://github.com/funwithagents/tts-engine) `engine` block, carried through verbatim: `module.type` picks the provider, and the matching `tts-*` extra must be installed. The remaining keys are the provider's own.

The local model (`tts-pocket`), no key and no network once its weights are cached:

```json
"tts": {
  "module": {
    "type": "pocket",
    "voice": "george",
    "device": "auto"
  }
}
```

A cloud provider (`tts-elevenlabs`), whose key is read from the named environment variable:

```json
"tts": {
  "module": {
    "type": "elevenlabs",
    "api_key_env": "ELEVENLABS_API_KEY",
    "voice_id": "..."
  }
}
```

Or Gradium (`tts-gradium`), keyed the same way:

```json
"tts": {
  "module": {
    "type": "gradium",
    "api_key_env": "GRADIUM_API_KEY",
    "voice_id": "..."
  }
}
```

Whatever the provider, `say` plays audio that arrives at the speaker's 16 kHz as is and resamples any other rate to it.

Omit the block and `say` raises unless you pass your own `SpeechSynthesizer`. A block that fails to build — extra not installed, API key unset — leaves the robot fully usable without a voice and puts the cause on `bridge.synthesizer_error`. Setting up a voice, task by task: [../guides/audio.md](../guides/audio.md).

## `audio` — the microphone array

| Field | Default | What it does |
|---|---|---|
| `xvf3800` | `null` | The XVF3800 audio-processor profile applied when the media session starts, as a list of `[name, [values…]]` pairs. `null` keeps the firmware defaults. Upstream reports a profile it could not write by a return value the bridge does not surface today — on the sim, which has no XVF3800, a profile is accepted and does nothing |

## `face_detection` — who is in front of the robot

| Field | Default | What it does | Runtime verb |
|---|---|---|---|
| `detector` | `null` | Which detector the bridge runs on the camera feed's frames: `null` none (no detection, no tracking — `enabled` or `tracking` on is then a config error); `yunet` the shipped detector, upstream's model run by the bridge (nothing to install; the weights download into the Hugging Face cache on first use); `custom` your own, registered from code with `FaceDetectionSettings(face_detector=...)` or `set_face_detector(...)` (see [../guides/custom-face-detector.md](../guides/custom-face-detector.md)); session entry refuses `custom` with none registered | — the name is config-only; `set_face_detector` registers the `custom` factory |
| `enabled` | `false` | Runs the detection loop from session entry, so `bridge.faces` reports who is there. Needs no motors, needs a `detector`. The loop also runs whenever `motion.tracking` is on, whatever this says | `set_face_detection` |
| `width` | `320` | The width the shipped detector works at — its cost per frame against its precision. 320 px is upstream's own; 640 quadruples the cost and halves the landmark error; `null` detects on the full frame. A custom detector's width is its author's | — |
| `target_fps` | `null` | A ceiling on detections per second, for any detector: the loop skips frames. `null` detects once per new camera frame. The lever for the wireless robot, whose camera streams at 30 fps. The loop logs the detector's mean time and rate once, 10 s in | — |

`face_detector` is a Python-only field (`FaceDetectionSettings(face_detector=MyDetector)`): a JSON file names the mode, code supplies the detector; the key in a file is a `ConfigError`.

## `motion` — what the robot does at rest

`presence` and `wobbling` default to `true`, `tracking` to `false` (it needs a `face_detection.detector`) and `idle` to `"breathing"`; each has a runtime verb that changes it while the session is entered.

| Field | Default | What it does | Runtime verb |
|---|---|---|---|
| `presence` | `true` | Fills every idle moment with the idle move, so the robot never looks dead between verbs. `false` commands the head only while a verb runs — for a caller driving the head itself. Emotions play either way | `set_presence` |
| `idle` | `"breathing"` | Which idle move presence plays: `breathing` (slow breaths with rests, the head roaming, the antennas flicking), `hold` (a still neutral) or `custom` (your own `IdleMove`, registered from code with `MotionSettings(idle_move=...)` or `set_idle_move`; the hold until one is registered). Ignored while `presence` is off | `set_idle` / `set_idle_move` |
| `wobbling` | `true` | Sways the head with every sound the robot plays. `false` keeps it still while audio plays | `set_wobbling` |
| `tracking` | `false` | The bridge's tracker keeps the reported face in view from session entry, the head breathing while it looks. Needs no motors (the head moves once they are `enabled`) but a `face_detection.detector` (`true` with none is a config error). `false` leaves it off until you call `start_head_tracking()` | `start_head_tracking` / `stop_head_tracking` |

`idle_move` is a Python-only field (`MotionSettings(idle="custom", idle_move=SlowNod)`), as `face_detector` is; the key in a file is a `ConfigError`. Writing one: [../guides/custom-idle-move.md](../guides/custom-idle-move.md).

## Validation

Every `from_*` constructor path validates identically, as it parses — before the bridge is constructed; a failure is a `ConfigError` (a `ValueError`) naming the field. The rules in full are in [specs/core/config.md](../../specs/core/config.md) "Validation rules"; the ones a consumer meets:

- `backend` is `real`, `sim` or `fake`; `daemon.spawn` is `never`, `auto` or `always`, and anything but `never` needs `sim` or `real`; `daemon.kinematics_engine` is `analytical`, `placo` or `nn`.
- `face_detection.enabled: true` or `motion.tracking: true` with no `face_detection.detector` is an error naming both fields.
- `daemon.sim_displays` entries `true` with `daemon.headless: true` is an error naming both.
- `robot.host`, when the bridge manages the daemon, must be `127.0.0.1` or `localhost`; `robot.use_sim` and `robot.spawn_daemon` are refused.
- Numbers are finite: `daemon.startup_timeout` positive, `face_detection.width` a positive integer, `face_detection.target_fps` positive, `daemon.camera.hfov_deg` strictly between 1 and 179.
- A `ReachyMiniConfig` assembled in code is not validated by the config layer; the bridge raises `ValueError` for the detector contradiction at session entry.
- The `tts` block is tts-engine's to validate, and it does so when the bridge is **constructed** — the provider is built then, a local model's weights loaded with it — reporting a failure through `bridge.synthesizer_error` rather than raising ([api.md](api.md#construction)). Everything else that can fail at run time — the daemon, the connection, the detector's build — fails in `start()`.
