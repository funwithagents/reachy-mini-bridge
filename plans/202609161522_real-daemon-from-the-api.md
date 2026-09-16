# A `real` daemon from `ReachyMiniApi`

**Status:** Done

Implements `specs/config.md` ("`daemon` block → `DaemonConfig`", "Validation rules") and `specs/daemon.md` ("One implementation, two users", closing open question 2) and `specs/api.md` ("Lifecycle"), plus `specs/config.md` / `specs/daemon.md` on `preload_datasets` (default and launch flag). A `ReachyMiniConfig` with `backend: "real"` and `daemon.spawn` `auto` / `always` makes `async with ReachyMiniApi(config)` bring up the hardware daemon of a robot attached to this machine over USB — the recipe and lifecycle `daemon.py` already implements for the testing harness — the same way a `sim` config brings up the MuJoCo daemon. It deliberately leaves out spawning on a remote host (a wireless robot runs its own daemon; the loopback rule stays) and any new `DaemonConfig` field. It also makes dataset preloading the default for every spawned daemon.

## Design decisions

- **`daemon.spawn != "never"` is valid for `sim` and `real`.** `fake` has no daemon and stays a `ConfigError`.
- **The fields that apply to `real`** are `spawn`, `preload_datasets` (→ `--no-preload-datasets`) and `startup_timeout`. `headless` and `scene` are MuJoCo knobs: on `real` they are accepted and play no part — the same stance as the `robot` block on `fake` — so one file switches `sim` ↔ `real` by changing `backend` alone.
- **Loopback only, unchanged.** With `spawn != "never"`, `robot.host` must be loopback for both backends: the bridge only starts a daemon on this machine.
- **Client options unchanged.** A managed `real` daemon gets the same filled-in defaults as `sim` (`127.0.0.1:8000`, `connection_mode="network"`, `media_backend="local"`) — the options the testing harness already connects to a harness-spawned real daemon with.
- **Datasets preload by default.** `DaemonConfig.preload_datasets` defaults to `true` (a missing field preloads). The launch command always passes the flag explicitly — `--preload-datasets` or `--no-preload-datasets` — because `reachy-mini-daemon`'s own default is not to preload: before this change `true` only omitted `--no-preload-datasets`, and the daemon still did not preload. Upstream preloads in a background executor after startup, so readiness and `startup_timeout` are unaffected. The testing harness follows the default.
- **The api passes its backend to `managed_daemon`**, which picks the launch recipe (`reachy-mini-daemon [--kinematics-engine Placo] [--no-preload-datasets]` for `real`). Teardown's 10 s grace already covers the robot going to sleep.

## Scope

- `src/reachy_mini_bridge/config.py` — the spawn rule accepts `real`; error message names both backends; `DAEMON_BACKENDS` lives here; `preload_datasets` defaults to `true`.
- `src/reachy_mini_bridge/daemon.py` — reuses `config.DAEMON_BACKENDS`; `launch_command` always passes `--preload-datasets` / `--no-preload-datasets`.
- `src/reachy_mini_bridge/api.py` — `__aenter__` passes `backend=cfg.backend` to `managed_daemon`.
- `tests/test_config.py` — spawn accepted on `real`, still refused on `fake`; loopback rule and filled-in options on `real`; sim-only knobs accepted on `real`.
- `tests/test_daemon.py` — launch commands carry the explicit preload flag.
- `tests/test_api.py` — the lifecycle recorder records the backend; a `real` config enters the daemon with `backend="real"`.
- `specs/config.md`, `specs/daemon.md`, `specs/api.md` — the design above (config / daemon → `Updated` until this plan is `Done`); `daemon.md` open question 2 removed.
- `config.example.json` (`preload_datasets: true`), `specs/testing_support.md`, `tests-e2e/test_api.py` (docstring), `specs/_overview.md`, `README.md`, `docs/running-the-sim-daemon.md`, `docs/testing-with-the-bridge.md` — "sim only" wording for `daemon.spawn` becomes sim or USB robot.

## Steps

1. Specs: `config.md` spawn rule + field table (`headless` / `scene` sim-only) + validation rules; `daemon.md` purpose, "One implementation, two users", relationships, drop OQ2; `api.md` lifecycle names the backend's recipe. Set `config.md` / `daemon.md` → `Updated` (file + index).
2. `config.py`: `daemon.spawn != "never"` requires `backend in ("sim", "real")`.
3. `api.py`: `managed_daemon(cfg.daemon, host=..., port=..., backend=cfg.backend)`.
4. Tests in `tests/test_config.py` and `tests/test_api.py` as scoped.
5. `preload_datasets` defaults to `true`; `launch_command` passes the explicit flag either way; example config, specs and tests follow.
6. Docs/overview/README wording.
7. Verify, then mark this plan `Done` and the specs `Implemented`.

## Verification

`uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest` all pass. With a Lite plugged in and no daemon running, a `{"backend": "real", "daemon": {"spawn": "auto"}}` config entered with `async with ReachyMiniApi(...)` wakes the robot, and exiting puts it to sleep (manual check; not part of the automated tiers).
