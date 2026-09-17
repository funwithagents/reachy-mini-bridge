# Live tier shared fixtures.
#
# This tier is NOT collected by the default `uv run pytest` (testpaths = ["tests"]);
# run it explicitly with `uv run pytest tests-e2e`.
#
# The harness itself is shipped library code — `reachy_mini_bridge.testing` — so consumers
# of the bridge reuse it (see specs/testing_support.md). The bridge dogfoods its own
# shipped harness here: we pull the `live_api` fixture (and the module-scoped `_live_daemon`
# it depends on) in from `reachy_mini_bridge.testing.fixtures` rather than defining them.
# A downstream project instead opts in from its root conftest with
# `pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]`; we import the fixtures here
# because this conftest isn't the rootdir conftest. `requires_caps` / `require_env` come
# from `reachy_mini_bridge.testing` directly at each test's import site.
#
# Mirror any isolation fixture the fast tier uses here — tests-e2e/ isn't a package that
# can import from tests/, so the few lines are duplicated rather than shared. (The library
# holds no process-global state today, so no reset fixture is needed yet — see
# specs/testing.md.)

from reachy_mini_bridge.testing.fixtures import (  # noqa: F401
    _live_daemon,
    live_api,
    sim_scene,
)
