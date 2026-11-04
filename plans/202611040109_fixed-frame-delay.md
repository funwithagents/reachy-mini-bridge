# A fixed frame delay for the tracker

**Status:** In progress

**Steps 1–3 done (2026-11-04):** the field, the tracker's use of it, the measurement on a Lite and the profiles — the static gate and the fast tier green. **Step 4 open:** the Lite with a person walking, the delay fixed at the measured 0.02 s.

Implements `specs/motion/head_tracking.md` ("The aim": "A fixed delay"; open question 7) and `specs/core/config.md` (`motion.tracking.frame_delay_s`). Delivers a config field that fixes the delay between a frame's arrival and the head pose it was taken from, used in place of the online estimate, and the measured value in the Lite profiles. Leaves out: correcting the estimate's bias (open question 7), and smoothing the aim between frames.

## Scope

- `src/reachy_mini_bridge/config.py` — `TrackingSettings.frame_delay_s` (`null`, or a number in `[0, 0.5]`), `FRAME_DELAY_MAX_S`
- `src/reachy_mini_bridge/head_tracking.py` — `delay_s` reads the fixed value when set; the aim uses `delay_s`; no detection joins the fit while it is set
- `examples/configs/lite-usb.json`, `examples/greeter/configs/lite-usb.json` — `frame_delay_s: 0.02`; `config.example.json` — `null`
- `docs/reference/configuration.md`, `examples/configs/README.md` — the field
- `tests/test_head_tracking.py`, `tests/test_config.py` — the closed loop settling with the delay fixed at the truth; switching between fixed and estimated; the field's parsing and bounds

## Steps

1. The field and its bounds (the estimate's own range).
2. The tracker: `delay_s` and the aim on the fixed value, the fit not fed while it is set, the estimate resuming where it stood once it is cleared.
3. The measurement on the Lite (`analysis/pitch_probe/measure_delay.py`): a person sitting still, the head swept through a known ±12° sine at 0.25 Hz, every new detection's room direction scored against the reported pose history at every delay from 0 to 0.5 s. 241 detections, a minimum spread of 0.34° at 0.02 s (0.98° at 0.12 s, 4.33° at 0.5 s). The Lite profiles fix 0.02 s.
4. **The Lite with a person walking side to side**, the delay fixed at 0.02 s: the stepping gone or not, against the A/B of the investigation (the estimate: 17 % aim reversals between frames, 7.2 % head reversals; 0.12 s: 10 %, 3.2 %). Then the specs' statuses, this plan `Done`.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the Lite check of step 4.
