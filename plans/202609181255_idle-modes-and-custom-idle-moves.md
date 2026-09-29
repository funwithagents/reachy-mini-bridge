# Idle modes and custom idle moves

**Status:** Done

Implements the `idle` mode and custom idle moves of [specs/motion/motion.md](../specs/motion/motion.md) ("Presence and the idle mode", "The moves", "Custom idle moves"), [specs/core/config.md](../specs/core/config.md) ("`motion` block") and [specs/core/bridge.md](../specs/core/bridge.md) ("Presence & the idle move"), plus the control panel's Idle radio ([specs/examples/control_panel.md](../specs/examples/control_panel.md)). The `breathing` on/off switch becomes an `idle` mode with three values — `"breathing"`, `"hold"`, `"custom"` — and a caller can register their own idle move (an `IdleMove` subclass, built by a factory) that plays in the `"custom"` mode.

Deliberately leaves out: naming a custom move from a JSON file by import path, and a bridge-side amplitude clamp on custom offsets (both are open questions in `specs/motion/motion.md`); any change to `BreathingMove`'s animation; a backward-compatible `breathing` alias (the old key and verbs are removed, not deprecated).

**Every code block in this plan was run**: the final state and the intermediate state after step 1 both pass `ruff`, `pyright` and the full fast test suite, and the live tests in step 3 pass on the headless sim. Copy the blocks as they are.

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md) ("Commands", "Verification", "Keeping statuses current"), then [specs/motion/motion.md](../specs/motion/motion.md) sections "Presence and the idle mode", "The moves" and "Custom idle moves". The specs are **already updated** for this plan: they are the design. Do not edit `specs/` except for the status changes in step 5. If this plan and a spec disagree, the spec wins; note it in this file.
- **Do the steps in order.** Each step ends green. After each step run:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

  Do **not** run `uv run ruff format .` — it rewrites the Python blocks inside `plans/*.md`. Format the code directories only, as above.
- **When a step says "replace all N occurrences"**, check the count with `grep -c -F '<text>' <file>` before and confirm `0` after. A different count means you are in the wrong file or the file changed: stop and look.
- **Do not commit** unless asked. Do not touch `docs/upstream-*.md` or `specs/_analysis.md` (pre-existing untracked files).
- Set this plan's status to `In progress` (here and in [_index.md](_index.md)) when you start.

## Vocabulary

| Word | Meaning |
|---|---|
| **idle move** | The move the motion loop plays when no emotion is queued and presence is on. |
| **idle mode** | Which idle move plays: `"breathing"` (built-in `BreathingMove`), `"hold"` (`HoldMove`, a still neutral), `"custom"` (the caller's). |
| **`IdleOffsets`** | A frozen dataclass: the pose as offsets **from neutral** in **human units** (mm, degrees). `IdleOffsets()` is neutral. |
| **`IdleMove`** | Base class of animated idle moves. One abstract method: `offsets(t) -> IdleOffsets`. `BreathingMove` becomes one. `HoldMove` is **not** one. |
| **factory** | A zero-argument callable returning a fresh `IdleMove`. An `IdleMove` subclass is a factory (`SlowNod` itself, not `SlowNod()`). The loop calls it at every idle entry. |
| **fade-out** | Leaving a playing `IdleMove`: it keeps playing while its offsets are scaled 1 → 0 over `BLEND_S`, landing at neutral at rest. |

## Facts you must not violate

1. **`IdleOffsets().pose()` and `anything.pose(0.0)` equal `NEUTRAL` bit-for-bit** (`np.array_equal`, verified). Keep the arithmetic of `pose()` exactly as given.
2. **`BreathingMove`'s animation does not change.** Only its `offsets()` return type changes (human units), and it inherits `duration` and `evaluate` from `IdleMove`. The tracks still run in metres and radians. Every existing seeded test must still pass.
3. **Antenna order is `[right, left]`** and "outward" is `ANTENNA_OUTWARD = [-1, +1]`. `antenna_right_deg` is index 0.
4. **`HoldMove` stays a plain `Move`.** `_playing_idle()` returns `None` for it, so leaving the hold is a plain re-blend, with no half-second fade of nothing.
5. **The factory is checked on the caller's thread** (`MotionSession.__init__` and `MotionSession.set_idle_move`, before the command is queued) and raises `ValueError`. A rejected factory changes nothing.
6. **On the motion thread a bad custom move never raises out of the loop and never floods the log**: it raises `_CustomIdleError`, which `_run` catches → one `WARNING`, `_idle_move_failed = True`, the hold plays instead. The `except _CustomIdleError` clause must come **before** `except Exception`.
7. **Setting the same idle mode is a no-op. Registering an idle move always re-enters the custom idle** (even the same factory) and clears the failed mark.
8. **An emotion is never interrupted**: with a primary playing, `_reenter_idle()` does nothing; the change applies when the queue drains.
9. **`config.py` must still import without `reachy_mini`**: `IdleMove` is imported there under `TYPE_CHECKING` only.
10. **Bad input is `ValueError`** (specs/core/bridge.md "Errors"), a malformed config is `ConfigError` (a `ValueError` subclass).

## Scope

- `src/reachy_mini_bridge/motion.py` — `IdleOffsets` (replaces `_IdleOffsets`), `IdleMove`, `IdleMode` / `IdleMoveFactory` aliases; `BreathingMove` on `IdleMove`; `_IdleFadeOut` (replaces `_BreathingFadeOut`); `_CustomIdleError`, `_checked_offsets`, `_build_custom_idle`, `check_idle_move_factory`, `_CustomIdle`; `MotionSession` takes `idle` / `idle_move`, gains `set_idle` / `set_idle_move` / `idle` / `idle_move`, loses `set_breathing` / `breathing`.
- `src/reachy_mini_bridge/config.py` — `IDLE_MODES`; `MotionSettings.idle` + `idle_move` replace `breathing`.
- `src/reachy_mini_bridge/api.py` — `set_idle` / `idle`, `set_idle_move` / `idle_move` replace `set_breathing` / `breathing`.
- `src/reachy_mini_bridge/__init__.py` — re-exports `IdleMove`, `IdleOffsets`.
- `config.example.json` — `"idle": "breathing"` replaces `"breathing": true`.
- `examples/control_panel/controller.py`, `examples/control_panel/app.py` — `idle` in `PanelState`, `set_idle`, an Idle radio.
- `tests/test_motion.py`, `tests/test_config.py`, `tests/test_api.py`, `tests/test_control_panel.py`, `tests-e2e/test_api.py` — renames plus the new tests below.
- `README.md`, `AGENTS.md` — wording.
- `plans/202609162000_motion-loop-presence-and-breathing.md` — its still-open on-robot checklist uses the new verbs and gains one custom-idle line.
- `specs/examples/control_panel.md` + `specs/_index.md` — status `Updated` → `Implemented` (step 5). `plans/_index.md` and this file — status.

## Steps

### Step 0 — Baseline

Run the check command from "How to work this plan". Everything must be green before you change anything. If it is not, stop and report.

### Step 1 — `motion.py`: idle offsets, idle moves, the idle mode in the loop

**Files:** `src/reachy_mini_bridge/motion.py`, `tests/test_motion.py`, and two one-line shims in `src/reachy_mini_bridge/api.py` (removed in step 2).

**1a. Imports and `__all__`.** Add `from abc import abstractmethod` next to the other standard-library imports (keep them sorted: it goes right after `import time`, before `from collections.abc import Callable`). Replace `__all__` with:

```python
__all__ = [
    "BreathingMove",
    "HoldMove",
    "IdleMode",
    "IdleMove",
    "IdleMoveFactory",
    "IdleOffsets",
    "MotionSession",
]
```

**1b. Replace the whole `_IdleOffsets` class** (from its `@dataclass(frozen=True)` line down to the line before `class BreathingMove`) with:

```python
@dataclass(frozen=True)
class IdleOffsets:
    """An idle move's signed offsets from neutral at one instant, in human units
    (specs/motion/motion.md "The moves"). ``IdleOffsets()`` is neutral.

    ``pose(scale)`` is the single place offsets become a pose: at ``1.0`` it is the
    move's pose, at ``0.0`` it is exactly ``NEUTRAL``, and the values between are the
    envelope ``_IdleFadeOut`` rides out on.
    """

    z_mm: float = 0.0  # head height above neutral
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_deg: float = 0.0
    # Each antenna's lean outward beyond its neutral lean; negative leans inward.
    antenna_right_deg: float = 0.0
    antenna_left_deg: float = 0.0

    def pose(self, scale: float = 1.0) -> Pose:
        # Scaling the Euler angles rather than slerping is indistinguishable at idle
        # amplitudes, and lands exactly on the identity at scale 0.
        head = create_head_pose(
            z=scale * self.z_mm / 1000.0,
            roll=math.radians(scale * self.roll_deg),
            pitch=math.radians(scale * self.pitch_deg),
            yaw=math.radians(scale * self.yaw_deg),
            degrees=False,
        )
        leans = np.radians(
            scale * np.array([self.antenna_right_deg, self.antenna_left_deg])
        )
        return head, NEUTRAL_ANTENNAS + ANTENNA_OUTWARD * leans, NEUTRAL_BODY_YAW


class IdleMove(Move):
    """Base class of every animated idle move (specs/motion/motion.md "The moves"): infinite,
    and described as offsets from neutral so the loop can fade it out to neutral at
    rest. Subclass it and implement ``offsets`` to write a custom idle move."""

    @property
    def duration(self) -> float:
        return math.inf

    @abstractmethod
    def offsets(self, t: float) -> IdleOffsets:
        """The pose's offsets from neutral at ``t`` seconds into this idle entry."""

    def evaluate(
        self, t: float
    ) -> tuple[
        npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
    ]:
        return self.offsets(t).pose()


# A zero-argument callable building a fresh idle move; the loop calls it at every idle
# entry. An ``IdleMove`` subclass is one.
type IdleMoveFactory = Callable[[], IdleMove]
# The idle mode (specs/motion/motion.md "Presence and the idle mode"); config.IDLE_MODES holds
# the same three values for the config layer, which cannot import this module.
type IdleMode = Literal["breathing", "hold", "custom"]
```

**1c. `BreathingMove`.** Change `class BreathingMove(Move):` to `class BreathingMove(IdleMove):`. Leave `__init__` untouched. **Delete** its `duration` property, its `offsets` method and its `evaluate` method (the three members after `__init__`), and put this one method in their place — `duration` and `evaluate` now come from `IdleMove`:

```python
    def offsets(self, t: float) -> IdleOffsets:
        """Every track's offset from neutral at ``t`` (the tracks run in metres and
        radians; ``IdleOffsets`` is in human units)."""
        roll, pitch, yaw = (math.degrees(track.value(t)) for track in self._rotations)
        right, left = (
            math.degrees(track.value(t) - ANTENNA_MIN_RAD) for track in self._antennas
        )
        return IdleOffsets(
            z_mm=self._breath.value(t) * 1000.0,
            roll_deg=roll,
            pitch_deg=pitch,
            yaw_deg=yaw,
            antenna_right_deg=right,
            antenna_left_deg=left,
        )
```

**1d. Replace the whole `_BreathingFadeOut` class** (down to the line before `def blend_into(`) with the fade-out generalised to any `IdleMove`, plus the custom-move machinery:

```python
class _IdleFadeOut(Move):
    """Leaving an idle move mid-plan (specs/motion/motion.md "The moves"): keep playing ``move``
    from ``t_offset`` while a minjerk envelope scales every offset from neutral down to
    zero over ``duration`` — landing at neutral at rest, so whatever follows (a blend,
    or nothing) starts from a source that is actually at rest. A plain blend assumes
    that, and a track caught mid-segment is not at rest.
    """

    def __init__(
        self, move: IdleMove, t_offset: float, duration: float = BLEND_S
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
        return self._move.offsets(self._t_offset + t).pose(envelope)


class _CustomIdleError(Exception):
    """A caller's idle move (or its factory) misbehaved on the motion thread."""


def _checked_offsets(move: IdleMove, t: float) -> IdleOffsets:
    """``move.offsets(t)``, or ``_CustomIdleError`` when it raises or returns anything
    but an ``IdleOffsets`` of finite numbers."""
    try:
        offsets = move.offsets(t)
    except Exception as e:
        raise _CustomIdleError(f"offsets({t:.3f}) raised: {e!r}") from e
    if not isinstance(offsets, IdleOffsets):
        raise _CustomIdleError(
            f"offsets({t:.3f}) returned {type(offsets).__name__}, not IdleOffsets"
        )
    values = (
        offsets.z_mm,
        offsets.roll_deg,
        offsets.pitch_deg,
        offsets.yaw_deg,
        offsets.antenna_right_deg,
        offsets.antenna_left_deg,
    )
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise _CustomIdleError(f"offsets({t:.3f}) holds a non-finite value: {offsets}")
    return offsets


def _build_custom_idle(factory: Callable[[], object]) -> IdleMove:
    """Call ``factory``; ``_CustomIdleError`` when it raises or builds no ``IdleMove``."""
    try:
        move = factory()
    except Exception as e:
        raise _CustomIdleError(f"the idle move factory raised: {e!r}") from e
    if not isinstance(move, IdleMove):
        raise _CustomIdleError(
            f"the idle move factory returned {type(move).__name__}, not an IdleMove"
        )
    return move


def check_idle_move_factory(factory: object) -> None:
    """Registration-time check (specs/motion/motion.md "Custom idle moves"): ``ValueError``
    unless ``factory`` is a callable building an ``IdleMove`` whose ``offsets(0.0)`` is
    an ``IdleOffsets`` of finite numbers. Runs on the caller's thread."""
    if not callable(factory):
        # ValueError, not TypeError: the api's one error for bad input (specs/core/bridge.md)
        raise ValueError(  # noqa: TRY004
            "an idle move factory must be a zero-argument callable returning an "
            f"IdleMove (an IdleMove subclass is one), got {type(factory).__name__}"
        )
    try:
        _checked_offsets(_build_custom_idle(factory), 0.0)
    except _CustomIdleError as e:
        raise ValueError(f"invalid idle move: {e}") from e


class _CustomIdle(IdleMove):
    """The caller's idle move as the loop plays it: every ``offsets`` call checked, so
    a misbehaving move surfaces as ``_CustomIdleError`` whatever stage reads it."""

    def __init__(self, move: IdleMove) -> None:
        self._move = move

    def offsets(self, t: float) -> IdleOffsets:
        return _checked_offsets(self._move, t)
```

Also fix the one remaining mention: in `_fade_in`'s docstring, `_BreathingFadeOut` → `_IdleFadeOut`.

**1e. `MotionSession.__init__`.** Replace the signature and the `self._breathing = breathing` line. The start of `__init__` becomes (everything from `self._lost = False` down is unchanged):

```python
    def __init__(
        self,
        robot: AnyReachyMini,
        *,
        presence: bool,
        idle: IdleMode,
        idle_move: IdleMoveFactory | None = None,
    ) -> None:
        if idle_move is not None:
            check_idle_move_factory(idle_move)
        self._robot = robot
        self._commands: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._run, name="reachy-mini-motion", daemon=True
        )
        # --- thread-owned state (touch only from closures run by the thread, or _run) ---
        self._presence = presence
        self._idle: IdleMode = idle
        self._idle_move = idle_move
        # The registered custom move failed on this thread: play the hold in its place
        # until another is registered (specs/motion/motion.md "Custom idle moves").
        self._idle_move_failed = False
        self._paused = True
```

**1f. Replace `set_breathing`, `_on_set_breathing` and `_playing_breathing`** (three methods, from `def set_breathing` down to the line before `def pause`) with:

```python
    def set_idle(self, mode: IdleMode) -> None:
        self._commands.put(lambda: self._on_set_idle(mode))

    def _on_set_idle(self, mode: IdleMode) -> None:
        if mode == self._idle:
            return
        self._idle = mode
        self._reenter_idle()

    def set_idle_move(self, factory: IdleMoveFactory | None) -> None:
        """Register (or clear, with ``None``) the custom idle move's factory. Checked
        here, on the caller's thread: ``ValueError`` for a bad one, nothing changed."""
        if factory is not None:
            check_idle_move_factory(factory)
        self._commands.put(lambda: self._on_set_idle_move(factory))

    def _on_set_idle_move(self, factory: IdleMoveFactory | None) -> None:
        self._idle_move = factory
        self._idle_move_failed = False
        if self._idle == "custom":
            self._reenter_idle()

    def _reenter_idle(self) -> None:
        """Make the loop re-select its idle move, now that the mode or the custom move
        changed. With a primary playing nothing happens: the change applies when the
        queue drains."""
        if self._playing is not None and self._playing.primary is not None:
            return
        idle = self._playing_idle()
        if idle is not None:
            # Fade the plan's offsets out rather than handing a track caught
            # mid-segment (a nonzero velocity) straight to a fresh blend, which assumes
            # rest (specs/motion/motion.md "Leaving an idle move mid-plan").
            move, elapsed = idle
            self._playing = _Playing(
                stages=[_IdleFadeOut(move, t_offset=elapsed)],
                primary=None,
                stage=0,
                stage_start=time.monotonic(),
            )
            return
        self._playing = None  # re-blend into the new idle move next tick

    def _playing_idle(self) -> tuple[IdleMove, float] | None:
        """The ``IdleMove`` currently playing past its entry blend (no primary), with
        the seconds elapsed into it — else ``None`` (the hold is not an ``IdleMove``)."""
        playing = self._playing
        if playing is None or playing.primary is not None or playing.stage != 1:
            return None
        move = playing.stages[1]
        if not isinstance(move, IdleMove):
            return None
        return move, time.monotonic() - playing.stage_start
```

**1g. `_on_close`.** Its exit stage now fades any playing `IdleMove`. Replace the `breathing = self._playing_breathing()` line and the `exit_stage` assignment after it with:

```python
            idle = self._playing_idle()
            exit_stage: Move = (
                _IdleFadeOut(idle[0], t_offset=idle[1])
                if idle is not None
                else blend_into(self._last_target, HoldMove())
            )
```

**1h. Properties.** Replace the `breathing` property with:

```python
    @property
    def idle(self) -> IdleMode:
        return self._idle

    @property
    def idle_move(self) -> IdleMoveFactory | None:
        return self._idle_move
```

**1i. `_run`.** Add the new `except` clause between the two existing ones — order matters (fact 6):

```python
                except _LOST_CONNECTION_ERRORS as e:
                    self._on_lost_connection(e)
                except _CustomIdleError as e:
                    self._on_custom_idle_failure(e)
                except Exception as e:  # noqa: BLE001 - the loop must survive a bad tick
```

**1j. `_select_next` and the failure handler.** Replace `_select_next` with the version below (its last line changed and `_build_idle` is new), and add `_on_custom_idle_failure` just before `_on_lost_connection`:

```python
    def _select_next(self) -> tuple[Move, _Primary | None] | None:
        while self._queue:
            primary = self._queue.pop(0)
            if primary.done.cancelled():
                continue
            return primary.move, primary
        if not self._presence:
            return None
        return self._build_idle(), None

    def _build_idle(self) -> Move:
        if self._idle == "breathing":
            return BreathingMove()
        if (
            self._idle == "custom"
            and self._idle_move is not None
            and not self._idle_move_failed
        ):
            return _CustomIdle(_build_custom_idle(self._idle_move))
        return HoldMove()
```

```python
    def _on_custom_idle_failure(self, error: Exception) -> None:
        """The caller's idle move misbehaved (specs/motion/motion.md "Custom idle moves"): one
        warning, then the hold plays in its place until another move is registered."""
        _logger.warning(
            "custom idle move failed; holding neutral until another is registered: %s",
            error,
        )
        self._idle_move_failed = True
        exit_blend = self._playing is not None and self._playing.exit_blend
        self._playing = None
        if exit_blend:
            self._stop = True  # nothing left to ease out with: stop now
```

Check: `grep -n "_playing_breathing\|_BreathingFadeOut\|_IdleOffsets\|_breathing" src/reachy_mini_bridge/motion.py` prints nothing.

**1k. Two temporary shims in `api.py`** so the rest of the code stays green until step 2. In `__aenter__`, the `MotionSession(...)` call becomes:

```python
            motion = MotionSession(
                robot,
                presence=self._presence,
                idle="breathing" if self._breathing else "hold",
            )
```

and in `set_breathing`, `self._require_motion().set_breathing(enabled)` becomes:

```python
        self._require_motion().set_idle("breathing" if enabled else "hold")
```

**1l. `tests/test_motion.py` — renames.** Replace all occurrences:

| Old text | New text | Count |
|---|---|---|
| `breathing=True)` | `idle="breathing")` | 15 |
| `breathing=False)` | `idle="hold")` | 1 |
| `_BreathingFadeOut` | `_IdleFadeOut` | 2 |
| `session.set_breathing(False)` | `session.set_idle("hold")` | 1 |
| `move.offsets(t).rpy_rad` | `_rpy_rad(move, t)` | 3 |
| `move.offsets(k * 0.01).rpy_rad` | `_rpy_rad(move, k * 0.01)` | 1 |

In the comment above `ROTATION_LIMITS`, `` `_IdleOffsets.rpy_rad` holds them`` → `` `_rpy_rad` returns them``. In the `from reachy_mini_bridge.motion import (...)` block add `IdleMove,` and `IdleOffsets,` between `HoldMove,` and `MotionSession,`. Add this helper just above `def _angle_from_neutral_deg(`:

```python
def _rpy_rad(move: BreathingMove, t: float) -> npt.NDArray[np.float64]:
    """The head's roll, pitch and yaw offsets from neutral, in rad."""
    o = move.offsets(t)
    return np.radians([o.roll_deg, o.pitch_deg, o.yaw_deg])
```

**1m. `tests/test_motion.py` — new tests.** Append at the end of the file:

```python
# --- idle offsets, custom idle moves (specs/motion/motion.md "Custom idle moves") -----------


def test_idle_offsets_pose_is_in_human_units_and_scales_to_neutral() -> None:
    offsets = IdleOffsets(
        z_mm=5.0, yaw_deg=10.0, antenna_right_deg=15.0, antenna_left_deg=5.0
    )
    head, antennas, body_yaw = offsets.pose()
    assert head[2, 3] == pytest.approx(0.005)
    assert math.degrees(math.atan2(head[1, 0], head[0, 0])) == pytest.approx(10.0)
    # outward is negative for the right antenna, positive for the left
    assert antennas[0] == pytest.approx(NEUTRAL_ANTENNAS[0] - math.radians(15.0))
    assert antennas[1] == pytest.approx(NEUTRAL_ANTENNAS[1] + math.radians(5.0))
    assert body_yaw == NEUTRAL_BODY_YAW

    half_head, half_antennas, _ = offsets.pose(0.5)
    assert half_head[2, 3] == pytest.approx(0.0025)
    assert half_antennas[1] == pytest.approx(NEUTRAL_ANTENNAS[1] + math.radians(2.5))

    for neutral in (offsets.pose(0.0), IdleOffsets().pose()):
        assert np.array_equal(neutral[0], NEUTRAL_HEAD)
        assert np.array_equal(neutral[1], NEUTRAL_ANTENNAS)


LIFT_MM = 8.0


class _Lift(IdleMove):
    """A custom idle move: the head held ``LIFT_MM`` above neutral. Constant, so it is
    at rest everywhere; records every ``t`` it is asked for."""

    def __init__(self) -> None:
        self.ts: list[float] = []

    def offsets(self, t: float) -> IdleOffsets:
        self.ts.append(t)
        return IdleOffsets(z_mm=LIFT_MM)


class _BreaksAfter(IdleMove):
    """A custom idle move whose ``offsets`` raises once ``t`` passes 0.1 s."""

    def offsets(self, t: float) -> IdleOffsets:
        if t > 0.1:
            raise RuntimeError("boom")
        return IdleOffsets()


def test_custom_idle_move_plays_in_custom_mode() -> None:
    async def run() -> tuple[list[float], FakeReachyMini]:
        robot = FakeReachyMini()
        async with MotionSession(
            robot, presence=True, idle="custom", idle_move=_Lift
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.3)
            zs = _head_zs(robot)
        return zs, robot

    zs, robot = asyncio.run(run())
    assert zs[0] == pytest.approx(0.0, abs=0.001)  # blended in from neutral
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.001
    # exit faded the custom move out: the robot is left at neutral (the loop evaluates
    # a finite stage up to 1 ms short of its end, hence the tolerance)
    assert robot.last_target is not None
    assert np.allclose(robot.last_target[0], NEUTRAL_HEAD, atol=1e-4)


def test_custom_mode_without_a_move_holds_neutral() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=True, idle="custom") as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert zs
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in zs)


def test_set_idle_move_in_custom_mode_takes_effect_at_once() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=True, idle="custom") as session:
            session.resume()
            await asyncio.sleep(0.2)
            session.set_idle_move(_Lift)
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)


def test_idle_move_is_stored_in_another_mode_and_plays_once_custom() -> None:
    async def run() -> tuple[list[float], list[float]]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=True, idle="hold") as session:
            session.resume()
            session.set_idle_move(_Lift)
            await asyncio.sleep(BLEND_S + 0.2)
            held = _head_zs(robot)
            session.set_idle("custom")
            await asyncio.sleep(BLEND_S + 0.3)
            return held, _head_zs(robot)

    held, zs = asyncio.run(run())
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in held)  # stored, not played
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)


def test_leaving_a_custom_idle_move_fades_it_out_to_neutral() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with MotionSession(
            robot, presence=True, idle="custom", idle_move=_Lift
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            before = len(robot.targets)
            session.set_idle("hold")
            await asyncio.sleep(
                2 * BLEND_S + 0.2
            )  # the fade-out, then the hold's blend
            return _head_zs(robot)[before:]

    zs = asyncio.run(run())
    assert zs[0] == pytest.approx(LIFT_MM / 1000.0, abs=0.001)
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in zs[-5:])
    assert all(b <= a + 1e-9 for a, b in itertools.pairwise(zs))  # only ever down
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.001


def test_each_idle_entry_builds_a_fresh_custom_move() -> None:
    built: list[_Lift] = []

    def factory() -> _Lift:
        built.append(_Lift())
        return built[-1]

    async def run() -> int:
        robot = FakeReachyMini()
        async with MotionSession(
            robot, presence=True, idle="custom", idle_move=factory
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            before = len(built)
            await asyncio.wrap_future(session.submit(_TestPrimary(0.15, 0.01), None))
            await asyncio.sleep(BLEND_S + 0.2)
            return before

    before = asyncio.run(run())
    assert len(built) > before  # the idle entry after the primary built a new move
    played = [move for move in built if len(move.ts) > 2]
    assert len(played) >= 2
    # each entry plays its own move from t = 0 (the first call is the blend's evaluate(0))
    assert all(move.ts[0] == 0.0 and move.ts[1] < 0.1 for move in played)


def test_failing_custom_idle_move_falls_back_to_the_hold_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> tuple[list[float], int, int, list[float]]:
        robot = FakeReachyMini()
        async with MotionSession(
            robot, presence=True, idle="custom", idle_move=_BreaksAfter
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.1 + BLEND_S + 0.3)  # breaks, then holds
            held = _head_zs(robot)[-5:]
            before = len(robot.targets)
            await asyncio.sleep(0.2)
            after = len(robot.targets)
            session.set_idle_move(_Lift)  # registering another move recovers
            await asyncio.sleep(BLEND_S + 0.3)
            return held, before, after, _head_zs(robot)

    with caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.motion"):
        held, before, after, zs = asyncio.run(run())
    warnings = [r for r in caplog.records if "custom idle move failed" in r.message]
    assert len(warnings) == 1
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in held)
    assert after > before  # the loop is still commanding (the hold), not dead
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)


class _NanOffsets(IdleMove):
    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=math.nan)


class _WrongReturn(IdleMove):
    def offsets(self, t: float) -> IdleOffsets:
        return (0.0, 0.0)  # type: ignore[return-value]


def _raising_factory() -> IdleMove:
    raise RuntimeError("no move today")


@pytest.mark.parametrize(
    "bad",
    [
        "breathing",  # not callable
        HoldMove,  # builds a Move that is not an IdleMove
        _raising_factory,
        _NanOffsets,
        _WrongReturn,
    ],
)
def test_a_bad_idle_move_factory_is_rejected_and_changes_nothing(bad: object) -> None:
    with pytest.raises(ValueError, match="idle move"):
        MotionSession(
            FakeReachyMini(),
            presence=True,
            idle="custom",
            idle_move=bad,  # type: ignore[arg-type]
        )

    async def run() -> tuple[object, list[float]]:
        robot = FakeReachyMini()
        async with MotionSession(
            robot, presence=True, idle="custom", idle_move=_Lift
        ) as session:
            session.resume()
            with pytest.raises(ValueError, match="idle move"):
                session.set_idle_move(bad)  # type: ignore[arg-type]
            await asyncio.sleep(BLEND_S + 0.3)
            return session.idle_move, _head_zs(robot)

    registered, zs = asyncio.run(run())
    assert registered is _Lift
    assert zs[-1] == pytest.approx(LIFT_MM / 1000.0, abs=1e-6)


def test_clearing_the_idle_move_returns_custom_mode_to_the_hold() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with MotionSession(
            robot, presence=True, idle="custom", idle_move=_Lift
        ) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            session.set_idle_move(None)
            await asyncio.sleep(2 * BLEND_S + 0.2)
            return _head_zs(robot)

    zs = asyncio.run(run())
    assert all(z == pytest.approx(0.0, abs=1e-6) for z in zs[-5:])
```

Notes on these tests, so you do not "fix" them: `_Lift` is constant on purpose (a constant is at rest everywhere, so the z stream is exactly predictable); the exit tolerance is `1e-4` because the loop evaluates a finite stage up to 1 ms short of its end; sleeps are sized from `BLEND_S = 0.5` — a fade-out followed by a blend needs `2 * BLEND_S`.

**Check:** the check command is green; `uv run pytest tests/test_motion.py -k "idle or custom"` shows 17 passed.

### Step 2 — Config, api, front door, control panel, live-tier renames

All of this lands in one step because the `breathing` → `idle` rename crosses every layer; the code is not green in between.

**2a. `src/reachy_mini_bridge/config.py`.**

- Imports: `from typing import Any` → `from typing import TYPE_CHECKING, Any`, and after `from .errors import ConfigError` add:

  ```python
  if TYPE_CHECKING:
      from collections.abc import Callable

      from .motion import IdleMove
  ```

- After `DEFAULT_WEBCAM_HFOV_DEG = 70.0` add:

  ```python
  # The idle modes (specs/motion/motion.md "Presence and the idle mode"); motion.IdleMode is the
  # same three values as a type.
  IDLE_MODES = ("breathing", "hold", "custom")
  ```

- Replace the `MotionSettings` class with (`from_json` / `from_json_file` are unchanged):

```python
@dataclass
class MotionSettings:
    """Everything that shapes the robot's behaviour at rest, applied when the session
    starts (specs/motion/motion.md, specs/core/bridge.md): the loop's own idle modes (``presence``,
    ``idle``, ``idle_move``) and the daemon-side modes the api arms around them (``wobbling``,
    ``tracking``)."""

    # The background behaviour: idle moments are filled with the idle move.
    presence: bool = True
    # Which idle move presence plays: "breathing" (built in), "hold" (a still neutral)
    # or "custom" (the caller's own; the hold while none is registered).
    idle: str = "breathing"
    # Python only: the custom idle move's factory, played whenever idle is "custom".
    # A JSON config names the mode; code supplies the move.
    idle_move: Callable[[], IdleMove] | None = None
    # Audio-reactive head sway, enabled on entry.
    wobbling: bool = True
    # Autonomous face tracking, armed once motors read enabled.
    tracking: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MotionSettings:
        block = _require_object(data, "motion")
        if "idle_move" in block:
            raise ConfigError(
                "'motion.idle_move' is set from code, not from a dict / JSON config: "
                "build MotionSettings(idle_move=...) or call set_idle_move(...)"
            )
        _reject_unknown_keys(
            block, "motion", {"presence", "idle", "wobbling", "tracking"}
        )
        presence = block.get("presence", True)
        idle = block.get("idle", "breathing")
        wobbling = block.get("wobbling", True)
        tracking = block.get("tracking", True)
        if not isinstance(presence, bool):
            raise ConfigError("'motion.presence' must be a boolean")
        if idle not in IDLE_MODES:
            raise ConfigError(
                f"'motion.idle' must be one of {IDLE_MODES}, got {idle!r}"
            )
        if not isinstance(wobbling, bool):
            raise ConfigError("'motion.wobbling' must be a boolean")
        if not isinstance(tracking, bool):
            raise ConfigError("'motion.tracking' must be a boolean")
        return cls(presence=presence, idle=idle, wobbling=wobbling, tracking=tracking)

    @classmethod
    def from_json(cls, text: str) -> MotionSettings:
        return cls.from_dict(_loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> MotionSettings:
        return cls.from_dict(_load_file(path))
```

**2b. `src/reachy_mini_bridge/api.py`.**

- Imports: add `cast` to the `typing` import; `from .config import ReachyMiniConfig` → `from .config import IDLE_MODES, ReachyMiniConfig`; inside the `if TYPE_CHECKING:` block add `from .motion import IdleMode, IdleMoveFactory` (after the `.audio` import).
- Add this module-level helper just above `class ReachyMiniApi:`:

```python
def _idle_mode(value: str) -> IdleMode:
    """``value`` narrowed to an idle mode, or ``ValueError``."""
    if value not in IDLE_MODES:
        raise ValueError(f"idle mode must be one of {IDLE_MODES}, got {value!r}")
    return cast("IdleMode", value)
```

- In `__init__`, replace the two lines `self._presence = …` / `self._breathing = …` (and fix the comment above them to name `set_presence/set_idle/set_idle_move`) with:

  ```python
          self._presence = self._config.motion.presence
          self._idle: IdleMode = _idle_mode(self._config.motion.idle)
          self._idle_move: IdleMoveFactory | None = self._config.motion.idle_move
  ```

  A few lines below, the tracking comment mentions `_presence/_breathing` and `presence/breathing`: write `_presence/_idle` and `presence/idle`.
- In `__aenter__`, replace the step-1 shim with the real call:

  ```python
            motion = MotionSession(
                robot,
                presence=self._presence,
                idle=self._idle,
                idle_move=self._idle_move,
            )
  ```

- In `__aexit__`'s `finally`, replace `self._breathing = self._config.motion.breathing` with:

  ```python
            self._idle = _idle_mode(self._config.motion.idle)
            self._idle_move = self._config.motion.idle_move
  ```

- Rename the section comment to `# --- presence & the idle move (background motion) ---`, and in `set_presence`'s docstring write "the idle move (breathing, a still neutral hold, or the caller's own)".
- Replace `set_breathing` and the `breathing` property (down to the `# --- audio in (microphone) ---` comment) with:

```python
    async def set_idle(self, mode: str) -> None:
        """Which idle move presence plays (specs/motion/motion.md): ``"breathing"`` (the
        built-in animation), ``"hold"`` (a still neutral) or ``"custom"`` (the move
        registered with :meth:`set_idle_move`; the hold while none is registered).

        A mode, not a move: it holds until changed and needs no motors. Idle, it
        transitions at once (a playing breathing or custom move fades out to neutral
        first); during an emotion it is recorded and applied when the emotion ends.
        Raises ``ValueError`` for any other value.
        """
        checked = _idle_mode(mode)
        motion = self._require_motion()
        self._idle = checked
        motion.set_idle(checked)

    @property
    def idle(self) -> str:
        """The idle mode — the config's value outside a session."""
        return self._idle

    async def set_idle_move(self, factory: IdleMoveFactory | None) -> None:
        """Register the custom idle move (specs/motion/motion.md "Custom idle moves"): a
        zero-argument callable returning a fresh ``IdleMove`` — a subclass itself, or a
        function — or ``None`` to clear it.

        Stored whatever the idle mode is, and played whenever the mode is ``"custom"``;
        in that mode, idle, it takes effect at once. Raises ``ValueError`` for a factory
        that is not callable, raises, builds no ``IdleMove``, or whose ``offsets(0.0)``
        is not an ``IdleOffsets`` of finite numbers — the registered move then stays.
        """
        self._require_motion().set_idle_move(factory)  # checks before it stores
        self._idle_move = factory

    @property
    def idle_move(self) -> IdleMoveFactory | None:
        """The registered custom idle move factory — the config's outside a session."""
        return self._idle_move
```

  The order inside `set_idle_move` matters: the session checks the factory and raises **before** `self._idle_move` is assigned, so a rejected factory leaves the property unchanged.

**2c. `src/reachy_mini_bridge/__init__.py`.** Add `from .motion import IdleMove, IdleOffsets` after the `.errors` import, and `"IdleMove",` / `"IdleOffsets",` to `__all__` (alphabetical: after `"GravityCompensationUnsupportedError",`).

**2d. `config.example.json`.** `"breathing": true,` → `"idle": "breathing",`.

**2e. Control panel.**

- `examples/control_panel/controller.py`: in `PanelState`, `breathing: bool` → `idle: str`; in `snapshot()`, `breathing=api.breathing,` → `idle=api.idle,`; replace `set_breathing` with:

  ```python
      def set_idle(self, mode: str) -> None:
          self._call(self._api.set_idle(mode))
  ```

- `examples/control_panel/app.py`: add `from reachy_mini_bridge.config import IDLE_MODES` after the `from reachy_mini_bridge import …` line; in `state_table`, the `("breathing", …)` row → `("idle", state.idle),`; the Breathing checkbox →

  ```python
                      idle = gr.Radio(
                          list(IDLE_MODES), value=config.motion.idle, label="Idle"
                      )
  ```

  and its binding → `bind(idle.input, "set_idle", controller.set_idle, idle)`.
- `tests/test_control_panel.py` (`test_snapshot_reflects_the_modes_and_the_camera_is_rgb`): the first tuple assertion → `assert (state.presence, state.idle, state.wobbling) == (True, "breathing", True)`; `controller.set_breathing(False)` → `controller.set_idle("hold")`; the second tuple assertion → `assert (state.presence, state.idle, state.wobbling) == (False, "hold", False)`.

**2f. `tests/test_config.py`.** Replace everything from `def test_motion_block_sets_the_switches` down to (not including) `def test_motion_must_be_an_object` with:

```python
def test_motion_block_sets_the_switches() -> None:
    cfg = ReachyMiniConfig.from_dict(
        {
            "motion": {
                "presence": False,
                "idle": "hold",
                "wobbling": False,
                "tracking": False,
            }
        }
    )
    assert cfg.motion == MotionSettings(
        presence=False, idle="hold", wobbling=False, tracking=False
    )
    cfg = ReachyMiniConfig.from_dict({"motion": {"idle": "custom"}})
    assert cfg.motion == MotionSettings(presence=True, idle="custom", idle_move=None)
    cfg = ReachyMiniConfig.from_json('{"motion": {"wobbling": false}}')
    assert cfg.motion == MotionSettings(wobbling=False)
    cfg = ReachyMiniConfig.from_dict({"motion": {"tracking": False}})
    assert cfg.motion == MotionSettings(tracking=False)


@pytest.mark.parametrize(
    "motion",
    [
        {"presence": "yes"},
        {"wobbling": "yes"},
        {"tracking": 1},
    ],
)
def test_motion_rejects_non_booleans(motion: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="motion"):
        ReachyMiniConfig.from_dict({"motion": motion})


@pytest.mark.parametrize("idle", [True, "breathe", "", None])
def test_motion_rejects_an_unknown_idle_mode(idle: object) -> None:
    with pytest.raises(ConfigError, match=r"motion\.idle"):
        ReachyMiniConfig.from_dict({"motion": {"idle": idle}})


def test_motion_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="breathing"):
        ReachyMiniConfig.from_dict({"motion": {"breathing": True}})


def test_motion_idle_move_is_python_only() -> None:
    with pytest.raises(ConfigError, match="set from code"):
        ReachyMiniConfig.from_dict({"motion": {"idle_move": "my.module:Nod"}})

    def factory() -> object:
        raise AssertionError("the config layer never calls the factory")

    settings = MotionSettings(idle="custom", idle_move=factory)  # type: ignore[arg-type]
    assert ReachyMiniConfig(motion=settings).motion.idle_move is factory


def test_idle_modes_match_the_motion_loops_type() -> None:
    from typing import get_args

    from reachy_mini_bridge.config import IDLE_MODES
    from reachy_mini_bridge.motion import IdleMode

    assert get_args(IdleMode.__value__) == IDLE_MODES
```

**2g. `tests/test_api.py`.**

- In the `from reachy_mini_bridge.motion import (...)` block add `HoldMove,`, `IdleMove,`, `IdleOffsets,`.
- `test_package_front_door_drives_the_fake`: add `"IdleMove",` and `"IdleOffsets",` to the expected set.
- Renames: `MotionSettings(breathing=False)` → `MotionSettings(idle="hold")`; `MotionSettings(presence=False, breathing=False)` → `MotionSettings(presence=False, idle="hold")`; `await api.set_breathing(False)` → `await api.set_idle("hold")`; both `assert api.breathing is False` → `assert api.idle == "hold"`; test names `test_breathing_off_holds_neutral` → `test_idle_hold_holds_neutral` and `test_set_breathing_while_idle_eases_to_neutral` → `test_set_idle_hold_while_breathing_eases_to_neutral`.
- In `test_switch_verbs_require_entry`, replace the `set_breathing(True)` line with:

  ```python
          asyncio.run(ReachyMiniApi("fake").set_idle("hold"))
      with pytest.raises(BridgeError):
          asyncio.run(ReachyMiniApi("fake").set_idle_move(None))
  ```

- Insert the new tests just above `def test_exit_leaves_the_head_at_neutral`:

```python
class _Lift(IdleMove):
    """A custom idle move: the head held 8 mm above neutral."""

    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=8.0)


def test_custom_idle_move_from_the_config_plays() -> None:
    config = ReachyMiniConfig(
        backend="fake", motion=MotionSettings(idle="custom", idle_move=_Lift)
    )
    api = ReachyMiniApi(config)
    assert (api.idle, api.idle_move) == ("custom", _Lift)  # readable before entry

    async def run() -> list[float]:
        async with api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_z(api)

    z = asyncio.run(run())
    assert z[-1] == pytest.approx(0.008, abs=1e-6)


def test_set_idle_move_and_set_idle_work_in_either_order() -> None:
    async def run(move_first: bool) -> tuple[list[float], str, object]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            if move_first:
                await api.set_idle_move(_Lift)  # stored while breathing plays
                await api.set_idle("custom")
            else:
                await api.set_idle("custom")  # the hold, until a move is registered
                await api.set_idle_move(_Lift)
            await asyncio.sleep(2 * BLEND_S + 0.4)
            return _head_z(api), api.idle, api.idle_move

    for move_first in (True, False):
        z, idle, idle_move = asyncio.run(run(move_first))
        assert z[-1] == pytest.approx(0.008, abs=1e-6)
        assert (idle, idle_move) == ("custom", _Lift)


def test_idle_modes_reset_to_the_config_on_exit() -> None:
    api = ReachyMiniApi("fake")

    async def run() -> None:
        async with api:
            await api.set_idle_move(_Lift)
            await api.set_idle("custom")
            assert (api.idle, api.idle_move) == ("custom", _Lift)

    asyncio.run(run())
    assert (api.idle, api.idle_move) == ("breathing", None)


def test_set_idle_rejects_an_unknown_mode() -> None:
    async def run() -> str:
        async with ReachyMiniApi("fake") as api:
            with pytest.raises(ValueError, match="idle mode"):
                await api.set_idle("sleeping")
            return api.idle

    assert asyncio.run(run()) == "breathing"


def test_set_idle_move_rejects_a_bad_factory_and_keeps_the_registered_one() -> None:
    async def run() -> object:
        async with ReachyMiniApi("fake") as api:
            await api.set_idle_move(_Lift)
            with pytest.raises(ValueError, match="idle move"):
                await api.set_idle_move(HoldMove)  # type: ignore[arg-type]
            return api.idle_move

    assert asyncio.run(run()) is _Lift


def test_a_bad_idle_move_in_the_config_fails_bring_up() -> None:
    config = ReachyMiniConfig(
        backend="fake",
        motion=MotionSettings(idle="custom", idle_move=HoldMove),  # type: ignore[arg-type]
    )

    async def run() -> None:
        async with ReachyMiniApi(config):
            pass

    with pytest.raises(ValueError, match="idle move"):
        asyncio.run(run())
```

  `set_idle` and `set_idle_move` are **instant verbs** (specs/core/bridge.md "Cancellation"): they hand one command to the loop and return, so they need no mid-flight cancellation test.

**2h. `tests-e2e/test_api.py` — renames.** Replace all 4 `api.set_breathing(False)` with `api.set_idle("hold")` and all 4 `api.set_breathing(True)` with `api.set_idle("breathing")`; in the breathing test's docstring `` `set_breathing(False)` holds`` → `` `set_idle("hold")` holds``.

**Check:** the check command is green. `grep -rn "set_breathing\|\.breathing\b\|breathing=" src examples tests tests-e2e` prints nothing.

### Step 3 — The live test

In `tests-e2e/test_api.py`, widen the motion import to:

```python
from reachy_mini_bridge.motion import (
    BLEND_S,
    BREATH_REST_S,
    BREATH_S,
    IdleMove,
    IdleOffsets,
)
```

and add, right after `test_breathing_moves_the_head_and_breathing_off_holds_it`:

```python
class _Lift(IdleMove):
    """A custom idle move: the head held 8 mm above neutral."""

    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=8.0)


def test_custom_idle_move_drives_the_head(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """specs/motion/motion.md "Custom idle moves": a registered `IdleMove` plays in the
    `custom` idle mode — the head rises to its offset — and leaving the mode brings the
    head back to neutral."""
    requires_caps(live_api, "motion")
    api, _caps = live_api
    robot: Any = api.robot

    async def head_z() -> float:
        pose = await asyncio.to_thread(robot.get_current_head_pose)
        return float(pose[2, 3])

    async def scenario() -> tuple[float, float, float]:
        await api.set_motors_state("enabled")
        await api.set_idle("hold")
        await asyncio.sleep(2 * BLEND_S + 1.0)
        neutral_z = await head_z()
        try:
            await api.set_idle_move(_Lift)
            await api.set_idle("custom")
            await asyncio.sleep(BLEND_S + 1.5)
            lifted_z = await head_z()
            await api.set_idle("hold")
            await asyncio.sleep(2 * BLEND_S + 1.0)
            back_z = await head_z()
        finally:
            await api.set_idle_move(None)
            await api.set_idle("breathing")
        return neutral_z, lifted_z, back_z

    neutral_z, lifted_z, back_z = asyncio.run(scenario())
    print(
        f"\n[e2e] custom idle: neutral z {neutral_z:.4f} m, lifted {lifted_z:.4f} m, "
        f"back {back_z:.4f} m"
    )
    assert lifted_z - neutral_z >= 0.005  # 8 mm commanded
    assert abs(back_z - neutral_z) < 0.002
```

Run the live tier on the headless sim (the harness starts the daemon itself — do not start one by hand):

```
uv run pytest tests-e2e -rs
```

Every motion test must **pass**. Read the skips (`-rs`): camera / faces tests skip on the headless sim, which is expected; a skipped *motion* test is a failure to report.

### Step 4 — Docs

- `README.md`:
  - "Staying alive" row of the comparison table: "the robot breathes (or holds a still neutral pose)" → "the robot breathes (or holds a still neutral pose, or plays an idle move you wrote)".
  - "Staying alive" row of the verbs table → `` `set_presence(enabled)` / `presence`, `set_idle("breathing" | "hold" | "custom")` / `idle`, `set_idle_move(factory)` / `idle_move` — the idle behaviour between verbs; set by the config's `motion` block ``.
  - The JSON example: `"breathing": true,` → `"idle": "breathing",`.
  - The `motion` field table: the sentence above it "All four default to `true`" → "The three switches default to `true` and `idle` to `"breathing"`"; the `breathing` row →

    ```
    | `idle` | `"breathing"` | Which idle move presence plays: `breathing` (slow breaths with rests, the head roaming, the antennas flicking), `hold` (a still neutral) or `custom` (your own `IdleMove`, registered from code with `MotionSettings(idle_move=...)` or `set_idle_move`; the hold until one is registered). Ignored while `presence` is off | `set_idle` / `set_idle_move` |
    ```

  - After the verbs-table paragraphs (next to **Talking.** / **Listening.**) add a short paragraph:

    ```
    **Your own idle move.** Subclass `IdleMove` and return the pose as offsets from neutral in human units: `IdleOffsets(z_mm=…, roll_deg=…, pitch_deg=…, yaw_deg=…, antenna_right_deg=…, antenna_left_deg=…)`. Register the class (a factory: the loop builds a fresh move at every idle entry, with `t` starting at 0) and select the `custom` mode, in either order: `await api.set_idle_move(SlowNod)` then `await api.set_idle("custom")`, or `MotionSettings(idle="custom", idle_move=SlowNod)` in the config. `offsets(t)` runs at 60 Hz on the motion thread, so keep it fast and start it at rest. See [specs/motion/motion.md](../specs/motion/motion.md) "Custom idle moves".
    ```

- `AGENTS.md`, project map, `motion.py` row: "over the idle move (breathing / neutral hold / nothing)" → "over the idle move (breathing / neutral hold / a caller's custom `IdleMove` / nothing)", and "the `presence` and `breathing` switches" → "the `presence` switch and the `idle` mode".
- `plans/202609162000_motion-loop-presence-and-breathing.md`, **Step 8 checklist only** (it has not been walked yet, so it must name verbs that exist): `set_breathing(False)` → `set_idle("hold")`, `set_breathing(True)` → `set_idle("breathing")`, "Toggling breathing mid-emotion" → "Changing the idle mode mid-emotion", "With breathing off" → "With the idle mode `hold`" (twice). Add one line: `- [ ] A custom idle move (`set_idle_move` + `set_idle("custom")`) plays smoothly, and leaving it (`set_idle("breathing")`) eases to neutral with no snap.` Leave the rest of that plan as written.

**Check:** `uv run pytest` is green (`tests/test_project_map.py` reads `AGENTS.md`).

### Step 5 — Statuses

- `specs/examples/control_panel.md`: `**Status:** Updated` → `**Status:** Implemented`, and the same in its `specs/_index.md` row.
- **Leave** `specs/motion/motion.md` at `Stable` and `specs/core/bridge.md` / `specs/core/config.md` / `specs/core/robot.md` at `Updated`: they wait on the on-robot checklist of [202609162000_motion-loop-presence-and-breathing.md](202609162000_motion-loop-presence-and-breathing.md), not on this plan.
- In `specs/_overview.md`, "Roadmap": delete item 4 (the **Todo** item that links this plan) and renumber the two items after it (5 → 4, 6 → 5).
- This plan: `**Status:** Done`, and its row in [_index.md](_index.md).

## Verification

- `uv run ruff check . && uv run ruff format --check src tests tests-e2e examples && uv run pyright && uv run pytest` — all green.
- `uv run pytest tests-e2e -rs` on the headless sim — `test_custom_idle_move_drives_the_head` and the renamed breathing / stillness tests pass; skips are capability skips only.
- `grep -rn "set_breathing\|motion\.breathing\|_BreathingFadeOut\|_IdleOffsets" src examples tests tests-e2e README.md AGENTS.md config.example.json` prints nothing.
- By hand, on the fake (no robot needed): `uv run python -m examples.control_panel` opens the panel; the Modes group shows an Idle radio with `breathing` / `hold` / `custom`, and the state table's `idle` row follows it.

Mark this plan `Done` (here and in [_index.md](_index.md)) only once all of the above pass.
