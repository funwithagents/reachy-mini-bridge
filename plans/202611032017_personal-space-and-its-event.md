# Personal space and its event

**Status:** Done

Implements `specs/motion/head_tracking.md` ("The posture": the personal space; "The personal space event"; "The head tracking report"), `specs/core/config.md` (`motion.tracking.personal_space`, the old key refused), `specs/core/bridge.md` ("Attention / gaze") and `specs/examples/control_panel.md`: the `personal_distance` behaviour renamed `personal_space`, the proxemics term for the zone it defends, and the tracker publishing when the followed person enters and leaves it. Leaves out an event for anyone else in view (their distance is on `bridge.faces`) and the detection width's default (640: 1280 runs YuNet on the Lite's full 1920 frame, about 68 ms, too heavy for onboard compute).

## Scope

- `src/reachy_mini_bridge/config.py` — `PersonalSpaceSettings`, `TrackingSettings.personal_space`; a `personal_distance` key a `ConfigError` naming the new one; `exit_margin_m` (null: `PERSONAL_SPACE_EXIT_SHARE = 0.125` of `distance_m`)
- `src/reachy_mini_bridge/head_tracking.py` — the exit margin, configured or a share of the distance; the followed person's state judged on every observation that shows them, reset on loss and on a switch; `HeadTrackingReport.in_personal_space`, in the published state
- `src/reachy_mini_bridge/__init__.py`, `bridge.py` — the renamed class
- `examples/control_panel/` — the renamed checkbox, `in personal space` in the posture readout
- `examples/greeter/` — the greeter reacts on the personal space edge while someone is engaged: a surprised emotion and "Whoa, you're a bit close!" on entering, a relieved one and "Ah, that's better." on leaving, nothing when the visitor leaves while still close; its README and the project map; `tests/test_greeter.py`, the greeter's first test, on the fake. After a test on the Lite: no emotion on entering (the tracker's retreat is the reaction), `grateful1` on leaving; the greeter's two configs and `config.example.json` carrying the tuned motion block (a 20° roll cap, a 15 mm lean, a 20 mm retreat over 0.2 m, a 0.2 m exit margin) and detection at 1280 for the roll
- `config.example.json`, `docs/reference/configuration.md`, `docs/reference/api.md`, `docs/guides/perception-and-tracking.md` — the new name, the field, a subscription recipe
- `tests/test_head_tracking.py`, `tests/test_config.py`, `tests/test_control_panel.py`, `tests-e2e/test_head_tracking.py` — the event's hysteresis and reset, the old key's message, the readout, the flag far and near on the viewer sim; the rename throughout

## Steps

1. Rename across code, tests, specs and docs (the merged plans keep their wording).
2. The event in the tracker and the report; the old key's error. The exit margin first a constant (5 cm), then a config field defaulting to a share of the distance: the distance's noise grows with it (±5 %), so a fixed 5 cm would flicker at a wider zone.
3. The panel's readout; the greeter's reaction; tests; docs.
4. The posture's defaults from the values tuned on the Lite: `mirroring.max_deg` 12 → 20°, `approach.lean_mm` 12 → 15 mm, `personal_space.max_back_mm` 25 → 20 mm over `span_m` 0.10 → 0.20 m (a gradual give-way); the exit margin stays relative (12.5 % of the distance), the greeter's configs and `config.example.json` keeping their 0.2 m; the posture tests reading their expectations from `TrackingSettings()`, the live retreat test expecting the share its measured distance gives.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the live head tracking file on the viewer sim.
