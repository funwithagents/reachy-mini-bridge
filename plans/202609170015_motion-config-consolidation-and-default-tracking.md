# Consolidate wobbling into `motion`, default-on head tracking

**Status:** Done

Implements the settled behavior in `specs/config.md` (`motion` block → `MotionSettings`) and `specs/api.md` ("Attention / gaze (autonomous)", "Lifecycle"): moves the top-level `wobbling` config flag into `MotionSettings` alongside `presence` / `breathing`, adds a new `tracking` field (default `true`) to the same block, and makes the api realize that default — arming face tracking at session entry when motors already read `enabled`, or on the next `set_motors_state("enabled")` otherwise — and stopping it on exit if still on, mirroring `wobbling`. This is a breaking config-schema change (no back-compat shim): a config with a top-level `wobbling` key now fails validation.

## Scope

- `src/reachy_mini_bridge/config.py` — `MotionSettings` gains `wobbling: bool = True` and `tracking: bool = True`; `ReachyMiniConfig` drops its top-level `wobbling` field; `from_dict` validation moves accordingly.
- `src/reachy_mini_bridge/api.py` — `ReachyMiniApi` reads `config.motion.wobbling` instead of `config.wobbling`; adds `_tracking_wanted` bookkeeping (mirrors `_presence`/`_breathing`) alongside the existing `_tracking_weight`; `__aenter__` arms tracking when motors already read `enabled`; `set_motors_state("enabled")` arms it if still wanted and not yet active (no extra motor-state read — the call just confirmed it); an exit-stack callback stops tracking on exit if still on (mirrors `_disable_wobbling_if_on`); `__aexit__` resets `_tracking_wanted` to the config's value.
- `config.example.json` — `wobbling` moves into the `motion` block; `tracking` added.
- `README.md` — config JSON example, the config bullet list, and the verb table's "Motion while talking" / "Gaze" rows.
- `tests/test_config.py` — move the `wobbling` tests under `motion`; add `tracking` coverage (default, round-trip, boolean validation) alongside the existing `presence`/`breathing` tests.
- `tests/test_api.py` — update every `ReachyMiniConfig(..., wobbling=...)` construction to `motion=MotionSettings(wobbling=...)`; add tests for: tracking on by default once motors are enabled at runtime, tracking armed at entry when motors already read `enabled`, `stop_head_tracking()` suppressing a later config-driven re-arm, the `tracking` property, and tracking stopped on exit. Existing tracking-weight-sequence tests (`test_play_emotion_pauses_tracking_and_restores_it`, `test_start_head_tracking_forwards_weight`) need `motion=MotionSettings(tracking=False)` to stay isolated from the new default-on auto-arm.

## Steps

1. `config.py`: add `wobbling` / `tracking` fields to `MotionSettings`, extend its `from_dict` (`_reject_unknown_keys` allowed set, boolean validation for both). Remove `wobbling` from `ReachyMiniConfig` (dataclass field, `from_dict`'s allowed top-level keys, and the `wobbling` local variable/validation block).
2. `api.py`:
   - `__init__`: read `self._config.motion.wobbling` where `self._config.wobbling` was read (in `__aenter__`); add `self._tracking_wanted = self._config.motion.tracking`.
   - `start_head_tracking`: after the existing dispatch, set `self._tracking_wanted = True`.
   - `stop_head_tracking`: after the existing dispatch, set `self._tracking_wanted = False`.
   - `__aenter__`: after the existing `if await self.get_motors_state() == "enabled": motion.resume()`, when that condition holds and `self._tracking_wanted` is true, start tracking directly against the robot (weight `1.0`, the verb's own default) and record it on `self._tracking_weight` — bypassing the public verb's own motor-state re-check, since this call just made that read. Register an exit-stack callback (alongside the existing `_disable_wobbling_if_on` one) that stops tracking on teardown if `self._tracking_weight is not None`.
   - `set_motors_state`: in the `"enabled"` branch, after `self._require_motion().resume()`, if `self._tracking_wanted` and `self._tracking_weight is None`, start tracking the same direct way (no second motor-state poll — see specs/api.md's note on the daemon's ~0.2s status lag).
   - `__aexit__`: reset `self._tracking_wanted = self._config.motion.tracking` alongside the existing `_tracking_weight = None` reset.
3. `config.example.json` + `README.md`: relocate `wobbling`, add `tracking`, update the prose bullets and verb table rows.
4. Tests: update existing `wobbling=` config constructions; move/extend the config tests; add the new api-level tracking tests described in Scope above.

## Verification

`uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest` all pass. Mark this plan `Done` (here and in [_index.md](_index.md)) only once they do.
