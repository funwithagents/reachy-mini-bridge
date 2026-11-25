# Control panel from a config file, and a config app

**Status:** Todo

Implements the settled behavior in `specs/examples/control_panel.md` ("Layout and how it runs", "The UI", "Tests"): the panel's app bare (its config is the bridge's document, in the folder and in a file), the panel run from a config file (`--config FILE`, `--env PATH`; read-only, the session started at launch), and a config app editing one file with no session. It leaves the greeter and the voice tour as they are (their own `--config`, keys from the shell).

## Scope

- `pyproject.toml`, `uv.lock` — app-setup on its `config-file` branch (back to `main` once merged)
- `examples/control_panel/app.py` — `App(..., bare=True)`, `DEFAULT_CONFIG` the bare document, `add_config_arguments` / `open_source`, `open_panel(argv)` (everything short of the web server), `main(argv)` over it
- `examples/control_panel/__init__.py` — the docstring's command line
- `examples/config_app/__init__.py`, `examples/config_app/__main__.py` — the config app: `open_model(argv)`, `main(argv)`
- `tests/test_control_panel.py` — the default config bare, a saved config bare in the folder, the panel from a file, `--no-start`, a missing file, every example config as the panel's file, the config app
- `specs/examples/control_panel.md`, `specs/_index.md`, `README.md`, `examples/configs/README.md` — the three launches, where the `.env` is, the wrapped folder of an earlier version to delete once

## Steps

1. Point app-setup at `rev = "config-file"`, `uv lock --upgrade-package app-setup`, `uv sync`.
2. `APP` bare; `DEFAULT_CONFIG = json.loads(full.json)`.
3. Replace the panel's hand-written `--folder` by `add_config_arguments(parser, APP)`; `open_source` in `open_panel`, a `ConfigFileError` to `parser.error`; `starts_at_launch` decides the start (true from a file unless `--no-start`); `main` builds the page over `open_panel`'s result and stops the session at exit.
4. `examples/config_app`: `add_config_app_arguments`, `open_config_app(options, APP)` (a `ConfigFileError` to `parser.error`), `config_page(model).launch(...)`.
5. Tests on the fake backend only (no sim from the fast tier); the existing panel tests kept over a folder model (the model still speaks the wrapped shape).
6. Docs: the spec's "Layout and how it runs", the README's "Try it from a browser", `examples/configs/README.md`.

## Verification

`uv run ruff check src tests tests-e2e examples`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`. Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass, and the spec back to `Implemented`.
