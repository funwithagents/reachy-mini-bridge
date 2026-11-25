# A config file's relative paths are the file's

**Status:** Done

Implements `specs/core/config.md` "Relative paths" as updated: `ReachyMiniConfig.from_json_file(path)` and `ReachyMiniBridge.from_json_file(path)` default `base_dir` to the file's own folder, as tts-engine's, wica's and asr-engine's file loaders do; an explicit `base_dir=` wins; `from_dict` / `from_json` keep the working directory.

## Scope

- `src/reachy_mini_bridge/config.py`, `src/reachy_mini_bridge/bridge.py` — the default, `base_dir` on the bridge's `from_json_file`
- `tests/test_config.py` — a relative scene and voice file found next to the file from another working directory; an explicit `base_dir` wins
- `specs/core/config.md`, `docs/reference/api.md`, `docs/reference/configuration.md` — the rule

## Steps

1. The default in `from_json_file`; the bridge's pass-through.
2. Check every caller (examples, tests, docs): none relies on a relative path against the working directory.
3. Tests, docs, statuses.

## Verification

`uv run ruff check src tests tests-e2e examples`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`.
