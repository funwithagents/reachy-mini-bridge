# The frame delay: 0 by default, tunable

**Status:** In progress

**Steps 1–3 done (2026-11-04):** the field, the tracker's use of it, the measurement on a Lite and the profiles — the static gate and the fast tier green. **Step 4 open:** the viewer-sim live tier on the default 0, then the Lite with a person walking.

Implements `specs/motion/head_tracking.md` ("The aim": "The frame delay"; open question 7) and `specs/core/config.md` (`motion.tracking.frame_delay_s`). Delivers a fixed delay between a frame's arrival and the head pose it was taken from in place of the online estimate: `0` by default — measured within 0.02 s of the truth on a Lite over USB, 0.06 s on the viewer sim — a number for a slower path, `"estimate"` for the online fit, and a guide to tuning it by the head's behaviour. No per-backend constant: a measured value per backend would go stale with the robot, the stream or the machine. Leaves out: correcting the estimate's bias (open question 7), and smoothing the aim between frames.

## Scope

- `src/reachy_mini_bridge/config.py` — `TrackingSettings.frame_delay_s` (`null`, or a number in `[0, 0.5]`), `FRAME_DELAY_MAX_S`
- `src/reachy_mini_bridge/head_tracking.py` — `fixed_delay_s` and `delay_s`; the aim uses `delay_s`; no detection joins the fit while the delay is fixed
- `tests/test_bridge.py` — the fake's tracker reads the default 0
- `config.example.json` — `frame_delay_s: 0.0`
- `docs/reference/configuration.md`, `docs/guides/perception-and-tracking.md` — the field, and "Tuning the frame delay": the head's behaviour when it is too short or too long, the procedure, the runtime change
- `tests/test_head_tracking.py`, `tests/test_config.py` — the closed loop settling with the delay fixed at the truth; 0 by default, a number fixing another, `"estimate"` running the fit, the fit not fed while the delay is fixed; the estimate's own tests opting into it; the field's parsing and bounds

## Steps

1. The field and its bounds (the estimate's own range), `"estimate"` among its values, 0 its default.
2. The tracker: `delay_s` and the aim on the setting's number, the fit not fed while it is a number, the estimate resuming where it stood on `"estimate"`.
3. The measurement on the Lite (`analysis/pitch_probe/measure_delay.py`): a person sitting still, the head swept through a known ±12° sine at 0.25 Hz, every new detection's room direction scored against the reported pose history at every delay from 0 to 0.5 s. 241 detections, a minimum spread of 0.34° at 0.02 s (0.98° at 0.12 s, 4.33° at 0.5 s). The same sweep before a portrait on the viewer sim: 0.06 s (0.82°; 1.09° at 0 s), twice. 0 is within either of them, and closer than any estimate the fit settled on: the default.
4. **The viewer-sim live tier on the default 0** (its tracking tests ran on the estimate until now), then **the Lite with a person walking side to side** on the default: the stepping gone or not, against the A/B of the investigation (the estimate: 17 % aim reversals between frames, 7.2 % head reversals; 0.12 s: 10 %, 3.2 %). Then the specs' statuses, this plan `Done`.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the Lite check of step 4.
