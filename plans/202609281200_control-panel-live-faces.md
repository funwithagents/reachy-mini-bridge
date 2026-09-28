# Control panel: live faces on the camera and the face update rate

**Status:** Done

Implements [specs/control_panel.md](../specs/control_panel.md) "The controller" (`snapshot()`'s `face_positions` / `face_rate`, the face meter, `draw_faces`) and "The UI" (the camera refreshed at 0.2 s with a marker per face, the positions and the rate in the state table). Panel-only: nothing in `src/` changes — the panel reads `api.faces` as any caller would.

## Scope

- `examples/control_panel/controller.py` — `PanelState.face_positions` / `face_rate`; a face meter task on the loop (samples `api.faces.value` at `FACE_METER_HZ` = 50, counts distinct report timestamps over `FACE_RATE_WINDOW_S`); `draw_faces(frame, positions)`.
- `examples/control_panel/app.py` — a second `gr.Timer` (`CAMERA_REFRESH_S = 0.2`) for the annotated camera frame; the faces line under the camera (count, positions, update rate) on the same timer.
- `tests/test_control_panel.py` — positions and rate after the fake's `show_face`; `draw_faces` marks the right pixels.
- `specs/control_panel.md`, `specs/_index.md`, this file, [_index.md](_index.md) — status.

## Steps

1. The face meter, beside the mic meter: every `1 / FACE_METER_HZ` s read `api.faces.value`; when it is active and its `ts` differs from the last one seen, record `time.monotonic()`; `face_rate` is the count of records within the last `FACE_RATE_WINDOW_S` divided by the window (`None` while the report is inactive). Started and cancelled with the mic meter.
2. `snapshot()` fills `face_positions` (the `(x, y)` of each face, `[]` when inactive) and `face_rate`.
3. `draw_faces(frame, positions)` — a copy of the RGB frame with a green square outline centred on each face (normalised `[-1, 1]` → pixels), side 15 % of the frame height, the target face (first) thicker. Pure numpy.
4. The app: the state timer keeps 0.5 s and loses the camera; a camera timer at 0.2 s returns `draw_faces(controller.camera_frame_rgb(), snapshot-free positions)` — the positions read straight from `api.faces.value` (no motor round trip at 5 Hz).

## Verification

`uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest` green; manual: the panel on `config.example.json` shows a square following your face and a rate near the detector's.
