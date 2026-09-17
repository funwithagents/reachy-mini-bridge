# Agent instructions

Start with [specs/_overview.md](specs/_overview.md) for the global view of the project (architecture, the three layers, the backends), then [specs/_index.md](specs/_index.md) for the index of the specs and their status — before making design decisions or writing code — it lists each spec and whether it's still open ("Draft"/"Not started"), design-validated ("Stable"), or built ("Implemented"). For what's been (or is being) built, see [plans/_index.md](plans/_index.md), which lists each implementation plan and its status ("Todo"/"In progress"/"Done").

## Project map

Where things live. This is a coarse, module-level map — for the full file inventory use `git ls-files`; for design detail follow the spec links.

### Top-level layout

| Path | What's there |
|---|---|
| `README.md` | Packaging front page — short intro + doc pointers |
| `config.example.json` | Every `ReachyMiniConfig` field with placeholder values — kept in sync with [specs/config.md](specs/config.md) |
| `src/reachy_mini_bridge/` | The library itself — one module per core concept (see below) |
| `specs/` | Pre-implementation design docs, one per concept, each with a `**Status:**` — indexed by [specs/_index.md](specs/_index.md) |
| `plans/` | Implementation plans turning settled specs into buildable steps — indexed by [plans/_index.md](plans/_index.md) |
| `tests/` | Fast, deterministic, no-network tests; mirrors the `src/reachy_mini_bridge/` module structure |
| `tests-e2e/` | Opt-in live tests that call real external services (not collected by default `pytest`) |
| `docs/` | Reference notes (not specs) — e.g. [docs/reachy-mini-api.md](docs/reachy-mini-api.md), what we learned about the upstream `reachy_mini` SDK |
| `examples/` | Runnable example apps, not part of the package — `control_panel/`, a Gradio control panel over `ReachyMiniApi` (`demo` dependency group; `uv run python -m examples.control_panel --config <file>`; spec: [specs/control_panel.md](specs/control_panel.md)) |

### `src/reachy_mini_bridge/` modules

<!-- One row per concept module. Keep this in sync with the code (a test enforces it). -->

| Module | Role | Spec |
|---|---|---|
| `src/reachy_mini_bridge/robot.py` | Connection seam to upstream `reachy_mini` — `AnyReachyMini` union alias + `build_robot` backend factory | [specs/robot.md](specs/robot.md) |
| `src/reachy_mini_bridge/fake_reachy_mini.py` | First-party `FakeReachyMini` stand-in (imports no `reachy_mini`) — records commands, returns synthetic perception/audio | [specs/robot.md](specs/robot.md) |
| `src/reachy_mini_bridge/api.py` | `ReachyMiniApi` high-level interaction verbs (motors, expression, gaze, audio) | [specs/api.md](specs/api.md) |
| `src/reachy_mini_bridge/audio.py` | Audio & media session — `say` via a pluggable `SpeechSynthesizer` to the robot speaker + echo-cancelled mic stream for the caller's ASR + conversion helpers | [specs/audio.md](specs/audio.md) |
| `src/reachy_mini_bridge/motion.py` | Motion loop — `MotionSession`, the one thread that owns `set_target`: plays emotions (with their sound) as exclusive primaries over the idle move (breathing / neutral hold / nothing), every transition a blend; the `presence` and `breathing` switches | [specs/motion.md](specs/motion.md) |
| `src/reachy_mini_bridge/config.py` | `ReachyMiniConfig` (+ `DaemonConfig`, `AudioSettings`) — the declarative api config with `from_dict` / `from_json` / `from_json_file` | [specs/config.md](specs/config.md) |
| `src/reachy_mini_bridge/daemon.py` | Bridge-owned `reachy-mini-daemon` lifecycle — `managed_daemon` (own-it-or-borrow-it), `is_daemon_ready`, `launch_command`, `scrubbed_env` | [specs/daemon.md](specs/daemon.md) |
| `src/reachy_mini_bridge/sim_daemon.py` | The launcher every bridge-spawned MuJoCo daemon runs through (`python -m reachy_mini_bridge.sim_daemon`) — upstream's daemon with the face-tracking corrections (tracking stepped each tick, true tracker intrinsics, fixed-camera aim for a webcam) and the `sim` / `webcam` camera source (`WebcamRelay`); `SimDaemonExtension` hooks | [specs/sim_daemon.md](specs/sim_daemon.md) |
| `src/reachy_mini_bridge/errors.py` | Bridge exception hierarchy — `BridgeError` base + `MotorsNotEnabledError`, `GravityCompensationUnsupportedError`, `DaemonError`; `ConfigError(ValueError)` | [specs/api.md](specs/api.md), [specs/config.md](specs/config.md), [specs/daemon.md](specs/daemon.md) |
| `src/reachy_mini_bridge/tools.py` | `ReachyMiniTools` agent/LLM tools (placeholder; Draft) | [specs/tools.md](specs/tools.md) |
| `src/reachy_mini_bridge/testing/` | Shipped testing harness (package) — `live_api` fixture, `requires_caps`, `require_env` for consumers' e2e tests (`fixtures.py` plugin, private `_daemon.py` wrapping `daemon.py`, `support.py`); `sim_scene.py` — the bridge's test scene: hidden-by-default scriptable props (a face today, `assets/face.png`) a test shows/moves/hides through a `python -m reachy_mini_bridge.testing.sim_scene` launcher (`SceneDirector` + `/api/sim-scene` router) and `SimSceneClient` | [specs/testing_support.md](specs/testing_support.md), [specs/sim_scene.md](specs/sim_scene.md) |

**Keep this map current:** when you add, rename, or remove a top-level `src/reachy_mini_bridge/` module or a root directory, update the map in the same change — same discipline as keeping spec/plan statuses honest (below). A test (`tests/test_project_map.py`) enforces that every `src/reachy_mini_bridge/*.py` module appears here and vice-versa — and that the spec frontmatter (see below) stays honest too.

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
- Every async verb whose effect spans time (`say`, `play_emotion`, the mic stream, session bring-up) is fully cancellable — [specs/api.md](specs/api.md) "Cancellation" defines what that means. When you add or change one, add a test on the `fake` that cancels it mid-flight and asserts the effect stopped and the session still works; the fake keeps real timing for these verbs precisely so there is a mid-flight to cancel in.

### Live/e2e tests

Some tests call real external services over the network. They live in `tests-e2e/`, a directory separate from `tests/`, so the default `uv run pytest` never runs them — no network access or credentials are needed for the normal dev loop. Run them explicitly, and only when you actually want to verify against a live service. Tests that lack their required credentials should **skip**, not fail, so the tier is safe to run with only the keys you happen to have.

### Running the live e2e tests

The `live_api` fixture brings up the daemon itself — **don't start one by hand**. It borrows a daemon already ready at the address (and leaves it running), otherwise spawns one and stops it at the end of the test module:

| Target | Command | What the harness does |
|---|---|---|
| sim, headless (default) | `uv run pytest tests-e2e -rs` | spawns the bridge's sim daemon headless (`python -m reachy_mini_bridge.sim_daemon --headless`; the `sim` extra, in the dev group) |
| sim, viewer | `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` | spawns the sim daemon with the MuJoCo viewer via `mjpython` — needs an unlocked GUI session; adds the camera, so the attention / gaze tests run: a hidden-by-default portrait in the test scene is shown and moved, and the head must turn onto it and follow it, hand back to breathing when it leaves, and re-engage ([specs/sim_scene.md](specs/sim_scene.md)) |
| real robot on USB (Lite) | `REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e -rs` | spawns `reachy-mini-daemon` (serial port auto-detected); the robot wakes up, moves and plays sound, and goes to sleep when the daemon stops |
| real robot on the network (wireless) | `REACHY_MINI_E2E_TARGET=real REACHY_MINI_HOST=<robot ip> uv run pytest tests-e2e -rs` | borrows the robot's own daemon, or skips — it never spawns on a remote host |

- **Read the skips.** `-rs` prints why each test skipped. A skip means a capability was probed absent (`motion`, `audio`, `camera`, `gravity_compensation`, `faces`) or a credential is missing — it is not a pass; report it as such.
- **Capabilities are probed**, not inferred from the target: the headless sim has no camera; `gravity_compensation` needs hardware on the Placo kinematics engine (`reachy-mini[placo_kinematics]` installed — a harness-spawned real daemon then uses it automatically).
- **Credentials:** `ELEVENLABS_API_KEY` enables the real-TTS test.
- **macOS permissions:** the process running the tests needs camera and microphone access; without it the camera probe finds no frame and the camera test skips.
- **Every sim the harness spawns runs the bridge's test scene** (upstream's empty scene plus a hidden portrait the tracking tests show), and **every sim runs through the bridge's launcher** ([specs/sim_daemon.md](specs/sim_daemon.md)), which corrects upstream's sim face tracking. A daemon started by hand with upstream's `reachy-mini-daemon --sim` lacks it: start `uv run python -m reachy_mini_bridge.sim_daemon` instead when you want one to borrow.
- **Manual testing with a webcam** is not a pytest target: run a sim config with `"daemon": {"camera": {"source": "webcam"}}` (e.g. through the control panel) and step in front of the computer — the simulated robot sees and follows you.
- `REACHY_MINI_PORT` (default `8000`) moves the address. Details: [specs/testing.md](specs/testing.md) ("E2E targets & capabilities"), [docs/testing-with-the-bridge.md](docs/testing-with-the-bridge.md), [specs/daemon.md](specs/daemon.md) (launch recipes).

## Implementation plans

- Write implementation plans as files in the [plans](plans/) folder.
- Name each file `YYYYMMDDHHmm_plan-title.md`: a compact date-time prefix, then an underscore, then a kebab-case title (words separated by `-`).
  - Example: `202607201830_world-registry-refactor.md`
- Give each plan a `**Status:**` line just under its title (`Todo`/`In progress`/`Done`) and add a row for it to [plans/_index.md](plans/_index.md). Keep both current as work progresses (see "Keeping statuses current" above).
- Start from [plans/_plan-template.md](plans/_plan-template.md).

## Verification

After any code change, run linting, type checking, and tests, and fix any failures before considering the work done.

## Commands

```
uv sync --dev
uv run ruff check .
uv run ruff format .
uv run pyright
uv run pytest
```
