# TTS provider extras: one alias per tts-engine extra

**Status:** Done

Implements `specs/project.md` ("Optional extras"), `specs/audio/audio.md` ("`tts-engine` is the default adapter; its providers are extras", "A `tts` block that fails to build") and `specs/core/config.md` (the `tts` block). The bridge mirrors every tts-engine provider extra and its two module bundles, names its own extra when a provider is missing, and points to tts-engine's module pages for each provider's needs. The `tts` dependency group and the live tier keep their three providers.

## Scope

- `pyproject.toml`: one `tts-<module.type>` alias per tts-engine provider (thirteen), `tts-hosted` → `tts-engine[hosted-modules]` and `tts-all` → `tts-engine[all-modules]`. tts-engine's `mcp` and `all` are not mirrored.
- `uv.lock`: tts-engine at the commit that ships `hosted-modules` / `all-modules`.
- `tests/test_tts_extras.py`: the aliases equal tts-engine's installed extras, `mcp` and `all` excepted, each bundle's `-modules` suffix dropped; every provider installs through `tts-<module.type>`.
- `src/reachy_mini_bridge/audio.py`: `TTSEngineSynthesizer` turns tts-engine's missing-provider `ConfigError` into a `BridgeError` naming `reachy-mini-bridge[tts-<type>]`, chained to the original; every other build error passes through.
- `src/reachy_mini_bridge/bridge.py`: the `_default_synthesizer` docstring.
- `tests/test_bridge.py`: a registered provider whose library is absent records that `BridgeError`, and `say` names the extra.
- `tests/test_audio.py`: the two fake tts-engine modules implement `synthesize_text`, the module method tts-engine calls since its streaming-text-input change.
- Docs: `README.md` (extras table), `docs/guides/audio.md`, `docs/reference/configuration.md`, `docs/reference/backends-and-capabilities.md`, `examples/configs/README.md`. Each states the rule and links tts-engine's module index instead of restating per-provider facts.
- Specs: `specs/project.md` (frontmatter gains the new test), `specs/audio/audio.md`, `specs/core/config.md`, `specs/_overview.md`.

## Steps

1. In tts-engine: the `hosted-modules` and `all-modules` bundles beside `all`, with their guard test. Then push to `main`.
2. `uv lock --upgrade-package tts-engine` here.
3. The aliases, the mirror test, the adapter's error, the test fakes, the docs and the specs, as scoped above.

## Verification

`uv run pytest tests/test_tts_extras.py tests/test_bridge.py tests/test_audio.py`, then the full gate: lint, format check, pyright, `uv run pytest`. Done against tts-engine `aef0762`, the merge of its module bundles: lint, format, pyright and the fast tier all pass.
