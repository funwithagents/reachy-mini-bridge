# Side-effect-free readiness probe, and a real daemon launcher with the macOS camera check

**Status:** Done

Implements [specs/daemon/daemon.md](../specs/daemon/daemon.md) ("Readiness means the backend is up", "The launch command") and the new [specs/daemon/real_daemon.md](../specs/daemon/real_daemon.md). Delivers the two bridge-side answers to "sometimes the real robot's camera never starts on macOS": the readiness probe stops making the daemon rebuild its media pipeline, and every hardware daemon the bridge spawns runs through a launcher that verifies which camera `avfvideosrc` opened and rebuilds until it is the robot's. It deliberately leaves the sim launcher's webcam source alone (open question 2 of the new spec) and files nothing upstream — the draft is [docs/upstream-macos-camera-device-index.md](../docs/upstream-macos-camera-device-index.md).

## Scope

- `src/reachy_mini_bridge/daemon.py` — `is_daemon_ready` reads `GET /api/daemon/status` over plain HTTP (`status_url` builds the URL, IPv6 hosts bracketed) instead of building an SDK client with `media_backend="no_media"`, which upstream 1.10 / 1.11 turns into `release_media()` + `acquire_media()` on the daemon; the `real` recipe of `launch_command` becomes `<this interpreter> -m reachy_mini_bridge.real_daemon [--kinematics-engine Placo] --[no-]preload-datasets`, with no launcher-on-`PATH` check (`reachy_mini` is a base dependency); `robot.py` is no longer imported here
- `src/reachy_mini_bridge/real_daemon.py` — new: `run_real_daemon` (argv rewrite, install, upstream `main()`), `install_macos_camera_check` (wraps `GstMediaServer.start`, idempotent), `select_camera` (the pure decision), `is_robot_camera`, and the GStreamer glue that reads `avfvideosrc`'s `device-name`
- `tests/test_daemon.py` — the real recipe's new argv; the probe against a scripted `GET /api/daemon/status` (ready, backend `null`, not JSON, 404, closed port); `status_url`
- `tests/test_real_daemon.py` — new: the selection with scripted names (kept, round-robin rebuilds with the log lines, giving up, an unnamed device), the media-server wiring with a stand-in server, the idempotent install, the argv rewrite
- `specs/daemon/daemon.md`, `specs/daemon/real_daemon.md`, `specs/_index.md`, `AGENTS.md` (project map, e2e table), `docs/reachy-mini-api.md`, `docs/running-the-sim-daemon.md`, `docs/testing-with-the-bridge.md`, `specs/testing/testing.md` — the design and the reference notes
- `docs/upstream-macos-camera-device-index.md` — the upstream issue draft with the measurements

## Steps

1. Replace the probe's body: `urllib.request.urlopen(status_url(host, port), timeout=3)`, `json.load`, `backend_status is not None`; every exception is "not ready". Drop the `build_robot` import.
2. Point the `real` recipe at the new module; keep `--kinematics-engine Placo` as a passthrough flag; remove the `reachy-mini-daemon` `PATH` check for `real`.
3. Write `real_daemon.py`: the decision first (`select_camera`, no GStreamer), then the glue (`_avfvideosrc`, `_opened_device_name`, `_video_device_count`, `_gst_null`, `_checked_start`), then `install_macos_camera_check` and the launcher.
4. Tests as scoped above; update the daemon tests' real-recipe expectations.
5. Specs, index rows, project map, docs, the upstream draft.

## Verification

`uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest` all pass (411 tests). The live check on a Reachy Mini Lite over USB (`REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e -rs`, `test_camera_frame_delivers_a_frame`) is the on-robot confirmation that the launched daemon's camera is the robot's; run it when the robot is at hand.
