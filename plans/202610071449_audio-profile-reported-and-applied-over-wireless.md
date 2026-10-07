# The audio profile's result reported, and applied through the daemon on a wireless robot

**Status:** Done

Implements the settled behavior in `specs/audio/audio.md` ("XVF3800 config applied on session start"): a profile that does not apply logs one warning naming the cause instead of being dropped silently, and on the `webrtc` client backend the profile is posted to the robot's daemon (`/api/audio/config/apply`) rather than looked for on the client's USB. It leaves the default profile (open question 2) and any live test of a write on hardware out.

## Scope

- `src/reachy_mini_bridge/audio.py` — `_apply_audio_profile(robot, profile)`, the backend dispatch behind `MediaSession.start`'s profile step (local `apply_audio_config` or the daemon's endpoint), returning why a profile did not apply; the warning; `_post_json`, the JSON POST it uses
- `tests/test_audio.py` — the warning on a local `False`, the webrtc request body, each daemon answer (applied, `{"applied": false}`, `503`, `500`, unreachable)
- `docs/reference/configuration.md`, `docs/guides/audio.md` — the profile's field and paragraph state the warning and the wireless path
- `specs/_index.md`, `specs/audio/audio.md` — status back to `Implemented`

## Steps

1. In `audio.py`, add `_apply_audio_profile(robot, profile) -> str | None`: on a `GstWebRTCClient` audio, `_post_json(f"{daemon_url}/api/audio/config/apply", body)` with the profile as `{"name", "values"}` items and `verify: true`; `503` → the reason "the robot's daemon found no XVF3800"; `{"applied": false}` → "a parameter was not written or did not read back"; `500` or a `URLError` → `BridgeError` with the detail; an empty `daemon_url` → a reason. Every other backend calls `media.audio.apply_audio_config(profile)` and returns a reason on `False`.
2. `MediaSession.start` runs it off the loop and, on a reason, logs `audio.xvf3800 profile not applied: <reason>; the session opens with what the chip holds`.
3. Fast tests in `tests/test_audio.py`, driving `MediaSession.start` on the fake (with `apply_audio_config` patched to `False`) and `_apply_audio_profile` on a bare `GstWebRTCClient` with `_post_json` patched, as the `stop_sound` webrtc test does.
4. Docs lines rewritten; spec status `Implemented` in the file and the index.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`. Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass.
