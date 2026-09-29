# `tts-gradium` extra

**Status:** Done

Implements the settled behavior in `specs/project.md` ("Optional extras") and `specs/audio/audio.md` ("The robot sink"): tts-engine gained a `gradium` provider behind its own `gradium` extra, and the bridge's provider extras mirror tts-engine's one-to-one, so a `tts-gradium` extra joins `tts-elevenlabs` / `tts-pocket`. Its live test runs the provider at 16 kHz, the speaker rate, so the say sink's no-resample path is covered on real audio (the other two providers' rates cover the resample path). No bridge code changes: the adapter carries the block verbatim and the sink already skips the resample at a matching rate.

## Scope

- `pyproject.toml` — `tts-gradium = ["tts-engine[gradium]"]`; the dev group pulls it alongside the other two so the key-gated live test runs rather than failing on a missing extra.
- `uv.lock` — the `gradium` SDK enters the dev resolution.
- `tests-e2e/test_api.py` — `test_say_with_gradium_speaks_through_the_robot`: key-gated on `GRADIUM_API_KEY`, asserts the synthesizer reports 16 kHz (the no-resample path) and that `say` completes.
- `specs/project.md`, `specs/audio/audio.md`, `specs/core/config.md`, `specs/testing/testing.md`, `specs/_overview.md`, `specs/core/bridge.md` — name the third extra; `audio.md` states the rule plainly — a synthesizer at 16 kHz is played as is, any other rate is resampled — with each provider's rate.
- `README.md`, `AGENTS.md`, `src/reachy_mini_bridge/audio.py` (adapter docstring) — the extras table, a `gradium` config example, the credential list.

## Steps

1. Add the extra and widen the dev group; `uv lock` and `uv sync --dev`.
2. Add the key-gated live test next to the ElevenLabs one.
3. Update every place that enumerates the provider extras (the Scope list).

## Verification

`uv run ruff check .`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest`; then `uv run pytest tests-e2e -rs -k gradium` on the headless sim: runs with `GRADIUM_API_KEY` set, skips without it.
