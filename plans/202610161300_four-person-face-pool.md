# Four-person face pool

**Status:** Done

Implements the settled behaviour in `specs/testing/sim_scene.md` ("A pool of portraits", the router table, "Geometry", "The testing harness"), plus a measured caveat in `specs/vision/user_perception.md` ("Detectors", YuNet's output). The test scene's pool becomes one prop per person, four bundled public-domain portraits: two frontal, one head turned, one looking down. A test spawns a given person by the prop's name. The plan leaves out a "person turns their head" test, which needs one person in two poses (sim_scene.md open question 4), and any profile portrait, which sits on the detector's edge.

## Scope

- `src/reachy_mini_bridge/testing/assets/`:
  - `face.png` renamed `armstrong.png`;
  - `chawla.png`, `schmid.png` and `malenchenko.png` added, cropped to the same framing;
  - `ATTRIBUTION.md` credits all four, one row per prop.
- `src/reachy_mini_bridge/testing/sim_scene.py`:
  - `FACE_IMAGES`, `FACE_POOL_SIZE = len(FACE_IMAGES)`, `TURNED_FACE`, `LOOKING_DOWN_FACE`;
  - `face_pool(count=None, images=None)`, one prop per image by default;
  - `spawn(..., name=None)` on the director, the router (`name`, 404 for an unknown one) and the client, in place of the `image=` filter.
- `tests/test_sim_scene.py` — the default pool (four people, no look-alikes, the constants), spawn by name (the same person back, already shown refused, unknown name refused), the client over the wire.
- `tests-e2e/test_head_tracking.py` — `test_head_tracking_follows_someone_not_looking_at_the_robot`, parametrised over the two poses, expecting the nose's bearing.
- `specs/testing/sim_scene.md`, `specs/_index.md`, `specs/vision/user_perception.md`, `docs/guides/testing.md`, `AGENTS.md` — the pool, the API, the measured floors and the roll caveat.

## Steps

1. Crop the portraits to `armstrong.png`'s framing: the YuNet box about 0.49 of the height and centred, 320x400. Schmid's crop is fitted to the photo rather than padded.
2. Install them as assets with their attribution.
3. Change the pool constants and `face_pool`; replace the image filter with `name` in the director, the router and the client.
4. Update the fast tests; run them.
5. Add the e2e test. The nose sits about 3 cm off the plane's centre on both photos (0.647 and 0.660 of the width), so the expected yaw is the nose's.
6. Update the spec, the index, the docs and the project map.

## Verification

- `uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples` and `uv run pyright` are clean.
- `uv run pytest`: 732 passed, 1 skipped.
- The face files of the live tier on the viewer sim pass with the new pool (`test_head_tracking`, `test_perception`, `test_sim_displays`, `test_custom_faces`), including the new test for both poses. In those runs the settled yaw was within 1.6° of the nose's bearing and the face was centred.
- `test_attention_hands_the_head_back_and_reengages_on_the_face` failed once: re-engaged at −12.4° against −18.4°, though the face was centred. It passed on the rerun (−16.4°). It spawns the same frontal portrait as before this change, so the failure is the settle check calling the head still while it creeps, not the pool.
