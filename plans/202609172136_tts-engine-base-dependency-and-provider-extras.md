# tts-engine as a base dependency, provider extras, pocket in the dev loop

**Status:** Done

Implements the updated dependency policy in `specs/project.md` ("Runtime-dependency policy", "Optional extras") and its consequences in `specs/audio.md` ("`tts-engine` is the default adapter"), `specs/config.md` ("`tts` block") and `specs/api.md` ("Constructed from a config"). tts-engine's providers moved behind its own extras (`tts-engine[elevenlabs]`, `tts-engine[pocket]`), so the bridge's `tts` extra now installs a provider-less engine. This plan makes `tts-engine` a base dependency (its base is numpy plus a lazily imported sounddevice the bridge never triggers), replaces the `tts` extra with one extra per provider, and puts the local pocket-tts model in the dev loop so the real-TTS live test runs with no credential. It deliberately leaves the path source in place: the pinned git URL migration stays a separate step.

## Scope

- `pyproject.toml` — `tts-engine` moves from the `tts` extra to `dependencies`; extras `tts-elevenlabs = ["tts-engine[elevenlabs]"]` and `tts-pocket = ["tts-engine[pocket]"]` replace `tts`; the `dev` group pulls both provider extras, the `demo` group `tts-pocket`.
- `uv.lock` — re-resolved.
- `src/reachy_mini_bridge/api.py` — `_default_synthesizer` drops the `ImportError → ConfigError` branch (the import can no longer fail); a missing provider extra is a tts-engine `ConfigError` and degrades to no voice like any other build failure; `say`'s error message no longer names a `tts` extra.
- `src/reachy_mini_bridge/audio.py` — `TTSEngineSynthesizer` docstring: tts-engine is a base dependency, the import stays local to the adapter to keep the module cheap to import.
- `tests/test_api.py` — the "without the extra" test goes; a test pins that a tts-engine-style `ConfigError` from the adapter degrades to no voice.
- `tests-e2e/test_api.py` — the real-TTS test runs on pocket (no credential; gated on `audio` only; asserts the 24 kHz rate so the resample path is covered); the ElevenLabs test stays, key-gated, as the cloud path.
- `config.example.json`, `README.md` — the example `tts` block is a pocket block (no key); the extras table lists the two provider extras.
- `specs/project.md`, `specs/audio.md`, `specs/config.md`, `specs/api.md`, `specs/testing.md`, `AGENTS.md`, `specs/_index.md` — the policy and its consequences, statuses kept honest.
- `plans/_index.md` — this plan's row.

## Steps

1. Rewrite the dependency declarations in `pyproject.toml` and re-lock (`uv lock`, `uv sync`).
2. Simplify `_default_synthesizer` in `api.py`; update the adapter docstring in `audio.py`; adjust the unit tests.
3. Add the pocket live test and re-scope the ElevenLabs one in `tests-e2e/test_api.py`.
4. Switch the example config and README to pocket; update the extras table.
5. Update the specs and AGENTS.md; `audio.md` and `project.md` go `Updated` while the code lags, back to `Implemented` when this plan is `Done`.

## Verification

`uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest`, then `uv run pytest tests-e2e -rs` on the headless sim: the pocket test must run (not skip), the ElevenLabs test skips without a key. Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass.
