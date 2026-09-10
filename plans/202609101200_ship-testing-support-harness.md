# Ship the e2e harness as importable testing support

**Status:** Done

Implements the settled behavior in `specs/testing_support.md` — promote the bridge's live-tier harness from `tests-e2e/` scaffolding into a shipped `reachy_mini_bridge.testing` package (opt-in pytest plugin, `test` extra) so downstream consumers drive the `fake`/`sim`/`real` backends in their own tests. Delivers the shipped surface + a consumer guide; deliberately leaves the deferred consumer knobs and the optional `fake` helper (spec open questions 3–4) out.

## Scope

- `src/reachy_mini_bridge/testing/__init__.py` — replace the placeholder: re-export `live_api`, `requires_caps`, `require_env`.
- `src/reachy_mini_bridge/testing/fixtures.py` — replace the placeholder: the pytest-plugin module holding `live_api` (module-scoped `(api, caps)`), moved verbatim-in-behavior from `tests-e2e/conftest.py` (target/backend resolution, own-it-or-borrow-it daemon lifecycle, GStreamer-bundle env scrub, readiness poll, capability probing).
- `src/reachy_mini_bridge/testing/support.py` — `requires_caps` and `require_env`, moved from `tests-e2e/support.py`.
- `pyproject.toml` — add the `test` extra (`pytest` floor); add `reachy-mini-bridge[test]` to the dev group; keep `sim`/`tts` as they are.
- `tests-e2e/conftest.py` — thin to a re-export/opt-in of `reachy_mini_bridge.testing.fixtures` (bridge dogfoods the shipped harness); keep the tier's own env/isolation notes.
- `tests-e2e/support.py` — re-export from `reachy_mini_bridge.testing.support` (or delete and repoint imports).
- `docs/testing-with-the-bridge.md` — new consumer guide: backend→tier/extra table, `pytest_plugins` opt-in line, env vars, `fake` unit-test snippet.
- `README.md` — link the new guide.
- `AGENTS.md` — project-map row already added for the package; keep in sync if the internal split changes.
- `tests/test_testing_support.py` — functional tests (see Verification); add its path to `specs/testing_support.md` frontmatter `tests:`.

## Steps

1. Move `requires_caps` / `require_env` into `src/reachy_mini_bridge/testing/support.py`; re-export from the package `__init__`.
2. Move the `conftest.py` harness into the package: the daemon-lifecycle internals (target/backend resolution, own-it-or-borrow-it, readiness poll, GStreamer scrub) into a private `src/reachy_mini_bridge/testing/_daemon.py`, and the `live_api` fixture + capability probing into `fixtures.py` as a plugin module (fixtures at module top level so `pytest_plugins` picks them up).
3. Repoint `tests-e2e/conftest.py` to `pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]` and `tests-e2e/support.py` to the shipped module; confirm the bridge's own e2e tier still resolves `live_api`/`requires_caps` unchanged.
4. Add the `test` extra (`pytest>=9.1.1`, matching the dev pin) and wire `reachy-mini-bridge[test]` into the dev group.
5. Write `docs/testing-with-the-bridge.md` and link it from the README.
6. Add `tests/test_testing_support.py` (fast tier — no daemon) and record it in the spec frontmatter.
7. Flip `specs/testing_support.md` Draft → (Stable →) Implemented and this plan → Done once verified; update both `_index.md`s.

## Verification

- `tests/test_testing_support.py` (deterministic, no daemon): `requires_caps` skips on missing caps and passes when present; `require_env` skips when unset and returns the value when set; the package re-exports the three public names; importing `reachy_mini_bridge.testing.fixtures` registers `live_api` as a fixture (e.g. via a `pytester`-based check that the fixture is collectable) without spawning a daemon.
- The bridge's own live tier still runs against the shipped harness: `uv run pytest tests-e2e` behaves as before (skips cleanly with no daemon; passes headless sim where available).
- Full gate: `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest`. Mark `Done` (here and in [_index.md](_index.md)) only once all pass.
