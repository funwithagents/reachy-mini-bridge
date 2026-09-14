# Degradable default voice

**Status:** Done

Implements `specs/api.md` ("Constructed from a config") and `specs/audio.md` ("`tts-engine` is the default adapter"): a configured `tts` block whose synthesizer cannot be built (typically its `api_key_env` unset) no longer fails `ReachyMiniApi` construction — the api comes up with **no voice**, exposes the cause as `synthesizer_error`, and `say` raises a `BridgeError` chained to it. Motivated by the host application `reachy-mini-interaction-wica`, whose "an unset TTS key degrades only `say`" contract currently forces it to build the adapter itself instead of using the config path; after this plan any host can use `reachy.tts` directly and still keep the robot up. Deliberately leaves out lazy (first-`say`) construction, retry/reload of the voice, and any change to the `tts` extra's missing-install error.

## Design decisions (settled with the user)

- **"No voice" is the existing degraded state; a failed build collapses into it.** The bridge already accepts `synthesizer=None` and has `say` raise `BridgeError` when there is nothing to speak with. A `tts` block whose adapter raises at construction is treated the same way, with the cause attached, rather than as a fatal error. The policy stays the host's: a host that wants hard failure checks `synthesizer_error` after construction and raises.
- **The missing `tts` extra stays a hard `ConfigError`.** An `ImportError` from the adapter is a setup error (nothing can fix it at runtime) and keeps today's behavior: `ConfigError` naming `reachy-mini-bridge[tts]`. Every *other* exception from the adapter's construction is recorded, not raised.
- **Known trade-off, accepted.** tts-engine raises the same `ConfigError` type for "env var unset" and for a typo in `module.type`, so the bridge cannot make the second hard while degrading the first. Both degrade to "no voice" with the cause exposed; a host surfaces it (the wica app shows it in its startup banner).
- **The build stays eager, at construction.** The adapter is still built in `__init__` (not on `__aenter__` or first `say`), so `synthesizer_error` is readable right after construction, before any connection — and `__aenter__` never touches TTS, so the daemon / robot / media lifecycle is untouched.
- **Surface.** One new read-only property, `synthesizer_error: Exception | None` (`None` when the voice built, when an explicit `synthesizer=` was passed, or when there is no `tts` block). `say`'s `BridgeError` message gains the cause when there is one ("… the configured `tts` block could not be built: <cause>"), chained with `from`. The failure is also logged at `WARNING` through the module logger.

## Scope

- `specs/api.md` — "Constructed from a config": after "The explicit `synthesizer=` keyword wins over the config's `tts` block, which is then not consumed", state the degrade rule (adapter build failure other than the missing extra ⇒ no voice, `synthesizer_error` carries the cause, `say` raises `BridgeError` chained to it; the missing extra stays `ConfigError`). Status `Implemented → Updated`, back to `Implemented` on completion.
- `specs/audio.md` — "`tts-engine` is the default adapter" bullet: the same rule from the adapter's side (its constructor resolving `api_key_env` is the usual failure). Status `Implemented → Updated → Implemented`.
- `specs/config.md` — the `tts` block's "extra must be installed" bullet gains one sentence pointing at api.md for the degrade rule. Editorial for `config.py` (which does not change), so its status stays `Implemented`.
- `specs/_index.md` — keep the two `Updated` rows in sync.
- `src/reachy_mini_bridge/api.py` — `_default_synthesizer` returns `(synthesizer, error)`; `__init__` stores both; `synthesizer_error` property; `say` message includes the cause and chains it; `logging.getLogger(__name__)` warning on a recorded failure.
- `tests/test_api.py` — the new tests below; the existing `test_tts_block_without_the_extra_is_a_config_error` stays unchanged (pins the hard case).
- `README.md` — one line in the config usage snippet: "a voice that cannot be built leaves the robot up; check `api.synthesizer_error`".
- `plans/_index.md` — this plan's row.

## Steps

1. **Specs first.** Edit `specs/api.md`, `specs/audio.md`, `specs/config.md` as scoped; set api/audio `**Status:**` to `Updated` and sync [specs/_index.md](../specs/_index.md).
2. **Tests first (red), in `tests/test_api.py`** (same `monkeypatch.setattr(api_module, "TTSEngineSynthesizer", …)` seam the neighbouring tests use):
   - `test_tts_block_build_failure_degrades_to_no_voice`: the patched adapter raises `ValueError("environment variable 'X' is unset")`; `ReachyMiniApi(ReachyMiniConfig(backend="fake", tts=block))` constructs; `api.synthesizer_error` is that exception; `async with` on the fake succeeds; `await api.say("hi")` raises `BridgeError` whose message contains `"X"` and whose `__cause__` is the recorded exception; `await api.list_emotions()` still works inside the same session (the robot is up).
   - `test_synthesizer_error_is_none_when_the_voice_builds`: with the adapter patched to a working `_ToneSynth` subclass, `synthesizer_error is None`.
   - `test_explicit_synthesizer_leaves_no_error`: `ReachyMiniApi(config_with_tts, synthesizer=_ToneSynth()).synthesizer_error is None` (the block is not consumed, so nothing can fail).
   - `test_say_without_any_synthesizer_raises_bridge_error` (existing): unchanged, and its message must **not** mention a cause (no block ⇒ nothing was attempted).
3. **Code (green).** `api.py` per Scope. Keep the `ImportError → ConfigError` branch exactly as it is.
4. **Docs.** `README.md` line.
5. **Close out.** Flip `api.md` and `audio.md` back to `Implemented` (file + index); mark this plan `Done` (file + index).

## Verification

- `uv run pytest tests/test_api.py` — the four tests above pass, including the unchanged missing-extra test.
- `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest` all clean.
- Cross-repo check (the reason for this plan): with this repo installed editable, `reachy-mini-interaction-wica`'s plan `202609141700_bridge-config-adoption.md` can put the voice under `reachy.tts` and its `test_missing_tts_key_degrades_only_the_voice` passes unchanged in its assertions.
