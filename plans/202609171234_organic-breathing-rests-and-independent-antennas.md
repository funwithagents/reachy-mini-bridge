# Organic breathing: rests between breaths, independent antennas

**Status:** Done

Implements [specs/motion.md](../specs/motion.md) "The moves" — the randomised `BreathingMove` (rest-to-rest segments: raised-cosine breaths separated by random 1–5 s rests on the head's z axis, and two independent antenna tracks that roam from the neutral lean to ~25° outward from vertical) and the "Leaving breathing mid-plan" fade-out. Replaces the fixed-cycle sine idle that plan [202609162000](202609162000_motion-loop-presence-and-breathing.md) step 3 built; that plan's step 8 on-robot checklist already carries one item for this behaviour and otherwise stands. Deliberately leaves out config knobs for the new parameters (they are module constants, like today's), the listening antenna cue (spec open question 3), and the deferred emotion-preempts-breathing handoff (spec open question 1).

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md) ("Commands", "Verification", "Keeping statuses current"), then [specs/motion.md](../specs/motion.md) — the sections "Toggle semantics", "The moves" and "`fake` backend support". The spec is the design; **do not redesign it**. If the code below and the spec disagree, the spec wins — fix the code and note it here.
- **Do the steps in order.** Every step ends with the same check; run it and fix everything red before starting the next step:

  ```
  uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest
  ```

- **All code in this plan was run and passes** (lint, pyright, its tests). Paste it as written; adapt only names that have to match the surrounding file. Do not "improve" the algorithms or loosen/tighten test bounds unless a step says so.
- **Edit by anchor.** Each edit names the exact existing text to find. If an anchor is not found verbatim, stop and read the file — do not guess.
- **The `Move` contract is sacred:** `evaluate(t)` must be a pure function of `t` for a given seed. The plan is generated lazily but never re-drawn.
- **Tests are functional and fast:** the plan itself is tested by seeding the move and calling `evaluate` at chosen times — no sleeping. Loop-level tests keep the existing budget (sleeps ≤ 1.5 s) and assert properties that hold for *any* seed (bounds, sign, continuity), never a specific random draw.
- **Do not commit** unless asked. Do not touch `docs/upstream-*.md` or `specs/_analysis.md` (pre-existing untracked files, not yours).

## Facts you must not violate

1. **Antenna sign convention:** joint value = `ANTENNA_OUTWARD * angle`, with `ANTENNA_OUTWARD = np.array([-1.0, 1.0])` for `[right, left]`. Outward is **negative for the right antenna, positive for the left** (the sign of upstream's `SLEEP_ANTENNAS_JOINT_POSITIONS = [-3.05, 3.05]`, the antennas folded fully out).
2. **The floor is exactly neutral.** `NEUTRAL_ANTENNAS = [-0.1745, 0.1745]` (upstream's constant, already in `motion.py`). `ANTENNA_MIN_RAD` is defined *from it* (`abs(NEUTRAL_ANTENNAS[0])` = `0.1745`), **not** as `math.radians(10)` (= `0.17453293`, which is *not* equal to neutral and breaks the at-rest-at-neutral tests). `ANTENNA_OUTWARD * ANTENNA_MIN_RAD` must equal `NEUTRAL_ANTENNAS` bit-for-bit.
3. **Every segment starts and ends at rest.** A breath is a raised cosine (zero slope at both ends), a hold is constant, an antenna move is minjerk (`time_trajectory(u, InterpolationTechnique.MIN_JERK)`, zero slope at both ends). Never add a segment shape without this property.
4. **`evaluate(0)` is neutral at rest** for every seed: the head track begins with a breath (value 0, slope 0 at `t = 0`); each antenna track begins with a hold at the floor.
5. **Only the loop calls `set_target`;** nothing in this plan touches the api's verbs, the queue, the blends, or `_tick`. The `MotionSession` changes are confined to the three methods named in step 1.

## Scope

- `src/reachy_mini_bridge/motion.py` — imports; constants; a private segment/track model (`_Segment`, `_Track`, `_breath`); `BreathingMove` rewritten on it (takes `rng`); `_BreathingFadeOut` generalised to wrap a `BreathingMove` instance; `_fade_in` kept as the ramp the fade-out uses; `MotionSession._breathing_steady_state_elapsed` → `_playing_breathing` and its two callers.
- `tests/test_motion.py` — imports; two sine tests replaced by eight plan tests.
- `tests/test_api.py` — imports; one breathing test rewritten; one comment.
- `tests-e2e/test_api.py` — imports; the live breathing test's sampling window and docstring.
- `plans/_index.md` and this file — status.

`README.md` needs no change (checked: its wording says "breathes", never "sine"). `specs/*` are already updated for this plan.

## Steps

### Step 0 — Baseline

Run the check command. Everything must be green before you change anything. If it is not, stop and report.

### Step 1 — `src/reachy_mini_bridge/motion.py`

Six edits, top to bottom.

**1a. Imports.** Find:

```python
import asyncio
import concurrent.futures
import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self
```

Replace with:

```python
import asyncio
import bisect
import concurrent.futures
import logging
import math
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Self
```

**1b. Constants.** Find the four lines:

```python
# BreathingMove parameters — the conversation app's, seen on hardware.
BREATH_Z_M = 0.005
BREATH_HZ = 0.1
ANTENNA_SWAY_RAD = math.radians(15)
ANTENNA_HZ = 0.5
```

Replace with:

```python
# BreathingMove parameters (specs/motion.md "The moves"). The peaks are the conversation
# app's, seen on hardware; the rests and the independent antennas are what make the idle
# read as organic rather than mechanical.
BREATH_Z_M = 0.005  # a breath peaks this far above neutral, then returns to it
BREATH_S = 5.0  # one breath: a raised-cosine rise and fall
BREATH_REST_S = (1.0, 5.0)  # uniform rest at neutral between two breaths
ANTENNA_HOLD_S = (0.5, 4.0)  # uniform hold between two antenna moves
ANTENNA_MOVE_S = (0.8, 2.5)  # uniform duration of one minjerk antenna move
```

Then find the line `NEUTRAL_BODY_YAW = 0.0` (a few lines below, after `NEUTRAL_ANTENNAS`) and insert **after** it:

```python
# The antennas roam between the neutral lean (upstream's ~10° anti-shake offset — the
# floor is exactly NEUTRAL_ANTENNAS, not the rounded radians(10)) and that lean plus the
# previous animation's 15° sway, i.e. ~25° outward.
ANTENNA_MIN_RAD = float(abs(NEUTRAL_ANTENNAS[0]))
ANTENNA_MAX_RAD = ANTENNA_MIN_RAD + math.radians(15)
# Joint sign of "outward from vertical" per antenna [right, left]: the sign of upstream's
# SLEEP_ANTENNAS_JOINT_POSITIONS ([-3.05, 3.05], the antennas folded fully out).
ANTENNA_OUTWARD: npt.NDArray[np.float64] = np.array([-1.0, 1.0])
```

(`ANTENNA_MIN_RAD` must come after `NEUTRAL_ANTENNAS` because it is derived from it.)

**1c. `_fade_in` docstring.** Find the docstring of `def _fade_in(...)` (it starts `"""Smooth 0 -> 1 ramp` and ends `matching the blend it follows.\n    """`). Replace the whole docstring with:

```python
    """The minjerk 0 -> 1 ramp (the entry blend's shape) that ``_BreathingFadeOut``
    inverts to fade a plan's offsets out to neutral at rest."""
```

Leave the function body as it is.

**1d. The moves.** Find the whole of `class BreathingMove(Move):` **and** the whole of `class _BreathingFadeOut(Move):` that follows it (together they run from `class BreathingMove(Move):` up to, but not including, `def blend_into(`). Replace both with this block:

```python
@dataclass(frozen=True)
class _Segment:
    """One rest-to-rest piece of a scalar track: a hold, a breath or a minjerk move.

    Every shape has zero slope at both ends, so consecutive segments hand off with
    continuous velocity whatever their order (specs/motion.md "The moves").
    """

    start: float  # value at the segment's start
    end: float  # value at its end (== start for a hold or a breath)
    duration: float
    shape: Literal["hold", "breath", "minjerk"]
    peak: float = 0.0  # breath only: the value at mid-segment

    def value(self, t: float) -> float:
        u = min(max(t / self.duration, 0.0), 1.0)
        if self.shape == "hold":
            return self.start
        if self.shape == "breath":
            return (
                self.start
                + (self.peak - self.start) * (1.0 - math.cos(2.0 * math.pi * u)) / 2.0
            )
        return self.start + (self.end - self.start) * float(
            time_trajectory(u, InterpolationTechnique.MIN_JERK)
        )


class _Track:
    """A lazily generated sequence of segments. ``value(t)`` is a pure function of
    ``t``: segments are drawn only when ``t`` runs past the last one, and never
    re-drawn, so any ``t`` evaluates the same whenever it is asked."""

    def __init__(
        self, first: _Segment, draw_next: Callable[[_Segment], _Segment]
    ) -> None:
        self._segments = [first]
        self._ends = [first.duration]  # cumulative end time of each segment
        self._draw_next = draw_next

    def value(self, t: float) -> float:
        while t >= self._ends[-1]:
            nxt = self._draw_next(self._segments[-1])
            self._segments.append(nxt)
            self._ends.append(self._ends[-1] + nxt.duration)
        i = bisect.bisect_right(self._ends, t)
        seg_start = self._ends[i - 1] if i > 0 else 0.0
        return self._segments[i].value(t - seg_start)


def _breath() -> _Segment:
    return _Segment(0.0, 0.0, BREATH_S, "breath", peak=BREATH_Z_M)


class BreathingMove(Move):
    """The idle move with breathing on (specs/motion.md "The moves"): a randomised plan
    of rest-to-rest segments — raised-cosine breaths separated by random rests on the
    head's z axis, and two independent antenna tracks roaming outward from vertical.

    ``evaluate(t)`` is a pure function of ``t`` for a given ``rng``: the plan extends
    lazily as ``t`` grows and is never re-drawn. The loop builds an unseeded move at
    each idle entry; tests pass ``random.Random(seed)``.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        rng = rng if rng is not None else random.Random()
        # One independent stream per track, so one antenna's draws never shift the
        # other's (or the head's).
        head_rng = random.Random(rng.random())
        antenna_rngs = [random.Random(rng.random()), random.Random(rng.random())]

        def next_head(prev: _Segment) -> _Segment:
            if prev.shape == "breath":
                return _Segment(0.0, 0.0, head_rng.uniform(*BREATH_REST_S), "hold")
            return _breath()

        # The plan begins with a breath, so a fresh idle shows life at once.
        self._head = _Track(_breath(), next_head)

        def antenna_drawer(r: random.Random) -> Callable[[_Segment], _Segment]:
            def next_antenna(prev: _Segment) -> _Segment:
                if prev.shape == "minjerk":
                    return _Segment(
                        prev.end, prev.end, r.uniform(*ANTENNA_HOLD_S), "hold"
                    )
                target = r.uniform(ANTENNA_MIN_RAD, ANTENNA_MAX_RAD)
                return _Segment(prev.end, target, r.uniform(*ANTENNA_MOVE_S), "minjerk")

            return next_antenna

        # Each antenna begins with a hold at the floor (== its neutral value).
        self._antennas = [
            _Track(
                _Segment(
                    ANTENNA_MIN_RAD, ANTENNA_MIN_RAD, r.uniform(*ANTENNA_HOLD_S), "hold"
                ),
                antenna_drawer(r),
            )
            for r in antenna_rngs
        ]

    @property
    def duration(self) -> float:
        return math.inf

    def evaluate(
        self, t: float
    ) -> tuple[
        npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
    ]:
        head = NEUTRAL_HEAD.copy()
        head[2, 3] += self._head.value(t)
        angles = np.array([track.value(t) for track in self._antennas])
        return head, ANTENNA_OUTWARD * angles, NEUTRAL_BODY_YAW


class _BreathingFadeOut(Move):
    """Leaving breathing mid-plan (specs/motion.md "The moves"): keep playing ``move``
    from ``t_offset`` while a minjerk envelope scales every track's offset from neutral
    down to zero over ``duration`` — landing at neutral at rest, so whatever follows
    (a blend, or nothing) starts from a source that is actually at rest. A plain blend
    assumes that, and a track caught mid-segment is not at rest.
    """

    def __init__(
        self, move: BreathingMove, t_offset: float, duration: float = BLEND_S
    ) -> None:
        self._move = move
        self._t_offset = t_offset
        self._duration = duration

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(
        self, t: float
    ) -> tuple[
        npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
    ]:
        envelope = 1.0 - _fade_in(t, self._duration)
        head, antennas, body_yaw = self._move.evaluate(self._t_offset + t)
        assert head is not None and antennas is not None  # BreathingMove sets both
        head = head.copy()
        head[2, 3] = NEUTRAL_HEAD[2, 3] + envelope * (head[2, 3] - NEUTRAL_HEAD[2, 3])
        antennas = NEUTRAL_ANTENNAS + envelope * (antennas - NEUTRAL_ANTENNAS)
        return head, antennas, body_yaw


```

**1e. `MotionSession`: identify the playing breathing move.** Find the whole method `def _breathing_steady_state_elapsed(self) -> float | None:` (its body ends with `return time.monotonic() - playing.stage_start`). Replace the whole method with:

```python
    def _playing_breathing(self) -> tuple[BreathingMove, float] | None:
        """The ``BreathingMove`` currently playing past its entry blend (no primary),
        with the seconds elapsed into it — else ``None``."""
        playing = self._playing
        if playing is None or playing.primary is not None or playing.stage != 1:
            return None
        move = playing.stages[1]
        if not isinstance(move, BreathingMove):
            return None
        return move, time.monotonic() - playing.stage_start
```

**1f. Its two callers.** In `_on_set_breathing`, find:

```python
        elapsed = self._breathing_steady_state_elapsed()
        if not enabled and elapsed is not None:
            # Fade breathing's own amplitude out rather than handing its current
            # (nonzero) velocity straight to a fresh blend, which assumes rest.
            self._playing = _Playing(
                stages=[_BreathingFadeOut(t_offset=elapsed)],
```

Replace with:

```python
        breathing = self._playing_breathing()
        if not enabled and breathing is not None:
            # Fade the plan's offsets out rather than handing a track caught
            # mid-segment (a nonzero velocity) straight to a fresh blend, which assumes
            # rest (specs/motion.md "Leaving breathing mid-plan").
            move, elapsed = breathing
            self._playing = _Playing(
                stages=[_BreathingFadeOut(move, t_offset=elapsed)],
```

In `_on_close`, find:

```python
            elapsed = self._breathing_steady_state_elapsed()
            exit_stage: Move = (
                _BreathingFadeOut(t_offset=elapsed)
                if elapsed is not None
                else blend_into(self._last_target, HoldMove())
            )
```

Replace with:

```python
            breathing = self._playing_breathing()
            exit_stage: Move = (
                _BreathingFadeOut(breathing[0], t_offset=breathing[1])
                if breathing is not None
                else blend_into(self._last_target, HoldMove())
            )
```

**Check:** `uv run ruff check . && uv run ruff format . && uv run pyright` must be clean. `uv run pytest` will fail only in `tests/test_motion.py` (an `ImportError` on `ANTENNA_HZ` etc.) and in `tests/test_api.py::test_breathing_targets_oscillate_in_z` — steps 2 and 3 fix those. Any other failure means an edit above went wrong.

### Step 2 — `tests/test_motion.py`

**2a. Imports.** Find the `from reachy_mini_bridge.motion import (` block and replace its contents so it reads:

```python
from reachy_mini_bridge.motion import (
    ANTENNA_MAX_RAD,
    ANTENNA_MIN_RAD,
    ANTENNA_OUTWARD,
    BLEND_S,
    BREATH_REST_S,
    BREATH_S,
    BREATH_Z_M,
    CONTROL_HZ,
    NEUTRAL,
    NEUTRAL_ANTENNAS,
    NEUTRAL_BODY_YAW,
    NEUTRAL_HEAD,
    BreathingMove,
    HoldMove,
    MotionSession,
    _BreathingFadeOut,
    blend_into,
)
```

Also add `import random` to the stdlib imports (alphabetically, after `import itertools`). The file already imports `itertools`, `numpy as np`, `pytest`, and `npt` under `TYPE_CHECKING`.

**2b. Replace the two sine tests.** Delete `def test_breathing_starts_at_neutral_and_breathes_in_z()` and `def test_breathing_antennas_sway_in_counter_phase()` entirely (from the first `def` to the blank lines before `def test_blend_into_goes_from_source_to_the_moves_start`). In their place paste:

```python
def _z(move: BreathingMove, t: float) -> float:
    head, _antennas, _yaw = move.evaluate(t)
    assert head is not None
    return float(head[2, 3])


def _outward(move: BreathingMove, t: float) -> npt.NDArray[np.float64]:
    """Each antenna's lean outward from vertical, in rad (sign folded away)."""
    _head, antennas, _yaw = move.evaluate(t)
    assert antennas is not None
    return ANTENNA_OUTWARD * antennas


@pytest.mark.parametrize("seed", range(5))
def test_breathing_starts_at_neutral_at_rest(seed: int) -> None:
    move = BreathingMove(random.Random(seed))
    assert move.duration == float("inf")
    head0, antennas0, yaw0 = move.evaluate(0.0)
    assert head0 is not None and antennas0 is not None
    assert np.allclose(head0, NEUTRAL[0])
    assert np.allclose(antennas0, NEUTRAL[1])
    assert yaw0 == NEUTRAL[2]
    head1, antennas1, _yaw1 = move.evaluate(1e-3)  # zero initial velocity
    assert head1 is not None and antennas1 is not None
    assert np.allclose(head1, NEUTRAL[0], atol=1e-6)
    assert np.allclose(antennas1, NEUTRAL[1], atol=1e-6)


def test_breath_is_a_raised_cosine_then_a_rest() -> None:
    move = BreathingMove(random.Random(0))
    assert _z(move, BREATH_S / 2) == pytest.approx(BREATH_Z_M, abs=1e-9)
    assert _z(move, BREATH_S) == pytest.approx(0.0, abs=1e-9)
    # the shortest rest is 1 s: right after the first breath z reads exactly neutral
    assert all(_z(move, BREATH_S + k * 0.01) == 0.0 for k in range(100))
    zs = [_z(move, k * 0.01) for k in range(6000)]
    assert min(zs) >= 0.0
    assert max(zs) <= BREATH_Z_M + 1e-12
    head, _antennas, _yaw = move.evaluate(BREATH_S / 2)
    assert head is not None
    diff = np.asarray(head) - NEUTRAL[0]
    diff[2, 3] = 0.0
    assert np.allclose(diff, 0.0)  # only z moves


def test_breathing_rests_vary_in_length() -> None:
    move = BreathingMove(random.Random(0))
    step = 0.01
    zs = [_z(move, k * step) for k in range(30000)]  # 300 s
    rests: list[float] = []
    for at_rest, run in itertools.groupby(zs, key=lambda z: z == 0.0):
        if at_rest:
            rests.append(len(list(run)) * step)
    # t = 0 is a breath's first sample (z == 0 for one sample, not a rest) and the last
    # run may be cut by the scan window: keep only whole runs at least half the shortest
    # rest
    rests = [r for r in rests[:-1] if r >= BREATH_REST_S[0] / 2]
    assert len(rests) >= 20
    for rest in rests:
        assert BREATH_REST_S[0] - 2 * step <= rest <= BREATH_REST_S[1] + 2 * step
    assert max(rests) - min(rests) > 0.5


@pytest.mark.parametrize("seed", range(5))
def test_antennas_stay_outward_within_range(seed: int) -> None:
    assert np.array_equal(ANTENNA_OUTWARD * ANTENNA_MIN_RAD, NEUTRAL_ANTENNAS)
    move = BreathingMove(random.Random(seed))
    for k in range(6000):  # 120 s at 20 ms
        outward = _outward(move, k * 0.02)
        assert np.all(outward >= ANTENNA_MIN_RAD - 1e-9)
        assert np.all(outward <= ANTENNA_MAX_RAD + 1e-9)


def test_antennas_move_independently() -> None:
    move = BreathingMove(random.Random(0))
    samples = np.array([_outward(move, k * 0.02) for k in range(6000)])
    right, left = samples[:, 0], samples[:, 1]
    assert not np.allclose(right, left)
    for series in (right, left):
        holds = {
            round(v, 3) for v, run in itertools.groupby(series) if len(list(run)) >= 25
        }
        assert len(holds) >= 3, (
            "each antenna should have reached several distinct holds"
        )


def test_breathing_is_continuous_and_pure() -> None:
    move = BreathingMove(random.Random(0))
    period = 1.0 / CONTROL_HZ
    ts = [k * period for k in range(int(120 * CONTROL_HZ))]
    poses = [move.evaluate(t) for t in ts]
    zs = [float(h[2, 3]) for h, _, _ in poses if h is not None]
    ants = [a for _, a, _ in poses if a is not None]
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.0003
    assert max(float(np.max(np.abs(b - a))) for a, b in itertools.pairwise(ants)) < 0.02
    twin = BreathingMove(random.Random(0))
    for t, (head, antennas, _yaw) in zip(ts[::7], poses[::7], strict=True):
        twin_head, twin_antennas, _ = twin.evaluate(t)
        assert head is not None and twin_head is not None
        assert antennas is not None and twin_antennas is not None
        assert np.array_equal(head, twin_head) and np.array_equal(
            antennas, twin_antennas
        )
    # out-of-order re-evaluation on the same move reads the same plan
    head_10, ant_10, _ = move.evaluate(10.0)
    head_10_ref, ant_10_ref, _ = poses[int(10.0 * CONTROL_HZ)]
    assert head_10 is not None and head_10_ref is not None
    assert ant_10 is not None and ant_10_ref is not None
    assert np.array_equal(head_10, head_10_ref) and np.array_equal(ant_10, ant_10_ref)


def test_fade_out_lands_at_neutral_at_rest() -> None:
    move = BreathingMove(random.Random(0))
    offset = next(
        k * 0.01
        for k in range(6000)
        if _z(move, k * 0.01) > 0.0
        and np.any(_outward(move, k * 0.01) > ANTENNA_MIN_RAD + 1e-6)
    )
    fade = _BreathingFadeOut(move, t_offset=offset)
    assert fade.duration == BLEND_S
    head_end, antennas_end, _ = fade.evaluate(BLEND_S)
    assert head_end is not None and antennas_end is not None
    assert np.allclose(head_end, NEUTRAL[0], atol=1e-6)
    assert np.allclose(antennas_end, NEUTRAL[1], atol=1e-6)
    head_near, antennas_near, _ = fade.evaluate(BLEND_S - 1e-3)
    assert head_near is not None and antennas_near is not None
    assert np.allclose(head_near, NEUTRAL[0], atol=1e-5)
    assert np.allclose(antennas_near, NEUTRAL[1], atol=1e-5)
    period = 1.0 / CONTROL_HZ
    poses = [fade.evaluate(k * period) for k in range(int(BLEND_S * CONTROL_HZ) + 1)]
    zs = [float(h[2, 3]) for h, _, _ in poses if h is not None]
    ants = [a for _, a, _ in poses if a is not None]
    # the envelope adds its own slope: 5 mm over BLEND_S at a minjerk peak (1.875x mean)
    # is ~0.31 mm per tick, and up to 15° of antenna lean the same way is ~0.016 rad
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.0005
    assert max(float(np.max(np.abs(b - a))) for a, b in itertools.pairwise(ants)) < 0.03
```

The existing loop tests further down (`test_resume_blends_from_the_present_pose_into_breathing`, `test_close_eases_to_neutral_when_commanding`, …) need **no change**: z is now in `[0, BREATH_Z_M]`, so `abs(zs[-1]) < BREATH_Z_M + 0.002` still holds, and the exit path still fades to neutral.

**Check:** the check command; `uv run pytest tests/test_motion.py` must be fully green (the eight new tests take about a second in total).

### Step 3 — Api-level and live tests

**3a. `tests/test_api.py`.** Find:

```python
from reachy_mini_bridge.motion import BLEND_S, BREATH_Z_M, NEUTRAL_ANTENNAS
```

Replace with:

```python
from reachy_mini_bridge.motion import (
    ANTENNA_MIN_RAD,
    ANTENNA_OUTWARD,
    BLEND_S,
    BREATH_Z_M,
    NEUTRAL_ANTENNAS,
)
```

Find the whole of `def test_breathing_targets_oscillate_in_z() -> None:` (it ends with the line `assert (a0 - NEUTRAL_ANTENNAS[0]) * (a1 - NEUTRAL_ANTENNAS[1]) <= 0`). Replace with:

```python
def test_breathing_rises_from_neutral_and_antennas_lean_outward() -> None:
    async def run() -> tuple[list[float], npt.NDArray[np.float64]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 1.0)
            _head, antennas, _yaw = _fake(api).last_target
            return _head_z(api)[-20:], np.asarray(antennas, dtype=np.float64)

    z, antennas = asyncio.run(run())
    # 1 s into the first breath z has risen ~1.7 mm, from ~0.8 mm twenty ticks earlier
    assert max(z) - min(z) > 0.0005
    assert all(-1e-6 <= v <= BREATH_Z_M + 1e-6 for v in z)
    # outward only: neither antenna ever leans inside its neutral lean
    assert np.all(ANTENNA_OUTWARD * antennas >= ANTENNA_MIN_RAD - 1e-6)
```

(`npt` and `np` are already imported at the top of that file.) In `test_set_breathing_while_idle_eases_to_neutral`, find the comment `# A fade-out (continuing breathing's own phase to zero velocity) precedes` and change it to `# A fade-out (playing the breathing plan on, its offsets fading to zero) precedes`. The test's assertions stay as they are.

**3b. `tests-e2e/test_api.py`.** Find `from reachy_mini_bridge.motion import BLEND_S` and replace with `from reachy_mini_bridge.motion import BLEND_S, BREATH_REST_S, BREATH_S`. In `test_breathing_moves_the_head_and_breathing_off_holds_it`:

- replace the docstring's `(a slow z-axis sine)` with `(slow breaths on the z axis, with random rests between them)`;
- find `breathing_range = await sample_z(6.0)` and replace with:

  ```python
            # long enough to always contain a whole breath, wherever the sample starts
            breathing_range = await sample_z(BREATH_S + BREATH_REST_S[1] + 1.0)
  ```

  (a 6 s window could start in a 5 s rest and see only the first second of a breath, ~1.7 mm, under the `0.002` threshold). Keep the `>= 0.002` assertion.

**Check:** the check command — fully green. Then run the live sim tier once:

```
uv run pytest tests-e2e -rs
```

The breathing test must pass (it takes ~20 s now); write the printed `[e2e] breathing z range …` line under Verification below. Tests that skip for a missing capability or credential are fine; a failure is not.

### Step 4 — Docs and statuses

- Re-read [specs/motion.md](../specs/motion.md) "The moves" once against the code; if something differs, fix the **code** (the spec is the design).
- Set this plan's `**Status:**` to `Done` and change its row in [_index.md](_index.md) to `Done`.
- Do **not** change `specs/motion.md`'s status: it stays `Stable`. Its promotion to `Implemented` is gated by plan 202609162000's on-robot checklist, which already carries the organic-breathing item.

## Common pitfalls

- **`math.radians(10)` is not neutral.** See fact 2. If `test_breathing_starts_at_neutral_at_rest` fails on the antennas with values like `0.17453293` vs `0.1745`, you defined the floor from `radians(10)`.
- **Zero runs at `t = 0`.** A breath's first sample is exactly `0.0`; it is not a rest. The rests test already filters it out — do not "fix" the move to avoid it.
- **`bisect_right` on the boundary.** At `t` exactly equal to a segment's end, the *next* segment is evaluated at its `t = 0`; both give the same value (rest-to-rest), so this is fine.
- **The fade-out needs the move instance.** `_BreathingFadeOut(t_offset=…)` with no move is the old signature; both callers must pass the `BreathingMove` from `_playing_breathing()`.
- **Do not seed the loop's move.** `_select_next` keeps calling `BreathingMove()` with no argument — production randomness comes from the system.
- **Do not add `random`-dependent assertions** to loop-level tests (they run an unseeded move). Assert bounds, sign and continuity only.
- **Ruff formatting** may re-wrap the pasted code; that is expected. Run `uv run ruff format .` and keep what it produces.

## Verification

- `uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest` — all green.
- `uv run pytest tests-e2e -rs` on the headless sim — `test_breathing_moves_the_head_and_breathing_off_holds_it` passes; record here: `[e2e] breathing z range … m, still … m` (date, machine).
  - Recorded: `[e2e] breathing z range 0.0049 m, still 0.0001 m` (2026-09-17, Apple Silicon Mac, macOS 26.5.2, headless sim). Tier: 11 passed, 3 skipped (camera, gravity_compensation, sim ignores motor modes).
- Implementation note: the check command's `uv run ruff format .` also reformats Python blocks inside Markdown (ruff 0.16), which would rewrite untracked docs and other plans; formatting was run on `src tests tests-e2e` instead.
- Mark this plan `Done` only once both pass. The on-robot confirmation (rests visible, antennas asymmetric and never converging, no snap at any segment boundary) is an item of plan 202609162000 step 8 and gates *that* plan, not this one.
