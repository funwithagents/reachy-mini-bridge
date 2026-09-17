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

`ReachyMiniConfig` is the one declarative object that describes everything needed to bring up a `ReachyMiniApi` ([api.md](api.md)): which backend, how to reach — or spawn — its daemon, the default speech synthesizer, and the audio profile. It is buildable from a dict, a JSON string, or a JSON file, so a host application keeps its robot settings in its own configuration next to everything else and constructs a talking robot in one call. The same file switches from the real robot to the simulator to the offline fake by changing one string.

The shape and the constructor trio mirror [`tts-engine`'s configuration](../../tts-engine/specs/configuration.md) (`TTSEngineConfig`), so the two first-party libraries read the same way — and the bridge's `tts` block *is* a tts-engine `engine` block, carried through verbatim.

## Core concepts / Decided

### Top-level structure

```json
{
  "backend": "sim",
  "robot":  { "host": "127.0.0.1", "port": 8000 },
  "daemon": { "spawn": "auto", "headless": false },
  "tts":    { "module": { "type": "elevenlabs", "api_key_env": "ELEVENLABS_API_KEY", "voice_id": "..." } },
  "audio":  { "xvf3800": null },
  "motion": { "presence": true, "breathing": true, "wobbling": true, "tracking": true }
}
```

Every block is optional: `ReachyMiniConfig()` is a valid config — the `real` backend, upstream's connection defaults, no daemon management, no default synthesizer, firmware audio defaults, and every `motion` switch on. `config.example.json` in the repo root documents every field with placeholder values and is kept in sync with this spec. It describes the **sim viewer** (`backend: "sim"`, `daemon.spawn: "auto"`, `daemon.headless: false`): the configuration that shows the most — the robot moving in the MuJoCo window, and a working camera — so it is what the README and the [control panel](control_panel.md) start from.

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
    # everything that shapes the robot's behaviour at rest: the loop's own switches
    # (presence, breathing) and the daemon-side modes the api arms (wobbling, tracking)
    motion: MotionSettings = field(default_factory=MotionSettings)
```

The config module (`config.py`) imports neither `tts_engine` nor `reachy_mini` at module load: the raw blocks it carries are consumed by the layer that needs them (`tts` by [audio.md](audio.md)'s adapter, `robot` by [robot.md](robot.md)'s factory). The one upstream lookup — the `robot` key check below — imports `reachy_mini` lazily inside `from_dict`.

### Constructors

Both config classes that a caller builds directly (`ReachyMiniConfig`, and `DaemonConfig` / `AudioSettings` / `MotionSettings` for the nested blocks) expose the same symmetric trio as tts-engine, layered file → json → dict so all three share one validation path:

| Constructor | Input | Notes |
|---|---|---|
| `from_dict(data)` | a parsed dict | validates and builds |
| `from_json(text)` | a JSON string | parses, then delegates to `from_dict` |
| `from_json_file(path)` | a file path | reads the file, then parses — an invalid-JSON error names the path |

`ReachyMiniApi` mirrors the trio (`ReachyMiniApi.from_dict` / `from_json` / `from_json_file`), each building the config and then the api — see [api.md](api.md) "Constructed from a config". The bridge has no free `load_config` function.

### `backend`

One of `"real"`, `"sim"`, `"fake"` (the three backends in [robot.md](robot.md)); default `"real"`. Any other value is a `ConfigError`.

### `robot` block — upstream kwargs, verbatim

The keyword arguments for upstream's `reachy_mini.ReachyMini(...)` constructor (`robot_name`, `host`, `port`, `connection_mode`, `media_backend`, `timeout`, `automatic_body_yaw`, `log_level`, …), carried as a raw `dict` and forwarded verbatim through `build_robot(backend, **robot)` ([robot.md](robot.md)). The bridge does not re-model these as typed fields: their names, defaults, and deprecations are upstream's, and forwarding them keeps the bridge in step with each upstream release without a mirror to maintain. Upstream's defaults stand for anything not given.

- **Keys are validated against upstream's signature.** `from_dict` checks each key against `inspect.signature(reachy_mini.ReachyMini)` and raises `ConfigError` naming an unknown key — so a typo fails at config time with the key's name rather than as a `TypeError` at connect time. Values are not validated here; upstream checks them when it connects.
- **Two keys are reserved and rejected:** `use_sim` (the bridge derives it from `backend`) and `spawn_daemon` (the bridge's own `daemon` block manages the daemon — upstream's flag launches the viewer variant with no readiness wait and no environment scrub, which does not work from a process that has imported `reachy_mini`; see [daemon.md](daemon.md)). Either key is a `ConfigError` that points at `backend` / `daemon.spawn`.
- **On `fake`, `robot` is validated but not applied** — `FakeReachyMini()` takes no options — so a config written for `sim` or `real` runs offline by flipping `backend` alone.
- **When the `daemon` block spawns or borrows a daemon**, the effective options fill in what a locally managed daemon needs unless the caller set them: `host="127.0.0.1"`, `port=8000`, `connection_mode="network"`, `media_backend="local"` (an externally started daemon serves neither the IPC transport nor the WebRTC media path — see [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md)). `host` must then be a loopback address (`127.0.0.1` / `localhost` / `::1`); anything else is a `ConfigError`.

### `daemon` block → `DaemonConfig`

How the bridge brings up the daemon the robot client talks to — the MuJoCo daemon for `sim`, the hardware daemon of a robot attached to this machine over USB for `real`. Behavior is specified in [daemon.md](daemon.md); this block is its configuration.

| Field | Type | Default | Description |
|---|---|---|---|
| `spawn` | `"never"` \| `"auto"` \| `"always"` | `"never"` | `never`: connect only, to a daemon someone else runs. `auto`: reuse a daemon already ready at `robot.host:port`, else spawn one and own its teardown. `always`: spawn and own one; the port already in use is an error. |
| `headless` | bool | `true` | `sim` only. `true` launches the headless MuJoCo daemon (motion + audio, no camera on macOS); `false` launches the viewer under `mjpython` (adds the camera's GL context; needs an unlocked GUI session). |
| `scene` | string \| null | `null` | `sim` only. An upstream MuJoCo scene *name* (`empty`, `minimal`), passed as `--scene` when set — or, when it ends in `.xml`, the path of a scene *file* the bridge's own launcher loads (hidden-by-default props — a face today — a test shows/moves from tests: [sim_scene.md](sim_scene.md), written by `write_test_scene`). |
| `preload_datasets` | bool | `true` | `true` passes `--preload-datasets`: the daemon downloads the recorded-move datasets (emotions, dances) in the background after it starts, so the first `play_emotion` does not wait on a download; readiness is not delayed. `false` passes `--no-preload-datasets` (the datasets then load on first use). |
| `startup_timeout` | number | `45.0` | Seconds to wait for a spawned or booting daemon to become ready. |

```python
@dataclass
class DaemonConfig:
    spawn: str = "never"
    headless: bool = True
    scene: str | None = None
    preload_datasets: bool = True
    startup_timeout: float = 45.0
```

`spawn` other than `"never"` is valid with `backend` `"sim"` or `"real"`; with `fake`, which has no daemon, it is a `ConfigError`. For `real` the bridge spawns the daemon of a robot plugged into this machine (a Lite over USB); a wireless robot runs its own daemon on the robot, so a config for one leaves `spawn` at `"never"` and points `robot.host` at it.

The fields that apply depend on the backend: `spawn`, `preload_datasets` and `startup_timeout` apply to both; `headless` and `scene` are MuJoCo knobs that play no part on `real`. They are accepted there, so one file switches `sim` ↔ `real` by changing `backend` alone.

### `tts` block — a tts-engine `engine` block, verbatim

The default synthesizer for `say`. When present, it is exactly a tts-engine **`engine` block** (`module` + optional `player`, *not* wrapped under an `"engine"` key), carried as a raw `dict` and handed to `TTSEngineSynthesizer` ([audio.md](audio.md)) at `ReachyMiniApi` construction — which runs it through `TTSEngineConfig.from_dict`, tts-engine's own validation. The config layer checks only the shape it can without importing tts-engine: the block is an object whose `module` is an object with a non-empty string `type`.

- The key is `tts`, not `synthesizer`: it configures the shipped tts-engine adapter specifically. A custom `SpeechSynthesizer` is code, passed as `ReachyMiniApi(config, synthesizer=...)`, and an explicit synthesizer wins over the block (the block is then not consumed, and tts-engine is not imported).
- The `tts` extra must be installed for the block to be consumed; otherwise `ReachyMiniApi` raises `ConfigError` naming `reachy-mini-bridge[tts]`. Any other adapter-build failure (typically the module's `api_key_env` unset) degrades to no voice instead — see [api.md](api.md) "Constructed from a config".
- `player` is accepted for symmetry with a tts-engine file and has no effect: the bridge feeds tts-engine a robot-speaker sink in place of its local player.
- No environment variables are read at config time; a module's `api_key_env` is resolved by the module at engine construction, as in tts-engine.

### `audio` block → `AudioSettings`

| Field | Type | Default | Description |
|---|---|---|---|
| `xvf3800` | list of `[name, [values…]]` pairs \| null | `null` | The XVF3800 audio-processor profile applied on session start ([audio.md](audio.md) "XVF3800 config applied on session start"). `null` keeps the firmware defaults. |

```python
@dataclass
class AudioSettings:
    xvf3800: list[Any] | None = None
```

The value is carried verbatim to `MediaSession(robot, audio_config=...)`, whose upstream target is `apply_audio_config(config: Sequence[tuple[str, Sequence[AudioControlValue]]])` — a JSON list of two-item lists satisfies that `Sequence` shape directly. The config layer checks the shape: a list whose items are two-item lists with a string first item.

### `motion` block → `MotionSettings`

The initial values of everything that shapes the robot's behaviour at rest: the loop's own idle switches (`presence`, `breathing`, [motion.md](motion.md) "Two switches") and the two daemon-side modes the api arms around them (`wobbling`, `tracking`). Applied when the session starts; each has a runtime verb — `set_presence(enabled)` / `set_breathing(enabled)` / `set_wobbling(enabled)` / `start_head_tracking(...)` / `stop_head_tracking()` — that changes it while entered ([api.md](api.md)).

| Field | Type | Default | Description |
|---|---|---|---|
| `presence` | bool | `true` | The background behaviour: while on, the loop fills every idle moment with the idle move so the robot never goes dead between verbs. `false` makes the bridge command the head only while a verb runs — for a caller driving the head through the raw robot. Emotions play either way. |
| `breathing` | bool | `true` | Which idle move presence plays: `true` breathes (slow breaths with random rests between them, the antennas roaming independently), `false` holds a still neutral. Ignored while `presence` is off. |
| `wobbling` | bool | `true` | A robot that talks sways its head while it talks: `ReachyMiniApi` enables upstream's audio-reactive head wobbling on entry and switches it off again on exit whenever it is still on ([api.md](api.md) "Audio-reactive motion (head wobbling)"; mechanism in [audio.md](audio.md) "Head wobbling"). `false` keeps the head still while audio plays (a caller driving the head precisely, or a quiet demo). |
| `tracking` | bool | `true` | Whether the robot autonomously keeps a detected face centered ([api.md](api.md) "Attention / gaze (autonomous)"). Since tracking moves the robot, the default is realized whenever motors read `enabled` — at session entry if they already are, otherwise on the next `set_motors_state("enabled")` — and, like wobbling, is stopped again on exit if still on. `false` leaves tracking off until `start_head_tracking(...)` is called explicitly. |

```python
@dataclass
class MotionSettings:
    presence: bool = True
    breathing: bool = True
    wobbling: bool = True
    tracking: bool = True
```

It is one block, not several top-level keys, because all four switches configure the same thing from a caller's perspective — what the robot looks like when nothing else is commanding it — even though two live in the bridge's own loop (`presence`, `breathing`) and two are daemon-side modes the api merely arms (`wobbling`, `tracking`); more knobs in either category (a listening cue, a tracking weight) would land beside them.

### `ConfigError`

`ConfigError(ValueError)`, in `errors.py` — a malformed config is invalid input data, the same taxonomy tts-engine uses and the one [robot.md](robot.md) reserves `ValueError` for (as opposed to `BridgeError`'s runtime failures). A caller catches `ConfigError` for the specific type or `ValueError` for any bad-config surface, including the tts-engine `ConfigError` raised when the `tts` block is consumed.

### Validation rules

All enforced by `ReachyMiniConfig.from_dict` (delegating to `DaemonConfig.from_dict` / `AudioSettings.from_dict`), so every constructor path validates identically:

- Invalid JSON raises `ConfigError` (with the file path from `from_json_file`).
- The top-level value and the `robot`, `daemon`, `tts`, `audio`, and `motion` blocks must be JSON objects (`tts` may be `null`); `backend` is the one top-level scalar. Shape failures raise `ConfigError`, never a raw `AttributeError` / `TypeError`.
- Unknown top-level keys, and unknown keys inside `daemon` / `audio` / `motion`, raise `ConfigError` naming the key (the blocks are ours, so a typo is caught). Unknown keys inside `robot` raise `ConfigError` per the upstream-signature check above; `tts.module` is left to tts-engine.
- `backend` ∈ {`real`, `sim`, `fake`}; `daemon.spawn` ∈ {`never`, `auto`, `always`}; `daemon.spawn != "never"` requires `backend` `sim` or `real`.
- `daemon.headless` / `daemon.preload_datasets` are booleans; `daemon.scene` a non-empty string or `null`; `daemon.startup_timeout` a positive number other than `bool`.
- `robot` must not contain `use_sim` or `spawn_daemon`; with `daemon.spawn != "never"`, `robot.host` (if given) must be a loopback address.
- `tts`, when not `null`, is an object with a `module` object whose `type` is a non-empty string.
- `audio.xvf3800`, when not `null`, is a list of two-item lists with a string first item.
- `motion.presence`, `motion.breathing`, `motion.wobbling` and `motion.tracking` are all booleans.

## Relationship to the other specs

- **[api.md](api.md):** `ReachyMiniApi` is constructed from a `ReachyMiniConfig` (or a backend-string shorthand for one), mirrors the `from_*` trio, applies `motion.wobbling` on entry, arms `motion.tracking` once motors allow it, and starts the motion loop with the `motion.presence` / `motion.breathing` switches.
- **[motion.md](motion.md):** the `motion` block is the initial state of the loop's presence and breathing switches (and, for `wobbling` / `tracking`, of the daemon-side modes the api arms around it).
- **[robot.md](robot.md):** the `robot` block is what `build_robot(backend, **robot)` forwards.
- **[daemon.md](daemon.md):** the `daemon` block configures the bridge-owned daemon lifecycle; `backend` selects its launch recipe.
- **[audio.md](audio.md):** the `tts` block builds the default `TTSEngineSynthesizer`; `audio.xvf3800` is the session's `audio_config`; `motion.wobbling` arms the head wobbler on the speaker path.
- **[testing_support.md](testing_support.md):** the `live_api` fixture builds its api from a `ReachyMiniConfig` whose `robot` block carries the harness's connection options.

## Open questions

1. **Environment-variable interpolation in config files.** Whether string values in the file may reference environment variables (e.g. a `${REACHY_MINI_HOST}` for `robot.host`) is deferred until a deployment needs it; tts-engine's modules already resolve their own `*_env` keys.
2. **Named XVF3800 profiles.** Whether `audio.xvf3800` also accepts a profile *name* (e.g. `"conversation"`) resolving to a bridge-shipped tuned profile is deferred with [audio.md](audio.md) open question 2 — it needs hardware to tune against.
