# Tracking aims within reach

**Status:** Done

Implements `specs/motion/motion.md` "The gaze layer" ("Within reach", `GazeAim`) and `specs/motion/head_tracking.md` "Within reach": every head pose the gaze layer composes is checked with upstream's analytical kinematics before it is sent and, when it has no solution, gives up the idle move's gaze motion, then the posture's offset, then its roll, then part of the direction, so the daemon never refuses it and freezes the head at the far sides. The tracker hands the loop the aim's parts; the loop, the one place that knows the exact pose it sends, runs the one search. Leaves out the daemon's audio wobble (composed after the loop) and the base yaw (head_tracking.md open question 6: it needs a heading composed onto the emotions).

## Scope

- `src/reachy_mini_bridge/motion.py` — `GazeAim`, `postured`, `within_reach` (four shares: idle motion, offset, roll, direction), `analytical_reach`, `REACH_STEPS = 10`, `REACH_RECOVER_PER_S = 1.0`; `set_gaze` takes a `GazeAim`; the gaze layer eases the parts and runs the search every tick, holding the last fitting shares at the edge of the workspace; `MotionSession(reachable=...)`
- `src/reachy_mini_bridge/head_tracking.py` — sends `GazeAim(look, roll, offset)`; the report publishes the posture asked
- `tests/test_motion.py` — the search's order; upstream's solver through the loop (a full retreat with a 25° roll at 45° of yaw under breathing); a cut share recovering by the time since it fit; stillness at the edge; the gaze tests on `GazeAim`
- `tests/test_head_tracking.py` — recorders compose the aim's parts
- `tests/test_bridge.py` — the focus test waiting for the engagement before the fade; the emotion-under-tracking test reading the emotion's own straight-ahead run (both races the macOS and Windows runners hit)
- `tests/test_voice_tour.py` — one Windows timer tick of slack on the pause between voices (a flake of the runner that turned this PR red twice)
- `specs/motion/motion.md`, `specs/motion/head_tracking.md`, `docs/reference/configuration.md`, `docs/guides/perception-and-tracking.md` — the behaviour and the measured workspace

## Steps

1. Map upstream's analytical IK (the daemon's call: `IK error: Collision detected or head pose not achievable!` when it has no solution, the previous joints kept) over yaw × roll × lean along the look × pitch (2026-11-03): the refusals come from a retreat with a roll, worse looking up and with breathing on top; the yaw alone never is the limit; the base turning with the head removes them all; the wobble's worst case would cost almost all of the retreat, so it stays out.
2. A first check in the tracker on its own aim, then the one search moved into the loop, where the idle motion is known too; the tracker on the parts.
3. Tests with a scripted check and with upstream's solver; the runners' timing races in the tests around it.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the live head tracking, custom faces and motion files on the viewer sim; CI green on every runner.
