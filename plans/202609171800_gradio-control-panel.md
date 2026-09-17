# Gradio control panel

**Status:** Done

Implements [specs/control_panel.md](../specs/control_panel.md): a Gradio app in `examples/control_panel/` that drives a `ReachyMiniApi` session from a config file — every verb a button, the api's readable state on screen — built as a gradio-free controller (tested on the `fake`) plus a thin Gradio layer. Leaves out the deferred extensions listed in the spec (mic record-and-playback, raw robot readouts, live config switching).

## Scope

- `pyproject.toml` — `demo` dependency group (`gradio>=5`, `reachy-mini-bridge[sim,tts]`), in `default-groups` with `dev`; pytest `pythonpath = ["."]`; pyright `include` gains `examples`
- `examples/control_panel/__init__.py`, `__main__.py` — package marker and `python -m` entry
- `examples/control_panel/controller.py` — `ControlPanelController`: background loop owning the api session, sync verbs, stoppable spanning verbs, `snapshot()`, mic meter
- `examples/control_panel/app.py` — `build_app(controller)` (Blocks: state panel on a timer, one control group per verb group, toasts + log) and `main(argv)` (`--config`, `--host`, `--port`)
- `tests/test_control_panel.py` — controller tests on the fake (lifecycle, verbs, snapshot, mid-flight stops) and a `build_app` smoke test (skips without Gradio)
- `AGENTS.md` — project map row for `examples/`; `README.md` — "Try it" section; `specs/_index.md` / `plans/_index.md` — rows

## Steps

1. Add the `demo` group and the tooling changes to `pyproject.toml`; `uv lock` / `uv sync`.
2. Write `controller.py`: the loop thread (`start` / `stop` / context manager, bring-up cancel on timeout or early stop), `_call` for instant verbs, the per-slot in-flight registry with `_run_spanning` / `_stop_slot` (cancel on the loop thread, then wait for the verbs to return), `snapshot()` / `PanelState`, the paced mic meter task.
3. Write `tests/test_control_panel.py` against the fake: start/stop records the session bring-up and teardown; instant verbs dispatch and errors propagate (`MotorsNotEnabledError`, unknown emotion `ValueError`); `snapshot()` follows `set_presence` / `set_breathing` / `set_wobbling` / tracking; `say` stopped mid-flight from another thread flushes the speaker and returns `False`, then a second `say` completes; `play_emotion` stopped mid-flight stops its sound and returns `False`, then a second one completes; verbs raise `BridgeError` when not running.
4. Write `app.py`: `build_app` with the state panel (timer → snapshot + frame → components, log deque), the control groups, a `_guarded` wrapper turning bridge/value errors into `gr.Error`; `main` with argparse, logging, `with controller: build_app(controller).launch(...)`.
5. Smoke-run the app on the `fake` and on `config.example.json` (sim), press the verbs, confirm the state refreshes and Stop interrupts `say` / an emotion.
6. Update `AGENTS.md` (project map), `README.md`, the two indexes; set the spec `Implemented`.

## Verification

`uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest` all green; `uv run python -m examples.control_panel` serves the panel on the fake and `--config config.example.json` brings up the sim daemon and drives it.
