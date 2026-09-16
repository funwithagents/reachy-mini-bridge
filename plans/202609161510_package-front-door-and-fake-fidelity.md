# Package front door and fake fidelity

**Status:** Done

Implements `specs/api.md` (a new "Front door" bullet) and `specs/robot.md` ("The consumed slice", "A checked slice"), clearing the no-design cleanups in [specs/_analysis.md](../specs/_analysis.md): replaces uv's generated `hello()` with a package front door, trims the uncalled `goto_target` from `FakeReachyMini`, and aligns the fake's `apply_audio_config` default with upstream — pinned by a new signature-parity test so the fake can't silently drift from upstream defaults again. Deliberately leaves out the media-session lifecycle work ([202609161520_media-session-lifecycle-hardening.md](202609161520_media-session-lifecycle-hardening.md)).

## Design decisions (settled with the user)

- **Front door.** `src/reachy_mini_bridge/__init__.py` re-exports exactly what a caller needs to drive the robot: `ReachyMiniApi`, `ReachyMiniConfig`, the `SpeechSynthesizer` contract and its shipped `TTSEngineSynthesizer` adapter, and the `BridgeError` / `MotorsNotEnabledError` / `GravityCompensationUnsupportedError` / `ConfigError` errors. [202609141536_config-and-daemon-lifecycle.md](202609141536_config-and-daemon-lifecycle.md) runs first and already replaces `hello()` with a docstring plus `ReachyMiniApi` / `ReachyMiniConfig` / `ConfigError`; this plan **extends** that `__all__` with the five remaining names rather than rewriting the file. Everything else (`MediaSession`, the conversion helpers, `FakeReachyMini`, `build_robot`, `AnyReachyMini`) stays imported from its own module. Imports are eager: `reachy_mini` is a hard base dependency that every useful path already imports, and `TTSEngineSynthesizer` imports `tts_engine` lazily in its constructor, so re-exporting it pulls in no extra. Side effect, accepted: importing `reachy_mini_bridge.testing` now imports `reachy_mini` via the package `__init__` (its `fixtures.py` already does).
- **`goto_target` is trimmed from the fake** (user decision). No v1 verb calls it, so pyright never checks it through the `AnyReachyMini` union — and a parity check confirms it has already drifted (fake `method=None` vs upstream `method=InterpolationTechnique.MIN_JERK`). The plan that builds the deferred manual movement verbs re-adds it, where a real caller type-checks it.
- **`apply_audio_config`'s `write_settle_seconds` default becomes `0.1`**, upstream's `WRITE_SETTLE_SECONDS` (`reachy_mini/media/audio_control_utils.py`).
- **A signature-parity test guards the fake.** pyright checks call compatibility, not parameter defaults — which is how the `0.5` slipped through. The new test compares each consumed member's parameter names and defaults (via `inspect.signature`) against its upstream counterpart. It lives in `tests/test_robot.py` (the seam's tests, which already import `reachy_mini`), because `tests/test_fake_reachy_mini.py` is deliberately `reachy_mini`-free. Checked before writing this plan: against the installed `reachy_mini` 1.10, every consumed member matches except the two drifts above.

## Scope

- `specs/api.md` — add a **Front door** bullet to "Core concepts / Decided" (affirmative, current-state wording: "`from reachy_mini_bridge import ReachyMiniApi` …", listing the eight re-exported names); add `src/reachy_mini_bridge/__init__.py` to its frontmatter `code:`. Status `Implemented → Updated`, back to `Implemented` on completion.
- `specs/robot.md` — "The consumed slice": the Motion / expression line lists `async_play_move` and `start_head_tracking` / `stop_head_tracking` only (drop the `goto_target` parenthetical). After "Signatures mirror the installed `reachy_mini` (1.10)", state that a parity test in `tests/test_robot.py` compares each consumed member's parameter names and defaults against upstream. Status `Implemented → Updated → Implemented`.
- `specs/_index.md` — keep both rows' Status in sync.
- `src/reachy_mini_bridge/__init__.py` — extend the existing front door (docstring + `ReachyMiniApi` / `ReachyMiniConfig` / `ConfigError`) with the five remaining re-exports in `__all__`.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — delete `goto_target`; `write_settle_seconds: float = 0.1`.
- `tests/test_robot.py` — add `test_fake_signatures_match_upstream`.
- `tests/test_fake_reachy_mini.py` — drop `goto_target` from `test_motion_commands_are_recorded`.
- `tests/test_api.py` — add a front-door test.
- `README.md` — a minimal usage snippet through the front door.
- `docs/testing-with-the-bridge.md` — import `ReachyMiniApi` from the front door in its examples.
- `specs/_analysis.md` — delete the items this plan clears.
- `plans/_index.md` — this plan's row.

## Steps

1. **Specs first.** Edit `specs/api.md` and `specs/robot.md` as scoped above; set both `**Status:**` lines to `Updated` and sync [specs/_index.md](../specs/_index.md).
2. **Parity test (red first).** In `tests/test_robot.py`, add `test_fake_signatures_match_upstream`, parametrized over a table of `(fake attribute path, upstream class, method name)`:
   - `FakeReachyMini` vs `reachy_mini.ReachyMini`: `async_play_move`, `start_head_tracking`, `stop_head_tracking`, `enable_motors`, `disable_motors`, `enable_gravity_compensation`;
   - `FakeReachyMini().media` vs `reachy_mini.media.media_manager.MediaManager`: `start_recording`, `stop_recording`, `get_audio_sample`, `get_input_audio_samplerate`, `get_input_channels`, `start_playing`, `stop_playing`, `push_audio_sample`, `get_output_audio_samplerate`, `get_output_channels`, `play_sound`, `get_frame`;
   - `FakeReachyMini().media.audio` vs `reachy_mini.media.audio_base.AudioBase`: `apply_audio_config`, `clear_player`.

   For each pair, compare `[(p.name, p.default) for p in signature.parameters.values() if p.name != "self"]` between the fake's bound method and the upstream function; on mismatch, show both lists. Lifecycle (`__enter__`/`__exit__`) stays out of the table — its parameter names differ by design and pyright covers it. Run it: it fails on `apply_audio_config` only.
3. **Fake.** Set `write_settle_seconds: float = 0.1` in `_FakeAudioControl.apply_audio_config` (parity test goes green). Delete `FakeReachyMini.goto_target`. In `tests/test_fake_reachy_mini.py::test_motion_commands_are_recorded`, drop the `goto_target` call and its assertion; the expected names become `["start_head_tracking", "async_play_move"]` (re-index the `commands[...]` assertions).
4. **Front door.** Extend `src/reachy_mini_bridge/__init__.py`: keep its docstring and existing re-exports, add `from .audio import SpeechSynthesizer, TTSEngineSynthesizer` and `from .errors import BridgeError, GravityCompensationUnsupportedError, MotorsNotEnabledError`, and grow `__all__` to the eight names.
5. **Front-door test.** In `tests/test_api.py`, add `test_package_front_door_drives_the_fake`: `import reachy_mini_bridge as rmb`; assert every name in `rmb.__all__` resolves; then `async with rmb.ReachyMiniApi("fake") as api:` assert `await api.say("hi")` raises `rmb.BridgeError` (no synthesizer) that `await api.play_emotion("happy")` raises `rmb.MotorsNotEnabledError` (the fake boots `disabled`), and — after setting `api.robot.client.kinematics_engine = "AnalyticalKinematics"` — that `await api.set_motors_state("gravity_compensation")` raises `rmb.GravityCompensationUnsupportedError` — the front-door names are the ones a caller catches.
6. **Docs.** `README.md`: add a short "Usage" section after the intro with a 5–8 line `async with ReachyMiniApi("fake") as api:` example imported from `reachy_mini_bridge`. `docs/testing-with-the-bridge.md`: switch `from reachy_mini_bridge.api import ReachyMiniApi` to `from reachy_mini_bridge import ReachyMiniApi` (leave the `FakeReachyMini` module import as is).
7. **Analysis.** Delete from [specs/_analysis.md](../specs/_analysis.md) the items this plan clears (`apply_audio_config` default, `goto_target`; the `hello()` item is already gone if the config plan ran first).
8. **Statuses.** Once verification passes, flip `specs/api.md` and `specs/robot.md` back to `Implemented` (file + `_index.md`) and set this plan to `Done` here and in [_index.md](_index.md). If the lifecycle-hardening plan also has `api.md` at `Updated`, it returns to `Implemented` only once both plans are `Done`.

## Verification

- `uv run ruff check .` and `uv run ruff format .` clean.
- `uv run pyright` clean (the fake no longer defines `goto_target`; nothing calls it, so no new errors).
- `uv run pytest` — `test_fake_signatures_match_upstream` (all parametrized cases), `test_package_front_door_drives_the_fake`, and the trimmed `test_motion_commands_are_recorded` pass; `tests/test_project_map.py` still passes with `__init__.py` added to `api.md`'s frontmatter; nothing regresses.
- `grep -rn "hello" src/` finds nothing; `grep -rn goto_target src/` finds nothing.
