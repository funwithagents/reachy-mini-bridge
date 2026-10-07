# A `motor_states` capability: the live motor test asserts the state where the target honors it

**Status:** Done

Implements the settled behavior in `specs/testing/testing.md` ("E2E targets & capabilities", the capability table) and `specs/testing/testing_support.md` ("Public surface", `requires_caps`): the harness probes `motor_states` — the daemon honors `set_motors_state("enabled" / "disabled")` — from the daemon status alone, and `tests-e2e/test_motors.py` asserts each state reads back as set wherever it was probed, a valid mode elsewhere. It leaves gravity compensation's own test as it is.

## Scope

- `src/reachy_mini_bridge/testing/fixtures.py` — `_probe_motor_states`, sharing the simulation read with `_probe_gravity_compensation`; `motor_states` added by `_probe_capabilities`
- `tests/test_testing_support.py` — the probe present on hardware, absent on a sim, a mockup sim and a daemon that does not answer
- `tests-e2e/test_motors.py` — the equality assertion gated on `motor_states`
- `specs/testing/testing.md`, `specs/testing/testing_support.md`, `specs/core/bridge.md` ("Motors", editorial), `docs/guides/testing.md` — the capability stated
- `specs/_index.md` — the two testing specs back to `Implemented`

## Steps

1. `_is_simulation(robot)` reads `simulation_enabled or mockup_sim_enabled`; `_probe_gravity_compensation` uses it; `_probe_motor_states` is `not _is_simulation(robot)`, `False` when the status cannot be read. It never switches torque: a probe that turned it off would drop a head no test has lowered.
2. `_probe_capabilities` adds `motor_states`.
3. `test_motor_state_reads_and_dispatches_over_the_live_path` asserts `results == {"enabled": "enabled", "disabled": "disabled"}` when `motor_states` is in the caps.
4. Fast tests beside the gravity-compensation probe's.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`, and the live tier on the headless sim (`uv run pytest tests-e2e/test_motors.py -rs`: `motor_states` absent, the validity assertion runs). The equality branch runs on a robot only. Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass.
