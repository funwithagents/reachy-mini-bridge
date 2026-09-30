# Test scene portrait pool — `face_pool`, `spawn` / `despawn` / `clear`, as many portraits as a test needs

**Status:** Done

**Done (2026-09-30):** every step implemented and verified — `ruff check`, `ruff format`, `pyright`, the fast tier (537 tests), the headless live tier (14 passed, 9 skipped: camera / gravity compensation, as expected) and the viewer-sim live tier (20 passed in the full run, plus `tests-e2e/test_custom_faces.py` on its own once the Mac was unlocked — it had crashed at start in the full run because the screen locked mid-run: yaw ahead +1.2°, aside +16.6° for +18.4° expected, 10.0 reports/s). Departures: `FACE_POOL_SIZE = 3` rather than 8, per the user (the followed face, a rival, one more); the two-portrait smoke test reads two faces at x ≈ ±0.24 and one after a despawn; the probe's own test lives in `tests/test_testing_support.py` (the one drafted in `tests/test_sim_scene.py` duplicated it). No visible change in the viewer's frame rate or the camera feed's 10 fps with the hidden pool. `specs/testing/sim_scene.md` stays `Updated` until [202609300845](202609300845_face-report-tracks-and-detection-knobs.md) lands its multi-portrait head tracking tests.

Implements [specs/testing/sim_scene.md](../specs/testing/sim_scene.md) as re-designed on 2026-09-30 ("A pool of portraits", the director's discovery of kind and image, the router's `spawn` / `clear` routes, the client, "The testing harness") and the matching capability wording of [specs/testing/testing_support.md](../specs/testing/testing_support.md) and [specs/testing/testing.md](../specs/testing/testing.md). It delivers: the bridge's test scene written with a pool of `FACE_POOL_SIZE = 3` hidden portraits (`face_1` … `face_3`) sharing one texture per image; `spawn(pos, …, kind="face", image=None)` taking a free portrait and showing it in one director command, `despawn(name)`, `clear()`; each body's `kind` and `image` discovered at attach and reported in its state; the existing live tests migrated from the single `face` body to the pool. It deliberately leaves out: portraits other than the bundled one (the pool takes `images=` today; new files come with their attribution later), runtime texture swaps (open question 4), and the multi-portrait head tracking tests themselves — they need the tracker's choice rule and land with [202609300845](202609300845_face-report-tracks-and-detection-knobs.md), which depends on this plan.

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/testing/sim_scene.md](../specs/testing/sim_scene.md) in full; `src/reachy_mini_bridge/testing/sim_scene.py`, `fixtures.py` (`_probe_faces`, the `sim_scene` fixture), `_daemon.py` (`write_test_scene` call); `tests/test_sim_scene.py` (the director is tested on a real `MjModel` from a written scene, the router through a served app).
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

- **The viewer sim run is the acceptance** (Step 5): `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` from the agent session, the Mac awake and unlocked.
- **Do not commit** unless asked.

## Scope

- `src/reachy_mini_bridge/testing/sim_scene.py` — `FACE_POOL_SIZE`, `face_pool(count, images)`, `FacePlane.name` default `face_1`; `write_test_scene` defaulting to `face_pool()`, one texture / material (`portrait_<stem>`) per distinct image; `BodyState.kind` / `image`; `SceneDirector` discovering them, `spawn(...)` and `clear()` under its lock; router `POST /api/sim-scene/spawn` and `POST /api/sim-scene/clear`; `SimSceneClient.spawn` / `despawn` / `clear`; `__all__`.
- `src/reachy_mini_bridge/testing/fixtures.py` — `_probe_faces`: any body of kind `face`.
- `tests/test_sim_scene.py` — as Steps 1–4 say.
- `tests-e2e/test_bridge.py`, `tests-e2e/test_custom_faces.py` — the `FACE = "face"` body replaced by `spawn` / `despawn`, the per-test reset by `clear()`.
- `docs/testing-with-the-bridge.md` (the `faces` capability row, the scene's pool, a spawn example), `AGENTS.md` (the `testing/` row: "a pool of hidden portraits (`face_1` … `face_3`, `assets/face.png`) a test spawns / moves / despawns"; the viewer-sim line of the e2e table).
- `specs/_index.md`, `plans/_index.md`, this file — statuses (Step 6).

## Steps

### Step 0 — Baseline

Check command green on `main`.

### Step 1 — The pool in the scene file

- `FACE_POOL_SIZE = 3`; `face_pool(count: int = FACE_POOL_SIZE, images: Sequence[str | Path] | None = None) -> tuple[FacePlane, ...]`: `face_1` … `face_<count>`, hidden, at `DEFAULT_FACE_POS`, `images` round-robin (`DEFAULT_FACE_IMAGE` alone when `None`); `count < 1` or an empty `images` → `ValueError`. `FacePlane.name` default → `"face_1"`.
- `write_test_scene(out_dir, faces=None)`: `None` → `face_pool()`. Textures and materials are keyed by the resolved image path: one `<texture name="portrait_<stem>_tex">` and one `<material name="portrait_<stem>">` per distinct image (two different files with the same stem → suffix `_2`, …, so the names stay unique); each body's geom references its image's material. Body and geom names unchanged in form (`<name>`, `<name>_geom`).
- Tests (on the written XML and a loaded `MjModel`): the default scene has three hidden mocap bodies `face_1` … `face_3` and exactly one portrait texture; `face_pool(4, images=[a, b])` gives `a, b, a, b`; two images → two textures, each body's geom on its image's material; the existing custom-props test updated (names, material names); `count=0` / `images=[]` refused.
- Check.

### Step 2 — The director: kind, image, spawn, clear

- At attach, per body: `kind` = the name with a trailing `_<digits>` removed (the whole name when there is none); `image` = its first geom's material name minus the `portrait_` prefix (and a uniqueness suffix), `None` without one. `BodyState` gains `kind: str` and `image: str | None` (`to_dict` / `from_dict` round-trip them).
- `spawn(pos, quat=None, *, kind="face", image=None, duration=0.0) -> BodyState`: under the lock, the lowest-numbered hidden body of `kind` (and `image`, when given); none → `LookupError` naming the kind, the image, and the pool size; an `image` no body carries → `ValueError` listing the images present. Then the same command as `place` + `show`, applied together (a `duration > 0` move starts from the parked pose, the body visible from the first step).
- `clear()`: every body hidden, under the lock, poses kept.
- Tests (real `MjModel`, injected clock, `mj_step` as the existing director tests do): three spawns take `face_1`, `face_2`, `face_3` and show them at their positions; despawning `face_2` and spawning again takes `face_2`; four spawns on the default pool of three → the fourth raises `LookupError` with `3` in the message; `spawn(image="face")` works, `spawn(image="nobody")` raises `ValueError` listing `face`; with a two-image pool, `spawn(image=b)` takes the first free body of image `b`; a timed spawn is visible at once and arrives at its target after `duration`; `clear()` hides everything; states report `kind == "face"` and `image == "face"`.
- Check.

### Step 3 — The router and the client

- Routes: `POST /api/sim-scene/spawn` (body `pos` required; `quat`, `duration`, `kind`, `image` optional; unknown field → `400`) → the state, `LookupError` → `409`, `ValueError` → `400`; `POST /api/sim-scene/clear` → `{"bodies": {...}}`.
- `SimSceneClient.spawn(pos, quat=None, *, kind="face", image=None, duration=0.0) -> str` (the body's name — what a test keeps to move or despawn it), `despawn(name) -> BodyState` (`hide`), `clear() -> dict[str, BodyState]`; every failure a `SimSceneError` carrying the server's message (the `409` one included).
- `fixtures.py`: `_probe_faces` → `any(state.kind == "face" for state in client.bodies().values())`.
- Tests through the served app (the existing `served_director` fixture): spawn / despawn / clear round-trip; the `409` surfacing as `SimSceneError` with the pool size; `400` for an unknown field and an unknown image; the probe true on the default scene, false on a scene of only non-face props.
- Check.

### Step 4 — Migrate the live tests

- `tests-e2e/test_bridge.py`: the module-level `FACE = "face"` and the fixture that placed / hid it become a fixture that `clear()`s before and after each test; each test `spawn`s its portrait (`name = sim_scene.spawn(DEFAULT_FACE_POS)`) and moves / despawns it by that name. `tests-e2e/test_custom_faces.py` likewise.
- No new live test here (they need the tracker's choice, [202609300845](202609300845_face-report-tracks-and-detection-knobs.md) Step 6). One smoke test, gated on `camera` + `faces`: two portraits spawned side by side at 0.45 m (±0.15 m) → `bridge.faces` reports two faces within a few seconds, and after `despawn` of one, one face.
- Check (the fast tier).

### Step 5 — Live

- `uv run pytest tests-e2e -rs` headless: green (the scene loads with the pool; `faces` probed; camera tests skip as before).
- `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs`: the attention / gaze tests, the custom test and the two-portrait smoke test green; skips read and reported. Note in the Done note whether the hidden portraits change the viewer's frame rate or the detection rate (the cost line, once [202609300845](202609300845_face-report-tracks-and-detection-knobs.md) lands; the feed's fps from `bridge.camera` meanwhile).

### Step 6 — Docs and statuses

- `docs/testing-with-the-bridge.md` and `AGENTS.md` as in Scope.
- `specs/testing/sim_scene.md` stays `Updated`: its multi-portrait head tracking tests land with [202609300845](202609300845_face-report-tracks-and-detection-knobs.md), which flips it. `specs/testing/testing_support.md` stays `Updated` (its `live_bridge` config's rename to `face_detection` is [202609300845](202609300845_face-report-tracks-and-detection-knobs.md)'s).
- This plan `Done` (file and [_index.md](_index.md)).

## Verification

- The check command green after every step; the new fast tests as listed (pool writing, shared textures, director spawn / despawn / clear / exhaustion / image choice, router and client).
- Headless and viewer-sim live tiers green, the two-portrait smoke test included.
