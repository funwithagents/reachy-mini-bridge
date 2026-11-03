# Personal space and its event

**Status:** Done

Implements `specs/motion/head_tracking.md` ("The posture": the personal space; "The personal space event"; "The head tracking report"), `specs/core/config.md` (`motion.tracking.personal_space`, the old key refused), `specs/core/bridge.md` ("Attention / gaze") and `specs/examples/control_panel.md`: the `personal_distance` behaviour renamed `personal_space`, the proxemics term for the zone it defends, and the tracker publishing when the followed person enters and leaves it. Leaves out an event for anyone else in view (their distance is on `bridge.faces`).

## Scope

- `src/reachy_mini_bridge/config.py` — `PersonalSpaceSettings`, `TrackingSettings.personal_space`; a `personal_distance` key a `ConfigError` naming the new one
- `src/reachy_mini_bridge/head_tracking.py` — `PERSONAL_SPACE_MARGIN_M = 0.05`; the followed person's state judged on every observation that shows them, reset on loss and on a switch; `HeadTrackingReport.in_personal_space`, in the published state
- `src/reachy_mini_bridge/__init__.py`, `bridge.py` — the renamed class
- `examples/control_panel/` — the renamed checkbox, `in personal space` in the posture readout
- `config.example.json`, `docs/reference/configuration.md`, `docs/reference/api.md`, `docs/guides/perception-and-tracking.md` — the new name, the field, a subscription recipe
- `tests/test_head_tracking.py`, `tests/test_config.py`, `tests/test_control_panel.py`, `tests-e2e/test_head_tracking.py` — the event's hysteresis and reset, the old key's message, the readout, the flag far and near on the viewer sim; the rename throughout

## Steps

1. Rename across code, tests, specs and docs (the merged plans keep their wording).
2. The event in the tracker and the report; the old key's error.
3. The panel's readout; tests; docs.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the live head tracking file on the viewer sim.
