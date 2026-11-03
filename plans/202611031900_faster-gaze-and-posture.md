# Faster gaze and posture

**Status:** Done

Implements the retuned speeds in `specs/motion/motion.md` ("The gaze layer": `GAZE_ALPHA = 0.15`, the weight and focus fades over `GAZE_FADE_S = 0.35`) and `specs/motion/head_tracking.md` ("The posture": `ROLL_SMOOTHING_S = 0.3`, `OFFSET_SMOOTHING_S = 0.6`), after a first test with a person found the head slow to follow and slow to mirror. Leaves detection unchanged.

## Scope

- `src/reachy_mini_bridge/motion.py` — `GAZE_ALPHA` 0.12 → 0.15; `GAZE_FADE_S`, the gaze weight's and focus's fade, in place of `BLEND_S`
- `src/reachy_mini_bridge/head_tracking.py` — the two posture filters' time constants
- `tests/test_head_tracking.py` — the posture tests' bounds derived from the constants; the offline convergence tests held to the live kit's thresholds (`OVERSHOOT_MAX_DEG`, `YAW_TOLERANCE_DEG`) in place of stricter numbers of their own
- `tests/test_bridge.py` — the pipeline test reads the direction of the first clear turn (its stub face never moves in the image); the detection-switch test waits for the published report, not the detector's call (a race the ARM runner hit)
- `specs/motion/motion.md`, `specs/motion/head_tracking.md`, `specs/core/bridge.md`, `docs/reference/configuration.md`, `docs/guides/perception-and-tracking.md` — the values and the measurements

## Steps

1. Measure on the viewer sim where the time goes: the commanded head against the actual one, the daemon's raw step response on the sim and on the Lite.
2. Sweep `GAZE_ALPHA` × the fade length offline (the closed-loop harness of `tests/test_head_tracking.py`): settle time without delay, overshoot and swing-back under a 0.3 s unreported delay; take 0.2 × 0.35 s at first, then 0.15 once CI's live tier caught 0.2 hunting on its runner (10.6° past a face, 7.4° back).
3. Shorten the posture filters; re-measure the mirror's response.
4. Update the tests and the texts.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` (twice: the timing tests); the live head tracking, custom faces, sim displays and motion files on the viewer sim. Measured (viewer sim, commanded head): a 20° acquisition to 90% in 0.39–0.43 s (0.58 s before); a 25° tilt mirrored to 63% in 1.12 s (1.85 s); a 14° tilt in 2.07 s (2.47 s). Live overshoot at most 3.5°.
