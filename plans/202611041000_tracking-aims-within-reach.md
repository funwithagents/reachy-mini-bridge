# Tracking aims within reach

**Status:** Done

Implements `specs/motion/head_tracking.md` "Within reach": every aim the tracker sends is checked with upstream's analytical kinematics and, when it has no solution, gives up the posture's offset, then its roll, then part of its direction, so the daemon never refuses it and freezes the head. Leaves the base yaw out (open question 6: it needs a heading composed onto the emotions).

## Scope

- `src/reachy_mini_bridge/head_tracking.py` — `_within_reach` (the prioritised search, `REACH_STEPS = 10`), `_postured`, `_analytical_reach` (built once, the pose lifted by `BREATH_Z_M`, body yaw 0), the tracker's injectable `reachable`; `HeadTrackingReport.roll_deg` / `offset_mm` publish what was sent
- `tests/test_head_tracking.py` — four tests: offset before roll, roll after the offset, a refused direction turned part-way, upstream's solver refusing a full retreat with a 25° roll at ~29° of yaw and the sent aim solving
- `specs/motion/head_tracking.md`, `docs/reference/configuration.md`, `docs/guides/perception-and-tracking.md` — the behaviour and the measured workspace

## Steps

1. Map upstream's analytical IK over yaw × roll × lean (along the look) × pitch with a breath of lift (2026-11-03): the refusals come from a retreat with a roll, worse looking up; the base turning with the head removes them.
2. The search in the tracker, after the filters; the report on the sent values.
3. Tests with a scripted check and with upstream's solver.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the live head tracking and custom faces files on the viewer sim.
