---
code:
  - src/reachy_mini_bridge/testing/__init__.py
  - src/reachy_mini_bridge/testing/fixtures.py
  - src/reachy_mini_bridge/testing/_daemon.py
  - src/reachy_mini_bridge/testing/support.py
  - src/reachy_mini_bridge/testing/gaze.py
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

A consumer's two tiers map onto the bridge's three backends exactly as the bridge's own do ([testing.md](testing.md), [robot.md](../core/robot.md)):

| Consumer tier | Backend | Extra needed | Daemon |
|---|---|---|---|
| Unit / integration | `fake` | none | none — offline, deterministic |
| Live / e2e | `sim` | `sim,test` (`reachy-mini-bridge[sim,test]` — the simulator, which the `sim` extra declares directly ([project.md](../project.md)), and this harness) | MuJoCo, harness-managed |
| Live / e2e | `real` | `test` (`reachy-mini-bridge[test]` — this harness) | robot at host/port — harness-managed for a USB robot on this machine |

- **Unit tests use `fake`.** The public `ReachyMiniBridge("fake")` ([bridge.md](../core/bridge.md)) already needs no daemon, no network, and no extra — a consumer constructs it directly and asserts through the `bridge.robot` escape hatch on recorded commands / synthetic perception. Importing the package pulls in `reachy_mini` (the base dependency), which needs its native libs installed, **not** a live daemon.
- **E2E tests use `sim` or `real`** through the shipped `live_bridge` fixture below.

### Shipped as `reachy_mini_bridge.testing`, behind a `test` extra

The harness ships as the `reachy_mini_bridge.testing` package, pulled in by the **`test` optional-dependency extra** (`reachy-mini-bridge[test]`). The extra carries `pytest` (floor `>=9.1.1`, matching the bridge's own dev pin) — the harness's only added dependency. The sim daemon launcher (`reachy-mini-daemon` / `mjpython`) comes from the `sim` extra, so a consumer running e2e against `sim` installs `reachy-mini-bridge[sim,test]`; against `real`, `reachy-mini-bridge[test]` alone.

### Opt-in as a pytest plugin — no auto-registration

The `live_bridge` fixture and its helpers live in `reachy_mini_bridge.testing.fixtures`, a module shaped as a **pytest plugin**. It is **not** auto-registered (no `pytest11` entry point): a consumer opts in explicitly from their own `conftest.py`:

```python
# their conftest.py
pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]
```

Explicit opt-in keeps installation side-effect-free — merely depending on the bridge never injects fixtures into an unrelated test run — and keeps the consumer in control of where the fixtures apply.

### Package layout

Five modules, with the daemon machinery kept private behind the plugin:

- `reachy_mini_bridge/testing/__init__.py` — re-exports the two skip gates (`requires_caps`, `require_env`) and the loop pair (`BridgeLoop`, `LiveBridge`) from `support.py`. The `live_bridge` fixture is deliberately *not* re-exported here: a fixture only registers through the plugin module a consumer names in `pytest_plugins`.
- `testing/fixtures.py` — the pytest-plugin module a consumer names in `pytest_plugins`: the `live_bridge` fixture and the capability probing it yields, `sim_scene`, `face_scene`, `emotions_library`.
- `testing/_daemon.py` — **private** glue between the environment and the bridge's daemon lifecycle: target/backend/address/engine resolution from the env vars below, and a thin wrapper over [daemon.md](../daemon/daemon.md)'s `managed_daemon` / `is_daemon_ready` that turns a `DaemonError` into a `pytest.skip`. The own-it-or-borrow-it decision, the readiness poll, the launch recipes, and the GStreamer-bundle env scrub live in the library module `daemon.py`, so the harness and `ReachyMiniBridge` share one implementation. Kept out of `fixtures.py` so the plugin module reads as the fixture surface.
- `testing/support.py` — `requires_caps`, `require_env`, `BridgeLoop`, `LiveBridge`.
- `testing/gaze.py` — the convergence kit of the head-tracking live tests (below), imported from its module like `sim_scene`.

### Public surface

The package exposes exactly the names the bridge's own live tier uses — the `live_bridge`, `sim_scene`, `face_scene` and `emotions_library` fixtures through the `reachy_mini_bridge.testing.fixtures` plugin module, the two skip gates and the loop pair re-exported from `reachy_mini_bridge.testing`, and the convergence kit in `reachy_mini_bridge.testing.gaze`:

- **`live_bridge`** — a **module-scoped** pytest fixture yielding a `LiveBridge`: a running `ReachyMiniBridge` over the resolved target (`bridge`), the `frozenset` of capabilities probed against that live daemon (`capabilities`) — it unpacks as `bridge, caps = live_bridge` — and `run(coro, timeout=None)`, which executes a coroutine on the harness's event loop and returns its result: the way a test awaits anything on the bridge (below). It brings the daemon up under own-it-or-borrow-it (reuse one already reachable, else spawn one and own its teardown — a MuJoCo daemon for `sim`, the hardware daemon for `real` when the address is loopback, i.e. a USB robot on this machine; a non-loopback `real` address is borrow-or-skip), builds the bridge from a `ReachyMiniConfig` ([config.md](../core/config.md)) whose `robot` block carries the harness's connection options (`robot_options(host, port)` in the private daemon glue: `connection_mode="network"`, the resolved host/port, and a `media_backend` chosen by the host's locality — `"local"`, the IPC media path a daemon on this machine serves, on a loopback address; upstream's `"default"` elsewhere, which a network client auto-detects to the WebRTC streaming path a wireless robot's daemon serves — or the value of `REACHY_MINI_E2E_MEDIA_BACKEND`) whose `face_detection` block names the shipped `yunet` detector with detection and tracking off (`face_detection.enabled` and `motion.tracking` at their defaults; the defaults run no detector) — a test turns on what it needs (`set_face_detection(True)`, `arm_tracking`), so every other test keeps the head on its idle move whoever stands in front of the robot, and whose `daemon.spawn` is `"never"` — the harness, not the bridge, owns the daemon: the private `_live_daemon` fixture behind `live_bridge` is **session-scoped**, one daemon for the whole `pytest` run, and the bridge session over it is the module's, so every file starts from the config's modes; the capabilities are probed once per run, on the first session over the daemon, and reused by every later module (`probed_capabilities` in the plugin module — they are the daemon's, not a session's; [testing.md](testing.md) "Daemon lifecycle") — and, on the viewer sim, whose `daemon.sim_displays.face_markers` is on, both in the daemon the fixture spawns and in the bridge's config, so the daemon mounts the displays route and the bridge sends its markers there ([sim_displays.md](../daemon/sim_displays.md): the e2e checks read them back, gated on the probed `face_markers` capability — the route answering), with media on, probes, and tears down what it spawned. It drives the bridge through the `start()` / `stop()` pair on **one event loop**, run on a background thread for the whole module (`BridgeLoop`, public in `reachy_mini_bridge.testing` for a consumer's own fixture — a second session, say): a synchronous module-scoped fixture cannot hold an `async with` open across the module's tests, which is what the pair is for ([bridge.md](../core/bridge.md) "Lifecycle"), and the bridge is loop-bound — its detection loop is an asyncio task and its observables publish on the loop thread ([observable.md](../core/observable.md)) — so the loop that ran `start()` keeps running until `stop()`, and every coroutine a test awaits on the bridge goes through `run`, never `asyncio.run` (a second loop: a verb that starts a task there leaves it to die with that loop). `run` blocks the test until the coroutine returns, propagates its exception, and cancels the coroutine on a timeout or a `KeyboardInterrupt` — the cancellable-verb contract makes that a clean stop.
- **`requires_caps(live, *caps)`** — the skip gate: given the `live_bridge` value, `pytest.skip(...)` unless every named capability (`motion` / `audio` / `camera` / `motor_states` / `gravity_compensation` / …, table in [testing.md](testing.md)) was probed on the current target. A test written once runs wherever its needs are met. **Required capabilities** are the run's, not a test's: when `REACHY_MINI_E2E_REQUIRED_CAPS` names capabilities, every harness fixture checks them right after its probe (`check_required_capabilities(caps)` in the plugin module, public for a consumer's own fixture) and *fails* — every test of the module errors at setup — where one was not probed, and an environment that cannot provide the daemon at all (a spawn that fails, the sim extra missing, a remote `real` address with nothing to borrow) fails the same way instead of skipping. The default, nothing required, keeps every skip a skip; CI requires what its runner is provisioned for ([ci.md](ci.md)).
- **`require_env(name)`** — return an env var or skip when it's absent, so a live test skips (never fails) without its credentials.
- **`sim_scene`** — a second **module-scoped** fixture in the plugin module: a `SimSceneClient` ([sim_scene.md](sim_scene.md)) on the fixture-managed daemon, to spawn, move and despawn portraits from the pool of the bridge's generated scene ([sim_scene.md](sim_scene.md) "A pool of portraits") — as many at once as a test needs. Meaningful only where `live_bridge` probed `faces`; gate with `requires_caps(live_bridge, "camera", "faces")`.
- **`face_scene`** — a function-scoped fixture in the plugin module for a test that puts faces in front of the robot: it gates on `camera` and `faces` (the test skips where `live_bridge` probed neither), clears the pool of portraits before the test and again after it, and yields the `sim_scene` client — so every face test starts with nobody in view and leaves nobody behind — and after the test turns the bridge's detection and tracking back off, the state `live_bridge` starts in, whatever the test turned on. It reads `live_bridge`'s capabilities; a test on a bridge session of its own writes the same gate and clears against that session.
- **`emotions_library`** — a function-scoped fixture a test that plays a recorded move takes (its value is `None`; the effect is the point): the client-side emotions library is in the local Hugging Face cache — a cache hit, else a download (a one-time cost on a fresh machine), else a `pytest.skip` (offline). `play_emotion` resolves the move on the client from that cache, whatever the daemon preloaded for itself.
- **`reachy_mini_bridge.testing.gaze`** — the convergence kit the bridge's own tracking tests and a consumer's "the robot looks at whoever is there" test share, over the test scene's geometry ([sim_scene.md](sim_scene.md)): `expected_yaw_deg(lateral, distance=DEFAULT_FACE_POS[0])` (the yaw a face at `lateral` metres to the side implies — `atan2` from the head's pivot), `yaw_pitch_deg(pose)`, `angle_from_neutral_deg(pose)`, `wait_for(predicate, timeout)` (a blocking predicate polled off the loop), `Track` (the head's sampled path onto a face: `yaw` / `pitch` as the mean over the last `SETTLE_WINDOW_S`, `overshoot_deg`, `swing_back_deg`, `settle_s`, the `face` followed at settle), `track_onto(bridge, where, lateral, *, settle_timeout, min_seconds, expected_yaw_deg)` (sample the head until it holds still — yaw within 1° over a second — then read the followed face from `bridge.faces` by `bridge.head_tracking`'s `track_id`), `assert_tracked(track, *, pitch_ahead=None)` (toward the face, past it by at most `OVERSHOOT_MAX_DEG`, never back past it by more than `YAW_TOLERANCE_DEG`, settled on the expected yaw within `YAW_TOLERANCE_DEG`, the face within `CENTRED` of the image centre, the pitch within `PITCH_TOLERANCE_DEG` of `pitch_ahead` when given), `arm_tracking(bridge, *, focus=False)` (tracking on, and the previous aim released — `attention` back to `watching` within `TRACKING_LOST_S` — the check that the detection loop is alive between tests), `sample_idle(robot, seconds)` (the head's z range and its mean angle from neutral: what the idle move shows). The thresholds are module constants (`LATERAL_M`, `YAW_TOLERANCE_DEG`, `SETTLE_WINDOW_S`, `OVERSHOOT_MAX_DEG`, `PITCH_TOLERANCE_DEG`, `CENTRED`, `NEUTRAL_THRESHOLD_DEG`, `MOVE_THRESHOLD_DEG`), measured on the viewer sim and stated with their measurements in the kit's docstrings; a consumer's tracker is the bridge's, so the same numbers hold for them.

A consumer's e2e test then reads:

```python
from reachy_mini_bridge.testing import requires_caps


def test_my_greeting_speaks(live_bridge):
    requires_caps(live_bridge, "audio")
    bridge, _ = live_bridge
    live_bridge.run(bridge.say("hello", my_synth))
```

### Configuration via the environment

Target and connection are chosen by the same env vars the bridge's tier uses, so the knobs are one documented set:

- `REACHY_MINI_E2E_TARGET` — `sim` (default) | `real`.
- `REACHY_MINI_HOST` / `REACHY_MINI_PORT` — the daemon address (borrow a daemon already there; for `real`, the robot's daemon — spawned by the harness when the address is loopback and nothing is ready).
- `REACHY_MINI_E2E_SIM_VIEWER` — headfull MuJoCo viewer instead of headless (local, needs a GUI/GL context; see [../docs/guides/running-daemons.md](../../docs/guides/running-daemons.md)).
- `REACHY_MINI_E2E_REQUIRED_CAPS` — comma-separated capabilities the run must probe (`motion,audio,camera,faces` in CI); a missing one, or a daemon the harness cannot bring up, fails instead of skipping. Unset: nothing required.
- `REACHY_MINI_E2E_MEDIA_BACKEND` — the `media_backend` the harness's bridges connect with, overriding the locality rule (`local` on a loopback host, upstream's `default` elsewhere); `no_media` for a motion-only run, say.
- `REACHY_MINI_E2E_KINEMATICS` — `analytical` (default) | `placo` | `nn`: the `daemon.kinematics_engine` of the daemon the harness spawns, `sim` and `real` alike ([../core/config.md](../core/config.md) "Kinematics engines"); a borrowed daemon runs its own, and the bridge's tier checks the two agree ([testing.md](testing.md) "One engine per run"). Any other value fails the harness fixture, naming the three. `kinematics_engine()` in the private daemon glue resolves it, for the bridge's own engine check.

A spawned `sim` daemon always runs the bridge's **test scene** ([sim_scene.md](sim_scene.md)): the harness writes it into a temporary directory that lives as long as the daemon and passes its path as `DaemonConfig.scene` — and the run's engine as `DaemonConfig.kinematics_engine`, on the `real` spawn too. Its props start hidden, so a test that never shows one runs on upstream's empty scene; the `faces` capability (a portrait exists — a body of kind `face`) is probed true on every harness-spawned sim, and the `sim_scene` fixture spawns, moves and despawns the portraits of its pool. There is no scene knob: one sim target serves every test. A daemon already ready at the address is borrowed as before, whatever it runs.

### The gotchas move into the shipped code

The hard-won details a consumer would otherwise have to rediscover are the whole reason to ship this rather than document it: the **GStreamer-bundle env scrub** before spawning a daemon from a process that has imported `reachy_mini` (else a doubled plugin path segfaults the child), **probing** capabilities against the live daemon rather than inferring them from the backend type, **probing without restarting the audio pipeline**, and **one event loop from `start()` to `stop()`** (a bridge started under one `asyncio.run` and driven under another has lost its detection task with the first loop). The scrub and the spawn live in the library's `daemon.py` ([daemon.md](../daemon/daemon.md)), the probes behind `live_bridge`, the loop in `BridgeLoop`; a consumer inherits all four for free.

The probes run after the bridge has opened its media session, so the pipeline is already recording and playing. The camera probe reads the bridge's camera feed (`bridge.camera.latest()`), the one reader of upstream's one-shot `get_frame()`, never `get_frame()` beside it. The audio probe reads the bridge's mic feed (`bridge.mic.latest()`), the one reader of upstream's one-shot `get_audio_sample()` ([microphone.md](../audio/microphone.md)), and waits for it to publish a chunk; it never calls `get_audio_sample()` beside the feed, nor `start_recording()` / `stop_recording()`. Upstream's GStreamer audio binds the robot's speaker and mic by device name once, when it builds that single shared pipeline, and on macOS a stop-then-start reopens both on the system defaults — the Mac's own speaker and microphone — for the rest of the test module ([../docs/internals/upstream-sdk-notes.md](../../docs/internals/upstream-sdk-notes.md)). A consumer's own tests should likewise leave the session's pipeline running.

### A documented guide accompanies the code

A consumer-facing guide (`docs/guides/testing.md`, reached from [docs/index.md](../../docs/index.md)) states the extras/backends mapping, the `pytest_plugins` opt-in line, and the env vars — so the entry point is discoverable, not buried in a spec.

## Open questions

1. **Consumer daemon knobs.** The scene, dataset preloading and the startup timeout exist only on `DaemonConfig` ([config.md](../core/config.md)) — the harness always uses the test scene; whether it exposes them (env vars, or a `DaemonConfig` a consumer's conftest hands in) is deferred until one actually needs it. (A genuine deferral, not a load-bearing unknown.)
