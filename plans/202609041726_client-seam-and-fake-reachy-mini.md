# Client seam and FakeReachyMini

**Status:** Done

Implements [specs/client.md](../specs/client.md) (the whole spec) plus the base-dependency additions it and [specs/project.md](../specs/project.md) call for: the `RobotClient` union alias, the `build_robot(...)` factory with lazy upstream import, and the first-party `FakeReachyMini` stand-in covering the v1 consumed slice. It deliberately leaves the interaction verbs to the api plan and the media session/resampling to the audio plan — here we only provide the seam and a fake the layers above can run and test against.

## Scope

- `pyproject.toml` — add base runtime deps (`numpy`, `reachy_mini`) and the `sim` extra (`reachy_mini[mujoco]`); keep the project-map/spec-frontmatter test green.
- `src/reachy_mini_bridge/client.py` — replace the placeholder with: the `RobotClient` type alias, `build_robot(...)`, `FakeReachyMini`, and its small fake stand-in objects (status / media / audio).
- `tests/test_client.py` — functional tests driving `build_robot("fake")` and `FakeReachyMini` directly (no `reachy_mini` import).
- `tests-e2e/conftest.py` — a module-scoped `sim_daemon` fixture (one daemon per file, reuse-or-spawn, skip-guarded) running the real MuJoCo backend headless (`--sim --headless`).
- `tests-e2e/test_client_sim.py` — the live tier: the client connects over the network and reads motor state against the MuJoCo daemon. The deterministic tier can never cover the real `reachy_mini` client path (it imports no `reachy_mini`).

## Steps

1. **Dependencies.** In `pyproject.toml`, add `numpy` and `reachy_mini` (version-pinned) to `[project.dependencies]`, and `[project.optional-dependencies]` `sim = ["reachy_mini[mujoco]"]`. Run `uv sync --dev`. **Checkpoint:** `reachy_mini` must import and pyright must resolve `reachy_mini.ReachyMini` — the union alias's type-checking depends on it. If it can't be installed in this environment, surface that as a blocker (the `fake` path and tests still run without it, but the real/sim path and static checking need it).

2. **`RobotClient` alias + `build_robot`.** In `client.py`:
   - Under `if TYPE_CHECKING:` import `ReachyMini`; define `RobotClient: TypeAlias = "ReachyMini | FakeReachyMini"`.
   - `build_robot(backend: str = "real", *, robot: FakeReachyMini | None = None, **opts) -> RobotClient`: if `robot` is given, return it; if `backend == "fake"`, return `FakeReachyMini()`; else `from reachy_mini import ReachyMini` (lazy, inside the function) and return `ReachyMini(use_sim=(backend == "sim"), **opts)`. Validate `backend` ∈ {real, sim, fake} with a clear `ValueError`.
   - The lazy import lives only here, so importing `client` (or `FakeReachyMini`) never imports `reachy_mini`.

3. **`FakeReachyMini` — motion / expression / motors / lifecycle.**
   - Records every command it receives on a public `commands: list[tuple[str, dict]]` so tests can assert on them.
   - Motor state: an internal mode string starting `"disabled"`; `enable_motors()` → `"enabled"`, `disable_motors()` → `"disabled"`, `enable_gravity_compensation()` → `"gravity_compensation"`. `get_status()` returns a `_FakeStatus` whose `.backend_status.motor_control_mode` is that string (plain-`str` stand-ins that import nothing — see [specs/client.md](../specs/client.md) "The upstream-typed returns").
   - `goto_target(...)`, `async def async_play_move(...)`, `start_head_tracking(...)`, `stop_head_tracking()` record their args (the fake performs no real motion; `async_play_move` is `async` to match upstream).
   - Context manager: `__enter__` returns self, `__exit__` records teardown. Usable as `with build_robot("fake") as r:`.

4. **`FakeReachyMini` — media.** A `.media` attribute (a `_FakeMedia`) exposing the v1 media slice: `start_recording()`/`stop_recording()`, `start_playing()`/`stop_playing()`, `push_audio_sample(x)` (records the pushed array), `play_sound(name)` (records), `get_audio_sample()` returning synthetic PCM, `get_input_audio_samplerate()`/`get_input_channels()`/`get_output_audio_samplerate()`/`get_output_channels()`, and a nested `.audio` (`_FakeAudioControl`) with `apply_audio_config(...)` and `clear_player()`. Synthetic capture matches the SDK source we read (float32, `(n, 2)` stereo, 16 kHz) but is produced from the getters so it tracks whatever the hardware confirmation settles ([specs/audio.md](../specs/audio.md) open question 1); the fake's constants are the single place to adjust if it differs.

5. **Fidelity kept minimal for now** ([specs/client.md](../specs/client.md) open question 2): `get_audio_sample` returns a short zero/low-tone buffer, perception getters return plausible zeros. Richer synthetic data is added later only if the api/audio tests need it.

6. **Live e2e test with a module-scoped `sim_daemon` fixture** (started once per file, stopped at the end — never per test; reuses a daemon already on `127.0.0.1:8000` if present). The fixture spawns `reachy-mini-daemon --sim --headless --no-media --no-preload-datasets` — the **real MuJoCo backend, headless** (no viewer) — and waits for the **backend** to actually come up (`get_status().backend_status` present), not just the WebSocket. It runs anywhere incl. CI, and skips when the `sim` extra / launcher is missing. The test connects with `build_robot("real", connection_mode="network", host, port)` and asserts a status read returns a recognized motor mode. (The in-process `FakeReachyMini` already covers the mock level, so no `--mockup-sim` tier; the interactive MuJoCo viewer via `mjpython` is a dev-only convenience.) Recipe + gotchas: [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md).

## Verification

- `tests/test_client.py` asserts, driving the public surface:
  - `build_robot("fake")` returns a `FakeReachyMini`; an unknown backend raises `ValueError`; `robot=` override is returned as-is.
  - Importing `reachy_mini_bridge.client` does **not** import `reachy_mini` (assert `"reachy_mini" not in sys.modules` after a fresh import of the fake path).
  - Motor state transitions: after `enable_motors()`, `get_status().backend_status.motor_control_mode == "enabled"`; `enable_gravity_compensation()` → `"gravity_compensation"`; `disable_motors()` → `"disabled"`.
  - Commands are recorded: `goto_target`/`start_head_tracking`/`async_play_move` land in `commands` with their args; `media.push_audio_sample` records the array; `media.play_sound` records the name.
  - `media.get_audio_sample()` returns an ndarray whose dtype/shape/rate agree with the `get_input_*` getters (so downstream conversion code can trust them).
- The e2e tier passes **headless** (verified: `uv run pytest tests-e2e` spawns a real MuJoCo `--headless` daemon and the client reads its status). This is the only coverage of the real `reachy_mini` client path — the deterministic tier can't reach it.
- `uv run ruff check .` and `uv run ruff format .` clean; `uv run pyright` clean (in particular the `RobotClient` union resolves and `FakeReachyMini` satisfies every call site once the api plan lands — for now, that the fake type-checks); `uv run pytest` green (the default run does not collect `tests-e2e/`; run the sim test explicitly with the `sim` extra installed).
- Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass.
