# Head tracking: a focus flag instead of a weight

**Status:** Done

Implements [specs/core/bridge.md](../specs/core/bridge.md) "Attention / gaze (autonomous)", [specs/motion/head_tracking.md](../specs/motion/head_tracking.md) "Focus" and [specs/motion/motion.md](../specs/motion/motion.md) "The gaze layer" as revised: `start_head_tracking(focus=False)` replaces the daemon-era `weight`. Tracking always composes the aim in full; `focus=True` holds the head exactly on the aim (the idle move's head motion left out) while the antennas keep the idle move's gaze-time motion. The control panel's Gaze group becomes a Tracking checkbox and a Focus checkbox. Leaves the daemon's own `start_head_tracking(weight)` (the detector arming) untouched.

## Scope

- `src/reachy_mini_bridge/motion.py` — `set_gaze(aim, *, focus=False)`: no weight; the effective weight fades between 0 and 1; with `focus` the gaze pose's head is neutral (the head on the aim), its antennas unchanged.
- `src/reachy_mini_bridge/head_tracking.py` — `HeadTracker.focus` in place of `weight`, handed over with every aim.
- `src/reachy_mini_bridge/api.py` — `start_head_tracking(focus: bool = False)`; no `ValueError`.
- `examples/control_panel/controller.py`, `app.py` — `start_head_tracking(focus)`; Tracking + Focus checkboxes in place of the slider and buttons.
- `tests/test_motion.py`, `tests/test_head_tracking.py`, `tests/test_api.py`, `tests/test_control_panel.py` — the weight tests replaced by focus tests.
- `specs/core/bridge.md`, `specs/motion/head_tracking.md`, `specs/motion/motion.md`, `specs/examples/control_panel.md`, `README.md` — the verb and the panel.

## Steps

1. Motion: `set_gaze(aim, *, focus=False)`; `w_target = 1.0` while an idle stage plays with an aim held; `_gaze_focus` replaces the head of the gaze pose by neutral. Tests: with focus on breathing, the head equals the aim (no z breath, no roaming) while the antennas still move; without it, the breath shows.
2. Tracker: `focus` attribute passed with every `set_gaze`; tests updated.
3. Api: `start_head_tracking(focus=False)` sets `tracker.focus`; test that `focus=True` on breathing holds the head still on the face.
4. Panel: controller `start_head_tracking(focus)`; Modes/Gaze checkboxes "Tracking" (start/stop) and "Focus" (re-applies `start_head_tracking(focus)` while tracking is on); initial value from `api.tracking`.
5. Specs and README.

## Verification

`uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest`; the viewer-sim tracking tests (`REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -k "face or attention or tracking or emotion_plays_over"`). Then `head_tracking.md` and `control_panel.md` back to `Implemented`, this plan `Done`.
