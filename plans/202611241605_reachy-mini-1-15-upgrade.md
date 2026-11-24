# Upgrade to reachy-mini 1.15.1

**Status:** In progress

Implements the 1.15 alignment of `specs/audio/audio.md` ("Stopping a sound file, per backend"), `specs/daemon/real_daemon.md` ("The camera check"), `specs/core/config.md` ("Kinematics engines"), `specs/daemon/daemon.md` ("The launch command", "The lifecycle"), `specs/testing/testing.md` and `specs/testing/testing_support.md`. The bridge moves from `reachy-mini>=1.11` (locked on 1.11.0) to `reachy-mini>=1.15.1`: the sound file stopped through upstream's new `SoundPlaybin`, the macOS camera check following upstream's reusing, bool-returning `GstMediaServer.start()`, the `nn` engine removed with upstream's, and upstream's anonymous usage reporting off for the daemons the testing harness spawns. It leaves out the upstream additions the bridge does not use (NFC, `MoveBusyError`, the app-server origin check) and the daemons a developer spawns outside the harness, which keep upstream's default.

## Scope

- `pyproject.toml`, `uv.lock` — `reachy-mini>=1.15.1`; the lock on 1.15.1 (with `certifi`, `pip`, `reachy-mini-motor-controller` as it resolves them)
- `src/reachy_mini_bridge/audio.py` — `_stop_sound_file` stops `media.audio._sound` (upstream's `SoundPlaybin`) instead of setting `_playbin` to `NULL`
- `src/reachy_mini_bridge/real_daemon.py` — the wrapped `start()` returns upstream's bool; a failed start is returned unchecked; a rebuild goes through `server.stop()` so upstream builds a new pipeline on the next index
- `src/reachy_mini_bridge/config.py` — `nn` out of `KINEMATICS_ENGINES` / `UPSTREAM_KINEMATICS_ENGINES`
- `src/reachy_mini_bridge/daemon.py` — `start_daemon` / `managed_daemon` take `env`, variables layered over `scrubbed_env()` for the child
- `src/reachy_mini_bridge/testing/_daemon.py` — the harness spawns with `REACHY_MINI_TELEMETRY=0`; `nn` out of its docstring
- `tests/test_audio.py`, `tests/test_robot.py` — the stop and the parity pin on `_sound`
- `tests/test_real_daemon.py` — the bool, the failed start, the rebuild through `stop()`
- `tests/test_config.py`, `tests/test_daemon.py`, `tests/test_testing_support.py` — `nn` refused; `env` reaching the child; the harness's telemetry switch
- `tests-e2e/test_motion.py` — the cancelled emotion's player read from `_sound.current`
- specs: `audio/audio.md`, `daemon/real_daemon.md`, `core/config.md`, `daemon/daemon.md`, `testing/testing.md`, `testing/testing_support.md` (status `Updated` until this plan is `Done`); `core/robot.md` (editorial: the installed version)
- docs: `docs/internals/upstream-sdk-notes.md` (the version paragraph, the stop and `no_media` notes, the engines), `docs/reference/configuration.md`, `docs/guides/testing.md`, `docs/guides/running-daemons.md` (usage reporting and the opt-out), `AGENTS.md` (the engine row)

## Steps

1. Bump the floor and the lock (`uv lock --upgrade-package reachy-mini`), `uv sync`.
2. `audio.py`: the local-backend branch of `_stop_sound_file` calls `audio._sound.stop()` — upstream's own stop for `play_sound`'s player, which leaves the shared pipeline alone; the wobbler reset through `clear_player()` stays (upstream's stop does not call its end-of-sound hook). Tests: the fake `SoundPlaybin` stopped; the parity pin asserts `play_sound` hands its playbin to `self._sound.play` and `SoundPlaybin` has `stop`.
3. `real_daemon.py`: `_checked_start` returns `bool`; `original_start` returning `False` returns `False` with no camera check (nothing runs to read); `restart(index)` calls `server.stop()` (upstream marks the pipeline for rebuild and releases the camera), moves `_cam_path`, calls the original start and records its result; the wrapper returns the last start's result. `install_macos_camera_check`'s `start` returns it. Tests updated to the bool and the `stop()` call; a failed first start and a failed rebuild.
4. `config.py` and the harness: `nn` removed; config, daemon and testing-support tests follow (`placo` takes `nn`'s place where a test needs a non-default engine).
5. `daemon.py`: `env` keyword on `start_daemon` and `managed_daemon`; `_daemon._spawned` passes `{"REACHY_MINI_TELEMETRY": "0"}`. Tests: the variable reaches `_spawn`; the harness passes it.
6. The e2e cancelled-emotion test reads `_sound.current`.
7. Specs and docs per Scope; statuses `Updated` in the same change, back to `Implemented` once this plan is `Done`.

## Verification

- `uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` — clean.
- Live tier on 1.15.1: headless sim (`uv run pytest tests-e2e -rs`), viewer sim (`REACHY_MINI_E2E_SIM_VIEWER=1`), and the Lite over USB on macOS (`REACHY_MINI_E2E_TARGET=real`) for the camera check's rebuild path; the daemon log read for the check's lines.
- Mark `Done` only after the Lite run.
