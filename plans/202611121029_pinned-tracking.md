# Pinned tracking

**Status:** Done

Implements the settled behavior in `specs/motion/head_tracking.md` ("A pinned face", "The head tracking report", "Configuration and verbs", "Lifecycle"), `specs/vision/user_perception.md` ("Tracks": "The held tracks") and `specs/core/bridge.md` ("Attention / gaze"). A caller pins the face the head follows by its `track_id` through `start_head_tracking(track_id=…)`; the tracker follows that face alone, never switches, and on its loss stays on and watching with the pin kept; `HeadTrackingReport.pinned_track_id` publishes the pin. The automatic target stays the default and unchanged. Recognising people, and re-pinning after a track breaks, stay the client's.

## Scope

- `src/reachy_mini_bridge/head_tracking.py` — `HeadTracker.pin(track_id)`, the pinned rules in `observe` (no switch, acquiring only the pinned face, no size gate), the pin cleared by `stop()`; `HeadTrackingReport.pinned_track_id`, compared by `same_state`
- `src/reachy_mini_bridge/face_detection.py` — `held_track_ids()` on the detection loop (and the ids on `_FaceTracks`)
- `src/reachy_mini_bridge/bridge.py` — `start_head_tracking(*, focus=None, track_id=None)`: the id checked against the held tracks (`ValueError`), the target set after the settings; docstrings of `head_tracking`
- `tests/test_head_tracking.py` — the pinned rules on the tracker's test clock
- `tests/test_face_detection.py` — the held track ids through misses and after a drop
- `tests/test_bridge.py` — the verb on the fake: pinning, the loss, the return to automatic, the refusals, what clears and keeps the pin
- `tests-e2e/test_head_tracking.py` — two portraits in the viewer sim: the pinned one is tracked, not the other
- `docs/reference/api.md`, `docs/guides/perception-and-tracking.md` — the verb, the report field, the re-pin loop

## Steps

1. Tracker: `_pinned`; `pin(track_id)` — to another id than the followed one: follow it as missing since now (the previous aim stands, the sighting forgotten, the personal space reset); while following nobody: watch; `None`: keep the followed face. Publishes when active.
2. `observe`: while pinned, a missing followed face never switches; following nobody, only the pinned face is acquired, with no size gate. `stop()` clears the pin.
3. Report: `pinned_track_id` in `_state`, `same_state`; `None` in the inactive report.
4. Detection loop: `held_track_ids()` — the ids of `_FaceTracks`, empty while not running.
5. Bridge: `start_head_tracking(track_id=…)` checks the type (an `int`, not a `bool`) and that the loop holds the id before anything changes; applies the settings through `set_tracking`, then pins (`None` returns to automatic).
6. Tests and docs as scoped; the specs back to `Implemented`.

Left out: a "follow" choice in the control panel (`examples/control_panel/`, `specs/examples/control_panel.md`) — a UI change of its own; the panel's tracking toggle keeps the automatic target.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the viewer sim tier for the new live test (`REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_head_tracking.py -rs`); CI green on the PR, its live job's skips read against `specs/testing/ci.md`. Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass.
