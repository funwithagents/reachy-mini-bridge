# Documentation reconciliation after the detector migration — the README quick start, the testing guide's example, the stale spec sentences, the links

**Status:** Done

**Done (2026-10-01):** every step implemented and verified — `ruff check`, `ruff format`, `pyright`, the fast tier (605 tests, the two documentation smoke tests included). All three sibling repositories resolved as public GitHub URLs, so every link was replaced rather than unlinked. The `real` row of `testing_support.md`'s consumer table now names the `test` extra too (the harness is what that row installs). The remaining `get_camera_frame()` mentions in `camera.md`, `_overview.md` and `_index.md` state that the verb was retired, which is current. Departure from Step 5: the comfort-heading analysis stays in the untracked `analysis/` folder — `motion.md`'s open question on it is removed rather than relinked, and no copy lives under `docs/`. Departure from Step 1: the quick start carries no tracking snippet — one sentence points at the API section and the configuration reference, which already cover detection and tracking; a second block needing a camera-backed sim would have been the one quick-start block a reader cannot run as pasted. The relative-link check of Step 5 was run by hand (a shell loop over tracked Markdown outside `plans/`), not added to the repo.

Editorial: makes the specs, the README and the guides describe the code at `f2fed40` where they stopped doing so — findings 6 to 10 of [analysis/repo-spec-code-consistency-review-20260929.md](../analysis/repo-spec-code-consistency-review-20260929.md) (an untracked analysis note; the findings are restated here so the plan stands on its own). No design changes, so no spec changes status; the only code is two documentation smoke tests that execute the README's quick start and the testing guide's unit-test example on the `fake`, so neither can drift again. It deliberately leaves out the testing guide's **e2e** snippets: those change with the harness loop of [202610011455](202610011455_detection-loop-robustness-and-harness-event-loop.md), not here.

Do this plan first of the three: it makes the specs honest before the two code plans edit them.

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md) ("Keeping statuses current": a purely editorial edit keeps a spec's status); [specs/core/config.md](../specs/core/config.md) "`face_detection` block" and "`motion` block" (the defaults the text must match); [specs/project.md](../specs/project.md) (why the `sim` extra declares `mujoco` directly).
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

  (`ruff format .` would reflow the Python blocks in `plans/*.md` and the docs; format the code directories only.)
- **Write affirmatively**: every corrected sentence states the current behaviour, never the change or what it used to be.
- **Do not commit** unless asked.

## Scope

- `README.md` — "Quick start" (two snippets: the default config, then tracking with a detector), the "Gaze" row of the verbs table.
- `docs/testing-with-the-bridge.md` — "Unit tests" example.
- `specs/_overview.md` — "One writer for motion" (defaults), "Three backends" (`sim` extra).
- `specs/examples/control_panel.md` — the `demo` group's extras.
- `specs/testing/testing.md` — "Credentials" (the custom-detector test's gate), "`tests-e2e/` uses the `sim` or `real` backends" (`sim` extra).
- `specs/testing/testing_support.md` — the consumer tier table (`sim` extra).
- `specs/core/robot.md` — the `sim` extra.
- `specs/daemon/sim_daemon.md`, `specs/daemon/real_daemon.md` — the no-camera sentences.
- `specs/motion/motion.md` (open question 8), `specs/_overview.md`, `specs/audio/audio.md`, `specs/core/bridge.md`, `specs/core/config.md` — the links.
- `docs/comfort-heading-analysis.md` — new: the tracked home of the comfort-heading analysis.
- `tests/test_docs_examples.py` — new: the two documentation smoke tests.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Check command green on `main`.

### Step 1 — The README quick start runs on the default config (finding 9)

**File:** `README.md` "Quick start".

- The first snippet keeps `ReachyMiniBridge("fake")` and shows only what the default config supports: `set_motors_state("enabled")`, `list_emotions()`, `play_emotion("happy")`, `bridge.camera.latest()`. The `start_head_tracking()` / `stop_head_tracking()` lines leave it — with no detector named they raise `ValueError` today.
- A second snippet, "Following a face", builds the bridge from a dict naming the detector — `ReachyMiniBridge.from_dict({"backend": "sim", "face_detection": {"detector": "yunet"}})` — and calls `start_head_tracking()`; one sentence says it needs a camera (the viewer sim with a webcam, or the robot) and that the `fake` would need a registered `custom` stub ([docs/custom-face-detector.md](../docs/custom-face-detector.md)).
- The verbs table's "Gaze" row: tracking is **off** by default and needs a `face_detection.detector`, as the configuration reference three sections below already says. Grep the README for every other "on by default" / "default" claim about tracking and detection and align each with `config.md`.

### Step 2 — The testing guide's unit-test example asserts what the bridge does (finding 7)

**File:** `docs/testing-with-the-bridge.md` "Unit tests".

- `my_greeting(bridge)` stays the consumer's code, marked so by a comment and by a one-line body shown above the test (`await bridge.play_emotion("happy")`), so the example is runnable end to end.
- The assertion measures the emotion through `robot.targets` (the motion loop streams `set_target`; the fake records the stream there, and `async_play_move` never appears — `tests/test_bridge.py` pins both). Assert the way those tests do: a pose the move's trajectory reaches and breathing never does — not `len(robot.targets) > 0`, which the breathing idle loop satisfies on its own. Say in one sentence why `commands` is the wrong place to look for a move.

### Step 3 — The stale sentences (finding 6)

- `specs/_overview.md` "One writer for motion": `presence` and `wobbling` on by default, `tracking` off (it needs a `face_detection.detector`), the idle mode `"breathing"`.
- `specs/examples/control_panel.md`: the `demo` group is `["gradio>=5", "reachy-mini-bridge[sim,tts-pocket]"]`, as `pyproject.toml` has it.
- `specs/testing/testing.md` "Credentials": `tests-e2e/test_custom_faces.py` is not key-gated — it runs whenever the viewer sim runs (`REACHY_MINI_E2E_SIM_VIEWER=1`, its own `live_bridge_custom_faces` session), skipping where `faces` is not probed; the `REACHY_MINI_E2E_FACE_DETECTOR` variable does not exist.
- `specs/daemon/sim_daemon.md` "Capture errors don't stop the daemon" and `specs/daemon/real_daemon.md` (the camera-not-opened paragraph): the client-side symptom is `bridge.camera.latest()` staying `None` — upstream's `get_frame()` returning nothing — and the bridge's detector reading inactive. `get_camera_frame()` names nothing today.

### Step 4 — One installation instruction for the sim (finding 8)

- Positive instructions name the bridge's extra: `specs/_overview.md` "Three backends" (`sim` needs `reachy-mini-bridge[sim]`), `specs/core/robot.md`, `specs/testing/testing.md` ("`tests-e2e/` uses…"), and `specs/testing/testing_support.md`'s consumer table (`sim,test` — the extras that install the simulator and the harness together). Each points to [specs/project.md](../specs/project.md) for why MuJoCo is declared directly.
- Then grep both spellings, `reachy-mini[mujoco]` and `reachy_mini[mujoco]`, across tracked Markdown: what remains must be a warning against installing it (`project.md`, the README's install table, `docs/running-the-sim-daemon.md`, `docs/testing-with-the-bridge.md`, `sim_displays.md`'s degrade rule), never an instruction to.

### Step 5 — Links that resolve from a clone (finding 10)

- Copy `analysis/comfort_heading_analysis.md` to `docs/comfort-heading-analysis.md` — the tracked home of reference notes, beside `docs/reachy-mini-api.md` — and point `specs/motion/motion.md` open question 8 at it (`../../docs/comfort-heading-analysis.md`). The untracked `analysis/` copy stays as it is.
- Replace the sibling-checkout links: `specs/_overview.md` (`../../tts-engine`, `../../asr-engine`), `specs/audio/audio.md` and `specs/core/bridge.md` (`../../../tts-engine`), `specs/core/config.md` (`../../../tts-engine/specs/configuration.md`) with the public URLs — `https://github.com/funwithagents/tts-engine` (the one `pyproject.toml` depends on; the configuration spec at `/blob/main/specs/configuration.md`) and the matching `asr-engine` URL. Confirm each URL resolves before linking it; a repository that is not public is named without a link.
- Run a relative-link check over tracked Markdown outside `plans/` (every `](path)` target exists), treating `specs/_spec-template.md`'s placeholder as the one exception. Add nothing to CI in this plan.

### Step 6 — The documentation smoke tests

**File:** `tests/test_docs_examples.py` (new).

- A helper extracts the first fenced `python` block under a given heading of a Markdown file (`README.md` "Quick start"; `docs/testing-with-the-bridge.md` "Unit tests").
- `test_the_readme_quick_start_runs_on_the_fake`: executes the quick-start block as written (it ends with `asyncio.run(main())`); passes when it raises nothing.
- `test_the_testing_guide_unit_example_passes`: executes the guide's block with `my_greeting` as the guide defines it, then calls the test function it defines.
- Both run offline, under the fast tier's real timing (the emotion plays its length on the fake). A snippet that no longer runs fails the test with the snippet's own exception.

### Step 7 — Close

- Verification below green. Delete findings 6 to 10 from `analysis/repo-spec-code-consistency-review-20260929.md` (untracked; not part of the commit). Mark this plan `Done` here and in [_index.md](_index.md).

## Verification

- `uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest` — green, the two smoke tests included.
- The link check of Step 5 reports nothing. `grep -rn "reachy.mini\[mujoco\]" --include='*.md' .` outside `plans/` shows warnings only.
- `grep -rn "get_camera_frame\|REACHY_MINI_E2E_FACE_DETECTOR\|\[sim,tts\]" specs docs README.md` is empty (`camera.md` may keep its sentence that the verb was retired).
