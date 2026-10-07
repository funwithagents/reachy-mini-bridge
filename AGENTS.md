# Agent instructions

Start with [specs/_overview.md](specs/_overview.md) for the global view of the project (architecture, the layers, the backends), then [specs/_index.md](specs/_index.md) for the index of the specs and their status — before making design decisions or writing code — it lists each spec and whether it's still open ("Draft"/"Not started"), design-validated ("Stable"), or built ("Implemented"). For what's been (or is being) built, see [plans/_index.md](plans/_index.md), which lists each implementation plan and its status ("Todo"/"In progress"/"Done").

## Project map

Where things live. This is a coarse, module-level map — for the full file inventory use `git ls-files`; for design detail follow the spec links.

### Top-level layout

| Path | What's there |
|---|---|
| `.github/workflows/` | The CI workflow — the static gate and both test tiers, three jobs side by side on GitHub's hosted Linux runners on every pull request and push to `main`; spec: [specs/testing/ci.md](specs/testing/ci.md) |
| `README.md` | The landing page — what the bridge is, its support status, install, the fake quick start, the control panel, and the links into `docs/`; its runnable quick start is executed by `tests/test_docs_examples.py` |
| `CONTRIBUTING.md` | How the project is run today — solo development, feedback through issues, no external pull requests yet |
| `config.example.json` | Every `ReachyMiniConfig` field with placeholder values — kept in sync with [specs/core/config.md](specs/core/config.md) |
| `src/reachy_mini_bridge/` | The library itself — one module per core concept (see below) |
| `specs/` | Pre-implementation design docs, one per concept, each with a `**Status:**` — indexed by [specs/_index.md](specs/_index.md). Grouped in folders named after the subsystem they specify (`core/`, `motion/`, `vision/`, `audio/`, `daemon/`, `testing/`, `examples/`), the same words as the config blocks; the repo-wide `project.md` sits at the root next to the overview and index. Basenames stay unique across folders |
| `plans/` | Implementation plans turning settled specs into buildable steps — indexed by [plans/_index.md](plans/_index.md) |
| `tests/` | Fast, deterministic, no-network tests; mirrors the `src/reachy_mini_bridge/` module structure |
| `tests-e2e/` | Opt-in live tests that call real external services (not collected by default `pytest`) |
| `docs/` | The consumer documentation, entered through [docs/index.md](docs/index.md): [getting-started.md](docs/getting-started.md); `reference/` — the API ([api.md](docs/reference/api.md): verbs, values, errors, lifecycle, cancellation and concurrency, units, extension contracts), the configuration ([configuration.md](docs/reference/configuration.md): every field, kept complete by `tests/test_docs_consistency.py`) and the one backends/capabilities matrix ([backends-and-capabilities.md](docs/reference/backends-and-capabilities.md)); `guides/` — task guides (audio, perception and tracking, a custom detector, a custom idle move, testing, running daemons, Linux, troubleshooting); `internals/` — [upstream-sdk-notes.md](docs/internals/upstream-sdk-notes.md), what we learned about the upstream `reachy_mini` SDK, dated and versioned, never a bridge API promise. One home per fact: a reference owns it, every other page links there; the specs stay the normative design |
| `examples/` | Runnable examples, not part of the package — `greeter/`, a demo that breathes until a face shows up, greets it with an emotion and speech while the head follows it, and says goodbye when it leaves, with its own configs carrying a voice (`uv run python -m examples.greeter [--config <file>]`); `control_panel/`, a Gradio control panel over `ReachyMiniBridge` (`demo` dependency group; `uv run python -m examples.control_panel --config <file>`; spec: [specs/examples/control_panel.md](specs/examples/control_panel.md)); `configs/`, one minimal `ReachyMiniConfig` profile per setup (fake, Lite over USB, sim with the rendered camera, sim with a webcam, wireless) with a README of what each needs and gives — parsed by `tests/test_docs_consistency.py` |

### `src/reachy_mini_bridge/` modules

<!-- One row per concept module. Keep this in sync with the code (a test enforces it). -->

| Module | Role | Spec |
|---|---|---|
| `src/reachy_mini_bridge/robot.py` | Connection seam to upstream `reachy_mini` — `AnyReachyMini` union alias + `build_robot` backend factory | [specs/core/robot.md](specs/core/robot.md) |
| `src/reachy_mini_bridge/fake_reachy_mini.py` | First-party `FakeReachyMini` stand-in (imports no `reachy_mini`) — records commands, returns synthetic perception/audio | [specs/core/robot.md](specs/core/robot.md) |
| `src/reachy_mini_bridge/bridge.py` | `ReachyMiniBridge` high-level interaction verbs (motors, expression, gaze, audio) | [specs/core/bridge.md](specs/core/bridge.md) |
| `src/reachy_mini_bridge/audio.py` | Audio & media session — `say` via a pluggable `SpeechSynthesizer` to the robot speaker + echo-cancelled mic stream for the caller's ASR + conversion helpers | [specs/audio/audio.md](specs/audio/audio.md) |
| `src/reachy_mini_bridge/motion.py` | Motion loop — `MotionSession`, the one thread that owns `set_target`: plays emotions (with their sound) as exclusive primaries over the idle move (breathing / neutral hold / a caller's custom `IdleMove` / nothing), every transition a blend; the gaze layer composing the head tracker's aim into the idle move; the `presence` switch and the `idle` mode | [specs/motion/motion.md](specs/motion/motion.md) |
| `src/reachy_mini_bridge/camera.py` | Camera feed — `CameraFeed` / `CameraFrame`, the one reader of the robot's camera (`bridge.camera`): a thread over upstream's one-shot `get_frame()` publishing the newest frame with its time and the head pose at that time, sampled by every consumer of frames — the `custom` detection source, a display, a vision graph plugged on by shape | [specs/vision/camera.md](specs/vision/camera.md) |
| `src/reachy_mini_bridge/face_detection.py` | User perception — the detection loop running one `FaceDetector` over the camera feed's frames (the shipped `yunet`, or a developer's registered through `custom`; none by default — detection is opt-in) at most `target_fps` a second, every face carried by a `track_id` from frame to frame, and the `Face` / `FaceReport` value published on `bridge.faces` (boxes, head angles, the frame they were found in); the detector's cost logged once per run | [specs/vision/user_perception.md](specs/vision/user_perception.md) |
| `src/reachy_mini_bridge/yunet.py` | The shipped face detector — upstream's YuNet model (`reachy_mini.vision.face_detector`, imported lazily; no new dependency) wrapped as a bridge `FaceDetector`, detecting on a 320 px wide subsample by default (`face_detection.width`); `face_detection.detector: "yunet"` | [specs/vision/user_perception.md](specs/vision/user_perception.md) |
| `src/reachy_mini_bridge/head_tracking.py` | Head tracking — the bridge's own tracker choosing whom to follow by `track_id` (the biggest face, held while seen, a vanished face waited for before switching) and turning that face into the aim the motion loop's gaze layer composes (a bridge camera model, the head pose at the frame's time, a fixed webcam, the loss timeout); its state published as `bridge.head_tracking` (`HeadTrackingReport`) | [specs/motion/head_tracking.md](specs/motion/head_tracking.md) |
| `src/reachy_mini_bridge/observable.py` | `Observable[T]` — a value read directly and subscribed to (`changes()` async iterator, `wait_for`, `set` publishes / `update` is silent), the bridge's one event mechanism | [specs/core/observable.md](specs/core/observable.md) |
| `src/reachy_mini_bridge/concurrency.py` | `owned(work)` — a coroutine run as a task of the bridge's own, awaited through any cancel of the caller and reporting it; what the session teardown, the bring-up unwind and the detection loop's stop are owned with | [specs/core/bridge.md](specs/core/bridge.md) |
| `src/reachy_mini_bridge/config.py` | `ReachyMiniConfig` (+ `DaemonConfig`, `AudioSettings`) — the declarative bridge config with `from_dict` / `from_json` / `from_json_file` | [specs/core/config.md](specs/core/config.md) |
| `src/reachy_mini_bridge/daemon.py` | Bridge-owned `reachy-mini-daemon` lifecycle — `start_daemon` + `DaemonHandle.stop()` (own-it-or-borrow-it; `managed_daemon` the context-manager form), `is_daemon_ready`, `launch_command`, `scrubbed_env` | [specs/daemon/daemon.md](specs/daemon/daemon.md) |
| `src/reachy_mini_bridge/real_daemon.py` | The launcher every bridge-spawned hardware daemon runs through (`python -m reachy_mini_bridge.real_daemon`) — upstream's `reachy-mini-daemon` in-process with the macOS camera check: which device `avfvideosrc` opened is read back and the media pipeline rebuilt on the next index until it is the robot's camera | [specs/daemon/real_daemon.md](specs/daemon/real_daemon.md) |
| `src/reachy_mini_bridge/sim_daemon.py` | The launcher every bridge-spawned MuJoCo daemon runs through (`python -m reachy_mini_bridge.sim_daemon`) — upstream's daemon with the `sim` / `webcam` camera source (`WebcamRelay`) and the `--sim-display` wiring of the sim displays; `SimDaemonExtension` hooks | [specs/daemon/sim_daemon.md](specs/daemon/sim_daemon.md) |
| `src/reachy_mini_bridge/sim_displays.py` | The sim displays — what the MuJoCo viewer shows besides the scene, one `--sim-display` / `daemon.sim_displays` switch each: `camera_overlay` (`ViewerOverlay`, the camera stream as a picture in the view's corner), `robot_gaze` (the eye camera's optical axis as a line) and `face_markers` (an ellipsoid per detected face where the bridge places it — `face_marker`, pushed by the bridge's `FaceMarkerPublisher` to `/api/sim/displays/face_markers`), the last two drawn through `SceneLayer` on the viewer's `user_scn`, never seen by the eye camera | [specs/daemon/sim_displays.md](specs/daemon/sim_displays.md) |
| `src/reachy_mini_bridge/errors.py` | Bridge exception hierarchy — `BridgeError` base + `MotorsNotEnabledError`, `GravityCompensationUnsupportedError`, `SpeechInterruptedError`, `SoundInterruptedError`, `DaemonError`; `ConfigError(ValueError)` | [specs/core/bridge.md](specs/core/bridge.md), [specs/core/config.md](specs/core/config.md), [specs/daemon/daemon.md](specs/daemon/daemon.md) |
| `src/reachy_mini_bridge/testing/` | Shipped testing harness (package) — the `live_bridge`, `sim_scene`, `face_scene` and `emotions_library` fixtures, `requires_caps`, `require_env` for consumers' e2e tests (`fixtures.py` plugin, private `_daemon.py` wrapping `daemon.py`, `support.py`); `sim_scene.py` — the bridge's test scene: a pool of hidden portraits (`face_1` … `face_3`, `assets/face.png`) a test spawns/moves/despawns — as many at once as it needs — through a `python -m reachy_mini_bridge.testing.sim_scene` launcher (`SceneDirector` + `/api/sim/inject` router) and `SimSceneClient`; `gaze.py` — the convergence kit of the head-tracking live tests (`track_onto` / `assert_tracked` / `arm_tracking`, the measured thresholds), importable by consumers | [specs/testing/testing_support.md](specs/testing/testing_support.md), [specs/testing/sim_scene.md](specs/testing/sim_scene.md) |

**Keep this map current:** when you add, rename, or remove a top-level `src/reachy_mini_bridge/` module or a root directory, update the map in the same change — same discipline as keeping spec/plan statuses honest (below). A test (`tests/test_project_map.py`) enforces that every `src/reachy_mini_bridge/*.py` module appears here and vice-versa — and that the spec frontmatter (see below) stays honest too. `tests/test_docs_consistency.py` guards the documentation the same way: every local Markdown link resolves (plans excepted), every spec's and plan's status matches its index row, `config.example.json` and the configuration reference name every bridge-owned config field, and every profile under `examples/configs/` parses.

## Keeping statuses current

Specs and plans both carry a status, and you are responsible for keeping it honest as work progresses — update it in the same change that does the work, not as an afterthought:

- **Spec status** (`**Status:**` line near the top of each spec, and the Status column in [specs/_index.md](specs/_index.md)) tracks *design maturity* and *whether the code reflects the spec*, as a lifecycle: `Not started` → `Draft` (open questions remain) → `Stable` (design settled, reviewed and validated — open questions are deferrals only — but **not necessarily implemented yet**) → `Implemented` (a `Done` plan has built it and the code matches the spec). Keep the `**Status:**` line and the index row in sync.
  - **`Stable` is the design-review gate, not an implementation claim.** Promote `Draft` → `Stable` once the core design is settled and its remaining open questions are genuine deferrals (not load-bearing unknowns) — this is where the design is validated *before* code is written. No implementation is required to be `Stable`.
  - **`Implemented` means code matches.** Promote `Stable` → `Implemented` only once a plan implementing it is `Done` (lint, type check, tests all pass — see Verification). This is the one transition that asserts design and code are in sync.
  - **When you edit an `Implemented` spec in a way that requires new code, set its status to `Updated` in the same change.** `Updated` means the design is settled but the existing implementation now lags it — a stronger warning than `Stable`, because there is stale code to fix, not just code to write. Then write a new implementation plan for the gap (see below) and, once that plan is `Done`, flip the spec back to `Implemented`. This `Implemented → Updated → Implemented` loop keeps a spec's status an honest signal of whether the code actually matches it — never leave a re-designed spec sitting at `Implemented`.
  - A purely editorial edit to a `Stable` or `Implemented` spec (typos, clarifications, reordering — nothing that changes what the code should do) keeps its status; it does **not** need `Updated`.
- **Plan status** (`**Status:**` line near the top of each plan, and the Status column in [plans/_index.md](plans/_index.md)) tracks *implementation progress*: `Todo` → `In progress` → `Done`. Mark a plan `Done` only once it's implemented and verified (lint, type check, tests all pass — see Verification). Keep the `**Status:**` line and the index row in sync.
- Whenever you add a spec or plan, add its row to the relevant `_index.md`; whenever you change a status, change it in both the file and the index.

## Spec frontmatter

Every spec opens with a YAML frontmatter block naming the code and tests it governs:

```
---
code:
  - src/reachy_mini_bridge/<module>.py
tests:
  - tests/test_<module>.py
---
```

This is the **spec → code/tests** mapping — the inverse of the module → spec column in the Project map above. Its job is to give the **spec-drift checks** an explicit, version-controlled scope: the exact files to diff a spec against, so a checker never has to guess which code implements a given spec. `code:` names the implementation the spec specifies; `tests:` names the tests that pin its behavior (may be empty/absent).

The mapping is **many-to-many**: a file can be governed by several specs, so the same path legitimately appears in more than one spec's frontmatter.

**Keep it current** (same discipline as statuses): when you move, rename, or delete a file a spec governs — or add a new `src/reachy_mini_bridge/` module — update the affected spec's `code:`/`tests:` in the same change. `tests/test_project_map.py` enforces three invariants: every listed path exists, every spec declares a non-empty `code:` list, and every concept module in `src/reachy_mini_bridge/` is named by at least one spec (`__init__.py` is exempt as package glue).

## Testing

- Write functional tests: exercise what a feature/function actually does (inputs → outputs, state changes, side effects), not just that it runs or matches its signature.
- Avoid trivial/tautological tests — e.g. asserting a constant, asserting an object is not `None`, asserting a mock was called. If a test would pass for a broken implementation, it's not worth writing.
- Prefer driving the public API the way a real caller would over asserting on internals.
- Every async verb whose effect spans time (`say`, `play_emotion`, the mic stream, session bring-up) is fully cancellable — [specs/core/bridge.md](specs/core/bridge.md) "Cancellation" defines what that means. When you add or change one, add a test on the `fake` that cancels it mid-flight and asserts the effect stopped and the session still works; the fake keeps real timing for these verbs precisely so there is a mid-flight to cancel in.
- The fast tier runs in parallel by default (`-n logical --maxprocesses 8` in `addopts`; pytest-xdist in the dev group) because its tests wait through the loop's real blends and breaths — about three minutes of sleeping that spread over eight workers takes about half a minute. `uv run pytest -n 0` runs it serially, the thing to try when a timing assertion looks flaky. `tests-e2e/` stays serial — its conftest forces the worker count to zero, its modules share one daemon.

### Live/e2e tests

Some tests call real external services over the network. They live in `tests-e2e/`, a directory separate from `tests/`, so the default `uv run pytest` never runs them — no network access or credentials are needed for the normal dev loop. Run them explicitly, and only when you actually want to verify against a live service. Tests that lack their required credentials should **skip**, not fail, so the tier is safe to run with only the keys you happen to have.

### Running the live e2e tests

The `live_bridge` fixture brings up the daemon itself — **don't start one by hand**. It borrows a daemon already ready at the address (and leaves it running), otherwise spawns one for the whole run and stops it at the end; each test file gets a bridge session of its own over it. The tier is one file per subject, each sharing one capability gate (`test_motors`, `test_audio`, `test_motion`, `test_perception`, `test_head_tracking`, `test_sim_displays`, `test_custom_faces`), so a headless run skips the camera files whole and `-rs` reads by subject:

| Target | Command | What the harness does |
|---|---|---|
| sim, headless (default) | `uv run pytest tests-e2e -rs` | spawns the bridge's sim daemon headless (`python -m reachy_mini_bridge.sim_daemon --headless`; the `sim` extra, in the dev group). On Linux the eye camera renders offscreen through EGL, so the camera and face tests run — what CI does ([specs/testing/ci.md](specs/testing/ci.md)); on macOS a headless sim has no camera and they skip |
| sim, viewer | `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` | spawns the sim daemon with the MuJoCo viewer via `mjpython` — needs an unlocked GUI session; adds the camera, so the attention / gaze tests run: a portrait spawned from the test scene's pool is moved, and the head must turn onto it and follow it, hand back to breathing when it leaves, and re-engage ([specs/testing/sim_scene.md](specs/testing/sim_scene.md)); the viewer also draws the face markers the bridge sends (`sim_displays.face_markers`), which the marker tests read back ([specs/daemon/sim_displays.md](specs/daemon/sim_displays.md)) |
| real robot on USB (Lite) | `REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e -rs` | spawns the bridge's real daemon launcher (`python -m reachy_mini_bridge.real_daemon`: upstream's hardware daemon with the macOS camera check, serial port auto-detected); the robot wakes up, moves and plays sound, and goes to sleep when the daemon stops |
| real robot on the network (wireless) | `REACHY_MINI_E2E_TARGET=real REACHY_MINI_HOST=<robot ip> uv run pytest tests-e2e -rs` | borrows the robot's own daemon, or skips — it never spawns on a remote host; the bridge connects with upstream's default media selection (the robot's WebRTC stream) rather than the local IPC path |

- **Read the skips.** `-rs` prints why each test skipped. A skip means a capability was probed absent (`motion`, `audio`, `camera`, `gravity_compensation`, `faces`, `face_markers`) or a credential is missing — it is not a pass; report it as such. `REACHY_MINI_E2E_REQUIRED_CAPS=motion,audio,camera,faces` turns those four from skips into failures (a daemon that cannot come up fails too); CI sets it, a local run may.
- **Capabilities are probed**, not inferred from the target: a macOS headless sim has no camera (a Linux one renders it offscreen); `gravity_compensation` needs hardware on the Placo kinematics engine (`reachy-mini[placo_kinematics]` installed — a harness-spawned real daemon then uses it automatically).
- **Credentials:** none needed for the real-TTS test — it runs on the local pocket-tts model (first run downloads the weights into the Hugging Face cache); `ELEVENLABS_API_KEY` and `GRADIUM_API_KEY` enable the ElevenLabs and Gradium cloud tests. The providers come from the `tts` dependency group, default in a plain `uv sync`; a sync without it (`--no-group tts`) makes the three provider tests skip on the missing module.
- **The detector is the bridge's:** `live_bridge` configures the shipped `yunet` detector (upstream's model, run by the bridge on the camera feed) with detection and tracking on — the defaults run no detector — so the tracking tests find the portrait in the rendered camera; the model downloads into the Hugging Face cache on the first live run. `tests-e2e/test_custom_faces.py` registers the same class through the `custom` path, in a bridge session of its own (the detector is config-only), and runs whenever the viewer sim runs.
- **macOS permissions:** the process running the tests needs camera and microphone access; without it the camera probe finds no frame and the camera test skips.
- **Every sim the harness spawns runs the bridge's test scene** (upstream's empty scene plus a pool of hidden portraits the tracking tests spawn), and **every sim runs through the bridge's launcher** ([specs/daemon/sim_daemon.md](specs/daemon/sim_daemon.md)), which adds the webcam camera source and the viewer overlay. Start `uv run python -m reachy_mini_bridge.sim_daemon` when you want one to borrow with those; the bridge's face detection works on upstream's `reachy-mini-daemon --sim` too.
- **Manual testing with a webcam** is not a pytest target: run a sim config with `"daemon": {"camera": {"source": "webcam"}}` (e.g. through the control panel) and step in front of the computer — the simulated robot sees and follows you.
- `REACHY_MINI_PORT` (default `8000`) moves the address. Details: [specs/testing/testing.md](specs/testing/testing.md) ("E2E targets & capabilities"), [docs/guides/testing.md](docs/guides/testing.md), [specs/daemon/daemon.md](specs/daemon/daemon.md) (launch recipes).

## Implementation plans

- Write implementation plans as files in the [plans](plans/) folder.
- Name each file `YYYYMMDDHHmm_plan-title.md`: a compact date-time prefix, then an underscore, then a kebab-case title (words separated by `-`).
  - Example: `202607201830_world-registry-refactor.md`
- Give each plan a `**Status:**` line just under its title (`Todo`/`In progress`/`Done`) and add a row for it to [plans/_index.md](plans/_index.md). Keep both current as work progresses (see "Keeping statuses current" above).
- Start from [plans/_plan-template.md](plans/_plan-template.md).

## Verification

After any code change, run linting, type checking, and tests, and fix any failures before considering the work done. CI runs the same gate — plus the live tier on a headless sim — on every pull request and push to `main` ([specs/testing/ci.md](specs/testing/ci.md)); read its live job's skips as you would a local run's: the spec's expected-skips table says which skips are by design, any other is a regression.

## Commands

```
uv sync --dev
uv run ruff check .
uv run ruff format src tests tests-e2e examples   # not `.`: ruff reformats the Python blocks in Markdown
uv run pyright
uv run pytest
```
