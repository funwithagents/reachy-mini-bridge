---
code:
  - src/reachy_mini_bridge/testing/__init__.py
  - src/reachy_mini_bridge/testing/fixtures.py
  - src/reachy_mini_bridge/testing/_daemon.py
  - src/reachy_mini_bridge/testing/support.py
tests:
  - tests/test_testing_support.py
---

# Testing support (shipped harness)

**Status:** Implemented

## Purpose

`reachy_mini_bridge.testing` is the bridge's **shipped, importable testing surface** — the piece that makes it clear to a downstream project *how* to test its own code against the three backends. A consumer adds `reachy-mini-bridge` (with the `sim` extra) as a dependency and needs to write both unit tests and live/e2e tests over the robot; this package hands them the same harness the bridge uses for its own live tier, so they never re-derive the daemon lifecycle and its gotchas.

This is distinct from [testing.md](testing.md): that spec is the bridge's *own* testing practice — a cross-cutting discipline, nothing there ships. This spec governs code that **does** ship in `src/`, so a consumer imports it. The harness (own-it-or-borrow-it daemon lifecycle, the GStreamer-bundle env scrub, capability probing, the `requires_caps` skip gate) is library code here; the bridge's own `tests-e2e/conftest.py` imports its fixtures from it, so the shipped harness is the exact code the bridge exercises rather than a second copy that can rot.

## Core concepts / Decided

### The consumer-facing backend → tier mapping

A consumer's two tiers map onto the bridge's three backends exactly as the bridge's own do ([testing.md](testing.md), [robot.md](robot.md)):

| Consumer tier | Backend | Extra needed | Daemon |
|---|---|---|---|
| Unit / integration | `fake` | none | none — offline, deterministic |
| Live / e2e | `sim` | `sim` (`reachy-mini[mujoco]`) | MuJoCo, harness-managed |
| Live / e2e | `real` | none (base only) | robot at host/port — harness-managed for a USB robot on this machine |

- **Unit tests use `fake`.** The public `ReachyMiniApi("fake")` ([api.md](api.md)) already needs no daemon, no network, and no extra — a consumer constructs it directly and asserts through the `api.robot` escape hatch on recorded commands / synthetic perception. Importing the package pulls in `reachy_mini` (the base dependency), which needs its native libs installed, **not** a live daemon.
- **E2E tests use `sim` or `real`** through the shipped `live_api` fixture below.

### Shipped as `reachy_mini_bridge.testing`, behind a `test` extra

The harness ships as the `reachy_mini_bridge.testing` package, pulled in by the **`test` optional-dependency extra** (`reachy-mini-bridge[test]`). The extra carries `pytest` (floor `>=9.1.1`, matching the bridge's own dev pin) — the harness's only added dependency. The sim daemon launcher (`reachy-mini-daemon` / `mjpython`) comes from the `sim` extra, so a consumer running e2e against `sim` installs `reachy-mini-bridge[sim,test]`; against `real`, `reachy-mini-bridge[test]` alone.

### Opt-in as a pytest plugin — no auto-registration

The `live_api` fixture and its helpers live in `reachy_mini_bridge.testing.fixtures`, a module shaped as a **pytest plugin**. It is **not** auto-registered (no `pytest11` entry point): a consumer opts in explicitly from their own `conftest.py`:

```python
# their conftest.py
pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]
```

Explicit opt-in keeps installation side-effect-free — merely depending on the bridge never injects fixtures into an unrelated test run — and keeps the consumer in control of where the fixtures apply.

### Package layout

Four modules, with the daemon machinery kept private behind the plugin:

- `reachy_mini_bridge/testing/__init__.py` — re-exports the two skip gates (`requires_caps`, `require_env`) from `support.py`. The `live_api` fixture is deliberately *not* re-exported here: a fixture only registers through the plugin module a consumer names in `pytest_plugins`.
- `testing/fixtures.py` — the pytest-plugin module a consumer names in `pytest_plugins`: the `live_api` fixture and the capability probing it yields.
- `testing/_daemon.py` — **private** glue between the environment and the bridge's daemon lifecycle: target/backend/address resolution from the env vars below, and a thin wrapper over [daemon.md](daemon.md)'s `managed_daemon` / `is_daemon_ready` that turns a `DaemonError` into a `pytest.skip`. The own-it-or-borrow-it decision, the readiness poll, the launch recipes, and the GStreamer-bundle env scrub live in the library module `daemon.py`, so the harness and `ReachyMiniApi` share one implementation. Kept out of `fixtures.py` so the plugin module reads as the fixture surface.
- `testing/support.py` — `requires_caps` and `require_env`.

### Public surface

The package exposes exactly the three names the bridge's own live tier uses — `live_api` through the `reachy_mini_bridge.testing.fixtures` plugin module, and the two skip gates re-exported from `reachy_mini_bridge.testing`:

- **`live_api`** — a **module-scoped** pytest fixture yielding `(api, capabilities)`: a connected `ReachyMiniApi` over the resolved target and the `frozenset` of capabilities probed against that live daemon. It brings the daemon up under own-it-or-borrow-it (reuse one already reachable, else spawn one and own its teardown — a MuJoCo daemon for `sim`, the hardware daemon for `real` when the address is loopback, i.e. a USB robot on this machine; a non-loopback `real` address is borrow-or-skip), builds the api from a `ReachyMiniConfig` ([config.md](config.md)) whose `robot` block carries the harness's connection options (`connection_mode="network"`, the resolved host/port, `media_backend="local"`) and whose `daemon.spawn` is `"never"` — the fixture, not the api, owns the daemon so one daemon serves a whole test module — with media on, probes, and tears down what it spawned.
- **`requires_caps(live, *caps)`** — the skip gate: given the `live_api` value, `pytest.skip(...)` unless every named capability (`motion` / `audio` / `camera` / `gravity_compensation` / …, table in [testing.md](testing.md)) was probed on the current target. A test written once runs wherever its needs are met.
- **`require_env(name)`** — return an env var or skip when it's absent, so a live test skips (never fails) without its credentials.

A consumer's e2e test then reads:

```python
from reachy_mini_bridge.testing import requires_caps


def test_my_greeting_speaks(live_api):
    requires_caps(live_api, "audio")
    api, _ = live_api
    ...
```

### Configuration via the environment

Target and connection are chosen by the same env vars the bridge's tier uses, so the knobs are one documented set:

- `REACHY_MINI_E2E_TARGET` — `sim` (default) | `real`.
- `REACHY_MINI_HOST` / `REACHY_MINI_PORT` — the daemon address (borrow a daemon already there; for `real`, the robot's daemon — spawned by the harness when the address is loopback and nothing is ready).
- `REACHY_MINI_E2E_SIM_VIEWER` — headfull MuJoCo viewer instead of headless (local, needs a GUI/GL context; see [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md)).

### The gotchas move into the shipped code

The hard-won details a consumer would otherwise have to rediscover are the whole reason to ship this rather than document it: the **GStreamer-bundle env scrub** before spawning a daemon from a process that has imported `reachy_mini` (else a doubled plugin path segfaults the child), **probing** capabilities against the live daemon rather than inferring them from the backend type, and **probing without restarting the audio pipeline**. The scrub and the spawn live in the library's `daemon.py` ([daemon.md](daemon.md)), the probes behind `live_api`; a consumer inherits all three for free.

The probes run after the api has opened its media session, so the pipeline is already recording and playing. The audio probe only waits for a mic sample from it; it never calls `start_recording()` / `stop_recording()`. Upstream's GStreamer audio binds the robot's speaker and mic by device name once, when it builds that single shared pipeline, and on macOS a stop-then-start reopens both on the system defaults — the Mac's own speaker and microphone — for the rest of the test module ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)). A consumer's own tests should likewise leave the session's pipeline running.

### A documented guide accompanies the code

A consumer-facing guide (`docs/testing-with-the-bridge.md`, linked from the [README](../README.md)) states the extras/backends mapping, the `pytest_plugins` opt-in line, and the env vars — so the entry point is discoverable, not buried in a spec.

## Open questions

1. **Consumer daemon knobs.** Beyond the env vars above, some consumer may want the harness to use a MuJoCo scene or a preloaded-datasets daemon. The knobs exist on `DaemonConfig` ([config.md](config.md)); whether the harness exposes them (env vars, or a `DaemonConfig` a consumer's conftest hands in) is deferred until one actually needs it — the borrow-or-spawn-sim path with the current env vars covers the known cases. (A genuine deferral, not a load-bearing unknown.)
