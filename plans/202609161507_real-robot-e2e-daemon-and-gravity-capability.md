# Real-robot e2e: harness-spawned daemon, gravity-compensation capability and guard, and on-robot audio

**Status:** Done

Implements `specs/api.md` ("Motors" — the gravity-compensation guard), `specs/robot.md` (the fields and daemon endpoint the guard reads; the fake's engine), `specs/daemon.md` ("The launch command" — the `real` recipe; "One implementation, two users"), `specs/testing_support.md` (`live_api` spawning for `real`; the `gravity_compensation` capability) and `specs/testing.md` ("Targets", "Daemon lifecycle", the capability table). Running the live tier against a USB-connected robot (Reachy Mini Lite) with no daemon up today skips everything; this plan makes `live_api` spawn a local `reachy-mini-daemon` for the `real` target (owned and torn down like the sim one), and turns gravity compensation — which the daemon only supports on the Placo kinematics engine, and whose rejection drops the client connection — into a probed capability so the e2e tier skips it instead of cascading failures. Running it on the robot also showed the pytest tier's say/mic audio going through the Mac's speaker and microphone: the fixture's audio probe stopped and restarted the SDK's shared audio pipeline, and on macOS a restarted pipeline reopens on the system default devices. The probe now reads a sample from the session's already-running pipeline and never restarts it. The same rejection hits any `ReachyMiniApi` caller — `set_motors_state("gravity_compensation")` returns normally, then every later robot call fails and the api cannot reconnect — so `set_motors_state` gains a guard that checks the daemon before sending and raises `GravityCompensationUnsupportedError` instead. It deliberately leaves out letting `ReachyMiniApi` itself spawn a real daemon (`config.md` keeps `daemon.spawn` sim-only). The upstream root cause is out of scope here: a bridge-side guard against restarts is tracked in [specs/_analysis.md](../specs/_analysis.md).

## Design decisions (settled with the user)

- **The harness spawns a real daemon when none is ready, for a loopback address only.** A USB robot's daemon runs on the host machine, so `live_api` launches it the way it launches the sim one: own-it-or-borrow-it, owned daemon stopped at module teardown (the daemon puts the robot to sleep on stop). A non-loopback `REACHY_MINI_HOST` (a wireless robot) still borrows-or-skips — the harness never starts a daemon on another machine.
- **Serial port auto-detected.** The real recipe passes no `-p`; `reachy-mini-daemon` finds the robot's port itself. No robot plugged in ⇒ the daemon exits or never becomes ready ⇒ `DaemonError` ⇒ skip.
- **Placo when installed.** The real recipe adds `--kinematics-engine Placo` when `placo` is importable (`reachy-mini[placo_kinematics]`), so gravity compensation works on a harness-spawned daemon whenever the library is present; otherwise the daemon runs its default engine.
- **The audio probe never restarts the media pipeline.** Upstream `GStreamerAudio` binds `osxaudiosink` / `osxaudiosrc` to the "Reachy Mini Audio" card by `unique-id` once, at construction; after the pipeline goes to `NULL` and back, both elements open the system defaults instead (measured: speaker 91 → 80 "MacBook Pro speakers", mic 91 → 75 "MacBook Pro microphone"). Recording and playback share that one pipeline, so the old probe's `stop_recording()` cleanup plus the fixture's `start_recording()` restore silently moved every later `say` and mic test off the robot. `live_api` probes after `ReachyMiniApi.__aenter__`, when the `MediaSession` is already recording, so the probe just polls `get_audio_sample()` and leaves the pipeline running; the restore step goes away.
- **`set_motors_state("gravity_compensation")` checks before sending.** Upstream's `enable_gravity_compensation()` is fire-and-forget, so the daemon's rejection can't be caught after the fact. The api reads `client.get_status()`: a simulation (`simulation_enabled` / `mockup_sim_enabled`) ignores motor modes, so the command goes out unchanged. Otherwise it reads the engine (`GET http://{client.host}:{client.port}/api/kinematics/info`, off the event loop). Not `Placo`, or unreadable, ⇒ `GravityCompensationUnsupportedError(BridgeError)` naming the engine and the fix, and nothing is sent. Checked on every call — no cache to go stale if the daemon restarts. The engine read (`_daemon_kinematics_engine` in `api.py`) is the one the harness probe reuses. The fake's daemon client carries `kinematics_engine = "Placo"` and `simulation_enabled` / `mockup_sim_enabled = False`, so existing fake flows keep working and tests flip them to exercise the refusal and the sim pass-through.
- **`gravity_compensation` is a probed capability.** Probe: the daemon is not a simulation (`simulation_enabled` / `mockup_sim_enabled` falsy — the MuJoCo backend ignores motor modes) **and** `GET /api/kinematics/info` reports `engine == "Placo"`. The e2e motor-state test is split: `enabled`/`disabled` under `motion`, `gravity_compensation` under `motion` + `gravity_compensation`.

## Scope

- `src/reachy_mini_bridge/api.py` — the gravity-compensation guard in `set_motors_state`; `_daemon_kinematics_engine` / `_fetch_json` (the engine read).
- `src/reachy_mini_bridge/errors.py` — `GravityCompensationUnsupportedError(BridgeError)`.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — the daemon client stand-in gains `kinematics_engine` and the two sim flags, mirrored on its status.
- `tests/test_api.py` — refusal (non-Placo, unreadable engine: nothing dispatched, state unchanged), sim pass-through.
- `src/reachy_mini_bridge/daemon.py` — `launch_command(config, *, backend="sim")` gains the `real` recipe (`reachy-mini-daemon [--kinematics-engine Placo] [--no-preload-datasets]`), `managed_daemon(..., backend="sim")` threads it through; `_placo_available` seam; error messages name the backend.
- `src/reachy_mini_bridge/testing/_daemon.py` — `real` target spawns through `daemon.managed_daemon(..., backend="real")` on a loopback host, skips otherwise.
- `src/reachy_mini_bridge/testing/fixtures.py` — `_probe_gravity_compensation(robot)` adds the capability, reusing the api's engine read; `_probe_audio` polls the open session without starting or stopping recording, and `live_api` drops its `start_recording()` restore.
- `tests/test_daemon.py` — the real recipe (with/without Placo, missing launcher), `managed_daemon` spawning the real recipe.
- `tests/test_testing_support.py` — the gravity-compensation probe against scripted status / kinematics answers; the audio probe leaves a running pipeline running.
- `tests-e2e/test_api.py` — split the motor-state test; gate the gravity step on the capability; on hardware without Placo, the guard refuses and the connection survives.
- `specs/daemon.md`, `specs/testing_support.md`, `specs/testing.md`, `specs/api.md`, `specs/robot.md` — the design above (daemon / testing_support / api / robot → `Updated` until this plan is `Done`).
- `AGENTS.md` — the `errors.py` row of the project map names the new error.
- `docs/testing-with-the-bridge.md`, `README.md`, `AGENTS.md` — real-robot e2e instructions; the new capability; an AGENTS.md "Running the live e2e tests" section; sim audio uses the robot's card when one is plugged in.
- `docs/reachy-mini-api.md` — the macOS restart gotcha.
- `specs/_analysis.md` — the not-yet-planned bridge-side restart guard.

## Steps

1. Specs first: daemon.md real recipe + resolved open question; testing_support.md / testing.md spawn-for-real and the capability; api.md gravity-compensation requirement.
2. `daemon.py`: `backend` keyword on `launch_command` / `managed_daemon`, `_placo_available`, backend-named errors.
3. `_daemon.py`: real → loopback spawn, else skip; drop the "never spawn a robot" branch.
4. `fixtures.py`: gravity-compensation probe (status + HTTP kinematics info, any failure ⇒ absent).
5. Fast tests for 2 and 4; e2e test split.
6. Docs + AGENTS.md section.
7. Audio probe: poll the open session, no start/stop; drop the restore in `live_api`; spec + tests; `_analysis.md` item; upstream issue draft.
8. Gravity-compensation guard: error type, fake fields, `_daemon_kinematics_engine`, the check in `set_motors_state`; the probe reuses the engine read; fast tests; e2e refusal test.
9. Verify (below), live run on the USB robot with no daemon running.

## Verification

`uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest` all pass; `REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e` with no daemon running spawns one, runs the tier (gravity compensation skipped without Placo, and refused by the guard with the connection intact), and leaves no daemon behind; the tones and the TTS phrase are heard from the robot, not the Mac. Then mark this plan `Done` and daemon.md / testing_support.md / api.md / robot.md `Implemented`.
