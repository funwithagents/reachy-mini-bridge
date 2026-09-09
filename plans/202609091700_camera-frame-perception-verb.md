# Add `get_camera_frame` perception verb; move e2e onto the API

**Status:** Done

Implements a small extension to the settled `specs/api.md` ("v1 scope") and `specs/robot.md` ("consumed slice"): pull a single camera-frame perception verb into v1 so the live camera test drives the public API instead of raw robot internals, and delete `tests-e2e/test_robot.py` (its motor-state check is already covered at the API layer, its camera check moves to `tests-e2e/test_api.py`). Deliberately narrow: **one** perception verb (`get_camera_frame`) — the rest of the deferred perception batch (`get_head_pose`, `get_imu`, `get_tracked_face`, DoA) stays deferred.

## Design decisions (settled with the user)

- **Verb name:** `get_camera_frame` (not `get_view`).
- **Return type:** the raw camera frame as a numpy **BGR `ndarray`** or `None` (`npt.NDArray[np.uint8] | None`, `HxWx3`) — a *perception-oriented* return. This establishes the convention that **perception verbs return rich objects** (like the mic path, which yields raw PCM), while **action verbs stay JSON-friendly**; the JSON/base64 encoding of frames is the [tools.md](../specs/tools.md) layer's job (already stated there).
- **No-frame handling:** `get_camera_frame` grabs one frame off the event loop (`asyncio.to_thread(self._robot.media.get_frame)`) and **mirrors upstream exactly** — upstream `get_frame()` returns `None` until a frame is ready / when there is no GL context (see [docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md)), so the verb returns `None` in that case rather than raising. It is a thin pass-through (like `play_sound`). No motors precondition (read-only). No internal poll loop — a caller that wants to wait polls in a short loop, and the e2e test only runs where the `camera` capability already probed a frame.

## Scope

- `specs/api.md` — add a **Perception** group to v1 scope with `get_camera_frame`; state the "perception verbs return objects, action verbs stay JSON-friendly" convention; remove `get_view(...)`/camera-frame from the Deferred "Rich perception" list (the rest stays); narrow open question 3 (the camera return shape is now settled; head-pose/IMU/tracked-face/DoA shapes remain deferred). Status `Implemented → Updated` in this change, back to `Implemented` on completion.
- `specs/robot.md` — add `media.get_frame` to the consumed-slice **Media** list. Status `Implemented → Updated → Implemented`.
- `specs/_overview.md` — update the v1 verb list (add camera perception) and drop the "otherwise blind (no `get_view`)" caveat (now: a single camera-frame read; still no gaze-from-pixels).
- `src/reachy_mini_bridge/api.py` — add `async def get_camera_frame(self) -> npt.NDArray[np.uint8]`.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — add `_FakeMedia.get_frame()` returning a synthetic BGR frame (not recorded as a command, matching the other perception getters).
- `tests/test_api.py` — fast-tier functional test: `get_camera_frame` on the fake returns an `HxWx3` uint8 array.
- `tests-e2e/test_api.py` — add `test_camera_frame_delivers_a_frame`, gated `requires_caps(live_api, "camera")`, driving `api.get_camera_frame()`.
- `tests-e2e/test_robot.py` — **delete** (motor-state already covered by `test_motor_state_reads_and_dispatches_over_the_live_path`; camera moved to `test_api.py`).
- `plans/_index.md` — add this plan's row.

Note: `tests-e2e/test_robot.py` is not named in any spec's `tests:` frontmatter (only the fast `tests/test_robot.py` is), so deleting it needs no frontmatter edit. `specs/api.md` already lists `tests-e2e/test_api.py`.

## Steps

1. **Specs first.** Edit `specs/api.md`: add the Perception group + `get_camera_frame` to v1, add the return-shape convention sentence, prune the Deferred list, narrow open question 3; set its `**Status:**` to `Updated` and sync [specs/_index.md](../specs/_index.md). Do the same for `specs/robot.md` (add `media.get_frame` to the Media slice; `**Status:** Updated`). Update `specs/_overview.md`'s v1 description.
2. **Fake.** Add `get_frame(self) -> npt.NDArray[np.uint8]` to `_FakeMedia` returning a deterministic synthetic frame — a small non-uniform pattern (e.g. a gradient over a fixed `HxWx3`) so a test asserts real structure, not a constant. Do **not** append to `commands` (perception getter, like `get_audio_sample`).
3. **API.** Add `get_camera_frame` to `ReachyMiniApi` (a new `# --- perception ---` block): `return await asyncio.to_thread(self._robot.media.get_frame)` — a thin pass-through that mirrors upstream, returning the BGR frame or `None`. Under the `AnyReachyMini` union, pyright now type-checks `media.get_frame` against both the real `MediaManager` and the fake — the point of step 2.
4. **Fast test.** In `tests/test_api.py`, add `test_get_camera_frame_returns_a_frame`: `async with ReachyMiniApi("fake")`, `frame = await api.get_camera_frame()`, assert `frame.ndim == 3 and frame.shape[2] == 3`, `frame.dtype == np.uint8`, `frame.size > 0`.
5. **Move the e2e camera test.** In `tests-e2e/test_api.py`, add `test_camera_frame_delivers_a_frame(live_api)`: `requires_caps(live_api, "camera")`, `frame = asyncio.run(api.get_camera_frame())`, assert the same BGR shape invariants as the old raw test. Carry over the docstring note that it skips headless (no GL) and runs headfull/real. Then delete `tests-e2e/test_robot.py`.
6. **Statuses.** Once lint/type-check/tests pass, flip `specs/api.md` and `specs/robot.md` back to `Implemented` (and their `_index.md` rows), and set this plan to `Done` here and in `plans/_index.md`.

## Verification

- `uv run ruff check .` and `uv run ruff format .` clean.
- `uv run pyright` clean — in particular `self._robot.media.get_frame` now type-checks under the union (the old raw test needed a loose `Any` cast because the fake lacked `get_frame`; that cast is gone).
- `uv run pytest` — the new fast `tests/test_api.py::test_get_camera_frame_returns_a_frame` passes; nothing else regresses; `tests-e2e/` still not collected by default.
- Optional live check (headfull sim, needs a GL context / unlocked screen): `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_api.py -k camera` runs and passes; headless it **skips** (camera cap absent), proving the gate still works.
- `git ls-files` no longer lists `tests-e2e/test_robot.py`.
