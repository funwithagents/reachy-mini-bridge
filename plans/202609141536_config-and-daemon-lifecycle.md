# Config and daemon lifecycle

**Status:** Done

Implements `specs/config.md` (all of it), `specs/daemon.md` (all of it), `specs/api.md` ("Constructed from a config", "Lifecycle"), and `specs/testing_support.md` (the `_daemon.py` wrapper and the `live_api` construction), and updates the editorial mentions in `specs/robot.md` / `specs/audio.md` that already describe the result. It delivers: a `ReachyMiniConfig` buildable from a dict / JSON string / JSON file; a `ReachyMiniApi` constructed from that config (or the backend-string shorthand) with `from_dict` / `from_json` / `from_json_file` mirrors, whose `tts` block builds the default `TTSEngineSynthesizer`; a bridge-owned `daemon.py` that spawns or borrows the sim daemon so `ReachyMiniApi(config)` with `daemon.spawn: "auto"` yields a working simulated robot; and the testing harness rebuilt on top of it. It deliberately leaves out the two `config.md` / `daemon.md` open questions (env-var interpolation, named audio profiles, forwarding daemon output, a Lite local daemon).

**Ordering.** Self-contained: it runs first, against the code as it is today (`hello()` still in `__init__.py`, `ReachyMiniApi.__aenter__` still a plain enter/enter sequence). It therefore builds the api-level `AsyncExitStack` lifecycle itself and a minimal package front door for the names it needs. The two earlier-dated plans, [202609111400_package-front-door-and-fake-fidelity.md](202609111400_package-front-door-and-fake-fidelity.md) and [202609111410_media-session-lifecycle-hardening.md](202609111410_media-session-lifecycle-hardening.md), stay `Todo` and later extend what this plan leaves: the remaining front-door names and the fake-fidelity items; the `MediaSession`-internal unwinding, the open-session guards, and the mic-poll fix. Neither is a prerequisite.

## Design decisions (settled with the user)

- **Robot construction moves from `__init__` to `__aenter__`.** Upstream `ReachyMini` connects in its constructor, and a spawned daemon must be ready before that. So `ReachyMiniApi.__init__` stores the config and the synthesizer only; `__aenter__` enters the managed daemon (when configured), builds the robot with `build_robot`, enters it, then opens the media session. `api.robot` / `api.raw` raise `BridgeError` before entry. `robot.py` itself does not change.
- **Upstream's `spawn_daemon` is rejected, not forwarded.** Verified on macOS from a process that imported `reachy_mini`: `ReachyMini(use_sim=True, spawn_daemon=True)` prints "Starting a new daemon…", connects once immediately (connection refused, no readiness wait), and leaves no daemon behind. The bridge's `daemon` block replaces it; `config.md` reserves the key.
- **`daemon.py` gets injectable seams for the process and the probe.** Module-level private callables `_spawn(cmd, env) -> Popen-like` and `_ready(host, port) -> bool`, read at call time by `managed_daemon`, so `tests/` script both without a daemon; `is_daemon_ready` is the real probe and the default for `_ready`.
- **`ConfigError(ValueError)` and `DaemonError(BridgeError)` land in `errors.py`**, per `config.md` / `daemon.md`.
- **The api-level lifecycle is an `AsyncExitStack`, built here.** `ReachyMiniApi.__aenter__` creates a local `contextlib.AsyncExitStack`, registers the daemon exit, the robot exit, and the media session's `__aexit__` as each step succeeds, and stores `stack.pop_all()` on success; a failure mid-way closes the local stack (unwinding in reverse) and re-raises. `__aexit__` takes the stored stack, clears the attribute first (so `robot` reads as unavailable even if a teardown step raises), then `aclose()`s it, which runs every remaining step even when one fails. `MediaSession`'s own internals (its `start_*` / `stop_*` pairing, the `_open` flag) are untouched here — that is the lifecycle-hardening plan's job, and it slots into this stack unchanged.
- **A minimal front door, extended later.** `__init__.py` loses uv's `hello()` and gains a package docstring plus re-exports of `ReachyMiniApi`, `ReachyMiniConfig`, and `ConfigError` in `__all__` — the names this plan's `api.md` wording promises. The front-door plan later adds `SpeechSynthesizer`, `TTSEngineSynthesizer`, `BridgeError`, `MotorsNotEnabledError` to the same `__all__` (its own note says so) rather than rewriting the file.
- **The `live_api` fixture keeps `daemon.spawn: "never"`** and manages the daemon itself through `_daemon.py` (module scope: one daemon per test file). `_daemon.py` keeps its env-var resolution and becomes a wrapper: `real` → `is_daemon_ready` or skip; `sim` → `managed_daemon(DaemonConfig(spawn="auto", headless=not viewer))` with `DaemonError` → `pytest.skip`, plus the existing `pytest.importorskip("mujoco")`.

## Scope

- `src/reachy_mini_bridge/errors.py` — add `ConfigError(ValueError)` and `DaemonError(BridgeError)`.
- `src/reachy_mini_bridge/config.py` — replace the placeholder: `DaemonConfig`, `AudioSettings`, `ReachyMiniConfig`, each with `from_dict` / `from_json` / `from_json_file`; the validation rules in `config.md`; the lazy `inspect.signature(reachy_mini.ReachyMini)` key check; the loopback-host check; `effective_robot_options()` that fills `host` / `port` / `connection_mode` / `media_backend` when `daemon.spawn != "never"`.
- `src/reachy_mini_bridge/daemon.py` — replace the placeholder: `DaemonHandle`, `is_daemon_ready`, `launch_command`, `scrubbed_env` (module-private constant list from `testing/_daemon.py`), `managed_daemon`, the `_spawn` / `_ready` seams.
- `src/reachy_mini_bridge/api.py` — constructor takes `config: ReachyMiniConfig | str = "real"`, keyword `synthesizer`; drop `**opts`, `audio_config`, and `connect`; `from_dict` / `from_json` / `from_json_file`; the `tts` block → `TTSEngineSynthesizer` (with `ImportError` → `ConfigError` naming the extra); a new `AsyncExitStack` lifecycle in `__aenter__` / `__aexit__` with the order daemon (via `asyncio.to_thread`) → robot build + enter → media, and the media session built in `__aenter__` too (it needs the robot); `robot` / `raw` guarded.
- `src/reachy_mini_bridge/__init__.py` — replace `hello()` with a package docstring and re-exports of `ReachyMiniApi`, `ReachyMiniConfig`, `ConfigError` (+ `__all__`).
- `src/reachy_mini_bridge/testing/_daemon.py` — wrapper over `daemon.py` (see decisions); delete the moved spawn / readiness / env code.
- `src/reachy_mini_bridge/testing/fixtures.py` — `live_api` builds `ReachyMiniApi(ReachyMiniConfig(backend=..., robot={...}))`.
- `config.example.json` — keep in sync (already written with the spec).
- `tests/test_config.py` — new (below).
- `tests/test_daemon.py` — new (below).
- `tests/test_api.py` — construction-from-config, `from_*`, `tts`-block, lifecycle-order, and `robot`-guard tests (below); adjust `test_robot_escape_hatch_is_the_fake` to enter first.
- `tests/test_testing_support.py` — adjust for the `_daemon.py` wrapper.
- `tests/test_project_map.py` — no change expected (the map and frontmatter are already updated).
- `specs/config.md`, `specs/daemon.md` — add `tests:` frontmatter (`tests/test_config.py`, `tests/test_daemon.py`) once the files exist; status `Stable → Implemented`.
- `specs/api.md`, `specs/testing_support.md` — status `Updated → Implemented` (the api.md Lifecycle bullet is satisfied at the api level by this plan; the media session's internal guarantees are `audio.md`'s and stay with the hardening plan); `specs/_index.md` rows in sync.
- `specs/_analysis.md` — delete the `hello()` scaffold item (cleared here) and, under "Robustness gaps", narrow the `ReachyMiniApi.__aenter__` half of the "Teardown isn't guarded on a failed open" item to the `MediaSession` half (the api half is cleared here).
- `plans/202609111400_package-front-door-and-fake-fidelity.md`, `plans/202609111410_media-session-lifecycle-hardening.md` — their notes already describe how they extend this plan's result; re-read them at the end and fix anything this plan's actual implementation made stale.
- `README.md` — a "Usage" snippet showing `ReachyMiniApi.from_json_file(...)` next to the `ReachyMiniApi("fake")` one.
- `docs/running-the-sim-daemon.md` — a line under "Launch modes" saying `daemon.py` (`daemon.spawn`) implements these recipes, and under "Connecting the client" that the config fills the network/local options in.
- `docs/testing-with-the-bridge.md` — mention that a consumer app can also let the api spawn the daemon (`daemon.spawn: "auto"`) outside of pytest.
- `plans/_index.md` — this plan's row.

## Steps

1. **Errors.** Add `ConfigError` and `DaemonError` to `errors.py`.
2. **Config, tests first (red).** In `tests/test_config.py` (no `reachy_mini` daemon; `reachy_mini` is imported for the signature check):
   - `test_defaults`: `ReachyMiniConfig()` is `real`, empty `robot`, `spawn == "never"`, `tts is None`, `xvf3800 is None`.
   - `test_from_dict_full_example`: `ReachyMiniConfig.from_json_file("config.example.json")` round-trips every field in the example (pins the example to the spec).
   - `test_from_json_and_from_json_file_delegate`: the same dict via string and via a `tmp_path` file yield equal configs; invalid JSON raises `ConfigError` naming the path.
   - `test_unknown_backend_rejected`, `test_unknown_top_level_key_rejected`, `test_unknown_daemon_key_rejected`.
   - `test_robot_keys_checked_against_upstream_signature`: `{"robot": {"hots": "x"}}` raises `ConfigError` mentioning `hots`; every real kwarg name (`host`, `port`, `connection_mode`, `media_backend`, `timeout`, `robot_name`) passes.
   - `test_reserved_robot_keys_rejected`: `use_sim` and `spawn_daemon` each raise `ConfigError` whose message names `backend` / `daemon.spawn` respectively.
   - `test_spawn_requires_sim_backend`: `spawn: "auto"` with `real` and with `fake` raises; with `sim` passes.
   - `test_spawn_requires_loopback_host`: `sim` + `spawn: "auto"` + `robot.host: "10.0.0.5"` raises; `"localhost"` / `"127.0.0.1"` pass.
   - `test_effective_robot_options_fill_in_for_a_managed_daemon`: with `spawn: "auto"` and `robot: {}` → `host == "127.0.0.1"`, `port == 8000`, `connection_mode == "network"`, `media_backend == "local"`; with `spawn: "never"` → unchanged (empty); caller-set values are kept.
   - `test_tts_block_shape`: `tts: {"module": {"type": "elevenlabs"}}` passes and is carried verbatim (same dict contents); missing `module`, non-string `type`, and a non-object `tts` raise; `tts: null` is `None`.
   - `test_audio_xvf3800_shape`: `[["AEC_ENABLED", [1]]]` passes verbatim; a list of non-pairs, or a non-string name, raises.
   - `test_daemon_field_types`: `headless: "yes"`, `startup_timeout: 0`, `startup_timeout: true`, `scene: ""` each raise.
3. **Config implementation.** Write `config.py` to make step 2 green. Keep the `reachy_mini` import inside the key-check helper. Include a `_loads(text, source)` JSON helper like tts-engine's.
4. **Daemon, tests first (red).** In `tests/test_daemon.py`, with a `_FakeProc` (scripted `poll()` results, records `terminate` / `kill` / `wait`) installed as `daemon._spawn` and a scripted `daemon._ready` via `monkeypatch`:
   - `test_launch_command_headless_and_viewer`: headless default → `[..."reachy-mini-daemon", "--sim", "--headless", "--no-preload-datasets"]`; `preload_datasets=True` drops the flag; `scene="minimal"` appends `--scene minimal`; `headless=False` → `mjpython -m reachy_mini.daemon.app.main --sim ...`. Resolve launchers through a patched `shutil.which`; a missing launcher raises `DaemonError` mentioning `reachy-mini-bridge[sim]`.
   - `test_scrubbed_env_drops_the_gstreamer_bundle_vars`: with the eight vars set, the child env has none of them and keeps `PATH`.
   - `test_auto_borrows_a_ready_daemon`: `_ready` → `True` at once; no spawn happens; handle is `owned=False`, `pid is None`; exit terminates nothing.
   - `test_auto_waits_for_a_booting_daemon`: port open (patched `_port_open`), `_ready` → `False, False, True`; no spawn; borrowed.
   - `test_auto_spawns_when_the_port_is_free`: `_ready` → `False` then `True` after spawn; handle `owned=True` with the fake pid; exit calls `terminate` then `wait`.
   - `test_always_errors_on_a_busy_port`: port open → `DaemonError`, no spawn.
   - `test_spawn_exit_during_startup_raises_with_exit_code`: `poll()` returns `139` → `DaemonError` containing `139` and the command.
   - `test_spawn_timeout_stops_the_child`: `_ready` always `False`, `startup_timeout=0.05` (patched sleep) → `DaemonError`; the fake was terminated.
   - `test_teardown_kills_after_terminate_timeout`: `wait` raises `TimeoutExpired` → `kill` called.
   - `test_teardown_runs_when_the_body_raises`.
5. **Daemon implementation.** Write `daemon.py` to make step 4 green. The readiness probe (`is_daemon_ready`) is a straight move of `testing/_daemon.py::_backend_ready`; the env scrub, the command builder, and the spawn/poll/teardown loop move from `managed_daemon` there, with `pytest.skip` calls replaced by `DaemonError`. Poll interval 1 s via a module-private `_sleep` for tests to patch.
6. **Api, tests first (red).** In `tests/test_api.py`:
   - `test_string_shorthand_builds_a_fake_config`: `ReachyMiniApi("fake").config == ReachyMiniConfig(backend="fake")`.
   - `test_from_dict_from_json_from_json_file`: each builds an api whose `config.backend == "fake"`; `synthesizer=` passes through.
   - `test_robot_requires_entry`: `api.robot` before `async with` raises `BridgeError`; inside, it is a `FakeReachyMini`; after exit it raises again.
   - `test_tts_block_builds_the_default_synthesizer` (guarded by `pytest.importorskip("tts_engine")`): a `tts` block with a stub module type registered in tts-engine's registry yields an api whose default synthesizer is a `TTSEngineSynthesizer`; without the block `say` still raises `BridgeError`.
   - `test_explicit_synthesizer_wins_over_tts_block`: with both, `say` uses the explicit `_ToneSynth` and `tts_engine` is never imported (assert via `sys.modules` after a `monkeypatch.delitem`, or a patched `TTSEngineSynthesizer` that fails if called).
   - `test_tts_block_without_the_extra_is_a_config_error`: patch the adapter's import to raise `ImportError` → `ConfigError` naming `reachy-mini-bridge[tts]`.
   - `test_managed_daemon_enters_before_the_robot_and_exits_after`: `sim` + `spawn: "auto"`, with `api._managed_daemon` (or `daemon.managed_daemon`) patched to a recording context manager and `build_robot` patched to return a `FakeReachyMini`; the recorded order is daemon-enter, robot-enter, media-open, …, media-close, robot-exit, daemon-exit.
   - `test_robot_build_failure_exits_the_daemon`: same setup, `build_robot` raises → the daemon context exited, the error propagates.
   - `test_media_open_failure_exits_the_robot_and_daemon`: `build_robot` patched to return a `FakeReachyMini` whose `media.start_recording` raises → `async with` re-raises, the fake records `__exit__`, the daemon context exited.
   - `test_exit_tears_down_everything_even_if_media_teardown_fails`: the fake's `media.stop_playing` raises → leaving the block re-raises, `__exit__` is still recorded and the daemon context still exited; `api.robot` raises `BridgeError` afterwards.
   - `test_front_door_exports`: `import reachy_mini_bridge as rmb`; `rmb.__all__` contains `ReachyMiniApi`, `ReachyMiniConfig`, `ConfigError` and each resolves; `hasattr(rmb, "hello")` is false.
   - Adjust `test_robot_escape_hatch_is_the_fake` to read `api.robot` inside `async with`.
7. **Api implementation.** Rewrite the constructor and lifecycle per `api.md`; expose `api.config`. `__aenter__`: raise `BridgeError` if already entered; build a local `AsyncExitStack`; if `cfg.daemon.spawn != "never"`, create `cm = managed_daemon(cfg.daemon, host=..., port=...)`, run `cm.__enter__` under `asyncio.to_thread`, and register `stack.push_async_callback(asyncio.to_thread, cm.__exit__, None, None, None)` (never `stack.enter_context(cm)` — the exit would then run on the loop thread); build the robot with `build_robot(cfg.backend, **cfg.effective_robot_options())` under `to_thread` (it connects), `stack.enter_context(robot)`, assign `self._robot`; build `MediaSession(robot, audio_config=cfg.audio.xvf3800)` and `await stack.enter_async_context(media)`; on success `self._exit_stack = stack.pop_all()`, on `BaseException` `await stack.aclose()` and re-raise. `__aexit__`: take the stack, set `self._exit_stack = None` and `self._robot = None`, then `await stack.aclose()`. `robot` / `raw` raise `BridgeError` when `self._robot is None`.
8. **Front door.** Rewrite `__init__.py`: a one-paragraph docstring (what the bridge is, pointer to `specs/_overview.md`), `from .api import ReachyMiniApi`, `from .config import ReachyMiniConfig`, `from .errors import ConfigError`, and `__all__` with those three names. Switch the `docs/testing-with-the-bridge.md` example import to `from reachy_mini_bridge import ReachyMiniApi`.
9. **Testing harness.** Rewrite `testing/_daemon.py` as the wrapper; update `fixtures.py` to build from a `ReachyMiniConfig`; fix `tests/test_testing_support.py` for whatever it asserted on the moved helpers.
10. **Docs.** README, `docs/running-the-sim-daemon.md`, `docs/testing-with-the-bridge.md` as scoped.
11. **Live check (sim).** `uv run pytest tests-e2e` on the headless sim (harness path). Then, outside pytest, run a scratch script: `ReachyMiniApi.from_dict({"backend": "sim", "daemon": {"spawn": "auto"}})` entered with `async with`, read `get_motors_state()`, exit — and confirm no `reachy-mini-daemon` process remains and port 8000 is free afterwards. Repeat with a daemon already running to confirm it is borrowed, not stopped.
12. **Analysis and sibling plans.** Delete the `hello()` item from `specs/_analysis.md` and narrow its api-teardown item as scoped; re-read the two sibling plans' notes against the code as built.
13. **Statuses.** Add `tests:` to `config.md` / `daemon.md` frontmatter; flip `config.md`, `daemon.md`, `api.md`, `testing_support.md` to `Implemented` (files + `_index.md`); set this plan `Done` here and in [_index.md](_index.md).

## Verification

- `uv run ruff check .` and `uv run ruff format .` are clean.
- `uv run pyright` is clean — including the union-typed `build_robot` result entered on the stack, and the `tts` block's `dict[str, Any]` passing into `TTSEngineSynthesizer`.
- `uv run pytest`: every test in steps 2, 4, and 6 passes; `tests/test_project_map.py` passes with the two new modules and the new frontmatter; nothing regresses.
- Step 11's live check passes on the headless sim, with no daemon left behind.
- `grep -rn "spawn_daemon" src/` finds only the reserved-key rejection in `config.py` and the readiness probe's explicit `spawn_daemon=False` in `daemon.py`; `grep -rn "def connect" src/reachy_mini_bridge/api.py` finds nothing; `grep -rn "hello" src/` finds nothing.
