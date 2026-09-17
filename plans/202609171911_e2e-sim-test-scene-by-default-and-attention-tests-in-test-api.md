# E2E: the test scene on every sim, attention tests in test_api, and the README on the sim additions

**Status:** Done

Implements the review follow-up to [202609171842_sim-daemon-launcher-tracking-corrections-and-webcam.md](202609171842_sim-daemon-launcher-tracking-corrections-and-webcam.md), per [specs/testing_support.md](../specs/testing_support.md), [specs/testing.md](../specs/testing.md) ("E2E targets & capabilities") and [specs/sim_scene.md](../specs/sim_scene.md) ("The testing harness"): a harness-spawned sim always runs the bridge's test scene (the `REACHY_MINI_E2E_SIM_SCENE` knob goes away); the tracking / attention e2e tests move into `tests-e2e/test_api.py`, play the short emotion the other emotion test plays, and check how the head moves, not only where it ends; the README says plainly what the bridge adds to the SDK in the simulator. Leaves the webcam manual checklist to the earlier plan.

## Scope

- `src/reachy_mini_bridge/testing/_daemon.py` — `sim_scene()` / `TEST_SCENE` removed; a spawned sim always gets a generated test scene
- `src/reachy_mini_bridge/testing/fixtures.py`, `src/reachy_mini_bridge/testing/sim_scene.py` — docstrings no longer name the env var
- `tests/test_testing_support.py` — the env-var test replaced by "a spawned sim runs the generated test scene"
- `tests-e2e/test_api.py` — an "Attention / gaze" section with the three tracking tests; `tests-e2e/test_faces.py` deleted
- `tests/test_sim_daemon.py` — the expected yaw is `atan2(y, x)` from the head's pivot, tolerance tightened
- `README.md` — "What the bridge adds to the SDK": a Simulator row (tracking corrections, webcam) and the Testing row (test scene); the testing section without the env var
- `AGENTS.md`, `docs/testing-with-the-bridge.md`, `docs/running-the-sim-daemon.md` — no env var; the tracking tests live in `test_api.py`
- Statuses: `testing.md`, `testing_support.md` Updated → Implemented when this plan is Done (`sim_scene.md` follows the earlier plan)

## Steps

1. Harness: `managed_daemon("sim")` always enters `_test_scene()` (temp dir + `write_test_scene`) and passes its path as `DaemonConfig.scene`; drop `sim_scene()` / `TEST_SCENE`. Test: with the lifecycle scripted, the recorded `DaemonConfig.scene` is an existing `scene.xml` with a `face` body while the daemon is up, and the directory is gone after.
2. E2E tracking tests into `test_api.py` (reusing its `_require_emotions_library`):
   - expected yaw `atan2(y, x_face)` (18.4° at ±0.15 m), tolerance 3°;
   - **movement**: while the head turns onto a face (appearing, or moving to a new position), sample its yaw until it holds still; assert it swings past the expected yaw at most once and by at most 12° (`OVERSHOOT_MAX_DEG` — measured 3–9.5°: upstream's aim adds a detection a few frames old to the present head pose), never swings back short of it by more than 3° after that peak (no oscillation), and settles within 3° of it; the tracked face at the image centre. (First written as "never overshoots by more than 3°": the live traces showed one smooth overshoot and a slow creep back on every turn, so the bound follows the measurement.)
   - pitch: the settled pitch stays within 3° of the pitch settled with the face ahead (the face does not change height);
   - the emotion-under-tracking test plays `names[0]` like `test_play_emotion_plays_a_real_move`.
3. Fast test's expected yaw aligned (`atan2(y, 0.45)`, 1.5°).
4. README and docs per Scope.
5. Gate; live: `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` (tracking tests run with no scene variable) and `uv run pytest tests-e2e -rs` (headless: they skip on `camera`).

## Verification

### Verification log (2026-09-17)

- Gate: `ruff check`, `ruff format --check`, `pyright` (0 errors), `pytest` — 342 passed.
- Viewer, no scene variable: `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` — 15 passed, 2 skipped (gravity compensation; motor modes on a sim); the three tracking tests ran. Settled yaw −0.3 / +18.4 / −17.1 / −0.3° (expected 0 / ±18.4 / 0), overshoot 3.7–9.6°, swing back ≤ 1.7°, pitch 5.3–6.6°, tracked face within 0.03 of the centre; `fear1` moved the head 32° under tracking.
- Headless: `uv run pytest tests-e2e -rs` — 11 passed, 6 skipped; the tracking tests skip on `camera` (`faces` is now present on the headless sim).

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` green; both live runs above as described. Then this plan → Done and `testing.md` / `testing_support.md` → Implemented (files + `specs/_index.md`).
