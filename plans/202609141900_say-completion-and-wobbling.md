# Say completion, cancel flush, and head wobbling

**Status:** Done

Implements `specs/audio.md` ("`say` completes when the utterance has been heard, and exits silent"; "Head wobbling — audio-reactive motion on the speaker path"), `specs/api.md` (the `say` guarantees; "Audio-reactive motion (head wobbling)"; the wobbling steps in "Lifecycle"), `specs/config.md` (the top-level `wobbling` flag) and `specs/robot.md` (the wobbling members of the consumed slice). It fixes the defect analysed in [specs/_tts-bug.md](../specs/_tts-bug.md) — `say` returns as soon as the audio is *queued* and cancelling it leaves the robot talking — and adds upstream's audio-reactive head wobbling — on by default through a config flag applied on entry — plus a `set_wobbling` verb. It deliberately leaves out a `stop_talking()` verb (still deferred in api.md: it needs the api to track its in-flight `say` task) and any suppression of wobbling during `play_emotion` (api.md open question 5; v1 composes).

## Design decisions (settled with the user)

- **Completion is a wall-clock estimate, not a GStreamer position.** `push_audio_sample` queues without blocking (upstream's `appsrc` has `block` unset; `_appsrc_pts` is bookkeeping only), so the sink keeps its own end-of-playback estimate: each non-empty push extends the end from `max(end, now)` by `frames / out_rate`, and `say` sleeps until `end + _PLAYBACK_TAIL_S` (0.1 s: the sink's 50 ms ring buffer plus device latency). `max(end, now)` handles a synthesizer slower than real time (the queue drains, the next chunk starts *now*), the same re-anchoring upstream applies to buffer timestamps after a gap. No private SDK state, so it is identical on the GStreamer, WebRTC and fake backends. The bug note's alternative (reading `_appsrc_pts` against the pipeline clock) is rejected for those two reasons.
- **Flush on any early exit, not only on cancel.** `say` wraps the push loop and the wait in one `try`; on `BaseException` (a `CancelledError`, a synthesizer error, an interpreter exit) it calls `clear_player()` and re-raises. The invariant is simple to state and rely on: `say` either plays the whole utterance or stops the speaker before it returns control. `clear_player()` also resets the wobbler, so a cancelled utterance leaves the head at neutral.
- **The fake keeps the timing.** `say` on `FakeReachyMini` takes the utterance's duration too — consumers (e.g. the interaction app's turn-taking tests) see real completion behavior on the fake. The existing `tests/` say tests use 0.15 s utterances and simply get slower by ~0.25 s each; no test patches `asyncio.sleep`.
- **Wobbling is a top-level config flag, toggled at the api layer.** `enable_wobbling` / `disable_wobbling` are `ReachyMini` methods (not `media.*`), so `ReachyMiniApi` dispatches them; the flag is the top-level `wobbling` (next to `backend`), not an `audio` key, because it configures a behavior of the api session as a whole rather than the media pipeline. `set_wobbling` needs no motors (a mode, not a move: the daemon composes the offsets regardless of torque, and enabling on a resting robot is the natural "sway whenever you talk" declaration). The `wobbling` property is the bridge's own record — upstream has no getter.
- **On by default.** `wobbling` defaults to `true`: a robot that talks sways its head while it talks, whatever the synthesizer — real TTS and a test tone behave alike. `wobbling: false` leaves the mode untouched for a caller that wants the head still. Measured on the sim, the head returns to neutral on its own after the audio ends (about a second, following the motors' dynamics), so leaving the mode on between utterances does not leave the head tilted.
- **Off at exit whenever on.** The daemon-side switch is shared state across clients, so exit disables wobbling if the bridge has it on (from the flag or a runtime `set_wobbling(True)`), before the media session closes and the robot disconnects.

## Relationship to the other `Todo` plans

Independent of both and kept separate (each is a verifiable unit with its own `Done`), with two merge points:

- [202609161520_media-session-lifecycle-hardening.md](202609161520_media-session-lifecycle-hardening.md) also edits `MediaSession.say` (it adds a `_require_open("say")` guard at the top and moves the session onto an `AsyncExitStack`). **Planned order: this plan first** (it is the user-visible fix the interaction app is blocked on), the lifecycle plan second — it then adds its guard as the first line of `say`, above the tracker/try block. The lifecycle plan's `test_say_requires_an_open_session` and this plan's tests do not overlap.
- [202609161510_package-front-door-and-fake-fidelity.md](202609161510_package-front-door-and-fake-fidelity.md) adds a fake↔upstream signature-parity table. Whichever lands second adds `enable_wobbling` and `disable_wobbling` to the `FakeReachyMini` vs `reachy_mini.ReachyMini` rows.

`api.md`, `audio.md` and `robot.md` return to `Implemented` only once every plan holding them at `Updated` is `Done`.

## Scope

- `src/reachy_mini_bridge/config.py` — `ReachyMiniConfig.wobbling: bool = True`; `from_dict` accepts the top-level key and rejects a non-boolean with `ConfigError` naming `wobbling`.
- `config.example.json` — a top-level `"wobbling": true`.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — `enable_wobbling()` / `disable_wobbling()` recording `("enable_wobbling", {})` / `("disable_wobbling", {})`.
- `src/reachy_mini_bridge/audio.py` — `_PLAYBACK_TAIL_S`, `_PlaybackTracker`, `_push` returns the pushed frame count, `say` awaits the tracker and flushes on early exit.
- `src/reachy_mini_bridge/api.py` — `set_wobbling(enabled)`, `wobbling` property, the entry/exit steps.
- `tests/test_config.py`, `tests/test_fake_reachy_mini.py`, `tests/test_audio.py`, `tests/test_api.py` — functional tests below.
- `tests-e2e/test_api.py` — a live completion-time check, a live "the head sways by default" check, and a live "wobbling off keeps the head still" check.
- `README.md` — the API table row, the "Talking" paragraph, and the `wobbling` config key.
- `specs/_analysis.md`, `specs/_tts-bug.md`, `specs/_index.md`, `plans/_index.md` — bookkeeping.

## Steps

1. **Specs.** Done alongside this plan: `audio.md`, `api.md`, `config.md`, `robot.md` carry the design above and sit at `Updated` (file + [specs/_index.md](../specs/_index.md)).

2. **Config.** In `ReachyMiniConfig`, add `wobbling: bool = True` (last field); in `from_dict`, add `"wobbling"` to the allowed top-level keys, read it with default `True`, raise `ConfigError("'wobbling' must be a boolean")` unless `isinstance(value, bool)`, and pass it to the constructor. Add a top-level `"wobbling": true` to `config.example.json` (after `audio`). Tests in `tests/test_config.py`:
   - `test_wobbling_defaults_on_and_round_trips`: `ReachyMiniConfig.from_dict({}).wobbling is True`; `from_dict({"wobbling": False}).wobbling is False`; `test_defaults` gains the `wobbling is True` assertion.
   - `test_wobbling_must_be_a_boolean`: `{"wobbling": "yes"}`, `{"wobbling": 1}` and `{"wobbling": null}` raise `ConfigError` whose message names `wobbling`.
   - `test_from_json_file_round_trips_the_repo_example` keeps passing with the new key (update its expected `AudioSettings` if it compares field-by-field).

3. **Fake.** Add `enable_wobbling(self) -> None` and `disable_wobbling(self) -> None` to `FakeReachyMini` (signatures mirror upstream: no parameters), each appending its command. In `tests/test_fake_reachy_mini.py`, add `test_wobbling_toggles_are_recorded`: after `enable_wobbling()` then `disable_wobbling()`, `[name for name, _ in robot.commands] == ["enable_wobbling", "disable_wobbling"]`.

4. **Audio — completion and flush.** In `audio.py`:
   - `_PLAYBACK_TAIL_S = 0.1` (module constant, documented as the sink buffer + device latency margin).
   - `class _PlaybackTracker` with `__init__(self, sample_rate: int, *, now: Callable[[], float] = time.monotonic)`, `push(self, frames: int) -> None` (no-op for `frames <= 0`; else `self._end = max(self._end, now()) + frames / sample_rate`, tracking whether anything was pushed), and `remaining(self) -> float` (`0.0` when nothing was pushed, else `max(0.0, self._end + _PLAYBACK_TAIL_S - now())`).
   - `_push` returns `int(mono.size)` (0 for an empty chunk) after pushing.
   - `say`: build the tracker from `out_rate`; wrap the loop in `try:` — each `tracker.push(self._push(...))`, the flush push likewise, then `remaining = tracker.remaining()` and `await asyncio.sleep(remaining)` only when `remaining > 0` — `except BaseException: self.clear_player(); raise`. Keep the docstring current: completion + early-exit flush.

   Tests in `tests/test_audio.py` (fake clock = a closure over a mutable float):
   - `test_playback_tracker_accumulates_contiguous_pushes`: rate 16000, clock at 100.0; `push(8000)` twice → `remaining() == pytest.approx(1.0 + 0.1)`.
   - `test_playback_tracker_reanchors_after_the_queue_drains`: `push(1600)` at 0.0, clock to 5.0, `push(1600)` → `remaining() == pytest.approx(0.1 + 0.1)`.
   - `test_playback_tracker_is_zero_when_nothing_was_pushed`: `push(0)` → `remaining() == 0.0`.
   - `test_say_returns_only_after_the_utterance_has_played`: `_ToneSynth(16000, chunks=4, block=800)` (0.2 s); `time.monotonic()` around `session.say`; `elapsed >= 0.2`.
   - `test_say_with_no_audio_returns_at_once`: a synth yielding nothing; `elapsed < 0.05`; no `media.push_audio_sample` and no `audio.clear_player` recorded.
   - `test_cancelled_say_flushes_the_speaker`: a `_StallingSynth` that yields one chunk then awaits an `asyncio.Event` never set; run `session.say` as a task, poll until a `media.push_audio_sample` is recorded, `task.cancel()`, `pytest.raises(asyncio.CancelledError)` on `await task`; `audio.clear_player` is recorded after the push.
   - `test_cancel_during_the_completion_wait_flushes_the_speaker`: `_ToneSynth` (0.2 s); start the task, poll until 4 pushes are recorded (synthesis done, `say` now sleeping), cancel; `audio.clear_player` recorded.
   - `test_synthesizer_failure_flushes_the_speaker_and_propagates`: a synth that yields one chunk then raises `RuntimeError("boom")`; `pytest.raises(RuntimeError)`; `audio.clear_player` recorded.

5. **Api — wobbling.** In `api.py`:
   - `self._wobbling = False` in `__init__`; `wobbling` property returns it.
   - `async def set_wobbling(self, enabled: bool) -> None`: `await asyncio.to_thread(self.robot.enable_wobbling if enabled else self.robot.disable_wobbling)`; then `self._wobbling = enabled`. No motor check.
   - `__aenter__`: after the media session is entered, `stack.push_async_callback(self._disable_wobbling_if_on)`; then `if cfg.wobbling: await self.set_wobbling(True)`. `_disable_wobbling_if_on` calls `set_wobbling(False)` only when `self._wobbling` is true. Registering the callback before the enable means a failing `enable_wobbling` still unwinds cleanly (the flag is still `False`, so the callback is a no-op). `__aexit__` resets `_wobbling` to `False` after the stack closes (defensive: the callback already did).

   Tests in `tests/test_api.py` (fake backend, `asyncio.run`):
   - `test_set_wobbling_dispatches_and_tracks_state`: `api.wobbling is True` after a default entry; `set_wobbling(False)` records `disable_wobbling` and reads `False`; `set_wobbling(True)` records `enable_wobbling` and reads `True`.
   - `test_set_wobbling_needs_no_motors`: the fake boots `disabled`; `set_wobbling(True)` succeeds while `play_emotion("happy")` in the same session raises `MotorsNotEnabledError`.
   - `test_wobbling_is_on_by_default_at_entry_and_off_at_exit`: `ReachyMiniApi("fake")` (the default config); after the block, the command order is `media.start_playing` < `enable_wobbling` < `disable_wobbling` < `media.stop_recording` < `__exit__`.
   - `test_wobbling_off_in_the_config_is_never_touched`: `ReachyMiniConfig(backend="fake", wobbling=False)`, a `say`, no `set_wobbling` call → neither toggle is recorded.
   - `test_wobbling_enabled_at_runtime_is_disabled_at_exit`: `wobbling=False` config, `set_wobbling(True)` inside the block → `disable_wobbling` recorded before `__exit__`, and `api.wobbling is False` afterwards.
   - `test_wobbling_property_is_false_outside_a_session` and `test_set_wobbling_requires_entry` (`BridgeError`, via the `robot` guard).

6. **Live tier** (`tests-e2e/test_api.py`, sim or real):
   - `test_say_completes_after_the_utterance_has_played` (`requires_caps(live_api, "audio")`): size the file's `_ToneSynth` to 1.0 s of audio; `asyncio.run(api.say(...))` takes `>= 1.0` s.
   - The shared `live_api` fixture builds its api from a default config, so wobbling is on for every live test — the real-TTS test sways the head too. Both wobbling tests read `api.robot.get_current_head_pose()` before, then run `say` of a 1.5 s tone at amplitude 0.25 (≈ −12 dBFS, well above the −35 dBFS VAD-on) as a task while sampling the pose every 50 ms, and measure the largest rotation deviation from the start pose with `reachy_mini.utils.interpolation.delta_angle_between_mat_rot`. On the sim the motors are always enabled, so the sway is observable.
   - `test_wobbling_is_on_by_default_and_sways_the_head` (`requires_caps(live_api, "audio", "motion")`): `api.wobbling is True` with no toggle; the peak deviation exceeds 1°; then, **with wobbling still on**, the pose returns within 1° of the start (polled to a 3 s deadline) — the natural return to neutral.
   - `test_wobbling_off_keeps_the_head_still_while_audio_plays` (same caps): `set_wobbling(False)`; the peak deviation stays below 0.5° (the sway peaks around 11° on the sim); a `finally` restores `set_wobbling(True)` so the module-scoped fixture keeps its default.

7. **Docs.** `README.md`: add an API-table row `| Motion while talking | \`set_wobbling(enabled)\`, \`wobbling\` — upstream's audio-reactive head sway; on at entry with the config's \`wobbling\` flag |`; extend the "Talking" paragraph with "`say` returns once the utterance has finished playing; cancel the task to stop it (queued audio is flushed)"; add `"wobbling": true` to the config example and a `wobbling` bullet (on by default; `false` keeps the head still) to the config-keys list. [docs/reachy-mini-api.md](../docs/reachy-mini-api.md) already carries the mechanism notes.

8. **Bookkeeping.** Delete the "`say` completion and barge-in" section from [specs/_analysis.md](../specs/_analysis.md). Set [specs/_tts-bug.md](../specs/_tts-bug.md)'s `**Status:**` to `Fixed by plans/202609141900_say-completion-and-wobbling.md`. Once verification passes, flip `audio.md`, `api.md`, `config.md`, `robot.md` back to `Implemented` (file + [specs/_index.md](../specs/_index.md)) — subject to the other plans' rule above — and set this plan to `Done` here and in [_index.md](_index.md).

## Implementation notes

- **The exit callback holds the robot.** `__aexit__` clears `self._robot` before closing the stack, so `_disable_wobbling_if_on` receives the robot captured at entry and calls its `disable_wobbling` directly rather than going through `set_wobbling` (whose `self.robot` guard would raise). An extra fast test pins that a failing `enable_wobbling` at entry unwinds the session without a `disable_wobbling`.
- **The live return-to-neutral check polls.** On the sim the sway peaks around 11° and outlasts `say` by a few hundred milliseconds; the head then converges on the motors' own dynamics — under 1° about a second after the audio ends, under 0.1° after about three, whether wobbling is left on or disabled. The test polls the pose to a 3 s deadline and keeps the 1° bound.

## Verification

- `uv run ruff check .` and `uv run ruff format .` are clean (the `except BaseException: ...; raise` re-raises, so no `BLE001`).
- `uv run pyright` is clean: `set_wobbling` type-checks `enable_wobbling` / `disable_wobbling` against both members of `AnyReachyMini`, and `tests/test_project_map.py` still passes (no new modules; the example JSON is already in `config.md`'s frontmatter).
- `uv run pytest`: every test in steps 2–5 passes; the pre-existing say tests still pass (now ~0.25 s slower each); nothing else regresses.
- Live check: `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e` against the headful sim passes, step 6's three tests included.
- `grep -n "_open\b" src/reachy_mini_bridge/audio.py` is unaffected by this plan (owned by the lifecycle-hardening plan).
