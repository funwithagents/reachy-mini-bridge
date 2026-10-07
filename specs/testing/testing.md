---
code:
  - tests/conftest.py
  - tests-e2e/conftest.py
tests:
---

# Testing

**Status:** Implemented

## Purpose

Reachy Mini Bridge's testing strategy — the two-tier structure and what a good test looks like. It's a **cross-cutting practice**, not a runtime concept: nothing here ships in the library. It exists as a spec so the decisions have one honest home that stays in sync with the setup, rather than living half in [project.md](../project.md) (the tooling choices) and half in [AGENTS.md](../../AGENTS.md) (the operational how-to). The concrete shell commands to run each tier live in [AGENTS.md](../../AGENTS.md) "Testing".

## Two tiers, physically separated

Tests split into two directories, and the split is structural — a directory boundary, not a marker or an opt-out flag:

| Tier | Directory | Network | Deterministic | Runs by default |
|---|---|---|---|---|
| Unit / integration | `tests/` | never | yes | **yes** |
| Live / e2e | `tests-e2e/` | real service | no | **no** |

- **`tests/` is the normal dev loop.** Fast, deterministic, no real network, no credentials. `pyproject.toml`'s `testpaths = ["tests"]` points the default `uv run pytest` here, so this is what runs on every change and what any contributor or CI can run with zero credentials. It runs **in parallel** on up to eight of the machine's cores (pytest-xdist, `-n logical` capped by `--maxprocesses 8`, since each worker is a whole interpreter with the package's imports loaded and the tier gains little past eight; `logical` rather than `auto` because `auto` counts physical cores when psutil is importable — it is, through `reachy_mini` — and a hyperthreaded CI runner would then get half the workers for a tier that sleeps rather than computes): the tier is sleep-bound, not CPU-bound — the motion loop and the fake run at real time ([../motion/motion.md](../motion/motion.md), [../core/robot.md](../core/robot.md)), so most of its tests wait through real blends, breaths and tracking convergences, about three minutes of waiting in all that spread over eight workers takes about half a minute. The default is `addopts` in `pyproject.toml` (`-n logical --maxprocesses 8`); an explicit `-n` on the command line wins, and `-n 0` runs the tier serially — the check to make when a timing assertion looks flaky, since a late tick under load is the one way parallelism can change what a test sees. Every test must therefore be independent of the others' process: no shared port, file or global.
- **`tests-e2e/` is opt-in.** It calls a real external service — network, credentials, non-deterministic output — so it is deliberately *not* collected by the default run. Because `testpaths` already excludes it, no pytest marker or `--run-e2e` flag is needed: the physical separation is the whole mechanism. Run it explicitly (`uv run pytest tests-e2e`). It runs **serially**: every module borrows or spawns the one daemon at the configured address, so `tests-e2e/conftest.py` forces the worker count to zero — it loads only when `tests-e2e` is among the collected paths, overriding the `addopts` default there and nowhere else — rather than let workers spawn, borrow and tear down each other's daemon.

The `tests/` tier mirrors the `src/reachy_mini_bridge/` module layout (`test_<module>.py`, plus the `test_project_map.py` drift-guard); `tests-e2e/` is organized around live scenarios rather than modules: one file per subject, its tests sharing one capability gate, so a headless run skips whole files and `-rs` reads by subject — `test_motors.py` (`motion`: the motor states, gravity compensation), `test_audio.py` (`audio`: the daemon's format, the mic tap, `say` with its interruption and its cancel, `play_sound` spanning its file, the TTS providers, wobbling), `test_motion.py` (`motion`: breathing and the hold, a custom idle move, an emotion played and cancelled), `test_perception.py` (`camera`, then `faces`: the camera feed, detection running, the faces report), `test_head_tracking.py` (`camera` + `faces`: convergence, attention, an emotion over tracking, whom the head follows), `test_sim_displays.py` (`face_markers`), `test_custom_faces.py` (`camera` + `faces`, a bridge session of its own).

### The fake robot is what makes the default tier possible

The bridge talks to the robot only through the `AnyReachyMini` seam ([robot.md](../core/robot.md)), which has three backends — `real`, `sim`, and `fake`. The tiers map onto them:

- **`tests/` uses the `fake` backend** — the first-party `FakeReachyMini` (which imports no `reachy_mini` itself) that needs no daemon, no hardware, and no network. It is the whole reason the default tier can exercise the `api`/`tools` stack deterministically and offline. (Importing the package pulls in `reachy_mini` as the base dependency it is; that needs its libs installed, not a live daemon.) Assert against the commands it recorded and the synthetic perception it returns.
- **`tests-e2e/` uses the `sim` or `real` backends** — both need a running daemon (MuJoCo for `sim`, hardware for `real`) and are non-deterministic, so they are live-tier only; the `sim` backend needs the bridge's `sim` extra (`reachy-mini-bridge[sim]`, which declares MuJoCo directly — see [project.md](../project.md)). The in-process `FakeReachyMini` already covers the mock level, so the e2e tier drives a real daemon rather than the daemon's own `--mockup-sim` mock. How the tier chooses a target, manages the daemon, and gates each test on capabilities is specified in "E2E targets & capabilities" below.

## E2E targets & capabilities

The e2e tier runs one suite against a **live daemon**, choosing the *target* at runtime and letting each test declare the capabilities it needs. A test is written once and runs wherever its needs are met — no per-environment duplication.

### Targets

Selected by `REACHY_MINI_E2E_TARGET`:

- **`sim`** (default) — the harness spawns and manages a MuJoCo daemon, in one of two **launch modes**:
  - **headless** (default; the CI target, [ci.md](ci.md)): `--sim --headless` — no window, runs anywhere. On Linux the eye camera renders offscreen ([../daemon/sim_daemon.md](../daemon/sim_daemon.md) "The headless camera"), so the headless sim there has the camera too; on macOS it has motion and audio only.
  - **headfull** (`REACHY_MINI_E2E_SIM_VIEWER=1`, local): the MuJoCo viewer, to watch the sim as a robot stand-in. On macOS the viewer must run under `mjpython` from a GUI session (see the doc).
  Both modes are the same daemon with the same capabilities, except the camera (below).
  - **scene** (both modes, no knob): a spawned sim always runs the bridge's **test scene** — upstream's empty scene plus props (a portrait plane today) that stay hidden until a test shows, moves and hides them through the `sim_scene` fixture ([sim_scene.md](sim_scene.md)). A test that never shows a prop sees upstream's scene; one sim target serves every test. Tracking tests gate on `camera` **and** `faces`, so they run on the viewer and on a Linux headless sim, and skip on a macOS headless sim (no rendered camera), on a borrowed daemon started without the scene, and on a robot.
- **`real`** — connects to a robot's daemon at `REACHY_MINI_HOST` / `REACHY_MINI_PORT`. When that address is loopback and no daemon is ready — a robot plugged into this machine over USB (Lite) — the harness spawns the hardware daemon (upstream's `reachy-mini-daemon` through the bridge's real daemon launcher, [real_daemon.md](../daemon/real_daemon.md) — serial port auto-detected; Placo kinematics when `placo` is installed, see [daemon.md](../daemon/daemon.md)). A wireless robot runs its own daemon: borrow it or skip — connecting with upstream's default media selection, the WebRTC stream the robot serves, where a local daemon gets the IPC media path ([testing_support.md](testing_support.md) "Configuration via the environment"); the bridge is not validated on a wireless robot ([../project.md](../project.md)).

Any target **reuses a daemon already reachable** at the address (a viewer sim you started by hand, or the robot), instead of spawning its own.

### Daemon lifecycle — own it or borrow it

The harness stops only daemons **it spawned** (the `sim` target in either launch mode, or the `real` target's local hardware daemon — which puts the robot to sleep on stop) and connects to but **never tears down** ones it didn't (a wireless robot's, or an already-running daemon it reused). A spawned daemon is **session-scoped**: one per `pytest` run, started by the first module that needs it and stopped when the run ends — never per file, never per test. The bridge session over it is **module-scoped**: one per test file, so a file starts from the config's modes (wobbling, idle, tracking, detection) whatever the previous file left, and the daemon's state a file inherits is the head's pose and the scene, which every test reads or clears at its start. A robot a harness-spawned hardware daemon drives wakes once and sleeps once per run. A daemon that dies mid-run fails every later file, as a borrowed one does.

### Capabilities are probed, not assumed

Environment quirks decide what actually works — audio needs `start_recording()` first, the sim camera needs a GL context, DoA needs the mic array — so inferring from the backend type is unreliable. The fixture instead **probes** each capability against the live daemon at setup, and a `requires_caps(...)` gate **skips** (never fails) a test whose needs the current target can't meet — unless the run declared that capability **required** (`REACHY_MINI_E2E_REQUIRED_CAPS`, [testing_support.md](testing_support.md)), in which case the harness fixture fails at setup, as it does when it cannot bring a daemon up at all; that is how CI tells a camera that did not come up from a camera nobody has ([ci.md](ci.md)):

```python
def test_say_is_audible(live_bridge):
    requires_caps(live_bridge, "audio")  # skips where audio isn't probed
    ...
```

| Capability | probe | sim headless | sim headfull | real robot |
|---|---|---|---|---|
| `motion` | backend reports a status | ✅ | ✅ | ✅ |
| `audio` | a mic sample arrives on the open media session (never restarted, see [testing_support.md](testing_support.md)) | ✅ software AEC, host device | ✅ | ✅ hardware AEC |
| `camera` | the bridge's camera feed publishes a frame — read on the feed, the one reader of upstream's one-shot `get_frame()` ([camera.md](../vision/camera.md)), never on `get_frame()` beside it | ✅ Linux (offscreen EGL render) · ❌ macOS (no display-less GL) | ✅ | ✅ |
| `gravity_compensation` | not a simulation, and `GET /api/kinematics/info` reports `engine == "Placo"` | ❌ | ❌ | ✅ with Placo (`reachy-mini[placo_kinematics]`) |
| `faces` | the daemon's `/api/sim/inject/bodies` lists a portrait (a body of kind `face`) — it runs the bridge's test scene ([sim_scene.md](sim_scene.md)), which every harness-spawned sim does; its pool of portraits starts hidden and a test spawns the ones it needs | ✅ scene loads (looked at on Linux; on macOS nothing looks at it: no camera) | ✅ | ❌ |
| `face_markers` | the daemon's `/api/sim/displays/face_markers` answers — it draws the faces the bridge sends it ([sim_displays.md](../daemon/sim_displays.md)), which every viewer sim the harness spawns does; a test reads back where the bridge placed a face | ❌ no viewer | ✅ | ❌ |
| `doa` · hardware-AEC quality · beamforming | — | ❌ | ❌ | ✅ |

The sim covers **motion and audio** (audio via the host's audio device with *software* AEC — the device named "Reachy Mini Audio" when a robot is plugged in over USB, else the machine's default speaker and mic, for the sim daemon's own sounds and the client's alike — only the XVF3800's hardware AEC/beamforming/DoA are robot-only); the sim **camera** needs a GL context, so it works headfull, and headless on Linux through Mesa's EGL, but not headless on macOS.

### The harness

The `live_bridge` fixture is the entry point: it resolves the target, brings up a daemon under the own-it-or-borrow-it rule, builds a `ReachyMiniBridge` over it with media on (`media_backend` by the host's locality: `"local"` on loopback, upstream's `"default"` for a remote robot) and the shipped `yunet` detector configured with detection and tracking off (a test turns on what it needs, and the `face_scene` fixture turns both off after it — [testing_support.md](testing_support.md)), probes capabilities through `bridge.robot` and the bridge's camera feed — once per run, on the first session over the daemon, every later module reusing the set, since the capabilities are the daemon's and not a session's; the camera probe waits up to 5 s for the feed to publish a frame, which a fresh session can take more than 2 s to deliver on a daemon that has served sessions before, and reads the feed rather than `get_frame()`: the feed's thread loops that one-shot call from the bridge's start, and a second reader beside it starves one or the other silently, which cost a CI run in two its camera for the whole run — and yields a `LiveBridge` (`bridge`, `capabilities`, `run`). `requires_caps(...)` takes that yielded value and skips a test whose needs the target can't meet. **The harness is shipped library code** — `reachy_mini_bridge.testing` (see [testing_support.md](testing_support.md)) — so downstream consumers reuse it; this tier dogfoods it, pulling the fixtures into `tests-e2e/conftest.py` from `reachy_mini_bridge.testing.fixtures` and importing `requires_caps` / `require_env` from `reachy_mini_bridge.testing`. The headless `sim` target spawns the media-on MuJoCo daemon and probes **motion**, **audio** and **faces** — and on Linux **camera** too, through the launcher's offscreen render ([../daemon/sim_daemon.md](../daemon/sim_daemon.md) "The headless camera"); on macOS the camera probe finds no frame headless (no display-less GL), so `requires_caps("camera")` skips there, and with it the tracking tests (`doa` is robot-only and reserved). Spawning the daemon from a process that has already imported `reachy_mini` scrubs the inherited GStreamer-bundle env vars first, so the child sets fresh ones — see [../docs/guides/running-daemons.md](../../docs/guides/running-daemons.md) for that gotcha and the launch recipes for every mode. The **motion**, **audio**, and **camera** e2e tests all plug into this harness (`tests-e2e/test_motors.py`, `test_motion.py`, `test_audio.py`, `test_perception.py`), each gated on the capability it needs; the camera tests run only where a GL context exists (the viewer sim, a Linux headless sim, a real robot). **Every test that turns detection or tracking on gates on `faces`**, the sim's scripted portraits: a real robot's view holds whoever stands in front of it, which no assertion can script, so the detection and tracking tests skip there, and the motion and audio tests, with both off, measure a head no one in the room pulls off its idle move. The **attention / gaze** tests (`tests-e2e/test_head_tracking.py` — the head converges on a face and follows it, the attention hand-back with breathing and the re-engage, an emotion over tracking, whom the head follows among several faces) gate on `camera` + `faces` and run on the viewer sim and on a Linux headless sim (CI), through the shipped convergence kit `reachy_mini_bridge.testing.gaze` ([testing_support.md](testing_support.md)).

## What a good test asserts

- **Functional, not tautological.** Exercise what a feature actually does — inputs → outputs, state changes, side effects — not that it runs or matches its own signature. A test that would pass against a broken implementation (asserting a constant, that an object isn't `None`, that a mock was called) isn't worth writing.
- **Drive the public API like a real caller.** Prefer exercising the public surface the way a consumer would over reaching into internals; assert on the observable result.
- **In the e2e tier, assert on behavior, not exact output.** Real service responses vary run to run, so a live test asserts a robust property ("a non-empty result came back", "the side effect happened"), never a specific string.

## Test isolation

If the package holds process-global or singleton state, both tiers carry an identical autouse fixture (in each tier's `conftest.py`) that resets it before and after every test, so no state — or background timers/threads — leaks across tests. The fixture is duplicated rather than shared because `tests-e2e/` isn't a package that imports from `tests/`, and it's only a few lines.

## Live tier: skip without credentials

A live test needs real credentials, and it must **skip — never fail** — when they're absent, so you exercise only the services you hold keys for and a contributor (or CI) with none is never broken. `reachy_mini_bridge.testing.require_env(NAME)` (shipped, see [testing_support.md](testing_support.md)) implements this: it returns the env var or calls `pytest.skip(...)` when it's unset. Credentials come from the environment, never committed. The bridge's own real-TTS live test needs none: it runs on the local pocket-tts model (the `tts` dependency group, default in a local sync, carries the three provider extras, see [project.md](../project.md)), so the default `uv run pytest tests-e2e` exercises `say` end to end; only the cloud tests are key-gated (`ELEVENLABS_API_KEY` for ElevenLabs, `GRADIUM_API_KEY` for Gradium). **A missing provider skips the way a missing key does:** each provider test calls `pytest.importorskip` on the provider's module (`pocket_tts`, `elevenlabs`, `gradium`) before it builds the synthesizer, so a sync without the `tts` group skips the three rather than failing on tts-engine's `ConfigError` (CI installs the group, [ci.md](ci.md): the pocket test runs on every pull request). The custom-detector test (`tests-e2e/test_custom_faces.py`, [user_perception.md](../vision/user_perception.md) "Custom detectors") needs no credential either: it runs the shipped YuNet class through the `custom` path, in a bridge session of its own (`live_bridge_custom_faces` — the detector is config-only), whenever the viewer sim runs (`REACHY_MINI_E2E_SIM_VIEWER=1`), and skips where the `faces` capability is absent; the model downloads into the Hugging Face cache on first use.

## Tooling

- **`pytest`** is the runner (with **`pytest-xdist`** for the fast tier's parallel run, above); **`ruff`** lints/formats; **`pyright`** (`standard` mode) type-checks. All three are the gate after any change — lint, type check, and tests must pass before work is considered done (see [AGENTS.md](../../AGENTS.md), "Verification").
- **`pyright` covers test code too:** its `include` is `src`, `tests`, and `tests-e2e`, so tests are type-checked alongside the library rather than being a blind spot.

## Open questions

None currently. Continuous integration — both tiers on GitHub's hosted runners — is [ci.md](ci.md).
