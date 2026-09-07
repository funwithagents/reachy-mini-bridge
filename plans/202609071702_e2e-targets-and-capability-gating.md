# E2E targets and capability gating

**Status:** Done

Implements [specs/testing.md](../specs/testing.md) ("E2E targets & capabilities"): turn the single motion-only `sim_daemon` fixture into a target-selectable, capability-probing e2e harness — `sim` (headless/headfull) and `real` targets, an "own-it-or-borrow-it" daemon lifecycle, runtime capability probing, and a `requires_caps(...)` skip gate. It deliberately leaves the actual `audio`/`camera` e2e *tests* to the `audio.py` / perception work; this plan delivers the harness they'll plug into, plus keeps the existing motion test green. Flips `testing.md` back to `Implemented` when done.

## Scope

- `tests-e2e/conftest.py` — replace the single sim fixture with: target selection (`REACHY_MINI_E2E_TARGET`), the headless/headfull sim launch modes (`REACHY_MINI_E2E_SIM_VIEWER`), the `real` target, reuse-or-spawn + own-it/borrow-it lifecycle, and capability probing → a `live_robot` fixture yielding `(robot, capabilities)`.
- `tests-e2e/support.py` — a `requires_caps(*caps)` helper that skips when the probed capability set lacks any requested cap.
- `tests-e2e/test_client_sim.py` — keep the existing motion assertion, now driven by `live_robot` + `requires_caps("motion")`.

## Steps

1. **Target selection & connection config.** Read `REACHY_MINI_E2E_TARGET` (`sim` default | `real`), `REACHY_MINI_HOST`/`REACHY_MINI_PORT` (default `127.0.0.1:8000`), and `REACHY_MINI_E2E_SIM_VIEWER` (sim viewer toggle). Resolve to a target descriptor.

2. **Daemon lifecycle — own it or borrow it.** If a daemon is already ready at the address, reuse it (never tear it down). Else, for `sim` only, spawn one and own its teardown; for `real`, skip (don't spawn). Launch commands:
   - sim headless: `reachy-mini-daemon --sim --headless --no-preload-datasets` (drop `--no-media` so audio/camera can be probed).
   - sim headfull: the MuJoCo viewer — on macOS `mjpython -m reachy_mini.daemon.app.main --sim --scene minimal …` (needs a GUI session; from a non-GUI process, `launchctl asuser`). See [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md).
   Keep it module-scoped (one daemon per file). Skip cleanly (never fail) when the `sim` extra / launcher is missing, the port is busy with something else, or the daemon can't become ready.

3. **Connect the client.** `build_robot("real", connection_mode="network", host, port, media_backend="local")` (a same-machine daemon serves media over local IPC; `media_backend="local"` avoids the WebRTC-producer path). Wait for backend readiness (`get_status().backend_status is not None`).

4. **Probe capabilities** against the live daemon at setup, returning a `frozenset`:
   - `motion` — backend reports a status.
   - `audio` — `media.start_recording()` then a real `get_audio_sample()` arrives within a short timeout (restore prior recording state after).
   - `camera` — `media.get_frame()` returns a frame within a short timeout.
   - `doa` — reserved (robot-only); probe `media.get_DoA()` if/when needed.
   Probing (not backend-type inference) is deliberate — audio needs `start_recording`, the sim camera needs a GL context, etc.

5. **`requires_caps` gate.** In `support.py`, `requires_caps(*caps)` reads the fixture's probed set and `pytest.skip(...)`s with a clear message naming the missing capability. Wire the existing motion test through `live_robot` + `requires_caps("motion")`.

## Verification

- `tests-e2e/test_client_sim.py` (motion) passes on the default `sim` target headless (real MuJoCo daemon), as today.
- With `REACHY_MINI_E2E_SIM_VIEWER=1` from a GUI session, the viewer launches and the same test passes (manual check — needs a display).
- Capability probing is exercised: on headless sim, `motion` (and `audio`, once the fixture runs media-on) are present and a `requires_caps("camera")`/`("doa")` test **skips** cleanly; pointed at a `real` target that's unreachable, the fixture **skips**, never fails.
- `uv run ruff check .` / `uv run ruff format .` clean; `uv run pyright` clean; `uv run pytest` green (default run still excludes `tests-e2e/`).
- Flip [specs/testing.md](../specs/testing.md) `Updated → Implemented` (and its index row) once the harness matches the spec. Mark this plan `Done` (here and in [_index.md](_index.md)) only when all pass.
