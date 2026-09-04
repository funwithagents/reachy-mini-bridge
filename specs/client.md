---
code:
  - src/reachy_mini_bridge/client.py
tests:
---

# Client (connection seam)

**Status:** Draft

## Purpose

The single seam between Reachy Mini Bridge and the upstream `reachy_mini` SDK. It owns the underlying `reachy_mini.ReachyMini` connection — construction, configuration (real robot vs. simulation, connection mode, timeouts), and lifecycle (context manager / teardown) — and exposes it behind a narrow **Protocol** so every layer above ([api.md](api.md), [tools.md](tools.md)) depends on an interface we control, not on the heavy upstream package directly.

Two problems this solves:

1. **Testability.** The upstream package pulls in native dependencies (GStreamer, etc.) and expects a running daemon, so it must not be imported in the deterministic `tests/` tier (see [testing.md](testing.md)). A Protocol seam lets tests inject a fake robot and exercise the layers above with no hardware, daemon, or network.
2. **Isolation.** Upstream API churn (renamed methods, changed matrix conventions) is absorbed here in one adapter rather than rippling through the whole codebase.

## Core concepts / Decided

- **`RobotClient` Protocol.** A `typing.Protocol` declaring the narrow slice of the upstream robot surface that the bridge actually uses — the motion, expression, perception, audio, and lifecycle primitives that [api.md](api.md) needs (e.g. `set_target`, `goto_target`, `wake_up`, `goto_sleep`, `look_at_image`, `look_at_world`, `get_current_head_pose`, `imu`, `media`, `enable_motors`/`disable_motors`, …). The exact member list is pinned when [api.md](api.md) settles, since the Api is its only consumer.
- **Three backends behind one Protocol.** The same `RobotClient` interface has three implementations, so [api.md](api.md) / [tools.md](tools.md) never know which is behind them:
  - **`real`** — the adapter over `reachy_mini.ReachyMini` (below). Default.
  - **`sim`** — the *same* real adapter constructed with `use_sim=True`, driving the upstream MuJoCo mockup daemon instead of hardware. Not a separate class; it needs the `sim` optional extra (`reachy_mini[mujoco]`) installed — see [project.md](project.md).
  - **`fake`** — a **first-party fake** we own and control (see below).
- **Adapter over the real SDK.** A concrete class wraps `reachy_mini.ReachyMini` and satisfies `RobotClient`. Where the upstream surface already matches, it passes through; where it differs, the adapter is the one place that translates. Upstream is a **hard runtime dependency** (installed by default; added to `pyproject.toml` `dependencies` when this spec is implemented; see [project.md](project.md)).
- **`FakeRobot` — a first-party fake `RobotClient`.** An in-package implementation of the Protocol that needs no daemon, no hardware, and **no import of `reachy_mini`**. It records the commands it receives (so tests can assert on them) and returns plausible synthetic perception data (e.g. a blank or generated camera frame, zeroed IMU, empty face). Its primary use is the deterministic `tests/` tier (see [testing.md](testing.md)), and it doubles as a way to run the full api/tools stack offline for development/demos. Because the real adapter imports `reachy_mini` **lazily** (inside `connect`/the adapter, not at module top level), importing the seam and the fake never drags in the heavy upstream package — keeping `tests/` fast and daemon-free even though `reachy_mini` is a base dependency.
- **`connect(...)` entry point.** A factory that builds and returns a connected client, choosing the backend (`real` default, `sim`, or `fake`) and forwarding the upstream connection options that matter (`robot_name`, `host`, `port`, `connection_mode`, `use_sim`, `timeout`, …) with bridge-appropriate defaults. Returns something usable as a context manager so callers get deterministic teardown.
- **Escape hatch.** The raw upstream `reachy_mini.ReachyMini` instance is reachable (e.g. a `.raw` property) so advanced callers can drop to the full native API — this is the "have access to reachy mini API" goal. The bridge does not attempt to re-expose every upstream method through the Protocol; the Protocol is only the slice the higher layers consume.
- **Units at this layer are the upstream's.** No unit translation here (still 4x4 matrices, radians). Human-friendly units (degrees, seconds, named emotions) are [api.md](api.md)'s job.

## Open questions

1. **Exact `RobotClient` membership.** Finalize once [api.md](api.md)'s method set is fixed — the Protocol should be exactly what the Api calls, no more.
2. **Fake fidelity.** How faithful `FakeRobot`'s synthetic perception needs to be (static placeholder frame vs. something that exercises `look_at_image` / face-tracking code paths) is deferred to the implementation plan — driven by what the api/tools tests actually need.
3. **Reconnection / error taxonomy.** Whether the seam wraps upstream connection errors in bridge-specific exception types (vs. letting them propagate) is deferred until we see how the Api and Tools want to report failures.
