# Motion loop: presence, breathing, and emotions through one writer

**Status:** In progress — steps 1–7 done (lint/type-check/tests all green, including the
live sim tier); step 8 (the on-robot checklist) needs actual hardware and has not been
walked, so the plan and `specs/motion.md` stay short of `Done`/`Implemented`.

Implements [specs/motion.md](../specs/motion.md) in full, and the `Updated` gaps it opened in [specs/api.md](../specs/api.md) ("Presence & breathing", `play_emotion` through the loop, tracking / wobbling paused around a move, `set_motors_state` pausing the loop, lifecycle order), [specs/config.md](../specs/config.md) (the `motion` block) and [specs/robot.md](../specs/robot.md) (the consumed slice: `set_target` + pose readers in, `async_play_move` out). Delivers a 60 Hz `MotionSession` thread that is the only `set_target` writer, breathing / hold idle moves with blended transitions, `play_emotion` re-based on it, and the `presence` / `breathing` switches. Deliberately leaves out the manual movement verbs (they queue into the same loop later) and any listening cue.

## How to work this plan

- **Read first, in this order:** [AGENTS.md](../AGENTS.md) (commands, status discipline), [specs/motion.md](../specs/motion.md) (the design you are building — do not redesign it), then the "Emotions through the loop" / "Motors" / "Lifecycle" sections again while doing steps 4–5. Skim [specs/api.md](../specs/api.md) "Cancellation" and "Lifecycle".
- **Do the steps in order.** Each step ends with a runnable check. Run `uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest` after every step and fix what breaks before moving on. Never leave a step with red tests.
- **Copy existing patterns**, named in each step, rather than inventing new ones. The codebase is small and consistent; matching it is the goal.
- **Code style:** async-native api, `from __future__ import annotations`, full type hints (pyright `standard` mode type-checks `tests/` and `tests-e2e/` too), module docstrings that point at the spec (see `src/reachy_mini_bridge/audio.py`'s header). Comments explain *why*, never restate the code.
- **Tests are functional:** assert on what the fake recorded (`api.robot.targets`, `api.robot.commands`) and on timing, never on internals or "mock was called". Keep fake-tier tests fast: moves ≤ 0.5 s, sleeps ≤ 1.5 s.
- **When something in this plan contradicts a spec, the spec wins**; note the discrepancy in the plan and fix the plan.

## Facts you must not violate (verified on SDK 1.10; details in [docs/reachy-mini-api.md](../docs/reachy-mini-api.md))

1. **Only `MotionSession` calls `robot.set_target`.** The api never calls `set_target`, `goto_target`, `async_play_move`, `play_move`, `wake_up` or `goto_sleep` while a session is open. A daemon-side move (`goto_target`, …) makes the daemon **drop every `set_target`** for its duration; `async_play_move` is a second writer.
2. **`enable_motors()` pins the targets to the present pose** before torque returns. So after a pause the loop must **re-anchor**: read the present pose and blend from it, never from a stale last target.
3. **Never snap.** Every entry into a move goes through a `GotoMove` blend of `BLEND_S` seconds from the source pose to `move.evaluate(0)`.
4. **The loop runs in its own thread** on `time.monotonic()`; it never uses `asyncio` and never blocks the event loop. The api talks to it only through thread-safe commands and awaits `concurrent.futures.Future`s via `asyncio.wrap_future`.
5. **Neutral** is `INIT_HEAD_POSE` (identity) and `INIT_ANTENNAS_JOINT_POSITIONS` (`[-0.1745, 0.1745]`, **not** zeros) from `reachy_mini.reachy_mini`, body yaw `0.0`.
6. **Body yaw** is element `0` of the head-joint list returned by `get_current_joint_positions()`.
7. **Presence off** means: nothing is sent while idle. It never blocks an emotion.

## Scope

- `src/reachy_mini_bridge/config.py` — `MotionSettings` (`presence`, `breathing`) with the `from_*` trio; the `motion` block on `ReachyMiniConfig`.
- `config.example.json` — the `motion` block with its defaults.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — `set_target` recorded on `targets` (+ `last_target`); `get_current_head_pose` / `get_current_joint_positions` returning the last commanded values; `async_play_move` removed.
- `src/reachy_mini_bridge/motion.py` — replaces the placeholder: constants, `HoldMove`, `BreathingMove`, `blend_into`, `MotionSession`.
- `src/reachy_mini_bridge/api.py` — `play_emotion` via the session; `set_presence` / `presence`, `set_breathing` / `breathing`; tracking-weight record; `set_motors_state` pause / resume; lifecycle; `_FakeRecordedMove` becomes a real `Move`.
- `tests/test_config.py`, `tests/test_robot.py`, `tests/test_motion.py` (new), `tests/test_api.py`, `tests-e2e/test_api.py`.
- `specs/motion.md` frontmatter (`tests/test_motion.py`), the four spec statuses + `specs/_index.md`, `AGENTS.md` and `README.md` wording, this plan's status.

## Steps

### Step 1 — Config: the `motion` block

**Files:** `src/reachy_mini_bridge/config.py`, `config.example.json`, `tests/test_config.py`.

Copy the `AudioSettings` pattern (same file, "`audio` block" section) exactly:

```python
# --- `motion` block -----------------------------------------------------------------


@dataclass
class MotionSettings:
    """The motion loop's switches, applied when the session starts (specs/motion.md)."""

    # The background behaviour: idle moments are filled with the idle move.
    presence: bool = True
    # Which idle move presence plays: breathing, or a still neutral hold.
    breathing: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MotionSettings:
        block = _require_object(data, "motion")
        _reject_unknown_keys(block, "motion", {"presence", "breathing"})
        presence = block.get("presence", True)
        breathing = block.get("breathing", True)
        if not isinstance(presence, bool):
            raise ConfigError("'motion.presence' must be a boolean")
        if not isinstance(breathing, bool):
            raise ConfigError("'motion.breathing' must be a boolean")
        return cls(presence=presence, breathing=breathing)

    # from_json / from_json_file: identical to AudioSettings'
```

Then in `ReachyMiniConfig`: add the field `motion: MotionSettings = field(default_factory=MotionSettings)` after `wobbling`; add `"motion"` to the allowed top-level keys in `from_dict`; build it with `motion = MotionSettings.from_dict(top.get("motion", {}))` and pass it to `cls(...)`. Export `MotionSettings` in the module's `__all__` if one exists (check how `AudioSettings` is exported, and mirror it in `src/reachy_mini_bridge/__init__.py` only if `AudioSettings` is re-exported there).

Add to `config.example.json`, after `"wobbling": true,`:

```json
  "motion": {
    "presence": true,
    "breathing": true
  }
```

**Tests** (`tests/test_config.py`, next to the `audio` tests; copy their style):

- `test_motion_defaults_are_on`: `ReachyMiniConfig.from_dict({}).motion == MotionSettings()`.
- `test_motion_block_sets_the_switches`: `{"motion": {"presence": False, "breathing": False}}` → both `False`; `{"motion": {"breathing": False}}` → presence stays `True`.
- `test_motion_rejects_non_booleans`: `{"motion": {"presence": "yes"}}` and `{"motion": {"breathing": 1}}` each raise `ConfigError` naming the key.
- `test_motion_rejects_unknown_keys`: `{"motion": {"idle": True}}` raises `ConfigError` mentioning `idle`.
- `test_motion_must_be_an_object`: `{"motion": true}` raises `ConfigError`.
- Extend `test_from_json_file_round_trips_the_repo_example` with `assert cfg.motion == MotionSettings()`.

**Done when:** the full check passes (the example round-trip test now needs the block to be accepted).

### Step 2 — Fake: `set_target` and the pose readers; drop `async_play_move`

**Files:** `src/reachy_mini_bridge/fake_reachy_mini.py`, `tests/test_robot.py`, `tests/test_fake_reachy_mini.py`.

In `FakeReachyMini.__init__` add:

```python
# The motion loop's stream (specs/motion.md): recorded here, not on `commands`
# (a 60 Hz stream would swamp it). The readers return the last commanded values.
self.targets: list[tuple[npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None]] = []
self._head = np.eye(4)
self._antennas = np.array([-0.1745, 0.1745])  # upstream INIT_ANTENNAS_JOINT_POSITIONS
self._body_yaw = 0.0
```

Replace the `async_play_move` method (delete it entirely) with, in the "motion / expression" section:

```python
def set_target(
    self,
    head: npt.NDArray[np.float64] | None = None,
    antennas: npt.NDArray[np.float64] | list[float] | None = None,
    body_yaw: float | None = None,
) -> None:
    """Record one target of the motion loop's stream and remember it as the present pose."""
    if head is None and antennas is None and body_yaw is None:
        raise ValueError("At least one of head, antennas or body_yaw must be provided.")
    if head is not None:
        if head.shape != (4, 4):
            raise ValueError(f"Head pose must be a 4x4 matrix, got shape {head.shape}.")
        self._head = np.array(head, dtype=np.float64)
    if antennas is not None:
        if len(antennas) != 2:
            raise ValueError("Antennas must be a list or 1D np array with two elements.")
        self._antennas = np.array(antennas, dtype=np.float64)
    if body_yaw is not None:
        self._body_yaw = float(body_yaw)
    self.targets.append(
        (
            None if head is None else self._head.copy(),
            None if antennas is None else self._antennas.copy(),
            body_yaw,
        )
    )

@property
def last_target(self) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], float]:
    """The present pose as the fake knows it: the last commanded head, antennas, body yaw."""
    return self._head.copy(), self._antennas.copy(), self._body_yaw

def get_current_head_pose(self) -> npt.NDArray[np.float64]:
    return self._head.copy()

def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
    # Upstream: seven head joints, body yaw first; the fake does no kinematics.
    return [self._body_yaw, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], list(self._antennas)
```

The parameter **names and defaults** must match upstream `ReachyMini.set_target` / `get_current_head_pose` / `get_current_joint_positions` exactly (the parity test compares them with `inspect.signature`). Check them in `.venv/lib/python3.12/site-packages/reachy_mini/reachy_mini.py`.

In `tests/test_robot.py`, in `_CONSUMED_SLICE`, remove `"async_play_move"` and add `"set_target"`, `"get_current_head_pose"`, `"get_current_joint_positions"` to the `("", ReachyMini, name)` group.

**Tests** (`tests/test_fake_reachy_mini.py`, in the existing style):

- `test_set_target_records_and_updates_the_present_pose`: send a head with `[2, 3] = 0.01`, antennas `[0.1, -0.1]`, body yaw `0.2`; `targets` has one entry; `get_current_head_pose()[2, 3] == 0.01`; `get_current_joint_positions() == ([0.2, 0, 0, 0, 0, 0, 0], [0.1, -0.1])`; `commands` is unchanged.
- `test_set_target_partial_keeps_the_other_components`: send only antennas; the head reader still returns the identity.
- `test_set_target_rejects_bad_input`: no args, a `(3, 3)` head, three antennas → `ValueError`.

**Done when:** the full check passes. (`tests/test_api.py` will now fail on `async_play_move`; that is expected until step 5 — you may temporarily skip those tests with `pytest.mark.skip(reason="step 5")` and must remove the skips in step 6.)

### Step 3 — Moves: `HoldMove`, `BreathingMove`, `blend_into`

**Files:** `src/reachy_mini_bridge/motion.py` (replace the placeholder), `tests/test_motion.py` (new).

Write the module header docstring like `audio.py`'s (what it is, which spec, the one-writer rule). Then:

```python
from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from reachy_mini.motion.goto import GotoMove
from reachy_mini.motion.move import Move
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS, INIT_HEAD_POSE
from reachy_mini.utils.interpolation import InterpolationTechnique

if TYPE_CHECKING:
    import numpy.typing as npt

# Not 100 Hz: the conversation app's breathing at ~100 Hz shivers the Stewart platform
# (specs/motion.md open question 1).
CONTROL_HZ = 60.0
# Every entry into a move is a minjerk blend of this length (specs/motion.md "The loop").
BLEND_S = 0.5
# BreathingMove parameters — the conversation app's, seen on hardware.
BREATH_Z_M = 0.005
BREATH_HZ = 0.1
ANTENNA_SWAY_RAD = math.radians(15)
ANTENNA_HZ = 0.5

NEUTRAL_HEAD: npt.NDArray[np.float64] = np.array(INIT_HEAD_POSE, dtype=np.float64)
NEUTRAL_ANTENNAS: npt.NDArray[np.float64] = np.array(INIT_ANTENNAS_JOINT_POSITIONS, dtype=np.float64)
NEUTRAL_BODY_YAW = 0.0

# (head 4x4, antennas [right, left] rad, body yaw rad) — a fully specified pose.
type Pose = tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], float]

NEUTRAL: Pose = (NEUTRAL_HEAD, NEUTRAL_ANTENNAS, NEUTRAL_BODY_YAW)


class HoldMove(Move):
    """The idle move with breathing off: a still head at the neutral pose, forever."""

    @property
    def duration(self) -> float:
        return math.inf

    def evaluate(self, t: float):  # return type: copy Move.evaluate's annotation
        return NEUTRAL_HEAD.copy(), NEUTRAL_ANTENNAS.copy(), NEUTRAL_BODY_YAW


class BreathingMove(Move):
    """The idle move with breathing on: a slow z sine, antennas swaying in counter-phase."""

    @property
    def duration(self) -> float:
        return math.inf

    def evaluate(self, t: float):
        head = NEUTRAL_HEAD.copy()
        head[2, 3] += BREATH_Z_M * math.sin(2.0 * math.pi * BREATH_HZ * t)
        sway = ANTENNA_SWAY_RAD * math.sin(2.0 * math.pi * ANTENNA_HZ * t)
        antennas = NEUTRAL_ANTENNAS + np.array([sway, -sway])
        return head, antennas, NEUTRAL_BODY_YAW


def blend_into(source: Pose, move: Move, seconds: float = BLEND_S) -> GotoMove:
    """A minjerk `GotoMove` from `source` to `move.evaluate(0)` (specs/motion.md: never snap).

    A component the move leaves `None` keeps the source value (GotoMove does that).
    """
    head, antennas, body_yaw = move.evaluate(0.0)
    src_head, src_antennas, src_yaw = source
    return GotoMove(
        start_head_pose=src_head,
        target_head_pose=None if head is None else np.asarray(head, dtype=np.float64),
        start_antennas=src_antennas,
        target_antennas=None if antennas is None else np.asarray(antennas, dtype=np.float64),
        start_body_yaw=src_yaw,
        target_body_yaw=body_yaw,
        duration=seconds,
        method=InterpolationTechnique.MIN_JERK,
    )
```

Copy the exact return annotation of `Move.evaluate` from `reachy_mini/motion/move.py` so pyright accepts the overrides.

**Tests** (`tests/test_motion.py`, plain functions, no robot):

- `test_hold_is_neutral_at_any_time`: `HoldMove().evaluate(t)` equals `NEUTRAL` for `t` in `0, 1.5, 100`; `duration` is `inf`.
- `test_breathing_starts_at_neutral_and_breathes_in_z`: `evaluate(0)` equals neutral (use `np.allclose`); at `t = 1 / (4 * BREATH_HZ)` (a quarter period, 2.5 s) `head[2, 3] == pytest.approx(BREATH_Z_M)`; only `head[2, 3]` differs from the identity; body yaw is `0.0`.
- `test_breathing_antennas_sway_in_counter_phase`: at `t = 1 / (4 * ANTENNA_HZ)` (0.5 s) `antennas == NEUTRAL_ANTENNAS + [ANTENNA_SWAY_RAD, -ANTENNA_SWAY_RAD]` (approx).
- `test_blend_into_goes_from_source_to_the_moves_start`: source = a head with `[2, 3] = 0.02`, antennas `[0.5, -0.5]`, yaw `0.3`; `b = blend_into(source, HoldMove())`; `b.duration == BLEND_S`; `b.evaluate(0)` ≈ source; `b.evaluate(BLEND_S)` ≈ neutral; `b.evaluate(BLEND_S / 2)` strictly between on `head[2, 3]`.

**Done when:** the full check passes.

### Step 4 — `MotionSession`: the thread

**Files:** `src/reachy_mini_bridge/motion.py`, `tests/test_motion.py`.

Add to `motion.py`. Design constraints: **all mutable state lives on the thread**; the event loop only enqueues closures. The `robot` is typed `AnyReachyMini` (import under `TYPE_CHECKING` from `.robot`).

```python
import concurrent.futures
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_logger = logging.getLogger(__name__)


@dataclass
class _Primary:
    """A queued primary move and the future the api awaits for it."""
    move: Move
    sound_path: Path | None
    done: concurrent.futures.Future[None] = field(default_factory=concurrent.futures.Future)


@dataclass
class _Playing:
    """What the loop is currently evaluating: an entry blend, then the move itself."""
    stages: list[Move]            # [blend, move]  (or [blend] alone for the exit blend)
    primary: _Primary | None      # None for an idle move / the exit blend
    stage: int = 0
    stage_start: float = 0.0      # monotonic time the current stage began
    exit_blend: bool = False      # True for close()'s final blend to neutral
    sound_started: bool = False


class MotionSession:
    """The one writer of the robot's target: a 60 Hz thread (specs/motion.md).

    Started paused; the api resumes it once the motors read ``enabled``. Every public
    method is safe to call from the event loop and returns at once; the thread applies
    it at the top of its next tick.
    """

    def __init__(self, robot: AnyReachyMini, *, presence: bool, breathing: bool) -> None:
        self._robot = robot
        self._commands: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="reachy-mini-motion", daemon=True)
        # --- thread-owned state (touch only from closures run by the thread) ---
        self._presence = presence
        self._breathing = breathing
        self._paused = True
        self._commanding = False          # sent a target on the previous tick
        self._last_target: Pose = NEUTRAL
        self._queue: list[_Primary] = []  # pending primaries, FIFO
        self._playing: _Playing | None = None
        self._stop = False
```

Public methods, each `self._commands.put(closure)` and nothing else (except `submit`, which also returns the future, and `close`):

- `submit(move, sound_path) -> concurrent.futures.Future[None]`: builds a `_Primary`, enqueues `lambda: self._on_submit(p)`, returns `p.done`. `_on_submit` appends to `self._queue` **and resumes** (`self._paused = False`) — spec: `play_emotion` resumes the loop.
- `set_presence(enabled)`, `set_breathing(enabled)`: `_on_set_presence` / `_on_set_breathing`: if unchanged → return (idempotent). Record. If a primary is playing (`self._playing and self._playing.primary`) → return (applies when the queue drains). Otherwise `self._playing = None` so the next tick re-selects the idle move (blend from `_last_target`); for presence **off** while idle also set `self._commanding = False` (goes quiet at once, no easing).
- `pause()`: `_on_pause`: `self._paused = True; self._commanding = False; self._playing = None`; fail every pending / in-flight primary with `BridgeError("the motors left 'enabled': the motion loop paused")` via `done.set_exception(...)` (guard `if not p.done.done()`); clear the queue.
- `resume()`: `_on_resume`: `self._paused = False` (the next tick re-anchors because `_commanding` is `False`).
- `close()` (blocking, called under `asyncio.to_thread`): enqueue `_on_close`, then `self._thread.join(timeout=BLEND_S + 2.0)`. `_on_close`: cancel pending primaries (`done.cancel()`), clear the queue; if `self._playing` has a primary, cancel its future; if presence is on and `self._commanding` and not paused → `self._playing = _Playing(stages=[blend_into(self._last_target, HoldMove())], primary=None, exit_blend=True)` (the loop stops when it ends); else `self._stop = True`.
- Properties `presence`, `breathing` read the flags (benign races are fine for reads).
- `async def __aenter__`: `self._thread.start()`; return self. `async def __aexit__`: `await asyncio.to_thread(self.close)`. (Importing `asyncio` here only for these two is fine.)

The thread:

```python
def _run(self) -> None:
    period = 1.0 / CONTROL_HZ
    next_tick = time.monotonic()
    while not self._stop:
        self._drain_commands()
        if self._stop:
            break
        if not self._paused:
            try:
                self._tick(time.monotonic())
            except Exception as e:  # noqa: BLE001 - the loop must survive a bad tick
                _logger.warning("motion tick failed: %s", e)
                self._fail_current(e)
        next_tick += period
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_tick = time.monotonic()  # fell behind: don't burst to catch up

def _drain_commands(self) -> None:
    while True:
        try:
            self._commands.get_nowait()()
        except queue.Empty:
            return
```

`_tick(now)`:

1. If `self._playing` is not `None` and its primary's future is cancelled (`done.cancelled()` — `asyncio.wrap_future` propagates the api's cancel to it) → `self._playing = None` (drop it; the head stays where it is; step 3 below blends the idle in).
2. If `self._playing` is not `None`: `stage = playing.stages[playing.stage]`; `t = now - playing.stage_start`; if `t >= stage.duration` → advance: `playing.stage += 1; playing.stage_start = now; t = 0.0`; if that was the last stage → finish: resolve the primary's future (`if not done.done(): done.set_result(None)`), if `exit_blend` set `self._stop = True` and return, then `self._playing = None`.
3. If `self._playing` is `None`: pick the next: pop the first pending primary whose future is not cancelled, else the idle move: `BreathingMove()` if presence and breathing, `HoldMove()` if presence, else `None`. With `None`: `self._commanding = False`; return. Otherwise `source = self._last_target if self._commanding else self._read_present_pose()`; `self._playing = _Playing(stages=[blend_into(source, move), move], primary=primary)` with `stage_start = now`; `t = 0.0`.
4. Evaluate: `head, antennas, body_yaw = stage.evaluate(t)` (clamp `t` to `min(t, stage.duration - 1e-3)` for finite stages, as upstream does). If the stage is the primary's own move (`playing.stage == 1 and playing.primary`) and `not playing.sound_started` and `sound_path` → `self._robot.media.play_sound(str(sound_path))`, set `sound_started`.
5. Fill `None` components from `self._last_target`; `self._robot.set_target(head=head, antennas=antennas, body_yaw=body_yaw)`; `self._last_target = (head, antennas, body_yaw)`; `self._commanding = True`.

`_read_present_pose()`: `head = self._robot.get_current_head_pose()`; `joints, antennas = self._robot.get_current_joint_positions()`; return `(np.asarray(head, float), np.asarray(antennas, float), float(joints[0]))`.

`_fail_current(e)`: if `self._playing` has a primary whose future is not done → `set_exception(e)`; `self._playing = None`.

**Tests** (`tests/test_motion.py`, driving the session on a bare `FakeReachyMini()`; helper that starts it: `s = MotionSession(robot, presence=True, breathing=True); s._thread.start()` is acceptable *only* via the public `asyncio.run(s.__aenter__())` — prefer `async with` inside `asyncio.run`):

- `test_paused_session_sends_nothing`: enter, sleep 0.2 s, `robot.targets == []`.
- `test_resume_blends_from_the_present_pose_into_breathing`: put the fake at a head with `[2, 3] = 0.02` (call `robot.set_target(head=...)` directly *before* resuming, simulating another writer), `resume()`, sleep `BLEND_S + 0.3`; the first recorded target after resume has `head[2, 3] ≈ 0.02` (the anchor), the one at `~BLEND_S` is within 1 mm of neutral, and no two consecutive targets differ by more than 2 mm in z (no snap). Rate: number of targets in 1 s is within `[40, 70]`.
- `test_breathing_off_holds_neutral`: `set_breathing(False)`, resume, sleep `BLEND_S + 0.2`; every target after the blend equals neutral.
- `test_presence_off_goes_quiet_when_idle`: presence `False`, resume, sleep 0.3 s → no targets; `submit(HoldMove-like finite move)` → targets appear; after it ends (`future.result(timeout=2)`) + 0.2 s the count stops growing.
- `test_primary_plays_after_a_blend_then_idle_resumes`: submit a 0.3 s move whose `evaluate` puts `head[2, 3] = 0.03`; `future.result(timeout=2)` returns after ≈ `BLEND_S + 0.3` s (assert `≥ 0.7` and `< 1.2`); some target reached `z ≥ 0.025`; afterwards targets return within 1 mm of neutral within `BLEND_S + 0.2` s.
- `test_sound_starts_with_the_trajectory_not_the_blend`: a move with `sound_path = Path("x.ogg")`; the fake's `commands` gets `("media.play_sound", {"sound_file": "x.ogg"})` no earlier than `BLEND_S * 0.8` after `submit`.
- `test_primaries_are_fifo_and_exclusive`: submit two 0.3 s moves; the second's `play_sound` (give both sounds) is recorded after the first future resolves.
- `test_cancelled_future_drops_the_primary_within_a_tick`: submit a 2 s move, wait `BLEND_S + 0.2`, `future.cancel()`, sleep 0.1; the move's distinctive `z` is no longer in the last 3 targets, and targets keep flowing (idle resumed).
- `test_toggle_during_a_primary_is_deferred`: submit a 0.5 s move; after 0.2 s `set_breathing(False)`; the move still completes at its duration; afterwards targets settle at neutral (hold), not breathing (z stays `< 1e-4` for 0.5 s after the return blend).
- `test_pause_fails_in_flight_primaries`: submit a 2 s move; after 0.7 s `pause()`; `future.exception(timeout=1)` is a `BridgeError`; no targets after the pause.
- `test_close_eases_to_neutral_when_commanding`: breathing on, resume, sleep 1.0 s (the head is mid-breath); leave the `async with`; `robot.last_target` head is within 1 mm of neutral and antennas ≈ `NEUTRAL_ANTENNAS`; the last targets approach it without a step `> 2 mm`.
- `test_close_is_immediate_when_quiet`: presence off, enter, exit → returns in `< 0.2` s, no targets.
- `test_a_failing_tick_fails_the_primary_and_keeps_the_loop_alive`: a move whose `evaluate` raises after `t > 0.1`; `future.exception(timeout=2)` is that error; targets keep flowing afterwards.

**Done when:** the full check passes, and `tests/test_motion.py` runs in under ~15 s total.

### Step 5 — Api wiring

**Files:** `src/reachy_mini_bridge/api.py`.

1. **State** in `__init__`: `self._motion: MotionSession | None = None`, `self._presence = self._config.motion.presence`, `self._breathing = self._config.motion.breathing`, `self._tracking_weight: float | None = None`. Add `_require_motion()` mirroring `_require_media()`.
2. **Lifecycle** in `__aenter__`, after the `if cfg.wobbling:` block: `motion = MotionSession(robot, presence=self._presence, breathing=self._breathing)`; `await stack.enter_async_context(motion)`; `self._motion = motion`; then `if await self.get_motors_state() == "enabled": motion.resume()`. Because the stack unwinds in reverse, the session exits **first** (before wobbling is disabled) — exactly the spec's order. In the `except BaseException` block and in `__aexit__` also clear `self._motion = None`, `self._tracking_weight = None`, and reset `self._presence` / `self._breathing` to the config values.
3. **Motors:** at the end of `set_motors_state`, after the upstream call: `if state == "enabled": self._require_motion().resume() else: self._require_motion().pause()`.
4. **Tracking record:** `start_head_tracking` sets `self._tracking_weight = weight` after the upstream call; `stop_head_tracking` sets it to `None`.
5. **Switches:**

```python
async def set_presence(self, enabled: bool) -> None:
    """..."""  # docstring from specs/api.md "Presence & breathing"
    self._presence = enabled
    self._require_motion().set_presence(enabled)

@property
def presence(self) -> bool:
    return self._presence
```
   Same for `set_breathing` / `breathing`. (They raise `BridgeError` outside a session like the other verbs; the properties read anywhere.)
6. **`play_emotion`** — replace the body after the motor check and move resolution:

```python
media = self._require_media()
motion = self._require_motion()
robot = self.robot
sound_path = getattr(move, "sound_path", None)
# Pause the two daemon-side layers for the move (specs/motion.md "Emotions through
# the loop"); restored below on every exit path, to their *current* record.
if self._tracking_weight is not None:
    await asyncio.to_thread(robot.start_head_tracking, 0.0)
if self._wobbling:
    await asyncio.to_thread(robot.disable_wobbling)
future = motion.submit(move, None if sound_path is None else Path(sound_path))
try:
    await asyncio.wrap_future(future)
except BaseException:
    future.cancel()  # idempotent; wrap_future already propagated a task cancel
    if sound_path is not None:
        media.stop_sound()
    raise
finally:
    await self._restore_layers_after_move()
```
   with

```python
async def _restore_layers_after_move(self) -> None:
    robot = self.robot
    for label, call in (
        ("wobbling", (lambda: robot.enable_wobbling()) if self._wobbling else None),
        ("head tracking", (lambda: robot.start_head_tracking(self._tracking_weight)) if self._tracking_weight is not None else None),
    ):
        if call is None:
            continue
        try:
            await asyncio.to_thread(call)
        except Exception as e:  # noqa: BLE001 - never mask the verb's own outcome
            _logger.warning("could not restore %s after the emotion: %s", label, e)
```
   (Write it as two plain `if` blocks if the tuple-of-lambdas reads badly; pyright must be happy.) Note the `await` inside `finally` after a cancel: the task's `CancelledError` has already been delivered, so these awaits run; a *second* cancel during the restore is acceptable (the mode is then left off, and a warning is logged where possible).
7. **The offline library:** turn `_FakeRecordedMove` into a `Move` subclass (a plain class, since `sound_path` is a property on `Move`): `__init__(self, name, sound_path=None)`, `duration` `0.3`, `sound_path` returning the ctor value, `evaluate(t)`: neutral with `head[2, 3] = 0.01 * math.sin(math.pi * t / self.duration)` (a small rise-and-fall so tests can spot it), neutral antennas, yaw `0.0`. Keep `_FakeRecordedMoves.get` raising `ValueError` on an unknown name and `"sad"` soundless.
8. Update the module docstring (the api now has two sessions) and the `play_emotion` docstring from `specs/api.md`.

**Done when:** `uv run pyright` is clean and `uv run pytest tests/test_api.py` runs (failures in the old emotion tests are expected until step 6).

### Step 6 — Fast tests for the api

**Files:** `tests/test_api.py`. Add a helper next to `_command_names`:

```python
def _head_z(api: ReachyMiniApi) -> list[float]:
    return [float(h[2, 3]) for h, _, _ in _fake(api).targets if h is not None]
```

Rewrite / add (remove any temporary skips from step 2):

- `test_play_emotion_plays_through_the_motion_loop`: enable motors, `play_emotion("happy")`; `max(_head_z(api)) >= 0.008`; `("media.play_sound", {"sound_file": "happy.ogg"})` in `commands`; the fake has no `async_play_move` attribute.
- `test_play_emotion_on_the_fake_takes_the_moves_duration`: keep; bound becomes `>= BLEND_S + 0.25`.
- `test_two_emotions_play_one_after_the_other`: two concurrent `play_emotion` tasks; both complete; the two `media.play_sound` commands are ≥ 0.3 s apart (record `time.monotonic()` per command by patching? no — assert via completion times of the two tasks: the second finishes ≥ 0.3 s after the first).
- `_cancel_emotion_mid_flight`: wait until `"media.play_sound" in _command_names(api)` for a sounded move, or `await asyncio.sleep(BLEND_S + 0.1)` for `"sad"`; then cancel as today. Keep `test_cancelled_play_emotion_stops_the_sound_and_keeps_the_session` (the `stop_sound` → `clear_player` order, `elapsed < 0.05`, `say` afterwards) and `test_cancelled_soundless_emotion_does_not_stop_a_sound`. Add to the sounded case: `len(_fake(api).targets)` still grows 0.3 s after the cancel (idle resumed).
- `test_play_emotion_failure_stops_the_sound_and_propagates`: patch `_FakeRecordedMoves.get` (monkeypatch, like the existing test) to return a move whose `evaluate` raises `RuntimeError` for `t > 0.05`; `play_emotion` raises that error; `media.stop_sound` recorded; a following `say` works.
- `test_play_emotion_pauses_tracking_and_restores_it`: `start_head_tracking(0.7)`; `play_emotion("sad")`; the `start_head_tracking` commands read weights `[0.7, 0.0, 0.7]` in order.
- `test_play_emotion_leaves_tracking_alone_when_off`: no tracking; no `start_head_tracking` command at all.
- `test_play_emotion_pauses_wobbling_and_restores_it`: wobbling on (default); commands around the move: `disable_wobbling` before `media.play_sound`, `enable_wobbling` after; `api.wobbling` still `True`.
- `test_play_emotion_restores_layers_after_a_cancel`: tracking at `0.7`; cancel mid-flight; the last `start_head_tracking` weight is `0.7` and the last wobbling command is `enable_wobbling`.
- `test_play_emotion_restores_to_the_current_record`: start with tracking `0.7`; mid-emotion call `stop_head_tracking()`; after the emotion the last tracking command is `stop_head_tracking` (no restore to `0.7`).
- `test_breathing_targets_oscillate_in_z`: enable motors; sleep 1.3 s; `z = _head_z(api)[-20:]`; `max(z) - min(z) > 0.0005` and every `abs(z) <= BREATH_Z_M + 1e-6`; the last target's antennas are not equal to neutral and their offsets from neutral have opposite signs.
- `test_breathing_off_holds_neutral`: config `motion.breathing = False` (`ReachyMiniConfig(backend="fake", motion=MotionSettings(breathing=False))`); enable; sleep `BLEND_S + 0.3`; the last 10 targets equal neutral.
- `test_presence_off_sends_nothing_when_idle`: `MotionSettings(presence=False)`; enable; sleep 0.3 → `targets == []`; `play_emotion("sad")` → targets appear; 0.3 s after it returns the count is unchanged; `api.presence is False`.
- `test_set_breathing_while_idle_eases_to_neutral`: breathing on, enable, sleep 1.0; `set_breathing(False)`; sleep `BLEND_S + 0.2`; last targets equal neutral, no z step `> 2 mm` between consecutive targets.
- `test_set_presence_on_resumes_from_the_present_pose`: presence off; enable; move the fake by `api.robot.set_target(head=...)` with `z = 0.02` (simulating the caller's raw control); `set_presence(True)`; the first target after has `z ≈ 0.02`, then it settles at neutral.
- `test_switches_are_recorded_and_default_from_the_config`: properties read the config values before entry; `set_presence(False)` flips the property; after exit they read the config values again.
- `test_switch_verbs_require_entry`: `set_presence` / `set_breathing` outside a session raise `BridgeError`.
- `test_motors_disabled_pauses_the_loop_and_enabled_resumes_anchored`: enable, sleep 0.3, disable, sleep 0.3 → count stops growing; move the fake's pose directly; enable → the first new target starts from that pose.
- `test_exit_leaves_the_head_at_neutral`: enable, sleep 1.0, exit; `api.robot` is gone, so keep a reference to the fake: `last_target` head within 1 mm of neutral; the `__exit__` command comes after the last `set_target` (compare: no target is appended after exit).
- `test_wobbling_is_on_by_default_at_entry_and_off_at_exit` and the other wobbling tests: still pass; the exit order is now motion → wobbling → media (a test may assert `disable_wobbling` is recorded after the last target).
- `test_cancel_during_bring_up_exits_the_robot`: still passes (the session is the last step).

**Done when:** the full check passes; `uv run pytest` stays under ~30 s.

### Step 7 — Live tests

**Files:** `tests-e2e/test_api.py`. All gated with `requires_caps(live_api, "motion")`; use `_require_emotions_library()` where a move plays. The sim tracks its target closely, so pose reads are reliable.

- `test_breathing_moves_the_head_and_breathing_off_holds_it`: `set_motors_state("enabled")`; sleep 1 s; sample `robot.get_current_head_pose()[2, 3]` every 0.1 s for 6 s → `max − min ≥ 0.002`; `set_breathing(False)`; sleep `BLEND_S + 0.5`; sample 3 s → `max − min < 0.001`; finally `set_breathing(True)`.
- `test_play_emotion_plays_a_real_move`: keep; add after the move: sleep 1.5 s, then the head translation `norm(pose[:3, 3]) < 0.008` and antennas within 0.1 rad of neutral (it came back to the idle move).
- `test_cancelled_emotion_stops_motion_and_sound`: add `await api.set_breathing(False)` before the dance (the hold keeps the joints still after the return blend, so the existing `travel < 0.02` bound holds); keep the rest.
- **Stillness tests:** `test_wobbling_off_keeps_the_head_still_while_audio_plays` and `test_wobbling_is_on_by_default_and_sways_the_head` call `await api.set_breathing(False)` and wait `BLEND_S + 0.5` before measuring (breathing would otherwise move the head by up to 5 mm).
- `test_motor_state_reads_and_dispatches_over_the_live_path` uses `robot.goto_target` to lower the head before torque goes off: call `await api.set_presence(False)` first (the loop would otherwise blend back to neutral the moment the goto ends), and restore `set_presence(True)` at the end.

Run: `uv run pytest tests-e2e -rs` (headless sim). Every motion test must **pass**, not skip.

### Step 8 — On-robot checks (manual)

On a Reachy Mini Lite over USB (`REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e -rs`, then a small script over `ReachyMiniApi("real")` with `daemon.spawn = "auto"`), walk this list and write the outcome of each line under Verification below:

- [ ] Idle robot breathes visibly, with no micro-vibration and no slow downward drift over 2 minutes.
- [ ] Face tracking: stand in front of the robot (it follows), leave the frame — after ~3 s the head breathes again; come back — it follows again with no jump either way.
- [ ] Breaths are separated by visible rests of varying length, the antennas move one at a time and never toward each other, and neither the start nor the end of a breath or an antenna move shows a snap (plan [202609171234](202609171234_organic-breathing-rests-and-independent-antennas.md)).
- [x] The idle head visibly looks about — turning, tilting and nodding a few degrees — without drifting away from neutral over 2 minutes, and the antennas read as expressive: a mix of quick flicks and slower roams, still one at a time and never toward each other, with no snap at any segment boundary (plan [202609172115](202609172115_expressive-idle-head-rotation-and-antenna-flicks.md)). **Confirmed on a robot, 2026-09-17: the idle reads markedly better than the previous one.** The finer sub-claims — no drift over a full two minutes, no snap at any segment boundary — were not walked separately; watch for them when the rest of this checklist is done.
- [ ] An emotion interrupts breathing, plays fully, eases back to neutral, breathing resumes.
- [ ] `set_breathing(False)` while idle eases the head to neutral and holds still.
- [ ] `set_breathing(True)` while idle resumes breathing without a visible jump.
- [ ] Toggling breathing mid-emotion does not disturb the emotion; it applies on the next idle.
- [ ] With breathing off, an emotion still plays and returns to neutral.
- [ ] With presence off, an emotion plays and the head stays where it ended; `set_presence(True)` eases it back.
- [ ] With breathing off, wobbling (`say`) and face tracking still work.
- [ ] Audio (`say`) wobbles on top of breathing.
- [ ] Face tracking follows a person and yields cleanly during an emotion, then resumes.
- [ ] `set_motors_state("disabled")`, push the head down by hand, `set_motors_state("enabled")`: the head eases into the idle move, no snap.
- [ ] Leaving `async with` leaves the robot at neutral with motors enabled; a daemon the bridge spawned then puts it to sleep.

If micro-vibration shows at 60 Hz: try `CONTROL_HZ = 50.0`, then a longer `BLEND_S`, before touching amplitudes; record what worked in `specs/motion.md` open question 1 (and keep the constant that worked).

### Step 9 — Statuses and docs

- `specs/motion.md`: add `tests/test_motion.py` under `tests:` in the frontmatter; if step 8 changed a constant, update the number in the spec too. **Done.**
- Flip statuses, both in each file's `**Status:**` line and in `specs/_index.md`. Correction to this plan (AGENTS.md's discipline wins: `Implemented` requires a `Done` plan, and this plan's own Verification below gates `Done` on the step 8 hardware checklist): `motion.md` `Draft` → `Stable` now (design settled, code matches it, only genuine deferrals left as open questions) — **done**; `motion.md` and `api.md` / `config.md` / `robot.md` `Updated` → `Implemented` only once this plan is `Done`.
- `AGENTS.md` project map and `README.md`: drop "(placeholder; Draft)" / "placeholder" for `motion.py` — **done**; add a "Staying alive" row to the README feature table (SDK alone: nothing; bridge: breathing / neutral hold between verbs, emotions blended in and out, one writer of the target) — **done**.
- Mark this plan `Done` here and in [_index.md](_index.md) — **not yet**: see Verification.

## Verification

- `uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest` — all green. **Done** (244 tests).
- `uv run pytest tests-e2e -rs` on the headless sim: the breathing, emotion, cancel and stillness tests **pass** (a skip is not a pass — read the reasons). **Done** (11 passed; the 3 skips are the pre-existing capability gaps of a headless sim — `gravity_compensation`, `camera`, and the sim-ignores-motor-modes case — not motion tests).
- The step 8 checklist walked on hardware, outcomes recorded here (one line per item, date, robot, SDK version). **Not done — no hardware was available in this session.** Whoever has a Reachy Mini Lite next should walk Step 8 and then flip `motion.md` / `api.md` / `config.md` / `robot.md` to `Implemented` and this plan to `Done`.

Mark this plan `Done` (here and in [_index.md](_index.md)) only once all three hold.
