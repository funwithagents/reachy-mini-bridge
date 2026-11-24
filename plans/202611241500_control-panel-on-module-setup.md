# The control panel on module-setup

**Status:** Done

Implements `specs/examples/control_panel.md` as updated: the control panel rebuilt on module-setup's Setup and Secrets panels over `reachy_mini_bridge.setup` — its config set up in the page, a session started at launch (module-setup's autostart) and from a Start button, stopped by Stop, the config locked while it runs. The controller and the Robot tab's controls are kept as they are.

## Scope

- `pyproject.toml`, `uv.lock` — `module-setup[ui]` in the `demo` group; module-setup from its `autostart` branch (back to `main` once it is merged)
- `examples/control_panel/session.py` — new: `RobotSession`
- `examples/control_panel/app.py` — `APP`, `DEFAULT_CONFIG`, `build_page(model, session)` (the Robot tab with Start / Stop, then the Setup and Secrets panels), the ticks with no session, `main` on `--folder` / `--host` / `--port` / `--start`
- `tests/test_control_panel.py` — the session, the page, the smoke test over a started session
- `README.md`, `AGENTS.md`, `docs/` — how the panel is run now
- `specs/examples/control_panel.md`, `specs/_index.md` — statuses

## Steps

1. Bump the sources; `uv lock`, `uv sync`.
2. `RobotSession`: `start` (blockers, else the controller from the saved section, started), `stop`, `locked`, `require`.
3. `app.py`: the page as three tabs; every Robot handler through `session.require()`; Start returns the controls' values from the session's config; the timers tolerate no session; `main` builds the model with `locked=session.locked`, starts at launch when `starts_at_launch` says so, stops the session when the server ends.
4. Tests, docs, statuses.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the page run once by hand on the fake.
