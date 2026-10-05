# The headless sim's camera on Linux — the eye camera rendered offscreen through EGL

**Status:** In progress

Implements [specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md) ("The headless camera") and the viewer-interpreter rule of [specs/daemon/daemon.md](../specs/daemon/daemon.md) ("The launch command"), which [specs/testing/testing.md](../specs/testing/testing.md) (the `camera` capability row, the headless target) and [specs/testing/ci.md](../specs/testing/ci.md) build on. It delivers:

- **a headless sim with frames on Linux**: the launcher's backend subclass starts upstream's render thread in headless mode wherever MuJoCo has a display-less GL backend — `MUJOCO_GL` defaulted to `egl` on Linux before `mujoco` is imported — so a CI runner's sim has a camera and the perception, head-tracking and custom-detector tests run there; macOS headless stays camera-less;
- **the viewer under the plain interpreter on Linux** (`mjpython` is macOS-only);
- **the `e2e-sim` job covering the camera tests**, the expected-skips table of `ci.md` losing its camera row, and the tracking thresholds measured on the runner;
- the docs saying so.

It builds on [202610051600_ci-on-github-runners-and-the-tts-group.md](202610051600_ci-on-github-runners-and-the-tts-group.md) being `Done` (a green `e2e-sim` without the camera is this plan's Step 0). It deliberately leaves out the viewer under Xvfb (the `face_markers` tests stay local — `ci.md` open question 2) and any change to what the daemon reports as camera intrinsics (`sim_daemon.md` open question 2).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md) "The backend subclass", "Camera sources", "The headless camera", "Testable without a daemon"; [specs/daemon/daemon.md](../specs/daemon/daemon.md) "The launch command"; [specs/testing/ci.md](../specs/testing/ci.md) "What the live job provides" and open question 1; `src/reachy_mini_bridge/sim_daemon.py` (`bridge_backend`, `run_sim_daemon`), `src/reachy_mini_bridge/daemon.py` (`launch_command`), upstream's `reachy_mini/daemon/backend/mujoco/backend.py` `run()` and `rendering_loop()` (the thread upstream starts only under the viewer, and what it needs started: the model built, the renderer created in the thread itself); `tests/test_sim_daemon.py`, `tests/test_daemon.py`; `src/reachy_mini_bridge/testing/gaze.py` (the thresholds and the measurements in their docstrings).
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

- **The runner is the proof.** Steps 1 and 2 are pinned by the fast tier on the Mac (the platform behind a seam); only the runner has Mesa, so Step 3 iterates on a branch and pull request as the first plan did. The Mac's own live tiers must stay as they are: headless still camera-less and green, the viewer tier green.
- **Write specs affirmatively**; keep statuses in sync at each flip. **Do not commit** unless asked; ask before the first push.

## Scope

- `src/reachy_mini_bridge/sim_daemon.py` — `MUJOCO_GL` defaulted on Linux for a headless `sim`-camera run, before the upstream import; the backend subclass starting the render thread headless on a display-less backend; the renderer failure logged, the run continuing. `tests/test_sim_daemon.py` — the four cases of the spec's "The headless camera" bullet.
- `src/reachy_mini_bridge/daemon.py` — the viewer interpreter per platform. `tests/test_daemon.py` — the viewer and scene-file recipes on Darwin and on Linux.
- `src/reachy_mini_bridge/testing/gaze.py` — thresholds re-measured on the runner, loosened only where the measurement says so, the docstrings carrying both numbers.
- `.github/workflows/ci.yml` — nothing new if the first plan installed Mesa; else `libegl1 libgl1-mesa-dri libosmesa6` in the live jobs.
- `specs/testing/ci.md` — the expected-skips table loses its camera row and the "until that launcher behaviour is built" sentence; open question 1 answered or kept with the measurement. `specs/daemon/sim_daemon.md`, `specs/daemon/daemon.md`, `specs/testing/testing.md`, `specs/_index.md` — status to `Implemented` at the end.
- `docs/running-the-sim-daemon.md` ("Headless sim" — the camera on Linux; the viewer command on Linux), `docs/testing-with-the-bridge.md` (the capability table's `camera` row), `AGENTS.md` ("Running the live e2e tests": the headless row gains the camera on Linux), `README.md` (the `headless` and `source` rows of the config table).
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

The first plan `Done`: `check` and `e2e-sim` green on `main`, the `e2e-sim` log listing the `camera` / `faces` tests among the skips. Locally, check command green and both sim tiers green on the Mac.

### Step 1 — The launcher: the render thread headless on a display-less backend

**Files:** `src/reachy_mini_bridge/sim_daemon.py`, `tests/test_sim_daemon.py`.

- A module-level `offscreen_gl_backend(platform, environ) -> str | None` (pure): `"egl"` when `platform == "linux"` and `MUJOCO_GL` is unset; the environment's own value when set; `None` on any other platform. `run_sim_daemon` calls it for a headless run with the `sim` camera, before the `reachy_mini.daemon` import (which imports `mujoco`), and sets `MUJOCO_GL` in `os.environ` when it returns a backend for an unset variable; it passes `headless_render=<backend is not None>` into `bridge_backend`.
- `bridge_backend(..., headless_render: bool = False)`: in `run()`, when `headless_render` and the camera source is `sim` and the backend is headless, start `Thread(target=self._headless_rendering, daemon=True)` before `super().run()` and join it after — `_headless_rendering` wraps upstream's `rendering_loop(CAMERA_REACHY, 5005)` in a `try` that logs one `ERROR` ("offscreen camera render failed on MUJOCO_GL=<backend>: <error> — is Mesa EGL installed?") and returns, so the daemon runs on without a camera. The thread starts at the top of `run()`: upstream's render loop only reads `self.data`, and a frame of the sleep pose before the first step is a valid frame. Step 3 confirms the first frames on the runner; if they are not sane, the thread's start waits on upstream's `ready` event instead.
- The renderer tap for the camera overlay (`_get_renderer`) is viewer-only and untouched: the overlay is refused headless by the parser already.
- Tests (`tests/test_sim_daemon.py`): `offscreen_gl_backend` for the four cases; `run_sim_daemon` on a fake upstream (the existing seams) sets `MUJOCO_GL=egl` for `--headless` on a `linux` platform and leaves `os.environ` alone on `darwin`, with `--camera webcam`, under the viewer, and when the variable is already set (`monkeypatch` both); the subclass started the render thread in the headless-render case (a recorded `rendering_loop` on the fake backend) and not otherwise; a `rendering_loop` that raises is logged once (`caplog`) and `run()` returns normally.

### Step 2 — The viewer interpreter per platform

**Files:** `src/reachy_mini_bridge/daemon.py`, `tests/test_daemon.py`.

- `launch_command`: the viewer recipes use `shutil.which("mjpython")` on Darwin (the existing `DaemonError` when absent) and `sys.executable` elsewhere; the viewer hint in the startup-timeout `DaemonError` stays (a GUI session is needed on every platform). The platform is read through a parameter with a `sys.platform` default, so the tests pin both.
- Tests: the viewer and scene-file recipes on `darwin` start with `mjpython`, on `linux` with the interpreter; `mjpython` absent is an error on Darwin only.

### Step 3 — The runner: frames, the tests, the thresholds

**Where:** a branch and a pull request; the Actions log.

- Push. In the `e2e-sim` log: the launcher's "sim daemon: camera sim" line, no offscreen-render error, `camera` probed present, the `faces` tests and the head-tracking tests running. Iterate on: the Mesa packages (the first plan installed them; else add), the EGL device (`EGL_PLATFORM=surfaceless` if Mesa picks a device that is not there), the camera probe's 5 s against llvmpipe's first frame at 1280×720.
- The thresholds: read the measurements the gaze kit prints (`track_onto` prints its settle time and overshoot); compare with the Mac numbers in the docstrings; loosen a constant only when the runner's measurement crosses it, and write the runner's number next to the Mac's. A test that is flaky across three runs for a timing reason is a threshold to measure, not a retry to add.
- Done when `e2e-sim` is green three runs in a row with the expected skips now `gravity_compensation`, `face_markers` and the three provider tests only, and the measurements below are filled.

### Step 4 — Docs, the spec's table, statuses

- `specs/testing/ci.md`: the camera row leaves the expected-skips table; the "Until that launcher behaviour is built" sentence goes; open question 1 restated with the runner's measurements or closed. `docs/running-the-sim-daemon.md`, `docs/testing-with-the-bridge.md`, `AGENTS.md`, `README.md` as in Scope.
- `specs/daemon/sim_daemon.md`, `specs/daemon/daemon.md`, `specs/testing/testing.md` → `Implemented`; `specs/_index.md` rows in sync. This plan `Done` here and in [_index.md](_index.md).

## Verification

- Check command green; `tests/test_sim_daemon.py` and `tests/test_daemon.py` pin the platform cases above.
- Locally on the Mac: `uv run pytest tests-e2e -rs` headless unchanged (camera skipped, green); `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` green (the viewer path untouched).
- On GitHub: `e2e-sim` green three times with the camera tests running and the reduced skip set; the `-rs` list matches `ci.md`'s table.

## Linux shakeout

What the runner found, in order — one line per change: the symptom, the fix, the spec it touches.

- **The render thread ran, no frame reached the bridge.** With `MUJOCO_GL=egl` the launcher's thread started and upstream's UDP sender logged itself, yet `camera` probed absent: upstream's media server — the piece that serves the stream to clients — failed to initialise on Linux, `Failed to create webrtcsink element`. The Rust GStreamer plugins are in no Ubuntu archive (24.04 and 26.04 both tried); upstream's own CI installs a prebuilt `libgstrswebrtc.so` from Pollen's public desktop-app repository through a composite action pinned to a commit and a sha256, and needs `gstreamer1.0-nice` / `libnice10`. The live job reuses that action, pinned. Spec: `ci.md` "The runner" (system packages) and "What the live job provides".
- **A camera at four frames a second.** With frames flowing, the rate floors of the perception and custom-detector tests (5 reports a second) failed on some runs and the head-tracking assertions on others (an overshoot of 15 to 26°, the head oscillating, a phantom face after a release). Measured on the runner under EGL: upstream's full render 140 to 230 ms a frame against 14 ms on a Mac — the shadow map of a 4096-texel light more than half of it, the robot's 349 000 triangles about 40 ms of the rest; the resolution irrelevant (640×360 cost the same); YuNet 1.7 ms; llvmpipe's thread count irrelevant. The headless render is now drawn for the detector: no shadows, reflections, skybox, haze, fog or multisampling, and the eye camera's kinematic tree hidden (alpha 0, which MuJoCo's scene builder skips before any vertex work) — about 10 ms a frame, upstream's rate. Every camera and tracking test then passed on the runner at the Mac's thresholds. Spec: `sim_daemon.md` "The headless camera" ("Drawn for the detector").
- **The cloud TTS keys as secrets.** Added to the repository and mapped onto the live job's test step; `require_env` treats an empty value as unset, so a fork's pull request skips the two tests. Spec: `ci.md` "Secrets and protection".
- **Pyright and `mujoco`.** The bindings carry no types: `mujoco.mj_name2id` is unknown to pyright. The trim helpers import the module as `Any` through `importlib`, as `sim_displays.py` and `sim_scene.py` do.

## Measurements

On the runner (four vCPUs, Mesa llvmpipe under EGL):

- Render of the test scene at 1280×720, upstream's settings: 140 to 230 ms a frame; without shadows 85; without multisampling 62; the robot's body left out as well: 10 to 12 ms. YuNet on a frame: 1.7 ms.
- The live tier with the trimmed render: 28 passed, 4 skipped (the two face-marker tests on `camera`-then-`face_markers`, `gravity_compensation`, the sim's motor-mode test), 208 s of pytest, the `e2e-sim` job about 270 s.
