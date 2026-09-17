# Expressive idle: head rotation tracks and antenna flicks

**Status:** Done

Implements [specs/motion.md](../specs/motion.md) "The moves" — `BreathingMove` grown from three tracks to six: the head's roll, pitch and yaw roam alongside the breath, and the antennas move at a speed drawn from their travel with quick raised-cosine flicks punctuating the roaming. The plan's `_Segment` / `_Track` model and the rest-to-rest contract are unchanged; what changes is how many tracks feed a pose, how a roam target is drawn, how an antenna segment's duration is decided, and that the fade-out now scales rotation too.

Deliberately leaves out: config knobs for any of it (module constants, as today — the "keep module constants" decision), lateral head translation and body yaw in the idle (rotation only), correlated antennas (the two stay independent), and the listening antenna cue ([specs/motion.md](../specs/motion.md) open question 3).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md) ("Commands", "Verification", "Keeping statuses current"), then [specs/motion.md](../specs/motion.md) "The moves" and "Leaving breathing mid-plan". The spec is the design; do not redesign it. If code and spec disagree, the spec wins.
- **Do the steps in order**, ending each with:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e && uv run pyright && uv run pytest
  ```

  (`ruff format .` also rewrites Python blocks inside `plans/*.md` — format the code directories only.)
- **The `Move` contract is sacred:** `evaluate(t)` stays a pure function of `t` for a given seed. Tracks extend lazily and are never re-drawn.
- **Do not commit** unless asked. Do not touch `docs/upstream-*.md` or `specs/_analysis.md` (pre-existing untracked files).

## Facts you must not violate

1. **`evaluate(0)` is exactly neutral, at rest, for every seed.** Every rotation track starts with a hold at `0.0`; every antenna track starts with a hold at `ANTENNA_MIN_RAD`; the breath track starts with a breath, whose raised cosine is flat at `t = 0`. `create_head_pose(z=0, roll=0, pitch=0, yaw=0, degrees=False)` is bit-for-bit `np.eye(4)` (verified), so it equals `NEUTRAL_HEAD`.
2. **Antenna sign convention unchanged:** joint value = `NEUTRAL_ANTENNAS + ANTENNA_OUTWARD * (lean − ANTENNA_MIN_RAD)`, with `ANTENNA_OUTWARD = [-1.0, 1.0]` for `[right, left]`. At `lean == ANTENNA_MIN_RAD` this is `NEUTRAL_ANTENNAS` bit-for-bit.
3. **Every segment starts and ends at rest.** The flick reuses the raised-cosine `"pulse"` shape (zero slope at both ends, ends where it started); rotation moves are minjerk. Never add a shape without this property.
4. **Only flicks pass `ANTENNA_MAX_RAD`**, and never past `ANTENNA_FLICK_MAX_RAD`. The roaming window stays `[ANTENNA_MIN_RAD, ANTENNA_MAX_RAD]`.
5. **No new dependency.** The pose is built with upstream's `reachy_mini.utils.create_head_pose`, not a direct `scipy` import.
6. **The loop is untouched.** `MotionSession`, the queue, the blends, `_tick`, `_playing_breathing`, `_on_set_breathing` and `_on_close` do not change: `_BreathingFadeOut` keeps its `(move, t_offset, duration)` signature.

## Scope

- `src/reachy_mini_bridge/motion.py` — the `"breath"` segment shape renamed `"pulse"` (both the breath and the flick use it); new head-rotation and antenna constants; `_roam_target`; `_IdleOffsets`; `BreathingMove` rebuilt on six tracks; `_BreathingFadeOut` rewritten on `_IdleOffsets.pose(scale)`.
- `tests/test_motion.py` — the breath test stops claiming "only z moves"; the continuity bounds rise to the new peak speeds; the outward-range test admits the flick ceiling; three new tests (head rotation roams within its envelope and moves often; antennas are fast and flick; the minimum-travel rule).
- `tests-e2e/test_api.py` — the attention hand-back test measures the head's *mean* angle from neutral over the breathing window instead of one instantaneous sample, against a threshold that admits the idle roam.
- `plans/_index.md` and this file — status.

`specs/*` are already updated for this plan (`motion.md` "The moves", plus the one-line idle descriptions in `api.md`, `config.md`, `sim_scene.md` and `_index.md`). `README.md` needs no change: it says "breathes" and never describes the tracks.

## Steps

### Step 0 — Baseline

Run the check command. Everything green before anything changes.

### Step 1 — `motion.py`: constants

Add `from reachy_mini.utils import create_head_pose` to the upstream imports.

After the existing `BREATH_*` block, add the head-rotation constants; after `ANTENNA_OUTWARD`, add the antenna ones. Values (measured over 300 s windows on three seeds, see Verification):

```python
HEAD_YAW_RAD = math.radians(8.0)
HEAD_PITCH_RAD = math.radians(5.0)
HEAD_ROLL_RAD = math.radians(4.0)
HEAD_HOLD_S = (1.2, 4.5)
HEAD_MOVE_S = (1.2, 2.8)
ROAM_MIN_TRAVEL_FRACTION = 0.4

ANTENNA_HOLD_S = (0.4, 2.5)          # replaces (0.5, 4.0)
ANTENNA_SPEED_RAD_S = (math.radians(20.0), math.radians(70.0))
ANTENNA_MOVE_S = (0.25, 1.2)         # replaces (0.8, 2.5); now a clamp on travel / speed
ANTENNA_FLICK_PROBABILITY = 0.35
ANTENNA_FLICK_S = (0.25, 0.5)
ANTENNA_FLICK_RAD = (math.radians(12.0), math.radians(25.0))
ANTENNA_FLICK_MAX_RAD = math.radians(45.0)
```

Each carries the comment the spec's prose justifies — in particular that `ANTENNA_MOVE_S` is a clamp, not a draw, and that only a flick passes `ANTENNA_MAX_RAD`.

### Step 2 — `motion.py`: the shape rename and `_roam_target`

Rename the `_Segment.shape` literal `"breath"` → `"pulse"` (an out-and-back raised cosine — the breath's shape, and the flick's). Update the `Literal[...]` annotation, the `value()` branch and `_breath()`'s constructor call.

Add, next to `_breath()`:

```python
def _roam_target(rng: random.Random, prev: float, lo: float, hi: float) -> float:
    """A new target in ``[lo, hi]`` at least ``ROAM_MIN_TRAVEL_FRACTION`` of the span
    away from ``prev`` — uniform over the range with the band around ``prev`` removed,
    so no roam is too small to see (specs/motion.md "The moves")."""
```

Implementation: `min_travel = ROAM_MIN_TRAVEL_FRACTION * (hi - lo)`; `low = max(0.0, (prev - min_travel) - lo)`; `high = max(0.0, hi - (prev + min_travel))`; draw `u = rng.uniform(0.0, low + high)`; return `lo + u` when `u < low`, else `prev + min_travel + (u - low)`. `low + high` is at least `(hi - lo) * (1 - 2 * ROAM_MIN_TRAVEL_FRACTION)` when both sides are open and at least `(hi - lo) * (1 - ROAM_MIN_TRAVEL_FRACTION)` when one is closed, so it is never zero for a fraction below 0.5 — no fallback branch is needed.

### Step 3 — `motion.py`: `_IdleOffsets`

A frozen dataclass holding one instant of the plan, and the single place a pose is built from it:

```python
@dataclass(frozen=True)
class _IdleOffsets:
    """Every idle track's signed offset from neutral at one instant, and the pose they
    make (specs/motion.md "The moves")."""

    z_m: float
    rpy_rad: npt.NDArray[np.float64]        # roll, pitch, yaw from neutral
    antennas_rad: npt.NDArray[np.float64]   # outward lean beyond the neutral lean, per antenna

    def pose(self, scale: float = 1.0) -> Pose:
        roll, pitch, yaw = scale * self.rpy_rad
        head = create_head_pose(
            z=scale * self.z_m, roll=roll, pitch=pitch, yaw=yaw, degrees=False
        )
        antennas = NEUTRAL_ANTENNAS + ANTENNA_OUTWARD * (scale * self.antennas_rad)
        return head, antennas, NEUTRAL_BODY_YAW
```

`scale=0` gives exactly `NEUTRAL` (fact 1 and fact 2); `create_head_pose` and the antenna expression each return a fresh array, so no caller needs `.copy()`.

### Step 4 — `motion.py`: `BreathingMove` on six tracks

Keep the constructor's shape (`rng: random.Random | None`, one `random.Random(rng.random())` stream per track — now six, drawn in a fixed order: breath, roll, pitch, yaw, right antenna, left antenna).

- The breath track is unchanged (`next_head`, `_breath()`).
- A rotation track per axis, built by a small factory taking `(rng, limit)`: first segment `_Segment(0.0, 0.0, rng.uniform(*HEAD_HOLD_S), "hold")`; `draw_next` returns a hold at `prev.end` for `HEAD_HOLD_S` after a `"minjerk"`, else a `"minjerk"` from `prev.end` to `_roam_target(rng, prev.end, -limit, limit)` over `rng.uniform(*HEAD_MOVE_S)`.
- An antenna track per side: first segment the hold at `ANTENNA_MIN_RAD` as today; `draw_next` returns a hold for `ANTENNA_HOLD_S` after any non-hold, else — with probability `ANTENNA_FLICK_PROBABILITY` — a `"pulse"` of `rng.uniform(*ANTENNA_FLICK_S)` peaking at `min(prev.end + rng.uniform(*ANTENNA_FLICK_RAD), ANTENNA_FLICK_MAX_RAD)`, otherwise a `"minjerk"` to `_roam_target(rng, prev.end, ANTENNA_MIN_RAD, ANTENNA_MAX_RAD)` over `min(max(travel / rng.uniform(*ANTENNA_SPEED_RAD_S), ANTENNA_MOVE_S[0]), ANTENNA_MOVE_S[1])`.

Add `offsets(t) -> _IdleOffsets` (reading the six tracks; the antenna component is `track.value(t) - ANTENNA_MIN_RAD`) and make `evaluate(t)` return `self.offsets(t).pose()`.

### Step 5 — `motion.py`: `_BreathingFadeOut`

Replace its `evaluate` body with `return self._move.offsets(self._t_offset + t).pose(1.0 - _fade_in(t, self._duration))`. The matrix surgery and the `assert`s go; the class's signature, docstring intent and `duration` stay. Rotation now fades with everything else.

### Step 6 — `tests/test_motion.py`

Imports gain `ANTENNA_FLICK_MAX_RAD`, `HEAD_HOLD_S`, `HEAD_MOVE_S`, `HEAD_PITCH_RAD`, `HEAD_ROLL_RAD`, `HEAD_YAW_RAD`, `ROAM_MIN_TRAVEL_FRACTION`, `_roam_target`.

Changes to existing tests:

- `test_breath_is_a_raised_cosine_then_a_rest` — read the breath through `move.offsets(t).z_m` rather than the pose's `head[2, 3]`, and replace the "only z moves" block with an assertion that the pose's **translation** is z-only (`head[0, 3] == head[1, 3] == 0.0`) and that the body yaw is `0.0`. The head's rotation is no longer identity, so the old whole-matrix diff is wrong, not merely loose.
- `test_antennas_stay_outward_within_range` — the ceiling becomes `ANTENNA_FLICK_MAX_RAD`; add that the fraction of samples above `ANTENNA_MAX_RAD` is small (flicks are punctuation, not the resting state) and that the floor `ANTENNA_MIN_RAD` still holds.
- `test_breathing_is_continuous_and_pure` — the antenna per-tick bound rises from `0.02` to `0.11` rad (measured max ~0.088 rad, a ~5° flick step at 60 Hz); add a per-tick bound on the rotation offsets (`offsets(t).rpy_rad`, measured max 0.0053 rad → assert `< 0.02`). The purity and out-of-order checks stay as they are.
- `test_fade_out_lands_at_neutral_at_rest` — pick the offset at the first sample where z, an antenna **and** a rotation are all off neutral, and raise the antenna per-tick bound to `0.12`; add that the faded pose's rotation angle from neutral decreases monotonically to zero.

New tests (seeded, `evaluate`/`offsets`-driven, no sleeping):

- `test_head_rotation_roams_within_its_envelope` — over 300 s at 60 Hz and five seeds: each of roll/pitch/yaw stays within its limit; each reaches at least 70 % of it in both directions; the pose's rotation angle from neutral never exceeds the envelope corner (`hypot` of the three limits) and averages between 4° and 8°.
- `test_head_rotation_keeps_at_least_one_axis_moving` — over the same window, the fraction of ticks where some rotation axis is moving faster than 0.5 °/s is above 0.5 (measured ~0.71). This is the test that would have failed before this plan, where it was exactly 0.
- `test_antennas_move_fast_enough_to_read_and_flick` — over the same window: the 90th percentile of antenna speed is above 20 °/s and the 99th above 80 °/s (measured 38 and 155; the old plan gave 4 and 13), and at least twenty distinct flick peaks exceed `ANTENNA_MAX_RAD`.
- `test_roam_target_always_travels` — `_roam_target` over a grid of `prev` values in several ranges: the result is inside `[lo, hi]` and at least `ROAM_MIN_TRAVEL_FRACTION * (hi - lo)` away from `prev`, minus a float epsilon.

Loop-level tests keep their existing sleep budget and assert only bounds/sign/continuity (the loop's move is unseeded).

### Step 7 — `tests-e2e/test_api.py`

In `test_attention_hands_the_head_back_and_reengages_on_the_face`, the head no longer holds still near neutral once handed back — it roams. Turn `_sample_z_range` into `_sample_idle(robot, seconds) -> tuple[float, float]` returning the z range **and** the mean angle from neutral over the same window, and take `settled` from that mean instead of one instantaneous pose read just after the hand-back.

Raise `NEUTRAL_THRESHOLD_DEG` from `5.0` to `12.0`, with the comment that the idle roam averages ~6° from neutral and peaks at 10.3° (`specs/motion.md` "The moves"), while a head still locked on the face sits at the face's 18.4° — so the assertion still separates the two. Update the printed `[e2e]` line to say it is a mean.

Leave the other e2e tests alone: they either turn breathing off first, or run at tracking weight 1.0 where the daemon discards the head target.

### Step 8 — Docs and statuses

- Re-read [specs/motion.md](../specs/motion.md) "The moves" against the code; if they differ, fix the code.
- Set this plan `Done` here and in [_index.md](_index.md).
- `specs/motion.md` stays `Stable` — its promotion to `Implemented` is gated by plan [202609162000](202609162000_motion-loop-presence-and-breathing.md)'s on-robot checklist, which covers the idle behaviour. Add the new behaviour to that checklist's idle item: the head visibly looks about without drifting, the antennas flick, and no segment boundary snaps.

## Verification

- `uv run ruff check . && uv run ruff format src tests tests-e2e && uv run pyright && uv run pytest` — all green. **Done** (359 tests, 0 pyright errors).
- `uv run pytest tests-e2e -rs` on the headless sim — **Done** (2026-09-17, Apple Silicon Mac, macOS 26.5.2): 11 passed, 6 skipped (`gravity_compensation`, the sim ignoring motor modes, and the four `camera`-gated attention tests the headless sim cannot run). `[e2e] breathing z range 0.0048 m, still 0.0002 m`.
- `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -s -k "breathing or attention or tracking"` on the viewer sim, for the `camera`-gated tests step 7 changed — **Done** (same machine): 4 passed. `[e2e] settled 3.7 deg from neutral on average while alone, breathing z range 0.0048 m, attention after the face returned: 'engaged'` — comfortably under the 12° threshold and clear of the face's 18.4°, so the hand-back assertion keeps its discriminating power. The tracking tests are unaffected: at weight 1.0 the daemon discards the idle head target (`face ahead` settles at −0.3° of an expected 0.0°, the lateral moves within 1.4° of ±18.4°).
- Measured on the generator before implementation (300 s at 60 Hz, seeds 0/1/2), for the bounds the tests assert:
  - head: at least one rotation axis moving 71–74 % of the time; angle from neutral p50 5.9–6.4°, max 9.2–9.8° (envelope corner 10.25°).
  - antennas: speed p90 37–47 °/s, p99 152–174 °/s, max 261–343 °/s; largest step 4.4–5.0° per tick.
  - against the previous plan over the same window: speed p90 4 °/s, p99 13 °/s, max 24 °/s, head rotation none.
- Both runs pass, so this plan is `Done`. On-robot confirmation belongs to plan [202609162000](202609162000_motion-loop-presence-and-breathing.md)'s step 8 checklist, which now carries an item for this behaviour.
