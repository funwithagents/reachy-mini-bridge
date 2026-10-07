# Live tier: detection and tracking off by default, face tests gated on `faces`

**Status:** Done

Implements the settled behavior in `specs/testing/testing_support.md` ("Public surface": `live_bridge`, `face_scene") and `specs/testing/testing.md` ("E2E targets & capabilities"): the harness's bridge starts with the `yunet` detector configured but detection and tracking off, a face test turns on what it needs and `face_scene` turns both off after it, and every test that turns either on gates on `faces`. It leaves `tests-e2e/test_custom_faces.py`'s own session as it is (its config turns tracking on; it gates on `camera` + `faces`).

## Scope

- `src/reachy_mini_bridge/testing/fixtures.py` — `live_bridge`'s config without `enabled` / `tracking`; `face_scene` turns detection and tracking off on teardown
- `src/reachy_mini_bridge/testing/gaze.py` — `arm_tracking`'s docstring and message for a tracker that starts off
- `tests-e2e/test_perception.py` — the live-camera detection test gated on `faces` and turning detection off after it; the "module's default state" restores removed
- `tests-e2e/test_audio.py` — the wobbling and cancelled-`say` checks read the head at rest as stillness near neutral, not as a return onto the earlier pose (on the robot the head rests anywhere within 4.3° of neutral, two rests up to 5.2° apart)
- `tests-e2e/test_sim_displays.py`, `tests-e2e/test_head_tracking.py` — the `stop_head_tracking` / `start_head_tracking` calls that undid and restored the old default removed
- `AGENTS.md`, `docs/guides/testing.md`, `specs/_index.md` — the harness's starting state stated

## Steps

1. Drop `enabled=True` and `MotionSettings(tracking=True)` from `live_bridge`'s config.
2. `face_scene`: after the test, clear the pool, then `stop_head_tracking()` and `set_face_detection(False)` through `live_bridge.run`.
3. Gate `test_detection_runs_on_the_live_camera` on `camera` + `faces`; turn detection off in a `finally`.
4. Remove the calls that stopped tracking at a face test's start and restarted it at its end.
5. `test_audio.py`: the wobble test asserts half a second of stillness within 3 s of the audio's end, within `REST_FROM_NEUTRAL_DEG` (6°) of neutral; the cancelled-`say` test asserts the head moves under 1° over the window from 1 s to 2.5 s after the cancel.

## Verification

Lint, format check, pyright, the fast tier. The live tier on the robot over USB (`REACHY_MINI_E2E_TARGET=real`): the motion and audio tests pass with someone in front of the robot, every detection and tracking test skips on `faces`. The live tier on the viewer sim (`REACHY_MINI_E2E_SIM_VIEWER=1`): every face test runs and passes.
