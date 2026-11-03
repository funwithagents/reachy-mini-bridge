# Reach check in the motion loop

**Status:** Done

Implements `specs/motion/motion.md` "The gaze layer" ("Within reach", `GazeAim`) and the matching `specs/motion/head_tracking.md` "Within reach": the reach check moves from the tracker into the motion loop, the one place that knows the exact pose it sends, and covers the idle move's gaze motion as well as the posture. The tracker hands over the aim's parts. Leaves the daemon's audio wobble out (composed after the loop) and the base yaw (open question 6 of head_tracking.md).

## Scope

- `src/reachy_mini_bridge/motion.py` — `GazeAim`, `postured`, `within_reach` (four shares: idle motion, offset, roll, direction), `analytical_reach`, `REACH_STEPS`, `REACH_RECOVER_PER_S`; `set_gaze` takes a `GazeAim`; the gaze layer eases the parts and runs the search every tick, holding the last fitting shares at the edge; `MotionSession(reachable=...)`
- `src/reachy_mini_bridge/head_tracking.py` — sends `GazeAim(look, roll, offset)`; its own check removed; the report publishes the posture asked
- `tests/test_motion.py` — the search's order, upstream's solver through the loop, recovery, stillness at the edge; the gaze tests on `GazeAim`
- `tests/test_head_tracking.py` — recorders compose the aim's parts; the tracker-side reach tests removed
- `specs/motion/motion.md`, `specs/motion/head_tracking.md`, `docs/reference/configuration.md`, `docs/guides/perception-and-tracking.md`

## Steps

1. Measure the reach left under breathing's and the wobble's envelopes (2026-11-03): breathing costs a retreat with a 12° roll 18° of yaw (56° → 38°); the wobble's worst case would cost almost all of it, so it stays out.
2. `GazeAim` and the loop-side search; the tracker on the parts.
3. Tests, specs, docs.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the live head tracking and custom faces files on the viewer sim.
