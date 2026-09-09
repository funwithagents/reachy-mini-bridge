---
code:
  - src/reachy_mini_bridge/client.py
tests:
---

# Client (connection seam)

**Status:** Implemented

## Purpose

The seam between Reachy Mini Bridge and the upstream `reachy_mini` SDK. It lets the layers above ([api.md](api.md), [audio.md](audio.md)) run against either the real robot or a first-party fake. On `real` and `sim` the bridge drives the upstream `reachy_mini.ReachyMini` object directly; on `fake` it drives a first-party `FakeReachyMini`.

The seam delivers two things:

1. **Testability.** `reachy_mini` pulls in native dependencies (GStreamer, etc.) and expects a running daemon, so the deterministic `tests/` tier (see [testing.md](testing.md)) runs against `FakeReachyMini` and exercises the layers above with no hardware, daemon, or network.
2. **A checked slice.** The `RobotClient` union alias and the `FakeReachyMini` class together pin the exact upstream surface the bridge depends on; pyright flags the fake when it diverges from what the Api calls.

## Core concepts / Decided

### `real` and `sim` are the same `ReachyMini`

`real` and `sim` are one upstream class — `ReachyMini(use_sim=False)` and `ReachyMini(use_sim=True)` — each talking to a daemon (hardware, or the MuJoCo mockup). The Api calls its methods directly; the human-unit surface (degrees, seconds, named emotions) lives one layer up in [api.md](api.md). `sim` selects the same class with `use_sim=True` and requires the `sim` extra (`reachy_mini[mujoco]`, see [project.md](project.md)).

### `RobotClient` — a union type alias

`RobotClient` is a union type alias over the two concrete robot types:

```python
if TYPE_CHECKING:
    from reachy_mini import ReachyMini  # type-only; loaded at type-check time only
RobotClient: TypeAlias = "ReachyMini | FakeReachyMini"
```

The Api holds `self._robot: RobotClient`. pyright checks every `self._robot.<method>(...)` against both members, so `FakeReachyMini` stays in lockstep with the surface the Api calls — a missing method or a drifted signature is a type error. `ReachyMini` is referenced under `TYPE_CHECKING`, so runtime code loads `reachy_mini` only on the real/sim path. Adding a backend later extends the union.

Units at this layer are the upstream's (4×4 matrices, radians); human units are [api.md](api.md)'s job. The upstream-typed returns (`get_status()`) are covered below.

### `FakeReachyMini` — a first-party stand-in

An in-package class that implements the slice of `ReachyMini` the bridge uses and imports no `reachy_mini`. It records the commands it receives (so tests assert on them) and returns synthetic perception / audio (a generated frame, zeroed IMU, synthetic mic samples). It is the backbone of the deterministic `tests/` tier (see [testing.md](testing.md)) and runs the full api/audio stack offline for development and demos. The real path imports `reachy_mini` lazily (inside `build_robot`), so importing the seam or the fake stays free of the heavy upstream package even though `reachy_mini` is a base dependency.

### Construction from a backend string

`ReachyMiniApi(backend="real"|"sim"|"fake", **opts)` (with a `connect(...)` convenience) builds the robot through a `build_robot(backend, **opts)` helper in `client.py`:

- `fake` → `FakeReachyMini()`;
- `real` (default) / `sim` → lazy `from reachy_mini import ReachyMini`, then `ReachyMini(use_sim=(backend == "sim"), **opts)`.

`build_robot` forwards the upstream connection options that matter (`robot_name`, `host`, `port`, `connection_mode`, `timeout`, …) with bridge-appropriate defaults, and returns a context-managed object for deterministic teardown (mirroring `ReachyMini`'s own `with`). The backend string is the only way in: `fake` builds a fresh `FakeReachyMini`, and a test that needs to assert on it reaches it back through the escape hatch (below).

### The robot object is the escape hatch

`api.robot` (a.k.a. `api.raw`) exposes the underlying object. On `real`/`sim` it is the full native `ReachyMini`, so advanced callers reach the entire upstream API through it. On `fake` it is the `FakeReachyMini`, which tests use to assert on recorded commands.

### The consumed slice (v1)

The members the v1 [api.md](api.md) / [audio.md](audio.md) surface calls — the checklist `FakeReachyMini` implements and the union type-checks against:

- **Motion / expression:** `goto_target`; `async_play_move` (with `RecordedMoves` loaded at the api layer for `play_emotion`); `start_head_tracking` / `stop_head_tracking`.
- **Motors:** `enable_motors` / `disable_motors` / `enable_gravity_compensation`, and the daemon client `client.get_status()`. The public `ReachyMini` has no motor-mode getter, so `get_motors_state` reads mode the way the SDK itself does — `robot.client.get_status().backend_status.motor_control_mode` (the setters above update what it reports).
- **Media** (see [audio.md](audio.md)): `media.start_recording` / `stop_recording`, `media.get_audio_sample`, `media.get_input_audio_samplerate`, `media.start_playing` / `stop_playing`, `media.push_audio_sample`, `media.play_sound`, `media.audio.apply_audio_config`, `media.audio.clear_player`.
- **Lifecycle:** context-manager enter/exit.

Exact signatures are pinned against the installed `reachy_mini` once the media capture-format facts settle (see open questions).

### The upstream-typed returns

A few members return upstream types — the daemon `client` and its `client.get_status() -> DaemonStatus`, and the nested `media` / `media.audio` objects. `FakeReachyMini` exposes plain stand-ins with the same attribute paths the Api reads (a `client` with `get_status()` → a status whose `.backend_status.motor_control_mode` is a plain `str`; a `media` with an `.audio`), which keeps the fake free of `reachy_mini` imports. Under the union, pyright verifies each accessed attribute exists on both the real type and the fake stand-in. Two boundary details the Api handles: real `motor_control_mode` is a `str`-`Enum` (the fake's a plain `str`), and real `backend_status` is `Optional` (guard for `None` before reading the mode).

### Extending the seam

`RobotClient` is a type alias, so a translating adapter or a `Protocol` can sit behind the same name without changing the layers above — the seam accommodates that shape if upstream churn ever warrants it.

## Open questions

1. **Exact consumed-slice signatures.** The member *list* is fixed by the v1 api/audio surface (above); the exact parameter/return signatures are pinned against the installed `reachy_mini`. The **media capture members depend on the capture-format facts** (dtype / channels / rate) that [audio.md](audio.md) normalizes — finalized alongside that audio verification.
2. **Fake fidelity.** How faithful `FakeReachyMini`'s synthetic perception/audio needs to be (a static placeholder frame, or data that exercises face-tracking and the say/mic loop) is set by the implementation plan, driven by what the api/audio tests need.
3. ~~**Error taxonomy.**~~ **Resolved:** the bridge ships a small hierarchy in `errors.py` — `BridgeError(RuntimeError)` as the base, with `MotorsNotEnabledError` for the motors-disabled state error (see [api.md](api.md) resolved open question 1). `ValueError` stays reserved for out-of-range input validation. Connection-error types remain upstream `reachy_mini`'s until a concrete need to wrap them appears.
