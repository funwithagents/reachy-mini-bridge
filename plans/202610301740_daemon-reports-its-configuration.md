# The daemon reports its configuration

**Status:** Done

Implements `specs/daemon/daemon.md` ("What the daemon reports"), with `specs/daemon/sim_daemon.md` ("The report"), `specs/daemon/real_daemon.md` ("The report"), `specs/motion/head_tracking.md` ("The sim's camera"), `specs/core/config.md` and `specs/core/bridge.md`. Both launchers serve `GET /api/bridge/daemon`; the session combines it with upstream's kinematics route into `bridge.daemon` and warns on what contradicts the config. It leaves out the cropped field of view (`effective_hfov_deg`).

## Scope

- `src/reachy_mini_bridge/config.py` — `DAEMON_INFO_ROUTE`; the `DaemonConfig` docstring's rule for `camera`
- `src/reachy_mini_bridge/daemon.py` — `DaemonInfo`, `daemon_differences`
- `src/reachy_mini_bridge/sim_daemon.py`, `real_daemon.py` — the route
- `src/reachy_mini_bridge/robot.py` — `fetch_daemon_json` typed on `AnyReachyMini`
- `src/reachy_mini_bridge/bridge.py` — the read at bring-up, `bridge.daemon`, the camera model, the face marker publisher's gate
- `tests/conftest.py`, `tests/test_bridge.py`, `tests/test_sim_daemon.py`, `tests/test_real_daemon.py` — functional tests of every read
- `tests-e2e/test_motion.py` — `bridge.daemon` on a live daemon
- `docs/reference/api.md`, `docs/reference/configuration.md`, `docs/guides/running-daemons.md`, `docs/guides/perception-and-tracking.md` — which value wins

## Steps

1. Add the route to both launchers.
2. Add `DaemonInfo.from_report` and `daemon_differences`.
3. Read at bring-up on a worker thread; warn; keep the answer for `bridge.daemon`.
4. Build the tracker's camera from it; gate the face marker publisher on it.
5. Keep the fast tier off the network with an autouse 404 on `fetch_daemon_json`.
6. Add the tests, update the docs, run the gate and the live tier.

## Verification

`uv run ruff check src tests tests-e2e examples`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`, `uv run pytest tests-e2e -rs`, and the viewer sim with `REACHY_MINI_E2E_SIM_VIEWER=1`.
