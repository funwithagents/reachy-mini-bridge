---
code:
  - tests/conftest.py
  - tests-e2e/conftest.py
  - tests-e2e/support.py
tests:
---

# Testing

**Status:** Implemented

## Purpose

Reachy Mini Bridge's testing strategy — the two-tier structure and what a good test looks like. It's a **cross-cutting practice**, not a runtime concept: nothing here ships in the library. It exists as a spec so the decisions have one honest home that stays in sync with the setup, rather than living half in [project.md](project.md) (the tooling choices) and half in [AGENTS.md](../AGENTS.md) (the operational how-to). The concrete shell commands to run each tier live in [AGENTS.md](../AGENTS.md) "Testing".

## Two tiers, physically separated

Tests split into two directories, and the split is structural — a directory boundary, not a marker or an opt-out flag:

| Tier | Directory | Network | Deterministic | Runs by default |
|---|---|---|---|---|
| Unit / integration | `tests/` | never | yes | **yes** |
| Live / e2e | `tests-e2e/` | real service | no | **no** |

- **`tests/` is the normal dev loop.** Fast, deterministic, no real network, no credentials. `pyproject.toml`'s `testpaths = ["tests"]` points the default `uv run pytest` here, so this is what runs on every change and what any contributor or CI can run with zero credentials.
- **`tests-e2e/` is opt-in.** It calls a real external service — network, credentials, non-deterministic output — so it is deliberately *not* collected by the default run. Because `testpaths` already excludes it, no pytest marker or `--run-e2e` flag is needed: the physical separation is the whole mechanism. Run it explicitly (`uv run pytest tests-e2e`).

The `tests/` tier mirrors the `src/reachy_mini_bridge/` module layout (`test_<module>.py`, plus the `test_project_map.py` drift-guard); `tests-e2e/` is organized around live scenarios rather than modules.

### The fake robot is what makes the default tier possible

The bridge talks to the robot only through the `AnyReachyMini` seam ([robot.md](robot.md)), which has three backends — `real`, `sim`, and `fake`. The tiers map onto them:

- **`tests/` uses the `fake` backend** — the first-party `FakeReachyMini` (which imports no `reachy_mini` itself) that needs no daemon, no hardware, and no network. It is the whole reason the default tier can exercise the `api`/`tools` stack deterministically and offline. (Importing the package pulls in `reachy_mini` as the base dependency it is; that needs its libs installed, not a live daemon.) Assert against the commands it recorded and the synthetic perception it returns.
- **`tests-e2e/` uses the `sim` or `real` backends** — both need a running daemon (MuJoCo for `sim`, hardware for `real`) and are non-deterministic, so they are live-tier only; the `sim` backend needs the `sim` extra (`reachy_mini[mujoco]`, see [project.md](project.md)). The in-process `FakeReachyMini` already covers the mock level, so the e2e tier drives a real daemon rather than the daemon's own `--mockup-sim` mock. How the tier chooses a target, manages the daemon, and gates each test on capabilities is specified in "E2E targets & capabilities" below.

## E2E targets & capabilities

The e2e tier runs one suite against a **live daemon**, choosing the *target* at runtime and letting each test declare the capabilities it needs. A test is written once and runs wherever its needs are met — no per-environment duplication.

### Targets

Selected by `REACHY_MINI_E2E_TARGET`:

- **`sim`** (default) — the harness spawns and manages a MuJoCo daemon, in one of two **launch modes**:
  - **headless** (default, for CI): `--sim --headless` — no window, runs anywhere.
  - **headfull** (`REACHY_MINI_E2E_SIM_VIEWER=1`, local): the MuJoCo viewer, to watch the sim as a robot stand-in. On macOS the viewer must run under `mjpython` from a GUI session (see the doc).
  Both modes are the same daemon with the same capabilities, except the camera (below).
- **`real`** — connects to a robot's daemon at `REACHY_MINI_HOST` / `REACHY_MINI_PORT`.

Any target **reuses a daemon already reachable** at the address (a viewer sim you started by hand, or the robot), instead of spawning its own.

### Daemon lifecycle — own it or borrow it

The harness stops only daemons **it spawned** (the `sim` target, either launch mode) and connects to but **never tears down** ones it didn't (`real`, or an already-running daemon it reused). The spawned `sim` daemon is **module-scoped**: one per test file, started once and stopped at the end — never per test.

### Capabilities are probed, not assumed

Environment quirks decide what actually works — audio needs `start_recording()` first, the sim camera needs a GL context, DoA needs the mic array — so inferring from the backend type is unreliable. The fixture instead **probes** each capability against the live daemon at setup, and a `requires_caps(...)` gate **skips** (never fails) a test whose needs the current target can't meet:

```python
def test_say_is_audible(live_robot):
    requires_caps(live_robot, "audio")  # skips where audio isn't probed
    ...
```

| Capability | probe | sim headless | sim headfull | real robot |
|---|---|---|---|---|
| `motion` | backend reports a status | ✅ | ✅ | ✅ |
| `audio` | `start_recording()` then a sample arrives | ✅ software AEC, host device | ✅ | ✅ hardware AEC |
| `camera` | `get_frame()` returns a frame | ⚠️ needs a GL context (not headless plain-python on macOS) | ✅ | ✅ |
| `doa` · hardware-AEC quality · beamforming | — | ❌ | ❌ | ✅ |

The sim covers **motion and audio** (audio via the host's audio device with *software* AEC — only the XVF3800's hardware AEC/beamforming/DoA are robot-only); the sim **camera** needs a GL context, so it works headfull (or with a headless GL backend) but not headless plain-python on macOS.

### The harness

A single `live_robot` fixture (in `tests-e2e/conftest.py`) is the entry point: it resolves the target, brings up a daemon under the own-it-or-borrow-it rule, connects the client with media on (`media_backend="local"`), probes capabilities, and yields `(robot, capabilities)`. `requires_caps(...)` (in `tests-e2e/support.py`) takes that yielded value and skips a test whose needs the target can't meet. The headless `sim` target spawns the media-on MuJoCo daemon and probes **motion** and **audio** (the sim camera stays unprobed headless, so `requires_caps("camera")` skips; `doa` is robot-only and reserved). The `motion` e2e test drives this harness. Spawning the daemon from a process that has already imported `reachy_mini` scrubs the inherited GStreamer-bundle env vars first, so the child sets fresh ones — see [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md) for that gotcha and the launch recipes for every mode. The `audio`/`camera` e2e *tests* that plug into this harness arrive with the [audio.md](audio.md) / perception work.

## What a good test asserts

- **Functional, not tautological.** Exercise what a feature actually does — inputs → outputs, state changes, side effects — not that it runs or matches its own signature. A test that would pass against a broken implementation (asserting a constant, that an object isn't `None`, that a mock was called) isn't worth writing.
- **Drive the public API like a real caller.** Prefer exercising the public surface the way a consumer would over reaching into internals; assert on the observable result.
- **In the e2e tier, assert on behavior, not exact output.** Real service responses vary run to run, so a live test asserts a robust property ("a non-empty result came back", "the side effect happened"), never a specific string.

## Test isolation

If the package holds process-global or singleton state, both tiers carry an identical autouse fixture (in each tier's `conftest.py`) that resets it before and after every test, so no state — or background timers/threads — leaks across tests. The fixture is duplicated rather than shared because `tests-e2e/` isn't a package that imports from `tests/`, and it's only a few lines.

## Live tier: skip without credentials

A live test needs real credentials, and it must **skip — never fail** — when they're absent, so you exercise only the services you hold keys for and a contributor (or CI) with none is never broken. `tests-e2e/support.require_env(NAME)` implements this: it returns the env var or calls `pytest.skip(...)` when it's unset. Credentials come from the environment, never committed.

## Tooling

- **`pytest`** is the runner; **`ruff`** lints/formats; **`pyright`** (`standard` mode) type-checks. All three are the gate after any change — lint, type check, and tests must pass before work is considered done (see [AGENTS.md](../AGENTS.md), "Verification").
- **`pyright` covers test code too:** its `include` is `src`, `tests`, and `tests-e2e`, so tests are type-checked alongside the library rather than being a blind spot.

## Open questions

1. **CI wiring.** Nothing here sets up continuous integration. The default `tests/` tier is CI-ready (deterministic, no credentials), and the e2e tier is designed to skip cleanly when keys are absent — but actually running either on a hosted runner is unbuilt. Today all testing is a local, manual command.
