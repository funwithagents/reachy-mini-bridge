# Presence in the tracks: one rule for the users and the head

**Status:** Done

Implements `specs/vision/user_perception.md` ("Tracks": the miss window, "A time, and a count"; "The users present": `missing_s`, one rule of presence; "Every observation reaches the observer") and `specs/motion/head_tracking.md` ("Whom the head follows", "A pinned face", "Easing, loss, focus", "Only a user is pinned"), with `specs/core/bridge.md`'s attention. The tracks own presence — a miss window in time with a floor of misses, the missing time per track, the 20 s net — and the head tracker chooses among the users, with no presence clock of its own: its switch reads `missing_s`, its loss is the user's departure, a face not yet a user is never acquired, and a pin names a user. Builds on `plans/202611121533_users-present.md`. Leaves out the recognition key on `PixelFace` (its own change).

## Scope

- `src/reachy_mini_bridge/face_detection.py` — `TRACK_LOST_S`, `TRACK_MIN_MISSES` (in place of `TRACK_MAX_MISSES`), `UNOBSERVABLE_MAX_S` moved here; `_Track.missing_s` / `updated_at`, the drop rule; `User.missing_s`; the observer hearing `(faces, users)` per observation and `(None, users)` per clearing; `held_track_ids()` removed
- `src/reachy_mini_bridge/head_tracking.py` — `observe(report, users)`; the choice among users; `TRACKING_LOST_S`, `UNOBSERVABLE_MAX_S`, `tick()` and the tracker's in-view clock removed
- `src/reachy_mini_bridge/bridge.py` — the observation hook; the pin checked against `users`
- `src/reachy_mini_bridge/testing/gaze.py` — `TRACK_LOST_S`
- Tests: `tests/test_face_detection.py` (the timed window and its floor, `missing_s`), `tests/test_head_tracking.py` (a stand-in for the tracks' presence; an unconfirmed face not acquired; silence held until the clear; the pin on a missing user), `tests/test_bridge.py`, `tests-e2e/test_head_tracking.py`
- Specs: `vision/user_perception.md`, `motion/head_tracking.md`, `core/bridge.md` (Updated → Implemented), `testing/sim_scene.md`, `testing/testing_support.md`, `_index.md`; docs: the API reference, the perception guide

## Steps

1. The tracks: missing time accumulated in view between observations, dropped at `TRACK_LOST_S` over `TRACK_MIN_MISSES`; the net here.
2. `User.missing_s`; the loop's observer fed the users report beside each face report, and each clearing.
3. The tracker: follow a user seen; hold while present and unseen; switch at `missing_s >= TRACKING_SWITCH_S`; lose on departure; acquire only users; the pin on a user.
4. The bridge's hook and pin check; the gaze kit.
5. Tests, specs, docs.

## Verification

`uv run ruff check src tests tests-e2e examples`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the viewer sim (`REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_head_tracking.py tests-e2e/test_perception.py -rs`).
