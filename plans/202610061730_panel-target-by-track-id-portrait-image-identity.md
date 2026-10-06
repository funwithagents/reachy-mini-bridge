# The panel's target by track id, a portrait's image identity kept

**Status:** Done

Brings the control panel in line with [user_perception.md](../specs/vision/user_perception.md) ("The face report" — faces in `track_id` order) and [head_tracking.md](../specs/motion/head_tracking.md) (whom the head follows is `bridge.head_tracking.value.track_id`), and the test scene in line with [sim_scene.md](../specs/testing/sim_scene.md) (a prop's image is its portrait file's stem). Fixes R7 and R8 of the [repository consistency review of 2026-10-06](../analysis/20261006_repository-consistency-review.md). The report's ordering does not change; the panel's spec is the one updated.

## Scope

- `examples/control_panel/controller.py` — `PanelState.face_target: int | None` (the index in `face_positions` of the face the head follows, from `head_tracking.value.track_id`, `None` when none is followed or it is not in the report); `face_positions()` keeps the report's order; `draw_faces(frame, positions, rolls=(), target=None)` draws the square at `target` thicker, none when `None`.
- `examples/control_panel/app.py` — passes the controller's `face_target()` to `draw_faces`.
- `specs/examples/control_panel.md` — `snapshot()` / `draw_faces` rows: positions in report (track id) order, the target by index. Status `Implemented` → `Updated` while this plan is open.
- `src/reachy_mini_bridge/testing/sim_scene.py` — `write_test_scene` disambiguates two same-stem files with `__<n>` (two underscores) and refuses a stem that itself ends in `__<digits>` (`ValueError`: the scene cannot carry it); `_image_of` strips a trailing `__<digits>` only.
- `specs/testing/sim_scene.md` — the material-name rule: `portrait_<stem>`, a second file of the same stem `portrait_<stem>__2`, a stem ending in `__<digits>` refused. Status `Implemented` → `Updated` while this plan is open.
- `tests/test_control_panel.py` — a fake detector reporting a smaller first face and a larger second: the snapshot's `face_target` is the index of the larger one; `draw_faces` thickens the square at `target` and none when `None`.
- `tests/test_sim_scene.py` — a portrait named `person_1.png` round-trips as image `person_1` through the written scene and the director (`spawn(image="person_1")` finds it); two same-stem files still get distinct materials and distinct images reported; a `foo__2.png` is refused.
- This plan and [the plans index](_index.md).

## Steps

1. **Mark this plan `In progress`** here and in the index; set `control_panel.md` and `sim_scene.md` to `Updated` in the same change as the code.

2. **R7 — the target by track id.** In `snapshot()`, read `report = bridge.faces.value` and `followed = bridge.head_tracking.value.track_id` once; `face_positions` stays `[(f.x, f.y) for f in report.faces]`; `face_target = next((i for i, f in enumerate(report.faces) if f.track_id == followed), None)` when `followed` is not `None` and the report is active. Add `face_target()` beside `face_positions()` / `face_rolls()` for the app's frame callback, reading the same two values. `draw_faces` takes `target: int | None = None` and thickens that index (`height // 120`) and no other (`height // 240`). The docstrings and the `PanelState` comment say "in the report's order (track id), the target by index". Spec rows in `control_panel.md` updated to the same words.

3. **R8 — the image identity.** In `write_test_scene`, collision suffixes become `f"{_PORTRAIT_MATERIAL_PREFIX}{image.stem}__{suffix}"`; before building the name, refuse an `image.stem` matching `r"__\d+$"` with a `ValueError` naming the file. `_image_of` strips `re.sub(r"__\d+$", "", name)` instead of `_kind_of`. Spec `sim_scene.md` "A prop's kind and image are discovered" bullet: the material carries the stem as `portrait_<stem>`, two files of one stem `portrait_<stem>__2`, `__3`…, and a file whose stem ends in `__<digits>` is refused by the writer, so the stem always reads back whole.

4. **Tests, each observed failing first:**
   - `test_control_panel.py`: drive the panel on `fake` with a custom detector returning two `PixelFace`s, the first smaller; with tracking on, wait for `head_tracking.value.track_id` to be the second face's id, then assert `snapshot().face_target == 1` and `face_positions` has the smaller face first. `draw_faces` on a blank frame with two positions and `target=1`: the thick ring is at the second position (count the coloured pixels per square), `target=None` draws both thin.
   - `test_sim_scene.py`: copy the default portrait to `person_1.png`, write the scene with it, load the model, and assert the director (or `_image_of` on the material it reads) reports `person_1`; spawn by that image succeeds. The existing same-stem test extended to assert both bodies report the same image stem. `foo__2.png` raises.

5. **Flip the statuses** once the gate passes: this plan `Done`, `control_panel.md` and `sim_scene.md` back to `Implemented`, index rows in sync.

## Verification

`uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`. The live tier's tracking tests spawn by `image` from the default pool (`face.png`), which the suffix change does not touch; a viewer or headless run of `tests-e2e/test_head_tracking.py` confirms the pool still spawns.
