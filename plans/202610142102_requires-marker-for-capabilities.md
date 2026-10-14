# The `requires` marker for capabilities

**Status:** Done

Implements the settled behavior in `specs/testing/testing_support.md` ("Public surface": the `requires` marker, the capability vocabulary, `live_capabilities`, the `live target` summary) and `specs/testing/testing.md` ("Capabilities are probed, not assumed"). The live tier states each test's needs with `@pytest.mark.requires(...)` — per file through `pytestmark`, per test on top — gated in a `pytest_runtest_setup` hook ahead of the test's fixtures; capability names checked against one vocabulary; the run's probed set reported at the end. `requires_caps` stays as the call form. It leaves out declared capabilities (a person vouching for something the target cannot probe) and per-target profiles: `REACHY_MINI_E2E_REQUIRED_CAPS` keeps that job.

## Scope

- `src/reachy_mini_bridge/testing/support.py` — `CAPABILITIES`, `check_capability_names`, `missing_reason`; `requires_caps` checks its names
- `src/reachy_mini_bridge/testing/__init__.py` — re-exports `CAPABILITIES`
- `src/reachy_mini_bridge/testing/_daemon.py` — `required_capabilities()` rejects names outside the vocabulary
- `src/reachy_mini_bridge/testing/fixtures.py` — the hooks (`pytest_configure` registering the marker and checking the env, `pytest_collection_modifyitems` checking the marks, `pytest_runtest_setup` gating once probed, `pytest_terminal_summary`), the autouse `_requires_gate` fixture for the first probe, `live_capabilities`, the harness config shared by `live_bridge` and the probe session, `known_capabilities()`
- `tests-e2e/conftest.py` — registers the plugin through `config.pluginmanager.import_plugin` instead of importing the fixtures
- `tests-e2e/test_*.py` — `pytestmark` per file, `@pytest.mark.requires` per test in place of the in-body `requires_caps` calls; `test_custom_faces.py` probes with the camera and mic feeds
- `tests/test_testing_support.py` — functional tests of the marker, the gate and the vocabulary through `pytester`
- `docs/guides/testing.md` — the consumer guide shows the marker, the vocabulary check, the summary
- `AGENTS.md` — the `testing/` row of the project map names the marker

## Steps

1. In `support.py`: `CAPABILITIES` (the probed names plus the reserved `doa`); `check_capability_names(names, where)` raising `pytest.UsageError` naming the vocabulary; `missing_reason(caps, needs)`; `requires_caps` checks then skips.
2. `_daemon.required_capabilities()` checks its names (a `pytest.UsageError`).
3. In `fixtures.py`: factor the harness's `ReachyMiniConfig` into `_harness_config(host, port)`; `known_capabilities()` reading the per-address cache at `_daemon.address()`; `live_capabilities` (session) returning the cached set or probing on a short session of its own; the gate `_gate(needs, caps)` — skip, or `pytest.fail` when a missing name is required; `pytest_runtest_setup` (tryfirst) gating when the set is known, the autouse `_requires_gate` fixture gating through `live_capabilities` otherwise; the marker registration, the collection check and the terminal summary.
4. `tests-e2e/conftest.py` loads the plugin in `pytest_configure`; migrate every e2e file to marks; `custom_face_scene` keeps its `requires_caps` (a fixture), `live_bridge_custom_faces` probes with its feeds.
5. Fast-tier tests via `pytester`: an unknown mark name stops the run; a known-lacking capability skips before a module fixture is set up; a required one fails instead; a present one runs; before any probe the gate goes through `live_capabilities`; an unknown name in `requires_caps` and in `REACHY_MINI_E2E_REQUIRED_CAPS`; the summary prints the probed set.
6. Docs and the project map; specs back to `Implemented`, this plan `Done`.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the live tier `uv run pytest tests-e2e -rs` headless on macOS (the camera files skip by the hook, without their bridge sessions) and with `REACHY_MINI_E2E_SIM_VIEWER=1`; the PR's CI (`e2e-sim` with `REACHY_MINI_E2E_REQUIRED_CAPS`). Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass.
