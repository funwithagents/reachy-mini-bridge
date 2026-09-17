---
code:
  - examples/control_panel/__init__.py
  - examples/control_panel/__main__.py
  - examples/control_panel/controller.py
  - examples/control_panel/app.py
tests:
  - tests/test_control_panel.py
---

# Control panel (`examples/control_panel`)

**Status:** Implemented

## Purpose

A Gradio web app for driving a robot by hand through [api.md](api.md)'s `ReachyMiniApi`: every verb has a button, and the api's readable state is on screen and kept fresh. It is a **manual test and demo tool**, not part of the shipped package — it lives in `examples/`, installs through the `demo` dependency group, and is started from a [config.md](config.md) file so the same panel drives the real robot, the simulator, or the offline fake by pointing it at a different config.

Besides being the quickest way to exercise a verb without writing code, it is the first consumer of the sync↔async bridging that [tools.md](tools.md) plans (a background event loop that sync callables submit to), and it makes the "Cancellation" contract of [api.md](api.md) something a person can press: a Stop button next to `say` and `play_emotion`.

## Core concepts / Decided

### Layout and how it runs

```
examples/control_panel/
  __init__.py      # package marker
  __main__.py      # `python -m examples.control_panel`
  controller.py    # ControlPanelController — gradio-free, owns the api session
  app.py           # build_app(controller) -> gr.Blocks, and main(argv)
```

- **Two layers, one gradio-free.** `controller.py` imports the bridge and the standard library only, so `tests/` exercise it on the `fake` backend without Gradio. `app.py` is the Gradio wiring: it maps components to controller calls and adds nothing the controller cannot do.
- **Run it as a module** from the repo root, so the example package imports the same way from the command line and from `tests/` (`pyproject.toml` adds `.` to pytest's `pythonpath`):

  ```
  uv run python -m examples.control_panel --config config.example.json   # the sim viewer
  uv run python -m examples.control_panel                                # no config: the fake
  ```

  `--config PATH` is the JSON config file; without it the panel runs the `fake` backend. `config.example.json` is the sim **viewer** with the host **webcam** as the robot's camera ([config.md](config.md), [sim_daemon.md](sim_daemon.md)): the MuJoCo window opens next to the panel, the camera frame is the webcam, and with the motors enabled the simulated robot follows the person in front of it — the manual face-tracking check. Set `daemon.camera.source` to `sim` for the rendered eye camera instead. `--host` / `--port` (default `127.0.0.1:7860`) place the web server. Ctrl+C in the terminal stops the server, then the controller closes the api session (which stops a daemon the bridge spawned).
- **Dependencies: the `demo` group.** `[dependency-groups] demo = ["gradio>=5", "reachy-mini-bridge[sim,tts]"]` — Gradio plus the two extras that make the sim and the default voice work out of the box. It is a group of its own so it can be left out (`uv sync --no-group demo`), and it is in `[tool.uv] default-groups` next to `dev` so a normal dev env has it: `ruff` and `pyright` cover `examples/` (pyright's `include`), and that gate must not depend on which groups happen to be synced.

### The controller — one api session on a background loop

`ControlPanelController(config, *, synthesizer=None)` owns exactly one `ReachyMiniApi` and exposes it to synchronous callers (Gradio handlers run in worker threads):

- **Lifecycle.** `start(timeout=None)` spawns a daemon thread running its own asyncio loop, which enters `async with ReachyMiniApi(config)` there, caches `list_emotions()` (a failure is logged, the list stays empty), starts the mic meter task, and then waits. `start` returns once the api is entered, or re-raises what bring-up raised (the thread is gone by then); a timeout, or `stop()` during bring-up, cancels the bring-up task and the api unwinds what it started ([api.md](api.md) "Bring-up is cancellable"). `stop()` cancels every in-flight verb, ends the loop's wait so the api exits in order, and joins the thread. `with controller:` is `start()` / `stop()`. Every verb raises `BridgeError` when the controller is not running.
- **Instant verbs are plain blocking calls** that submit the coroutine to the loop and wait for its result: `get_motors_state()`, `set_motors_state(state)`, `play_sound(path)`, `start_head_tracking(weight)`, `stop_head_tracking()`, `set_wobbling(on)`, `set_presence(on)`, `set_breathing(on)`, `camera_frame_rgb()` (the api's BGR frame flipped to RGB for display, `None` when there is none). Bridge errors (`MotorsNotEnabledError`, `GravityCompensationUnsupportedError`, …) and `ValueError`s propagate unchanged to the caller.
- **Spanning verbs block for their whole effect and can be stopped from another thread.** `say(text)` and `play_emotion(name)` return `True` when the verb completed and `False` when it was stopped; `stop_saying()` / `stop_emotion()` cancel every in-flight task in that slot on the loop thread, wait until those verbs have actually returned (the api stops the effect *before* re-raising its `CancelledError`, so the wait is the guarantee that the speaker is silent or the trajectory no longer commanded), and return how many they stopped. A new `say` while one is playing stops the old one first (barge-in: two `say`s on one media session would interleave); emotions are not pre-empted — the api queues them FIFO — so `stop_emotion()` stops the playing one and every queued one. `busy` reports the slots currently occupied (`["say"]`, `["emotion"]`, both, or none).
- **`snapshot()`** reads everything the panel shows in one call and returns a `PanelState` dataclass: `backend`, `motors` (the daemon's motor state, or the error text when the read failed), `presence`, `breathing`, `wobbling`, `tracking`, `attention`, `voice` (`"ready"`, `"none"` when the config has no `tts` block and no synthesizer was passed, or `"unavailable: <cause>"` from `synthesizer_error`), `mic_level`, `mic_sample_rate`, `busy`, `emotions`. The four mode flags, `attention` and `synthesizer_error` are the api's own properties (plain reads); `motors` is a `get_motors_state()` round trip.
- **The mic meter.** A task on the loop iterates `api.audio_input()` (mono int16) and keeps `mic_level` — the RMS of the latest chunk on a 0–1 scale, held with a short exponential decay so a 0.5 s refresh still shows a burst — paced at half the chunk's duration so it never spins on a backend that always has a chunk ready (the fake). A failing tap is logged and leaves the level at 0; it never brings the panel down. On the fake the level reads 0 (synthetic silence).

### The UI

`build_app(controller)` returns a `gr.Blocks`:

- **State panel** (left), refreshed by a `gr.Timer` at 0.5 s from one `snapshot()` plus one `camera_frame_rgb()`: motor state, attention, voice, the four mode flags, what is busy, the mic level as a read-only slider, the camera frame as an image (blank when the backend has none — the headless sim on macOS), and a **log** of the last 50 outcomes (each verb's completion, stop, or error, timestamped). The backend name is in the title and the effective config is shown read-only in a collapsed accordion.
- **Controls** (right), one group per [api.md](api.md) verb group: Motors (radio `enabled` / `disabled` / `gravity_compensation` + Apply), Expression (dropdown from the cached emotions + Play + Stop), Speech (text + Say + Stop; a sound-file path + Play sound), Gaze (weight slider + Start tracking + Stop tracking), Modes (Wobbling / Presence / Breathing checkboxes, applied on user input; their initial values come from the config's `motion` block).
- **Every handler is a named endpoint** (`api_name` = the verb, plus `refresh`), so the panel doubles as a small HTTP API: `gradio_client.Client(url).predict("happy", api_name="/play_emotion")` drives the same controller from a script.
- **Errors never crash a handler.** A `BridgeError` or `ValueError` from a verb becomes a `gr.Error` toast with the exception's message and a log line; a stopped `say` / `play_emotion` logs "stopped", a completed one "done".

### What the panel shows honestly

The panel displays what the api reports, so backend limits show as they are: the MuJoCo sim ignores motor-state changes and keeps reporting `enabled`; the headless sim (`daemon.headless: true`) returns no camera frame on macOS, while the viewer the example config opens does; `say` on a config without a working `tts` block reports the voice as `none` / `unavailable` and the Say button surfaces the api's `BridgeError`.

### Tests

`tests/test_control_panel.py` drives `ControlPanelController` on the `fake` backend the way the app does — from a caller thread — and asserts through `api.robot` (the `FakeReachyMini`'s recorded commands): bring-up and teardown, instant verbs dispatching and their errors propagating, `snapshot()` reflecting mode changes, and, per [AGENTS.md](../AGENTS.md) "Testing", the two spanning verbs stopped mid-flight from another thread (the speaker flushed / the sound stopped, the verb returning `False`, the session still usable). A `build_app` smoke test constructs the Blocks when Gradio is importable and skips otherwise.

## Open questions

None load-bearing. Deferred extensions, each a small addition: a "record N seconds and play it back" mic widget next to the level meter; a few raw `api.robot` readouts (head pose, IMU) once [api.md](api.md)'s rich-perception verbs land; switching config or backend from the UI (today the config is fixed at start, and changing it means restarting the process).
