# The sim daemon reports its camera

**Status:** Done

Implements the settled behavior in `specs/daemon/sim_daemon.md` ("The camera report"), `specs/motion/head_tracking.md` ("The sim's camera") and `specs/core/config.md` (`daemon.camera`). The launcher serves `GET /api/sim/camera` on every run and the bridge builds the tracker's camera model from the answer, the config standing in for a daemon that reports nothing. It leaves out the cropped field of view (`effective_hfov_deg`), the `real` backend and `sim_displays`.

## Scope

- `src/reachy_mini_bridge/config.py` — `SIM_CAMERA_ROUTE`, shared by the launcher and the bridge; the `DaemonConfig` docstring's rule for `camera`
- `src/reachy_mini_bridge/sim_daemon.py` — the route, mounted in `run_sim_daemon`'s `create_app` wrap whatever the flags
- `src/reachy_mini_bridge/robot.py` — `fetch_daemon_json` typed on `AnyReachyMini`
- `src/reachy_mini_bridge/bridge.py` — the session reads the route and resolves the camera per the spec's table
- `tests/test_sim_daemon.py`, `tests/test_bridge.py` — functional tests of both sides
- `tests-e2e/test_motion.py` — the route read from the harness's sim daemon
- `docs/reference/configuration.md`, `docs/guides/running-daemons.md`, `docs/guides/perception-and-tracking.md` — which value wins

## Steps

1. Add the route: `source`, and `hfov_deg` for a `webcam`, `null` for a `sim` source.
2. In the bridge, add a reader returning the daemon's camera as `SimCameraSettings`, `None` on a 404, raising on anything else; validation goes through `SimCameraSettings.from_dict`, and unknown fields are ignored so the report can grow.
3. Turn `_camera_model` into an async step: the read through `asyncio.to_thread` on the `sim` backend, the mismatch and failure warnings, the one `INFO` line.
4. Point the fake-robot `sim` test fixtures at a daemon with no route, so the fast tier stays off the network.
5. Add the fast tests: every row of the table through a session, and a webcam behind a silent config aiming as a fixed camera.
6. Add the live test, update the docs, run the gate and the live tier, flip the statuses.

## Verification

`uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`, `uv run pytest tests-e2e -rs`, and the viewer sim with `REACHY_MINI_E2E_SIM_VIEWER=1`.
