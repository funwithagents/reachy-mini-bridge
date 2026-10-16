# Fresh tracks first, and a detection run on its own frames

**Status:** Done

Implements the settled behavior in `specs/vision/user_perception.md` ("Tracks": the Match rule, "Fresh tracks first", "A run starts on its own frames"), `specs/testing/testing_support.md` and `specs/testing/sim_scene.md` (the `face_scene` teardown). Fixes the id churn measured on the viewer sim on 2026-10-16: a track opened on a stale frame at a detection run's start, and a turning face continuing a departed face's track parked at the image centre, each costing the head a `TRACKING_SWITCH_S` hold. Leaves out the moves beyond the gate (three of eight measured) and any compensation of the head's own motion, which needs the pose at each frame's capture; both are re-measured once this lands.

## Scope

- `src/reachy_mini_bridge/face_detection.py` — `_FaceTracks.update` sorts candidate pairs by `(track.misses > 0, distance)`; `FaceDetection` records `_tracks_since` when a run's tracks begin (at `start()` and at a detector swap) and `_poll` skips a frame whose `ts` precedes it
- `src/reachy_mini_bridge/testing/fixtures.py` — `face_scene`'s teardown waits (`_seen_empty`, up to 3 s) for the running detection to report nobody after `clear()`, before turning tracking and detection off
- `tests/test_face_detection.py` — the departed track never takes a face its fresh track continues; a face back from a miss takes its own track beside a fresh one; a run never detects on a frame from before it started
- `tests/test_bridge.py` — three tests that slept a fixed time for the first observation now wait for it (it may come one frame later)
- `specs/vision/user_perception.md`, `specs/testing/testing_support.md`, `specs/testing/sim_scene.md`, `docs/guides/custom-face-detector.md` — the rules and the teardown

## Steps

1. **The pairing.** Add `track.misses > 0` as the first sort key of the candidate pairs in `_FaceTracks.update`; nothing else in the match changes.
2. **A run's own frames.** `_tracks_since = time.perf_counter()` wherever `_tracks` is renewed for a run; in `_poll`, after the new-frame check, `return None` for `frame.ts < _tracks_since` (a poll that produced nothing: the observer is still ticked).
3. **The fixture.** `_seen_empty(bridge)` polls `bridge.faces.value` until it is inactive or empty, bounded; called between `clear()` and the two switch-offs.
4. **Tests** as in Scope; check that the two behavioural tests fail on the previous code.

## Verification

Lint, format, `pyright`, `pytest` (744 passed, three runs). The two new behavioural tests fail on the previous `face_detection.py` and pass on this one. Live: the probe of `analysis/face-world-positions.md` re-run on the viewer sim, every track's `ids followed` read for the churn left.
