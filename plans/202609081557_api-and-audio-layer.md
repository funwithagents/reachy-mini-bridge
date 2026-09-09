# API + audio layer (`ReachyMiniApi` + media session)

**Status:** Done

Implements the settled behavior in [specs/api.md](../specs/api.md) ("v1 scope") and [specs/audio.md](../specs/audio.md) ("Core concepts / Decided"): the async-native `ReachyMiniApi` and the audio media session it depends on, built together because the two are coupled (the api is async-native *because* of audio). Delivers the v1 conversational-presence slice — talk, listen, express, follow a face, manage motors — over the existing `client.py` seam. Deliberately leaves out everything api.md defers to post-v1 (manual gaze/pose, `wake`/`sleep`, antennas, rich perception, true barge-in flush) and everything audio.md defers to hardware (XVF3800 profile tuning, full-vs-half-duplex default, DoA verbs).

## Scope

The exact files this plan touches, each with a one-line note on what changes:

- `src/reachy_mini_bridge/audio.py` — the `SpeechSynthesizer` Protocol, the `MediaSession` (owns record+play, XVF3800 config, teardown), the say sink (resample + channel fan-out → `push_audio_sample`), the mic tap (`audio_input`, `mic_sample_rate`, `mic_channels`), the barge-in flush, the public conversion helpers (`int16_to_float32`, `float32_to_int16`, `downmix_to_mono`), and the lazy `TTSEngineSynthesizer` default adapter (under the `tts` extra).
- `src/reachy_mini_bridge/api.py` — `ReachyMiniApi` async-native class: backend-string construction + `connect(...)`, `robot`/`raw` escape hatch, motor verbs (`set_motors_state`/`get_motors_state`), expression (`play_emotion`/`list_emotions`), gaze (`start_head_tracking`/`stop_head_tracking`), audio verbs delegating to `audio.py` (`say`, `play_sound`, `audio_input`, `mic_sample_rate`, `mic_channels`), and the motors-not-enabled fail-fast.
- `src/reachy_mini_bridge/errors.py` *(new)* — a tiny exception module (`BridgeError` base, `MotorsNotEnabledError`) so the api/tools failure shape is one named type; resolves the deferred error-taxonomy question (api.md open Q1 / client.md open Q3).
- `tests/test_audio.py` *(new)* — functional tests for the conversion helpers, the say sink, and the mic tap on the `fake` backend with a trivial in-test `SpeechSynthesizer`.
- `tests/test_api.py` *(new)* — functional tests for the api verbs, motor precondition, and orchestration, asserting through `api.robot` (the fake) and its recorded commands.
- `tests-e2e/conftest.py` — add a `live_api` fixture: builds a `ReachyMiniApi` against the fixture-managed daemon (no robot injection — construction stays backend-string-only per client.md) and probes capabilities through `api.robot`, mirroring `live_robot`.
- `tests-e2e/test_api.py` *(new)* — opt-in live tests over `live_api`: capability-gated (`requires_caps`) and credential-gated (`require_env`) verification of the real audio format, the mic tap, the say pipeline, and motor state against a live daemon (sim by default, real when targeted).
- `pyproject.toml` — add `samplerate` as a base runtime dependency; add a `tts` optional extra (`tts-engine`, local path dep now) and pull it into the dev group so the tts adapter is type-checked/tested.
- `specs/api.md`, `specs/audio.md`, `specs/client.md` — record the resolved error taxonomy in the relevant open questions; fill each spec's `tests:` frontmatter; flip `api.md`/`audio.md` to `Implemented` on completion.

## Steps

Ordered, buildable steps. The design ("what") is settled in the specs; this is the "how".

### 1. Dependencies + error module

1. In `pyproject.toml`: add `samplerate>=0.2` to base `dependencies` (permissive streaming resampler for the TTS path — audio.md "The robot sink"). Add `[project.optional-dependencies].tts = ["tts-engine"]` as a local path dependency (mirror how `sim` is wired), and add `reachy-mini-bridge[tts]` to the dev group so pyright/pytest see the adapter. Run `uv sync --dev`.
2. Add `src/reachy_mini_bridge/errors.py`: `class BridgeError(RuntimeError)` and `class MotorsNotEnabledError(BridgeError)`. This is the concrete answer to the deferred error-taxonomy question — a state error (not a `ValueError`, which api.md reserves for out-of-range input validation). Add `errors.py` to the Project map in AGENTS.md and name it in a spec's frontmatter (governed by api.md).

### 2. `audio.py` — conversion helpers (pure, no robot)

3. Implement the three module-level helpers exactly as audio.md "Conversion helpers" specifies:
   - `int16_to_float32(pcm)` → `pcm.astype(float32) / 32768.0`.
   - `float32_to_int16(pcm)` → `(clip(pcm, -1, 1) * 32767).astype(int16)`.
   - `downmix_to_mono(pcm, channels)` → average across the channel axis of interleaved/2-D input; passthrough when `channels == 1`.
   These are pure numpy and fully unit-testable with no robot — do them first so the sink/tap build on tested primitives.

### 3. `audio.py` — `SpeechSynthesizer` Protocol + `MediaSession`

4. Define the `SpeechSynthesizer` `Protocol` verbatim from audio.md: `sample_rate: int` property and `stream(text) -> AsyncIterator[np.ndarray]` yielding **float32 mono `[-1, 1]`, shape `(n,)`**.
5. Implement `MediaSession(robot)` bound to a `RobotClient`:
   - `async def __aenter__`: `start_recording()` + `start_playing()`, then `media.audio.apply_audio_config(<shipped default profile>)`; `__aexit__`: `stop_recording()` + `stop_playing()`. Opened once per connection, shared by say and mic (audio.md "One media session").
   - **say sink** — `async def say(text, synth)`: iterate `synth.stream(text)`; per chunk, resample `synth.sample_rate → media.get_output_audio_samplerate()` via a stateful `samplerate.Resampler(channels=1)` **skipped when rates already match**, fan mono → `media.get_output_channels()` (copy, only when `> 1`), then `media.push_audio_sample(...)`. Keep the resampler behind an internal `resample_to_16k(chunks, src_rate)` helper so the backing lib stays swappable. Read rates/channels from the getters — never hardcode.
   - **mic tap** — `def audio_input(*, mono=True) -> AsyncIterator[bytes]`: loop `media.get_audio_sample()`; when `mono` and `get_input_channels() > 1`, `downmix_to_mono(...)`; always `float32_to_int16(...)`; yield `.tobytes()` (int16 LE). `mono=False` skips the downmix and yields interleaved. Expose `mic_sample_rate` (`get_input_audio_samplerate()`) and `mic_channels` (`get_input_channels()`) as properties.
   - **barge-in** — `def clear_player()` → `media.audio.clear_player()` (deferred `clear_output_buffer()` fallback noted, not required for v1).
6. Implement the lazy `TTSEngineSynthesizer` default adapter: import `tts-engine` **inside** the constructor (so core install without the `tts` extra never imports it); adapt its int16 native output to the float32 contract via `int16_to_float32`; default its config to 16 kHz-native so the sink resamples nothing. `say` raises a clear `BridgeError` if no synthesizer is given and the extra isn't installed.

### 4. `api.py` — `ReachyMiniApi`

7. `__init__(self, backend="real", *, synthesizer=None, **opts)`: call `build_robot(backend, **opts)`, store `self._robot: RobotClient`; expose `robot`/`raw` properties (the escape hatch). Add a `connect(...)` classmethod/contextmanager convenience mirroring it. Hold a `MediaSession` and the optional default synthesizer.
8. Motors: `get_motors_state()` reads `robot.client.get_status().backend_status.motor_control_mode` — **guard `backend_status is None`**, and coerce the real `str`-Enum to a plain `str` (fake already returns `str`). Map to `"enabled"|"disabled"|"gravity_compensation"`. `set_motors_state(state)` dispatches to `enable_motors`/`disable_motors`/`enable_gravity_compensation`; unknown state → `ValueError`. No auto-toggling, no state set on connect (api.md Motors).
9. A private `_require_motors_enabled()` that raises `MotorsNotEnabledError` when `get_motors_state() != "enabled"`; call it at the top of `play_emotion` and `start_head_tracking` (the two verbs that move the robot).
10. Expression: build `RecordedMoves` **lazily, once per connection**, cached, off-loop via `asyncio.to_thread` (api.md "Emotions library"). `list_emotions()` enumerates names; `play_emotion(name)` resolves the move and `await robot.async_play_move(...)`. On `fake`/offline, stub the emotions source (no HF access).
11. Gaze: `start_head_tracking(weight=...)` / `stop_head_tracking()` wrap the daemon tracker (blocking upstream calls via `asyncio.to_thread`).
12. Audio verbs delegate to the `MediaSession`: `say(text, synth=None)` (default to the configured synthesizer), `play_sound(...)`, `audio_input(mono=True)`, and the `mic_sample_rate`/`mic_channels` passthrough properties. Blocking upstream calls (`goto_target`, `play_sound`) run under `asyncio.to_thread`.

### 5. Tests

13. `tests/test_audio.py`:
    - Helpers: `int16_to_float32`/`float32_to_int16` round-trip within tolerance; clipping past `[-1, 1]` saturates to int16 max; `downmix_to_mono` averages a known stereo chunk to the expected mono values and is a passthrough at 1 channel.
    - say sink: a trivial `SpeechSynthesizer` emitting a known float32 tone; assert the fake recorded `media.push_audio_sample` with the expected total frame count and channel fan-out. Add a case where `synth.sample_rate != 16 kHz` to exercise the resampler (assert output frame count matches the ratio, no exception across chunk boundaries).
    - mic tap: drive `audio_input()` over the fake's synthetic stereo capture; assert `mono=True` yields int16 mono bytes of the expected length and `mic_channels == 2`; `mono=False` yields interleaved. Assert `break` stops iteration (the tap is a drainable async iterator).
14. `tests/test_api.py`:
    - Motor precondition: `play_emotion`/`start_head_tracking` raise `MotorsNotEnabledError` when disabled; succeed and record the expected upstream command once `set_motors_state("enabled")`. `get_motors_state()` reflects each transition.
    - `set_motors_state` dispatch: each state hits the matching upstream setter (assert via `robot.commands`); bad state → `ValueError`.
    - `say`/`play_sound`/`audio_input` reach the media layer (assert recorded media commands); the escape hatch `api.robot` is the `FakeReachyMini`.
    - `list_emotions`/`play_emotion` against the offline-stubbed emotions source.

### 6. E2E coverage (live tier)

These run only when a live target is reachable and skip cleanly otherwise (testing.md), so they are **not** part of the `Done` gate — but they must type-check (pyright includes `tests-e2e/`), and they are the real-world check on the assumptions the `fake` tier encodes.

15. Add a `live_api` fixture to `tests-e2e/conftest.py`: reuse `_live_daemon` for `(host, port)`, build `ReachyMiniApi(backend="real", connection_mode="network", host=host, port=port, media_backend="local")`, probe capabilities via `_probe_capabilities(api.robot)`, and yield `(api, caps)`. This keeps one connection (the api's own) and respects the no-injection design. Ensure the probe and the api's `MediaSession` don't fight over `start_recording` (probe around the session, or open the session lazily).
16. Add `tests-e2e/test_api.py` over `live_api`:
    - **Real audio format (closes audio.md open Q1).** Gated `audio`: assert `api.mic_sample_rate == 16000` and `api.mic_channels == media.get_input_channels()`, and that a raw `get_audio_sample()` is `float32` with that channel count — i.e. the numbers the `fake` hardcodes actually match a live daemon. This is the highest-value e2e test: it validates the very assumption the fast tier can't.
    - **Mic tap.** Gated `audio`: drain a few chunks of `api.audio_input()`, assert each is non-empty `bytes` whose length is a whole number of int16 mono frames; `break` stops it.
    - **say pipeline.** Gated `audio`, credential-free: route a **trivial in-test tone `SpeechSynthesizer`** (float32 mono @ 16 kHz) through `api.say(...)`; assert it completes without error (the daemon accepted the pushed samples). A `TTSEngineSynthesizer` variant can be added later gated on the adapter's credentials via `require_env(...)`.
    - **Motor state.** Gated `motion`: `api.get_motors_state()` returns one of the three values and reflects a `set_motors_state(...)` round-trip where the target permits it.
    - **play_emotion (dataset-permitting).** Gated `motion`: `list_emotions()` returns names; `play_emotion(name)` with motors enabled records a move — skip if the emotions dataset isn't in the local HF cache (the sim daemon launches `--no-preload-datasets`), so it never triggers a network download in the tier.

### 7. Wrap-up

17. Fill `tests:` frontmatter in api.md (`tests/test_api.py`, `tests-e2e/test_api.py`) and audio.md (`tests/test_audio.py`, `tests-e2e/test_api.py`); add `errors.py` to api.md's `code:` and the AGENTS.md Project map. The `live_api` fixture change is governed by testing.md.
17a. Consistency rename (surfaced by this plan's naming): `git mv tests-e2e/test_client_sim.py tests-e2e/test_client.py` so the live tier parallels the fast tier by directory, not a misleading `_sim` suffix (both tiers are target-agnostic — sim by default, real when targeted). Update its module docstring and any frontmatter reference in the same move.
18. Resolve the error-taxonomy open questions: api.md open Q1 and client.md open Q3 now point at `MotorsNotEnabledError` (edit both to record the decision rather than defer it). Close audio.md open Q1 once step 16's format test has actually run against a target (leave it open, annotated "pending live confirmation", until then).
19. Run the full verification gate; flip `api.md` and `audio.md` `Stable → Implemented` (spec `**Status:**` lines + [specs/_index.md](../specs/_index.md) rows) and this plan `Todo → Done` (here + [plans/_index.md](../plans/_index.md)).

## Verification

- New tests: `tests/test_audio.py`, `tests/test_api.py` (functional, `fake` backend, no network) as above, plus the existing `tests/test_project_map.py` staying green after the frontmatter/map edits.
- Full gate, all must pass before `Done`:
  ```
  uv run ruff check .
  uv run ruff format .
  uv run pyright
  uv run pytest
  ```
- The `tests/` tier must remain daemon-free and offline: no `reachy_mini` import on the `fake` path and no `tts-engine` import unless a test constructs the adapter (mirror `tests/test_client.py`'s fresh-interpreter leak check if useful for the tts lazy-import guarantee).
- **E2E tier (not part of the `Done` gate, but must type-check and should be run for live validation):**
  ```
  uv run pytest tests-e2e/test_api.py            # sim (default target)
  REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e/test_api.py   # headfull sim
  ```
  Every test skips cleanly when its capability isn't probed or its credentials are unset, so the tier is safe to run with whatever target/keys are on hand. Running step 16's format test against at least the sim target is what lets audio.md open Q1 move from "pending live confirmation" to closed.
