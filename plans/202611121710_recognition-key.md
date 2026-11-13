# The recognition key: recognised faces keep their tracks

**Status:** Done

Implements `specs/vision/user_perception.md` ("Tracks": "Recognised faces"; "The face report": `Face.identity`; "Custom detectors": `PixelFace.identity`). A detector that recognises people gives each face it is confident about an opaque key; the tracks veto a match between two different keys, take a face carrying its track's key first, and rescue a recognised face beyond the gate onto its held track, the key kept through frames without one and never reviving a dropped track. The users present carry their track's key (`User.identity`), and a user recognised after arriving is published once, in `recognised`. Builds on `plans/202611121632_presence-in-the-tracks.md`. Leaves out any identity held by the bridge itself.

## Scope

- `src/reachy_mini_bridge/face_detection.py` — `PixelFace.identity`, `Face.identity`, `_Track.identity`; the pairs filtered by the veto and ordered key first; the rescue; the key on new tracks and the report; the tracks' keys per update, `User.identity`, `UsersReport.recognised` published by `_Users`
- `tests/test_face_detection.py` — the veto, no veto without a key, the rescue, no resurrection, crossing, the sticky key, the key in the reports and on the user, a user recognised after arriving published once, a recognised jump over the fake's feed
- `tests/test_bridge.py` — someone stepping into the followed person's spot, with and without keys
- `tests-e2e/test_custom_faces.py` — the same on the viewer sim, with the shipped detector wrapped by a test-side recogniser (portraits enrolled from the sim's camera, keyed by correlation); the rescue stays the fake's to test, a correlation not being reliable off the place it enrolled
- `specs/vision/user_perception.md`, `specs/_index.md`; docs: the custom detector guide, the API reference

## Steps

1. The fields and the report's copy.
2. In `_FaceTracks.update`: build the pairs without vetoed ones, sorted by (not the same key, stale, distance); match; then rescue each unmatched recognised face onto the most recently seen unmatched track carrying its key.
3. The users: `_FaceTracks` exposes each held track's key after an update; `_Users` copies it onto `User.identity` and names in `recognised` a confirmed user whose key changed, publishing that report.
4. Tests, spec, docs.

## Verification

`uv run ruff check src tests tests-e2e examples`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -k custom`.
