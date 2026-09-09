# Rename `RobotClient` → `AnyReachyMini`; import `reachy_mini` normally

**Status:** Done

Implements the updated [specs/client.md](../specs/client.md) ("`AnyReachyMini` — a union type alias"). Renames the seam's union alias and drops the `TYPE_CHECKING`/lazy-import indirection now that `reachy_mini` is imported as the base dependency it is. No behavior change — this is a naming + import-shape refactor that fixes two readability problems: the old `RobotClient` name clashed with the robot object's own `.client` daemon-client field, and its `TYPE_CHECKING`-only `ReachyMini` import made IDE go-to-definition on a union member land only on `FakeReachyMini`.

## Scope

- `src/reachy_mini_bridge/client.py` — rename `RobotClient` → `AnyReachyMini`; import `ReachyMini` at module load (drop the `TYPE_CHECKING` guard and `build_robot`'s in-function lazy import); update `__all__` and docstring.
- `src/reachy_mini_bridge/api.py` — use `AnyReachyMini` for `self._robot`, `robot`, `raw`.
- `src/reachy_mini_bridge/audio.py` — `MediaSession` typed against `AnyReachyMini`.
- `tests-e2e/conftest.py`, `tests-e2e/test_client.py` — rename in imports and fixture type hints.
- `specs/client.md` — rename alias throughout; revise the alias/testability/fake/construction sections for the eager import; add a "Why not `RobotClient`" note.
- `specs/_overview.md`, `specs/_index.md`, `specs/audio.md`, `specs/testing.md` — rename references.
- `AGENTS.md` — project-map row for `client.py`.

## Steps

1. In `client.py`: import `from reachy_mini import ReachyMini` at top; remove the `TYPE_CHECKING` block; `type AnyReachyMini = ReachyMini | FakeReachyMini`; drop the lazy import inside `build_robot`; update `__all__` and the module docstring.
2. Propagate the rename through `api.py` and `audio.py` (the only two consumers of the alias in `src/`).
3. Propagate through `tests-e2e/` (imports + fixture type hints). `tests/` never referenced the alias by name.
4. Update the specs and `AGENTS.md` per Scope, keeping [specs/client.md](../specs/client.md) at `Implemented` since code and spec land together in this change.

## Verification

- `uv run ruff check .` and `uv run ruff format .` clean.
- `uv run pyright` clean — the renamed union resolves and `FakeReachyMini` still satisfies every call site.
- `uv run pytest` green (default tiers; `tests-e2e/` not collected). The existing `tests/test_client.py`, `tests/test_api.py`, and `tests/test_project_map.py` cover the seam and the spec-frontmatter/map invariants.
