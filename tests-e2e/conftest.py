# Live tier shared fixtures.
#
# This tier is NOT collected by the default `uv run pytest` (testpaths = ["tests"]);
# run it explicitly with `uv run pytest tests-e2e`. Mirror any isolation fixture the
# fast tier uses here — tests-e2e/ isn't a package that can import from tests/, so the
# few lines are duplicated rather than shared.
