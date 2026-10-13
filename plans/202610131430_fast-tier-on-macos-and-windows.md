# Fast tier on macOS and Windows

**Status:** In progress

Implements `specs/testing/ci.md` ("The runner", "The workflow"), `specs/daemon/daemon.md` ("The child runs in its own session", "Teardown stops what the bridge started") and `specs/project.md` ("Platforms", "Dependency groups"). It makes the fast tier a three-platform CI matrix (Linux, macOS, Windows), gives the spawned daemon a Windows process group and a graceful stop there, and keeps the `placo` extra out of a Windows dev sync. It leaves out a Windows live tier, a Windows webcam device recipe and any on-robot validation on Windows.

## Scope

- `.github/workflows/ci.yml` — `fast-tier` as a matrix over `ubuntu-24.04`, `macos-15`, `windows-2025`, `fail-fast: false`, the apt step on Linux only; the header comment
- `pyproject.toml`, `uv.lock` — the `dev` group's `reachy-mini-bridge[placo]` under `sys_platform != 'win32'`; relocked
- `src/reachy_mini_bridge/daemon.py` — `_spawn` with `CREATE_NEW_PROCESS_GROUP` on Windows; `_stop` asking to exit through `_ask_to_exit` (`SIGTERM`, or `CTRL_BREAK_EVENT` on Windows with `terminate()` as the fallback); `send_signal` on the `_Process` protocol
- `tests/test_daemon.py` — the stand-in process records the platform's exit request; a real child spawned through the seam and stopped by `_stop` runs its shutdown handler on every platform
- every `time.monotonic()` in `src/`, `examples/`, `tests/`, `tests-e2e/` → `time.perf_counter()` (`specs/vision/camera.md` "The bridge's clock", `specs/audio/microphone.md`, `specs/audio/audio.md`): on Windows before Python 3.13 `monotonic` ticks every 15.6 ms, which stepped the 60 Hz motion loop unevenly on the first Windows run; on macOS and Linux the two read the same clock
- `src/reachy_mini_bridge/testing/sim_scene.py` — `upstream_scene_name` refusing a scene on another drive than upstream's with the drive it must be on (`specs/testing/sim_scene.md`); a Windows-only test
- `tests/test_audio.py` — the sound-and-say test waits for both to start before the stop (a race the slower Windows runner lost); `tests/test_docs_examples.py` reads Markdown as UTF-8
- `README.md`, `docs/reference/backends-and-capabilities.md`, `docs/reference/api.md`, `AGENTS.md` — the platform statements, the clock

## Steps

1. Mark the specs `Updated` (and their index rows) with the design above.
2. Add the Windows marker on the dev group's `placo` entry; `uv lock`; confirm a Windows install of the export resolves from wheels alone (`uv pip install --dry-run --python-platform x86_64-pc-windows-msvc --only-binary :all:`).
3. Windows branches in `_spawn` and `_stop`; the tests.
4. The workflow matrix; dispatch it on the branch and fix what the macOS and Windows entries surface — the first run found the clock, the drive, the encoding and the race above, and a macOS runner too slow for the per-tick bounds at a worker per core (two workers there and on Windows).
5. Once every job is green, mark the specs `Implemented` and this plan `Done`.

## Verification

`uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` locally; the CI run on the branch green on all six jobs (`check`, three `fast-tier` entries, two `e2e-sim` entries), its `fast-tier (windows-2025)` log showing `test_stop_lets_the_child_shut_down_gracefully` passed rather than skipped.
