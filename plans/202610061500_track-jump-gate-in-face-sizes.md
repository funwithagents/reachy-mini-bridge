# Track jump gate in face sizes, and the live change recorder drained

**Status:** Done

Implements the revised "Tracks" rule of [user_perception.md](../specs/vision/user_perception.md): a face continues a track only when its centre lies within `TRACK_MAX_JUMP_FACES` face sizes of the track's last centre — in pixels, relative to the larger box side — instead of within a quarter of the frame. And repairs the live head-tracking tests' change recorder, which cancelled its subscriber before the last published report could reach it. Motivated by the two runner-only failures of the two-face tests on `main` (2026-10-05 `d656261`, 2026-10-06 `a1dd4c5`): one swung the head onto the other portrait during the hold, one recorded `engaged` as the last state although the final report read `watching`. The first failure's exact path is not pinned by the logs (a track continuing onto the other face a quarter of the frame away, or the hold already consumed by detection flicker); the gate closes the first path and the test now prints the evidence that tells them apart.

## Scope

- `src/reachy_mini_bridge/face_detection.py` — `TRACK_MAX_JUMP_FACES = 1.5` replaces `TRACK_MAX_JUMP`; `_Track` keeps its face's pixel centre and size; `_FaceTracks.update` gates candidate pairs on the distance in pixels against the larger of the two sizes.
- `specs/vision/user_perception.md` — "Tracks" states the rule and its reason.
- `tests/test_face_detection.py` — the jump test's comment; a lone face two and a half sizes away does not continue a vanished track; the gate scales with the face.
- `tests-e2e/test_head_tracking.py` — `_Changes.stop()` is async, settles 0.2 s and awaits the cancelled subscriber; the hold test prints the followed and final track ids and the recorded changes before its assertions.
- This plan, [the plans index](_index.md).

## Steps

1. The gate in face sizes: `_Track(track_id, centre_px, size_px, misses)`; pairs sorted by pixel distance where `dist <= TRACK_MAX_JUMP_FACES * max(track.size, face.size)`; `max_jump` stays an optional override for tests, now in face sizes. Fast tests pin: a 52 px jump of a 12 px face opens a new track (unchanged behaviour); a track whose face vanished is not continued by a lone face 30 px away (2.5 sizes) — the runner scenario; a 32×48 face moving 40 px keeps its id while an 8×12 one does not.
2. The spec: the "Tracks" list and the paragraph after it, with the runner measurement (two portraits 2.4 face sizes apart, 0.51 of the old normalised gate).
3. The live tests: `await changes.stop()` at the three call sites; the hold test returns and prints the evidence.
4. The gate below, then the live tier on the viewer sim; mark `Done` with the results.

## Verification

```sh
uv run pytest tests/test_face_detection.py tests/test_head_tracking.py tests/test_bridge.py
uv run ruff check . && uv run ruff format --check src tests tests-e2e examples && uv run pyright && uv run pytest && git diff --check
REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs
```

The runner itself is verified by the next pushes to `main`: the two-face tests passing, or failing with evidence naming the path.

## Completion record — 2026-10-06

The gate is `TRACK_MAX_JUMP_FACES = 1.5` on the pixel distance against the *smaller* of the track's last size and the face's (a size being a box's larger side): the first draft used the larger, and a small face landing near a large track's last place took it; the smaller size holds each face to its own scale. `_Track` keeps pixel centre and size; the frame size no longer enters the association. The live change recorder's `stop()` is async: it settles 0.2 s, cancels and awaits its subscriber; the hold test prints the followed id, the final id and the recorded changes before asserting.

- Ruff, format check, pyright: clean. Fast suite: **708 passed in 47 s** (two new association tests; the jump test's comment updated).
- Live tier, viewer sim: **30 passed, 2 skipped** (the by-design skips), 3 min 28 s. The two portraits read at normalised x −0.24 and +0.27 — 0.51 apart, the measurement in the spec; the hold held (+19.3…+19.6°, same id, no change); the switch test's changes now end on `('watching', None)`.

Not verified here: the runner itself — the next pushes to `main` tell whether the two-face tests hold there, and the hold test's evidence line names the path if one does not.
