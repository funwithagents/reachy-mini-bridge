# Setup items one by one, and a daemon's cold first start

**Status:** Done

Implements `specs/core/setup.md` as updated for app-setup's contract (every item `required` or not, `prepare` of one item with its progress), `specs/core/config.md` (`daemon.startup_timeout` 120 s) and `specs/daemon/daemon.md` (the startup-timeout message naming a first start after an install first), plus a preparable item warming GStreamer's plugin registry before a spawned daemon's first start.

## Scope

- `pyproject.toml`, `uv.lock` — `module-setup` renamed `app-setup` (dev group); app-setup and tts-engine relocked
- `src/reachy_mini_bridge/setup.py` — `required` on every item; `prepare(config, item, *, on_progress, base_dir)`; progress from the Hugging Face bars; the `media:gstreamer-registry` item and its warm-up; a voice item handed to tts-engine's `prepare`
- `src/reachy_mini_bridge/config.py`, `examples/configs/full.json` — `startup_timeout` 120 s
- `src/reachy_mini_bridge/daemon.py` — the timeout message
- `tests/test_setup.py` — the conformance check on app-setup, `required` per item, one item alone, two at once, progress fractions, failures, an unknown id, the voice item delegated, the registry warm-up
- `specs/core/setup.md`, `specs/core/config.md`, `specs/daemon/daemon.md`, `docs/reference/api.md`, `docs/reference/configuration.md`, the indexes

## Steps

1. Rename the dependency; relock app-setup.
2. Items: `required` per item; the registry item for a spawned daemon.
3. `prepare` of one item: the bridge's downloads with a `tqdm_class` hook, the registry in a child with the daemon's environment, a voice item to tts-engine.
4. The startup timeout and its message.
5. Time a cold GStreamer load in a fresh environment, before and after the warm-up.
6. Relock tts-engine once its one-item `prepare` is pushed; tests; statuses.

## Verification

`uv run ruff check src tests tests-e2e examples`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`.
