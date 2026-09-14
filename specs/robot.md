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

The seam between Reachy Mini Bridge and the upstream `reachy_mini` SDK. It lets the layers above ([api.md](api.md), [audio.md](audio.md)) run against either the real robot or a first-party fake. On `real` and `sim` the bridge drives the upstream `reachy_mini.ReachyMini` object directly; on `fake` it drives a first-party `FakeReachyMini`.

The seam delivers two things:

1. **Testability.** `reachy_mini` expects a *running daemon* and hardware to do anything, so the deterministic `tests/` tier (see [testing.md](testing.md)) runs against `FakeReachyMini` and exercises the layers above with no hardware, daemon, or network. (`reachy_mini` is a base dependency and is imported normally — importing it needs its native libs installed, not a live daemon.)
2. **A checked slice.** The `AnyReachyMini` union alias and the `FakeReachyMini` class together pin the exact upstream surface the bridge depends on; pyright flags the fake when it diverges from what the Api calls.

## Core concepts / Decided

### `real` and `sim` are the same `ReachyMini`

`real` and `sim` are one upstream class — `ReachyMini(use_sim=False)` and `ReachyMini(use_sim=True)` — each talking to a daemon (hardware, or the MuJoCo mockup). The Api calls its methods directly; the human-unit surface (degrees, seconds, named emotions) lives one layer up in [api.md](api.md). `sim` selects the same class with `use_sim=True` and requires the `sim` extra (`reachy_mini[mujoco]`, see [project.md](project.md)).

### `AnyReachyMini` — a union type alias

`AnyReachyMini` is a union type alias over the two concrete robot types — *any* Reachy Mini implementation, the real SDK object or our fake:

```python
from reachy_mini import ReachyMini

type AnyReachyMini = ReachyMini | FakeReachyMini
```

The Api holds `self._robot: AnyReachyMini`. pyright checks every `self._robot.<method>(...)` against both members, so `FakeReachyMini` stays in lockstep with the surface the Api calls — a missing method or a drifted signature is a type error. Adding a backend later extends the union.

**Why not `RobotClient`.** The upstream `ReachyMini` *is itself* the client to the daemon, and it *holds its own* daemon client at `.client` (the api reads `robot.client.get_status()`). A `RobotClient` alias over that object made `robot.client` read as "the client's client," and clashed with the rest of the code, which calls the object "the robot" everywhere (`self._robot`, `api.robot`, `build_robot`). `AnyReachyMini` names what the union actually is and leaves `.client` to mean plainly "the robot's daemon client." It is **not** a wrapper — the two members are peer implementations; `ReachyMiniApi` ([api.md](api.md)) is the layer that wraps.

Units at this layer are the upstream's (4×4 matrices, radians); human units are [api.md](api.md)'s job. The upstream-typed returns (`get_status()`) are covered below.

### Module layout: `robot.py` + `fake_reachy_mini.py`

The concept is split across two modules, one spec:

- **`robot.py`** — the seam proper: the `AnyReachyMini` union alias and the `build_robot` backend factory. It imports `reachy_mini` (for `ReachyMini`) and `FakeReachyMini` from `fake_reachy_mini.py` — a clean one-way dependency (seam → fake).
- **`fake_reachy_mini.py`** — `FakeReachyMini` and its stand-in helpers (the fake daemon `client`, `media`, and `media.audio`). Imports no `reachy_mini`.

The fake lives in its own file because it's a substantial chunk of stand-in code with a different job from the seam (it *is* a backend, not the machinery that selects one) — keeping `robot.py` down to the alias and the factory. Callers/tests that need the fake directly import it from its module (`from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini`); everyone else goes through `build_robot("fake")`.

### `FakeReachyMini` — a first-party stand-in

An in-package class (in `fake_reachy_mini.py`) that implements the slice of `ReachyMini` the bridge uses and imports no `reachy_mini` itself. It records the commands it receives (so tests assert on them) and returns synthetic perception / audio (a generated frame, zeroed IMU, synthetic mic samples). It is the backbone of the deterministic `tests/` tier (see [testing.md](testing.md)) and runs the full api/audio stack offline for development and demos — no daemon, hardware, or network. (The `robot.py` module imports `reachy_mini` at module load for the union alias and `build_robot`; the fake itself needs no live daemon to run.)

### Construction from a backend string

`ReachyMiniApi` builds the robot on entry (see [api.md](api.md) "Lifecycle") through a `build_robot(backend, **opts)` helper in `robot.py`, passing the config's `backend` and its `robot` block as `opts` ([config.md](config.md)):

- `fake` → `FakeReachyMini()`;
- `real` (default) / `sim` → `ReachyMini(use_sim=(backend == "sim"), **opts)`.

`build_robot` forwards the upstream connection options verbatim as `**opts` (`robot_name`, `host`, `port`, `connection_mode`, `media_backend`, `timeout`, …), leaving their defaults to upstream, and returns a context-managed object for deterministic teardown (mirroring `ReachyMini`'s own `with`). `use_sim` is derived from the backend here and upstream's `spawn_daemon` flag is left at its default: the daemon a `sim` client talks to is managed by the bridge's own lifecycle ([daemon.md](daemon.md)), configured by the `daemon` block ([config.md](config.md)), which rejects both keys in `opts`. The backend string is the only way in: `fake` builds a fresh `FakeReachyMini`, and a test that needs to assert on it reaches it back through the escape hatch (below).

### The robot object is the escape hatch

`api.robot` (a.k.a. `api.raw`) exposes the underlying object. On `real`/`sim` it is the full native `ReachyMini`, so advanced callers reach the entire upstream API through it. On `fake` it is the `FakeReachyMini`, which tests use to assert on recorded commands.

### The consumed slice (v1)

The members the v1 [api.md](api.md) / [audio.md](audio.md) surface calls — the checklist `FakeReachyMini` implements and the union type-checks against:

- **Motion / expression:** `async_play_move` (with `RecordedMoves` loaded at the api layer for `play_emotion`); `start_head_tracking` / `stop_head_tracking`. (`goto_target` is called by no v1 verb; the fake implements it anyway, pre-seeded for the deferred manual movement verbs in [api.md](api.md).)
- **Motors:** `enable_motors` / `disable_motors` / `enable_gravity_compensation`, and the daemon client `client.get_status()`. The public `ReachyMini` has no motor-mode getter, so `get_motors_state` reads mode the way the SDK itself does — `robot.client.get_status().backend_status.motor_control_mode` (the setters above update what it reports).
- **Media** (see [audio.md](audio.md)): `media.start_recording` / `stop_recording`, `media.get_audio_sample`, `media.get_input_audio_samplerate` / `get_input_channels`, `media.start_playing` / `stop_playing`, `media.push_audio_sample`, `media.get_output_audio_samplerate` / `get_output_channels`, `media.play_sound`, `media.audio.apply_audio_config`, `media.audio.clear_player`, and `media.get_frame` (camera, for `get_camera_frame` — returns a BGR frame or `None`; see [api.md](api.md)).
- **Lifecycle:** context-manager enter/exit.

Signatures mirror the installed `reachy_mini` (1.10); the media members' dtype and rates are confirmed on the sim, with the physical channel count still pending hardware (see open questions).

### The upstream-typed returns

A few members return upstream types — the daemon `client` and its `client.get_status() -> DaemonStatus`, and the nested `media` / `media.audio` objects. `FakeReachyMini` exposes plain stand-ins with the same attribute paths the Api reads (a `client` with `get_status()` → a status whose `.backend_status.motor_control_mode` is a plain `str`; a `media` with an `.audio`), which keeps the fake free of `reachy_mini` imports. Under the union, pyright verifies each accessed attribute exists on both the real type and the fake stand-in. Two boundary details the Api handles: real `motor_control_mode` is a `str`-`Enum` (the fake's a plain `str`), and real `backend_status` is `Optional` (guard for `None` before reading the mode).

### Extending the seam

`AnyReachyMini` is a type alias, so a translating adapter or a `Protocol` can sit behind the same name without changing the layers above — the seam accommodates that shape if upstream churn ever warrants it.

## Open questions

1. **Physical channel count.** The member *list* is fixed by the v1 api/audio surface (above) and the parameter/return signatures mirror the installed `reachy_mini` 1.10. What remains is the **channel count the media capture members report on real hardware** (the fake assumes 2 / stereo) — the same fact [audio.md](audio.md) open question 1 tracks; dtype and rates are confirmed on the sim.
2. **Fake fidelity.** How faithful `FakeReachyMini`'s synthetic perception/audio needs to be (a static placeholder frame, or data that exercises face-tracking and the say/mic loop) is set by the implementation plan, driven by what the api/audio tests need.
3. ~~**Error taxonomy.**~~ **Resolved:** the bridge ships a small hierarchy in `errors.py` — `BridgeError(RuntimeError)` as the base, with `MotorsNotEnabledError` for the motors-disabled state error (see [api.md](api.md) resolved open question 1). `ValueError` stays reserved for out-of-range input validation. Connection-error types remain upstream `reachy_mini`'s until a concrete need to wrap them appears.
