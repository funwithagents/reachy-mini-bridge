# Live tier shared fixtures.
#
# This tier is NOT collected by the default `uv run pytest` (testpaths = ["tests"]);
# run it explicitly with `uv run pytest tests-e2e`.
#
# The harness itself is shipped library code — `reachy_mini_bridge.testing` — so consumers
# of the bridge reuse it (see specs/testing/testing_support.md). The bridge dogfoods its own
# shipped harness here: we pull the `live_bridge` fixture (and the session-scoped `_live_daemon`
# it depends on — one daemon per run, one bridge session per file) in from
# `reachy_mini_bridge.testing.fixtures` rather than defining them.
# A downstream project instead opts in from its root conftest with
# `pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]`; we import the fixtures here
# because this conftest isn't the rootdir conftest. `requires_caps` / `require_env` come
# from `reachy_mini_bridge.testing` directly at each test's import site.
#
# The tier is one file per subject, each sharing a capability gate (specs/testing/testing.md):
# test_motors / test_motion (`motion`), test_audio (`audio`), test_perception (`camera`,
# then `faces`), test_head_tracking and test_custom_faces (`camera` + `faces`),
# test_sim_displays (`face_markers`). The fixtures several of them share — `face_scene`,
# `emotions_library` — are the plugin module's too.
#
# Mirror any isolation fixture the fast tier uses here — tests-e2e/ isn't a package that
# can import from tests/, so the few lines are duplicated rather than shared. (The library
# holds no process-global state today, so no reset fixture is needed yet — see
# specs/testing/testing.md.)

import pytest

from reachy_mini_bridge.testing.fixtures import (  # noqa: F401
    _live_daemon,
    emotions_library,
    face_scene,
    live_bridge,
    sim_scene,
)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Run this tier serially, overriding the ``-n auto`` the fast tier sets in
    ``addopts``: every module borrows or spawns the one daemon at
    ``REACHY_MINI_HOST:REACHY_MINI_PORT``, and xdist workers would race to spawn it,
    borrow each other's, and tear it down under one another. This conftest loads only
    when ``tests-e2e`` is among the paths collected, so the fast tier keeps its
    workers; forcing zero is harmless wherever it runs, controller or worker."""
    config.option.numprocesses = 0
