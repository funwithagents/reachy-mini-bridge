---
code:
  - src/reachy_mini_bridge/robot.py
  - src/reachy_mini_bridge/fake_reachy_mini.py
tests:
  - tests/test_robot.py
  - tests/test_fake_reachy_mini.py
---

# Robot (connection seam)

**Status:** Implemented

## Purpose

The seam between Reachy Mini Bridge and the upstream `reachy_mini` SDK. It lets the layers above ([bridge.md](bridge.md), [audio.md](../audio/audio.md)) run against either the real robot or a first-party fake. On `real` and `sim` the bridge drives the upstream `reachy_mini.ReachyMini` object directly; on `fake` it drives a first-party `FakeReachyMini`.

The seam delivers two things:

1. **Testability.** `reachy_mini` expects a *running daemon* and hardware to do anything, so the deterministic `tests/` tier (see [testing.md](../testing/testing.md)) runs against `FakeReachyMini` and exercises the layers above with no hardware, daemon, or network. (`reachy_mini` is a base dependency and is imported normally — importing it needs its native libs installed, not a live daemon.)
2. **A checked slice.** The `AnyReachyMini` union alias and the `FakeReachyMini` class together pin the exact upstream surface the bridge depends on; pyright flags the fake when it diverges from what the bridge calls.

## Core concepts / Decided

### `real` and `sim` are the same `ReachyMini`

`real` and `sim` are one upstream class — `ReachyMini(use_sim=False)` and `ReachyMini(use_sim=True)` — each talking to a daemon (hardware, or the MuJoCo mockup). The bridge calls its methods directly; the human-unit surface (degrees, seconds, named emotions) lives one layer up in [bridge.md](bridge.md). `sim` selects the same class with `use_sim=True` and requires the bridge's `sim` extra (`reachy-mini-bridge[sim]`, which declares MuJoCo directly — see [project.md](../project.md)).

### `AnyReachyMini` — a union type alias

`AnyReachyMini` is a union type alias over the two concrete robot types — *any* Reachy Mini implementation, the real SDK object or our fake:

```python
from reachy_mini import ReachyMini

type AnyReachyMini = ReachyMini | FakeReachyMini
```

The bridge holds `self._robot: AnyReachyMini`. pyright checks every `self._robot.<method>(...)` against both members, so `FakeReachyMini` stays in lockstep with the surface the bridge calls — a missing method or a drifted signature is a type error. Adding a backend later extends the union.

**Why not `RobotClient`.** The upstream `ReachyMini` *is itself* the client to the daemon, and it *holds its own* daemon client at `.client` (the bridge reads `robot.client.get_status()`). A `RobotClient` alias over that object made `robot.client` read as "the client's client," and clashed with the rest of the code, which calls the object "the robot" everywhere (`self._robot`, `bridge.robot`, `build_robot`). `AnyReachyMini` names what the union actually is and leaves `.client` to mean plainly "the robot's daemon client." It is **not** a wrapper — the two members are peer implementations; `ReachyMiniBridge` ([bridge.md](bridge.md)) is the layer that wraps.

Units at this layer are the upstream's (4×4 matrices, radians); human units are [bridge.md](bridge.md)'s job. The upstream-typed returns (`get_status()`) are covered below.

### Module layout: `robot.py` + `fake_reachy_mini.py`

The concept is split across two modules, one spec:

- **`robot.py`** — the seam proper: the `AnyReachyMini` union alias and the `build_robot` backend factory. It imports `reachy_mini` (for `ReachyMini`) and `FakeReachyMini` from `fake_reachy_mini.py` — a clean one-way dependency (seam → fake).
- **`fake_reachy_mini.py`** — `FakeReachyMini` and its stand-in helpers (the fake daemon `client`, `media`, and `media.audio`). Imports no `reachy_mini`.

The fake lives in its own file because it's a substantial chunk of stand-in code with a different job from the seam (it *is* a backend, not the machinery that selects one) — keeping `robot.py` down to the alias and the factory. Callers/tests that need the fake directly import it from its module (`from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini`); everyone else goes through `build_robot("fake")`.

### `FakeReachyMini` — a first-party stand-in

An in-package class (in `fake_reachy_mini.py`) that implements the slice of `ReachyMini` the bridge uses and imports no `reachy_mini` itself. It records the commands it receives (so tests assert on them) and returns synthetic perception / audio (a generated frame, synthetic mic samples). It is the backbone of the deterministic `tests/` tier (see [testing.md](../testing/testing.md)) and runs the full api/audio stack offline for development and demos — no daemon, hardware, or network. (The `robot.py` module imports `reachy_mini` at module load for the union alias and `build_robot`; the fake itself needs no live daemon to run.)

Its fidelity is set by what the bridge/audio tests exercise:

- **Media format matches the real robot:** float32 `(160, 2)` capture chunks (zeros) at 16 kHz, 2 input and 2 output channels — the values the sim and a Reachy Mini Lite report ([audio.md](../audio/audio.md) "Background") — so the mono downmix and raw passthrough both run in `tests/`.
- **Camera:** a deterministic 64×48 BGR `uint8` horizontal gradient, never `None` (unlike the daemon), so tests assert real structure — the first read immediate, each later read blocking until the next frame period at `FAKE_FRAME_HZ` (10 fps) has elapsed, as the real `get_frame()` blocks for the next frame, so the camera feed that is its only reader publishes at a realistic rate ([camera.md](../vision/camera.md) "`fake` backend support").
- **Motion:** the loop's `set_target` stream is recorded on a dedicated `targets` list (the 60 Hz stream would swamp `commands`), the latest kept as `last_target`, and the pose readers return the last commanded values — so the fake is a trivially consistent robot for the motion loop's re-anchoring ([motion.md](../motion/motion.md)). Wobbling and motor toggles are recorded as commands; nothing is simulated kinematically. The motor setters update the mode its daemon-client stand-in reports.
- **Emotions:** the bridge substitutes a small stubbed library on the `fake` backend, with no Hugging Face access.

### Construction from a backend string

`ReachyMiniBridge` builds the robot on entry (see [bridge.md](bridge.md) "Lifecycle") through a `build_robot(backend, **opts)` helper in `robot.py`, passing the config's `backend` and its `robot` block as `opts` ([config.md](config.md)):

- `fake` → `FakeReachyMini()`;
- `real` (default) / `sim` → `ReachyMini(use_sim=(backend == "sim"), **opts)`.

`build_robot` forwards the upstream connection options verbatim as `**opts` (`robot_name`, `host`, `port`, `connection_mode`, `media_backend`, `timeout`, …), leaving their defaults to upstream, and returns a context-managed object for deterministic teardown (mirroring `ReachyMini`'s own `with`). `use_sim` is derived from the backend here and upstream's `spawn_daemon` flag is left at its default: the daemon a `sim` client talks to is managed by the bridge's own lifecycle ([daemon.md](../daemon/daemon.md)), configured by the `daemon` block ([config.md](config.md)), which rejects both keys in `opts`. The backend string is the only way in: `fake` builds a fresh `FakeReachyMini`, and a test that needs to assert on it reaches it back through the escape hatch (below).

### The robot object is the escape hatch

`bridge.robot` (a.k.a. `bridge.raw`) exposes the underlying object. On `real`/`sim` it is the full native `ReachyMini`, so advanced callers reach the entire upstream API through it. On `fake` it is the `FakeReachyMini`, which tests use to assert on recorded commands.

### The consumed slice (v1)

The members the v1 [bridge.md](bridge.md) / [audio.md](../audio/audio.md) surface calls — the checklist `FakeReachyMini` implements and the union type-checks against:

- **Motion / expression:** `set_target(head, antennas, body_yaw)` — the one target writer, called only by the motion loop ([motion.md](../motion/motion.md)) at its tick rate; `get_current_head_pose()` and `get_current_joint_positions()` — the pose readers the loop re-anchors on (body yaw is the first head joint). The fake records each `set_target` on `targets` (not `commands`) and returns the last commanded values from the readers (identity / neutral before any). `RecordedMoves` is loaded at the bridge layer for `play_emotion` and the moves are evaluated by the loop; upstream's `async_play_move` / `play_move` and `goto_target` are **not** consumed — each is a second writer of the target the loop owns (a daemon-side move even makes the daemon drop the loop's targets while it runs). Timing for a cancel to interrupt comes from the loop itself, which runs at real time on the fake. The head is steered by the bridge's own tracker through `set_target` alone: the daemon's head tracking (`start_head_tracking` / `stop_head_tracking` / `get_tracked_face`) is **not** consumed — the bridge never arms it, never stops it, and never reads its face target — so a caller who wants the daemon's own tracking reaches it through the escape hatch and the bridge does not interfere.
- **Wobbling:** `enable_wobbling` / `disable_wobbling` — the audio-reactive head sway behind `set_wobbling` ([bridge.md](bridge.md); mechanism in [audio.md](../audio/audio.md) "Head wobbling"). Upstream exposes no getter, so the bridge keeps its own record of what it last set. The fake records the two toggles as commands.
- **Motors:** `enable_motors` / `disable_motors` / `enable_gravity_compensation`, and the daemon client `client.get_status()`. The public `ReachyMini` has no motor-mode getter, so `get_motors_state` reads mode the way the SDK itself does — `robot.client.get_status().backend_status.motor_control_mode` (the setters above update what it reports). The gravity-compensation guard ([bridge.md](bridge.md) "Motors") also reads `status.simulation_enabled` / `status.mockup_sim_enabled`, and the daemon's kinematics engine: on `ReachyMini` from `GET http://{client.host}:{client.port}/api/kinematics/info` (upstream has no SDK getter), on the fake from `client.kinematics_engine` (`"Placo"` by default; both sim flags `False`).
- **Faces and head tracking:** faces are detected by the bridge on the camera feed's frames ([user_perception.md](../vision/user_perception.md)), so nothing beyond `media.get_frame` is consumed for them; the head is aimed by the bridge's own tracker ([head_tracking.md](../motion/head_tracking.md)), which reads `media.camera.camera_specs` (`K`, `D`, `default_resolution` — the client's camera calibration). The fake carries a Lite-like calibration stand-in; it has no detector and no face of its own — a test that needs faces registers a stub detector over the fake's frames ([user_perception.md](../vision/user_perception.md) "`fake` backend support").
- **Media** (see [audio.md](../audio/audio.md)): `media.start_recording` / `stop_recording`, `media.get_audio_sample`, `media.get_input_audio_samplerate` / `get_input_channels`, `media.start_playing` / `stop_playing`, `media.push_audio_sample`, `media.get_output_audio_samplerate` / `get_output_channels`, `media.play_sound`, `media.stop_sound` (fake-only: the member [audio.md](../audio/audio.md) "Stopping a sound file" proposes upstream; on `ReachyMini` the bridge stops the local backend's `media.audio._playbin` instead, so the parity test skips this member until upstream ships it), `media.audio.apply_audio_config`, `media.audio.clear_player`, `media.get_frame` (camera — returns a BGR frame or `None`; consumed by the camera feed alone, [camera.md](../vision/camera.md), which every consumer of frames samples: `bridge.camera`, the detection loop of [user_perception.md](../vision/user_perception.md), a caller's display), and `media.camera.camera_specs` (`K`, `D`: the calibrated intrinsics the head tracker aims with on a robot; `media.camera` is `None` on a client without media, and the fake's stand-in carries the Lite's matrix).
- **Lifecycle:** context-manager enter/exit.

Signatures mirror the installed `reachy_mini` (1.10). pyright checks call compatibility through the union but not parameter defaults, so a parity test in `tests/test_robot.py` compares each consumed member's parameter names and defaults (via `inspect.signature`) against its upstream counterpart. It also pins the one SDK internal the bridge reaches — `GStreamerAudio._playbin` ([audio.md](../audio/audio.md) "Stopping a sound file") — so an upstream rename fails loudly instead of leaving a cancelled emotion's sound playing. The media members' dtype, rates, and channel count (float32, 16 kHz, 2 channels) are confirmed on the sim and on a real Reachy Mini Lite (see [audio.md](../audio/audio.md) "Background").

### The upstream-typed returns

A few members return upstream types — the daemon `client` and its `client.get_status() -> DaemonStatus`, and the nested `media` / `media.audio` objects. `FakeReachyMini` exposes plain stand-ins with the same attribute paths the bridge reads (a `client` with `get_status()` → a status whose `.backend_status.motor_control_mode` is a plain `str` and whose `simulation_enabled` / `mockup_sim_enabled` mirror the client's; a `media` with an `.audio`), which keeps the fake free of `reachy_mini` imports. Under the union, pyright verifies each accessed attribute exists on both the real type and the fake stand-in. Two boundary details the bridge handles: real `motor_control_mode` is a `str`-`Enum` (the fake's a plain `str`), and real `backend_status` is `Optional` (guard for `None` before reading the mode).

### Errors

The seam raises upstream `reachy_mini`'s own connection errors unwrapped. The bridge's error hierarchy (`BridgeError` and its subclasses in `errors.py`) belongs to the layers above — see [bridge.md](bridge.md) "Errors".

### Extending the seam

`AnyReachyMini` is a type alias, so a translating adapter or a `Protocol` can sit behind the same name without changing the layers above — the seam accommodates that shape if upstream churn ever warrants it.

## Open questions

None currently.
