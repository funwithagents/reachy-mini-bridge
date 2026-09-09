# Split `FakeReachyMini` into `fake_reachy_mini.py`; rename `client` → `robot`

**Status:** Done

Implements the module-layout section of [specs/robot.md](../specs/robot.md). Splits the connection seam into two focused modules and renames the "client" concept (a misnomer inherited from upstream, where `ReachyMini` is itself the daemon client) to "robot". No behavior change — a file/name reorganization only.

## Scope

- `src/reachy_mini_bridge/fake_reachy_mini.py` — **new**: `FakeReachyMini` + its stand-in helpers (`_FakeDaemonClient`, `_FakeMedia`, `_FakeAudioControl`, `_FakeStatus`, `_FakeBackendStatus`) and the audio-format constants, moved out of the seam.
- `src/reachy_mini_bridge/robot.py` — **renamed** from `client.py`; now holds only the `AnyReachyMini` alias + `build_robot`, importing `FakeReachyMini` from `fake_reachy_mini.py`.
- `src/reachy_mini_bridge/api.py`, `audio.py`, `errors.py` — updated imports / doc references.
- `tests/test_robot.py` — **renamed** from `test_client.py`; trimmed to the `build_robot` seam/factory tests.
- `tests/test_fake_reachy_mini.py` — **new**: the `FakeReachyMini` behavior tests (motor state, recorded motion/media commands, capture format).
- `tests/test_audio.py`, `tests/test_api.py` — import `FakeReachyMini` from `.fake`.
- `tests-e2e/test_robot.py` (renamed from `test_client.py`), `tests-e2e/conftest.py` — import from `.robot`.
- `specs/robot.md` — **renamed** from `client.md`; title, frontmatter (`code:` = `robot.py` + `fake_reachy_mini.py`; `tests:` = `test_robot.py` + `test_fake_reachy_mini.py`), and a "Module layout" section.
- `specs/_index.md`, `specs/_overview.md`, `specs/api.md`, `specs/audio.md`, `specs/project.md`, `specs/testing.md`, `docs/reachy-mini-api.md`, `docs/running-the-sim-daemon.md`, `AGENTS.md` — link/name references retargeted.

## Steps

1. `git mv` the four files (`client.py`→`robot.py`, `client.md`→`robot.md`, and both `test_client.py`→`test_robot.py`).
2. Extract the fake + helpers + audio constants into `fake_reachy_mini.py`; reduce `robot.py` to the alias + factory importing `FakeReachyMini` from `fake_reachy_mini.py`.
3. Repoint every importer (`api.py`, `audio.py`, tests, e2e).
4. Split the seam tests: `test_robot.py` keeps `build_robot`/lifecycle; `test_fake_reachy_mini.py` gets the fake-behavior tests.
5. Update `specs/robot.md` (title, frontmatter, module-layout section) and retarget all `client.md`/`client.py` references across specs, docs, and `AGENTS.md`.

## Verification

- `uv run ruff check .` / `uv run ruff format .` clean.
- `uv run pyright` clean.
- `uv run pytest` green — including `tests/test_project_map.py`, which enforces that `robot.py` and `fake_reachy_mini.py` each appear in the `AGENTS.md` map and are governed by `robot.md`'s `code:` frontmatter, and that all frontmatter paths exist.
