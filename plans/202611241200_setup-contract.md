# The setup contract

**Status:** Todo

Implements `specs/core/setup.md`: `reachy_mini_bridge.setup`, the seven functions of the module contract over the bridge's config document, the `tts` block delegated to `tts_engine.setup`, and the keys a caller holds in code (`ReachyMiniBridge(config, secrets=...)`, `specs/core/bridge.md` "Constructed from a config", `specs/audio/audio.md`). The full config inventory moves to `examples/configs/full.json`. The control panel rebuilt on the contract is a plan of its own.

## Scope

- `pyproject.toml`, `uv.lock` — tts-engine from its `setup-contract` branch (back to `main` once its pull request #47 is merged); `module-setup` in the `dev` group, from GitHub `main`
- `src/reachy_mini_bridge/fields.py` — new: tts-engine's field helper, copied unchanged
- `src/reachy_mini_bridge/setup.py` — new: the contract
- `src/reachy_mini_bridge/bridge.py`, `src/reachy_mini_bridge/audio.py` — `secrets` through to `TTSEngine`; the missing-provider hint named by the module's declared extra; `_run` typed for tts-engine's `say` returning its result
- `tests/test_setup.py` — new; `tests/test_bridge.py`, `tests/test_audio.py`, `tests/test_tts_extras.py`, `tests/test_voice_tour.py` — the stand-ins take `secrets`, the alias rule reads the declared extra, `openai-speech-compatible`
- `config.example.json` → `examples/configs/full.json`; `tests/test_config.py`, `tests/test_docs_consistency.py`, `README.md`, `AGENTS.md`, `examples/configs/README.md`, `docs/reference/configuration.md`, `specs/core/config.md`, `specs/examples/control_panel.md` — the new path
- `docs/reference/api.md` — `secrets`, and a section on the setup functions
- `specs/core/setup.md`, `specs/_index.md`, `AGENTS.md` — the spec, its row, the two modules in the map

## Steps

1. Bump the sources; `uv lock`, `uv sync`.
2. Copy `fields.py`; write `setup.py`: the schema (the `backend` field, the raw blocks, tts-engine's schema without `player`), `required_extras`, `check_config` (the loader with the `tts` block set aside, the pointer read from its message; tts-engine's problems under `/tts`), `check_setup` (the extras by import, the YuNet weights and the emotions dataset from the Hugging Face cache, then the voice's items), `prepare`, `options`, `probe` (a quiet session on a thread, then the voice's own probe played nowhere).
3. `secrets` on `ReachyMiniBridge`, its trio, `_default_synthesizer` and `TTSEngineSynthesizer`.
4. Move the inventory config and update every path naming it.
5. Tests; docs; statuses.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` — the new `tests/test_setup.py` (the conformance check over every shipped config included) and the whole fast tier.
