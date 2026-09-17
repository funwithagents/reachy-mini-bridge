# Attention: hand the head back to the idle move when nobody is there

**Status:** Done

Implements [specs/api.md](../specs/api.md) "Attention — the head is handed back when nobody is there" and [specs/motion.md](../specs/motion.md) "Re-anchor on request", with the consumed-slice addition in [specs/robot.md](../specs/robot.md) (`get_tracked_face`, the fake's `face_detected`). Delivers an attention loop in `ReachyMiniApi` that, while tracking is active, polls the daemon's face target and re-arms tracking at a small watching weight after a grace period without a face (so the head breathes when the robot is alone), re-engaging the requested weight when a face reappears; a `MotionSession.reanchor()` command the hand-back uses to avoid a jump; an `attention` property. Deliberately leaves out any config knob (three module constants), a perception verb exposing the face target (still deferred), and the upstream fix.

Independent of plan [202609171234](202609171234_organic-breathing-rests-and-independent-antennas.md) (organic breathing): either can land first; both touch `motion.py`, in different places.

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/api.md](../specs/api.md) "Attention / gaze (autonomous)" in full (the design — do not redesign it) and the `play_emotion` bullet; [specs/motion.md](../specs/motion.md) "Re-anchor on request" and "Motors"; [docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking" for the daemon facts the design rests on.
- **Do the steps in order.** Each ends with the same check; fix everything red before the next step:

  ```
  uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest
  ```

- **The code below is written to the current files but has not been executed** (unlike plan 202609171234's). Paste it as written, then let the tests tell you what is off; when a test in this plan fails, first suspect the test's timing budget, then the code — and never delete a test to get green.
- **Edit by anchor.** Each edit names the exact existing text to find. If an anchor is not found verbatim, stop and read the file — do not guess.
- **Tests are functional:** assert on what the fake recorded (`_fake(api).commands`, the `weight` of each `start_head_tracking`) and on the `attention` property, never on internals. Keep fake-tier tests fast: monkeypatch the grace period and poll period to tenths of a second; sleeps ≤ 1.5 s.
- **Do not commit** unless asked. Do not touch `docs/upstream-*.md` or `specs/_analysis.md` beyond what a step says.

## Facts you must not violate (daemon, SDK 1.10)

1. The daemon applies a requested tracking weight **only at a face detection**. Requesting a lower non-zero weight while no face is seen changes nothing. Only weight `0` clears its aim and weight — and weight `0` also **pauses the detector**. Hence the watching transition is *two* sends, `0.0` then `ATTENTION_WATCH_WEIGHT`, in that order.
2. `robot.get_tracked_face(wait=False)` returns the face target of the last status message without blocking. Always pass `wait=False` from the attention loop (a `wait=True` blocks a thread until the next status).
3. Every send to the tracker goes through `self._tracking_lock`; the attention loop never transitions while `self._moves_in_flight > 0`.
4. `MotionSession.reanchor()` must be awaited (through `asyncio.wrap_future`) **before** the two hand-back sends, so the first head target that flows is a blend from the present pose.
5. Only the motion loop calls `set_target`; `reanchor` changes what the loop does on its next tick, nothing more.

## Scope

- `src/reachy_mini_bridge/fake_reachy_mini.py` — `_FakeFaceTarget`; `face_detected` flag; `get_tracked_face`.
- `tests/test_robot.py` — `get_tracked_face` added to the consumed-slice parity list.
- `src/reachy_mini_bridge/motion.py` — `reanchor()` / `_on_reanchor`.
- `tests/test_motion.py` — `test_reanchor_resumes_idle_from_the_present_pose`, `test_reanchor_is_a_noop_during_a_primary`.
- `src/reachy_mini_bridge/api.py` — constants; attention state, lock, task; `_run_attention`; `_daemon_tracking_weight`; changes to `_start_tracking_now`, `_stop_tracking_if_on`, `stop_head_tracking`, `play_emotion`, `_restore_layers_after_move`, `__aexit__` reset; `attention` property.
- `tests/test_api.py` — six attention tests; one existing test's expectation re-checked.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Run the check command. Everything must be green before you change anything.

### Step 1 — The fake: a face target the tests can drive

**File:** `src/reachy_mini_bridge/fake_reachy_mini.py`.

**1a.** Find `class _FakeDaemonClient:` and insert **before** it:

```python
class _FakeFaceTarget:
    """Stand-in for upstream's ``FaceTarget`` (the fields the api reads)."""

    def __init__(self, detected: bool) -> None:
        self.detected = detected
        self.x: float | None = 0.0 if detected else None
        self.y: float | None = 0.0 if detected else None
        self.roll: float | None = None
        self.ts: float | None = time.time() if detected else None


```

Add `import time` to the module's imports (stdlib group, before `from typing import Any, Self`).

**1b.** Find the line `self.commands: list[tuple[str, dict[str, Any]]] = []` in `FakeReachyMini.__init__` and insert **after** it:

```python
        # Whether the daemon-side tracker currently sees a face: tests flip it to
        # simulate a person arriving or leaving (specs/robot.md, api.md "Attention").
        self.face_detected = False
```

**1c.** Find:

```python
    def stop_head_tracking(self) -> None:
        self.commands.append(("stop_head_tracking", {}))
```

and insert **after** it:

```python
    def get_tracked_face(self, wait: bool = True, timeout: float = 5.0) -> _FakeFaceTarget:
        return _FakeFaceTarget(self.face_detected)
```

**1d.** `tests/test_robot.py`: in `_CONSUMED_SLICE`, find the line `"stop_head_tracking",` (inside the `("", ReachyMini, name)` tuple list) and add `"get_tracked_face",` on the next line. The parity test then checks the fake's signature against upstream's `(wait: bool = True, timeout: float = 5.0)`.

**Check:** the check command — green.

### Step 2 — `MotionSession.reanchor()`

**File:** `src/reachy_mini_bridge/motion.py`. Find the method `def resume(self) -> None:` (the one-liner that puts `self._on_resume`) and insert **before** it:

```python
    def reanchor(self) -> concurrent.futures.Future[None]:
        """Ask the loop to re-enter its idle move from the present pose read from the
        robot (specs/motion.md "Re-anchor on request") — for the api, when a daemon-side
        layer is about to hand the head back and the stream may be far from the head.
        The future resolves when the command has been taken; a no-op (resolved at
        once) with a primary playing, an exit blend, presence off, or the loop paused.
        """
        done: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._commands.put(lambda: self._on_reanchor(done))
        return done

    def _on_reanchor(self, done: concurrent.futures.Future[None]) -> None:
        playing = self._playing
        idle = playing is None or (playing.primary is None and not playing.exit_blend)
        if idle and self._presence and self._commanding and not self._paused:
            # The next tick re-selects the idle move and, since the loop is no longer
            # "commanding", blends into it from the present pose (as after a pause).
            self._playing = None
            self._commanding = False
        done.set_result(None)

```

**Tests** (`tests/test_motion.py`, append after `test_close_is_immediate_when_quiet`; the file already has `_head_zs`, `_TestPrimary`, `BLEND_S`, `BREATH_Z_M`, `np`, `pytest`, `itertools`, `asyncio`):

```python
def test_reanchor_resumes_idle_from_the_present_pose() -> None:
    async def run() -> list[float]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=True, breathing=True) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.2)
            # The daemon had the head elsewhere (tracking): the fake's readers return
            # the last set_target, so write it as another writer would.
            head = np.eye(4)
            head[2, 3] = 0.02
            robot.set_target(head=head)
            before = len(robot.targets)
            await asyncio.wrap_future(session.reanchor())
            await asyncio.sleep(BLEND_S + 0.2)
            return _head_zs(robot)[before:]

    zs = asyncio.run(run())
    assert zs
    assert zs[0] == pytest.approx(0.02, abs=0.003)  # blends from the present pose…
    assert abs(zs[-1]) < BREATH_Z_M + 0.002  # …back into the idle move
    assert max(abs(b - a) for a, b in itertools.pairwise(zs)) < 0.002


def test_reanchor_is_a_noop_during_a_primary() -> None:
    async def run() -> tuple[bool, list[float]]:
        robot = FakeReachyMini()
        async with MotionSession(robot, presence=True, breathing=True) as session:
            session.resume()
            await asyncio.sleep(BLEND_S + 0.1)
            future = session.submit(_TestPrimary(duration=0.6, z=0.01), None)
            await asyncio.sleep(BLEND_S + 0.1)  # the primary's trajectory is playing
            head = np.eye(4)
            head[2, 3] = 0.02
            robot.set_target(head=head)
            before = len(robot.targets)
            done = session.reanchor()
            await asyncio.sleep(0.1)
            resolved = done.done()
            zs = _head_zs(robot)[before:]
            await asyncio.wrap_future(future)
            return resolved, zs

    resolved, zs = asyncio.run(run())
    assert resolved
    assert zs and all(z < 0.015 for z in zs)  # the primary kept playing, no re-blend from 0.02
```

`_TestPrimary(duration, z)` is the file's existing test move (a z displacement of `z` metres over `duration`); with `z=0.01` its targets stay under the `0.015` bound while a re-blend from the `0.02` write would not.

**Check:** the check command — green.

### Step 3 — The api: attention loop

**File:** `src/reachy_mini_bridge/api.py`.

**3a. Imports.** Find `import math` and add `import time` after `import math` (alphabetical: `asyncio, json, logging, math, time, urllib.request`). Find `from contextlib import AsyncExitStack` and replace with `from contextlib import AsyncExitStack, suppress`.

**3b. Constants.** Find the module-level line that starts `_MOTOR_STATES` (search `_MOTOR_STATES =`) and insert **before** it:

```python
# Attention (specs/api.md "Attention"): the head is handed back to the motion loop when
# nobody has been tracked for the grace period. Module constants, not config.
ATTENTION_GRACE_S = 3.0  # longer than the daemon's own 2 s recentre on face loss
ATTENTION_WATCH_WEIGHT = 0.05  # keeps the detector running (weight 0 would pause it)
ATTENTION_POLL_S = 0.2

```

**3c. State.** Find (in `__init__`; the same assignment also appears in `__aexit__`, so match the comment line with it):

```python
        # presence/breathing, needs motors enabled to actually engage.
        self._tracking_wanted = self._config.motion.tracking
```

and insert **after** it:

```python
        # The attention loop (specs/api.md "Attention"): its state while tracking is
        # active ("engaged" / "watching", None otherwise), the task running it, the
        # lock every tracker send goes through, and the in-flight play_emotion count
        # during which the loop makes no transition.
        self._attention: str | None = None
        self._attention_task: asyncio.Task[None] | None = None
        self._tracking_lock = asyncio.Lock()
        self._moves_in_flight = 0
```

**3d. Start / stop.** Find the whole of `_stop_tracking_if_on` and `_start_tracking_now` (from `    async def _stop_tracking_if_on(self, robot: AnyReachyMini) -> None:` to the end of `_start_tracking_now`, whose last line is `        self._tracking_wanted = True`). Replace both with:

```python
    async def _stop_tracking_if_on(self, robot: AnyReachyMini) -> None:
        # The daemon-side switch is shared across clients: never leave it armed.
        await self._cancel_attention()
        if self._tracking_weight is not None:
            await asyncio.to_thread(robot.stop_head_tracking)
            self._tracking_weight = None

    async def _start_tracking_now(self, weight: float = 1.0) -> None:
        """Start tracking without re-checking motor state: the caller (__aenter__ or
        set_motors_state) has just confirmed motors are enabled, and a second read
        risks the daemon's ~0.2s status lag reporting the pre-change state. Starts
        the attention loop engaged (specs/api.md "Attention")."""
        async with self._tracking_lock:
            await asyncio.to_thread(self.robot.start_head_tracking, weight)
            self._tracking_weight = weight
            self._tracking_wanted = True
            self._attention = "engaged"
        if self._attention_task is None or self._attention_task.done():
            self._attention_task = asyncio.create_task(self._run_attention())

    async def _cancel_attention(self) -> None:
        task = self._attention_task
        self._attention_task = None
        self._attention = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _daemon_tracking_weight(self) -> float | None:
        """The weight the daemon should hold right now: the requested weight while
        engaged, the watch weight while watching, None when tracking is not active."""
        if self._tracking_weight is None:
            return None
        if self._attention == "watching":
            return ATTENTION_WATCH_WEIGHT
        return self._tracking_weight

    async def _run_attention(self) -> None:
        """The attention loop (specs/api.md "Attention"): one task per tracking start,
        cancelled by stop_head_tracking and at exit."""
        last_seen = time.monotonic()  # engaged at start: a full grace period first
        while True:
            await asyncio.sleep(ATTENTION_POLL_S)
            robot = self._robot
            if robot is None:
                return
            try:
                face = await asyncio.to_thread(robot.get_tracked_face, False)
                detected = bool(face.detected)
            except Exception as e:  # noqa: BLE001 - a status hiccup must not end attention
                _logger.debug("attention: could not read the face target: %s", e)
                continue
            now = time.monotonic()
            if detected:
                last_seen = now
            async with self._tracking_lock:
                weight = self._tracking_weight
                if weight is None:
                    return  # tracking stopped meanwhile
                if self._moves_in_flight:
                    continue  # never transition inside a play_emotion
                if (
                    self._attention == "engaged"
                    and not detected
                    and now - last_seen >= ATTENTION_GRACE_S
                ):
                    # Hand the head back: re-anchor the loop first so the first
                    # target that flows is a blend from the present pose, then clear
                    # the daemon's aim (weight 0) and keep the detector running.
                    motion = self._motion
                    if motion is not None:
                        await asyncio.wrap_future(motion.reanchor())
                    await asyncio.to_thread(robot.start_head_tracking, 0.0)
                    await asyncio.to_thread(
                        robot.start_head_tracking, ATTENTION_WATCH_WEIGHT
                    )
                    self._attention = "watching"
                elif self._attention == "watching" and detected:
                    await asyncio.to_thread(robot.start_head_tracking, weight)
                    self._attention = "engaged"
```

Note `asyncio.to_thread(robot.get_tracked_face, False)` passes `wait=False` positionally (the first parameter of both the upstream and the fake signature).

**3e. `stop_head_tracking`.** Find:

```python
    async def stop_head_tracking(self) -> None:
        """Stop the autonomous face tracker."""
        await asyncio.to_thread(self.robot.stop_head_tracking)
        self._tracking_weight = None
        self._tracking_wanted = False
```

Replace with:

```python
    async def stop_head_tracking(self) -> None:
        """Stop the autonomous face tracker (and the attention loop with it)."""
        await self._cancel_attention()
        async with self._tracking_lock:
            await asyncio.to_thread(self.robot.stop_head_tracking)
            self._tracking_weight = None
            self._tracking_wanted = False
```

**3f. The `attention` property.** Find the `tracking` property (`    def tracking(self) -> bool:` with its docstring and `return self._tracking_wanted`) and insert **after** it:

```python
    @property
    def attention(self) -> str | None:
        """The attention loop's state while tracking is active — ``"engaged"`` (a
        face was seen within the grace period; the requested weight is on the daemon)
        or ``"watching"`` (nobody for a while; the head is handed back to the idle
        move) — else ``None`` (specs/api.md "Attention")."""
        return self._attention
```

**3g. `play_emotion`.** Find:

```python
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

Replace with:

```python
        self._moves_in_flight += 1  # the attention loop holds its transitions meanwhile
        try:
            async with self._tracking_lock:
                if self._tracking_weight is not None:
                    await asyncio.to_thread(robot.start_head_tracking, 0.0)
            if self._wobbling:
                await asyncio.to_thread(robot.disable_wobbling)
            future = motion.submit(
                move, None if sound_path is None else Path(sound_path)
            )
            try:
                await asyncio.wrap_future(future)
            except BaseException:
                future.cancel()  # idempotent; wrap_future already propagated a cancel
                if sound_path is not None:
                    media.stop_sound()
                raise
            finally:
                await self._restore_layers_after_move()
        finally:
            self._moves_in_flight -= 1
```

**3h. Restore.** In `_restore_layers_after_move`, find:

```python
        if self._tracking_weight is not None:
            try:
                await asyncio.to_thread(
                    robot.start_head_tracking, self._tracking_weight
                )
            except Exception as e:  # noqa: BLE001 - never mask the verb's own outcome
                _logger.warning(
                    "could not restore head tracking after the emotion: %s", e
                )
```

Replace with:

```python
        async with self._tracking_lock:
            weight = self._daemon_tracking_weight()
            if weight is not None:
                try:
                    await asyncio.to_thread(robot.start_head_tracking, weight)
                except Exception as e:  # noqa: BLE001 - never mask the verb's outcome
                    _logger.warning(
                        "could not restore head tracking after the emotion: %s", e
                    )
```

**3i. Exit reset.** In `__aexit__`, find:

```python
        finally:
            self._wobbling = False
            self._tracking_weight = None
            self._tracking_wanted = self._config.motion.tracking
```

and replace with:

```python
        finally:
            self._wobbling = False
            self._tracking_weight = None
            self._tracking_wanted = self._config.motion.tracking
            self._attention = None
            self._moves_in_flight = 0
```

(`_stop_tracking_if_on`, on the exit stack, has already cancelled the task by then.) In `__aenter__`'s `except BaseException:` block, find `            self._wobbling = False
            await stack.aclose()` and insert `            self._attention = None` before `await stack.aclose()` — the stack's `_stop_tracking_if_on` cancels the task.

**Check:** `uv run ruff check . && uv run ruff format . && uv run pyright` clean; `uv run pytest` green — in particular `test_play_emotion_pauses_tracking_and_restores_it` must still see `[0.7, 0.0, 0.7]` (engaged at restore) and `test_tracking_left_on_is_stopped_at_exit` must still pass (the task is cancelled before `stop_head_tracking` is sent).

### Step 4 — Api tests

**File:** `tests/test_api.py`. The file already imports `api_module`, `asyncio`, `pytest`, `MotionSettings`, `ReachyMiniConfig`, `_fake`, `_command_names`. Add after `test_tracking_left_on_is_stopped_at_exit`:

```python
# --- attention (specs/api.md "Attention") -------------------------------------------


def _tracking_weights(api: ReachyMiniApi) -> list[float]:
    return [
        args["weight"]
        for name, args in _fake(api).commands
        if name == "start_head_tracking"
    ]


@pytest.fixture
def fast_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module, "ATTENTION_GRACE_S", 0.3)
    monkeypatch.setattr(api_module, "ATTENTION_POLL_S", 0.05)


def test_attention_starts_engaged_and_watches_when_nobody_is_there(
    fast_attention: None,
) -> None:
    async def run() -> tuple[str | None, str | None, list[float]]:
        async with ReachyMiniApi("fake") as api:  # tracking on by default
            await api.set_motors_state("enabled")
            at_start = api.attention
            await asyncio.sleep(0.6)  # > grace + a poll
            return at_start, api.attention, _tracking_weights(api)

    at_start, later, weights = asyncio.run(run())
    assert at_start == "engaged"
    assert later == "watching"
    # requested weight, then the hand-back: through 0, then the watch weight
    assert weights == [1.0, 0.0, api_module.ATTENTION_WATCH_WEIGHT]


def test_attention_reengages_when_a_face_appears(fast_attention: None) -> None:
    async def run() -> tuple[str | None, list[float]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(0.6)
            assert api.attention == "watching"
            _fake(api).face_detected = True
            await asyncio.sleep(0.2)  # a few polls
            return api.attention, _tracking_weights(api)

    state, weights = asyncio.run(run())
    assert state == "engaged"
    assert weights == [1.0, 0.0, api_module.ATTENTION_WATCH_WEIGHT, 1.0]


def test_attention_stays_engaged_while_a_face_is_seen(fast_attention: None) -> None:
    async def run() -> tuple[str | None, list[float]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            _fake(api).face_detected = True
            await asyncio.sleep(0.6)
            return api.attention, _tracking_weights(api)

    state, weights = asyncio.run(run())
    assert state == "engaged"
    assert weights == [1.0]


def test_emotion_restores_the_watch_weight_while_watching(
    fast_attention: None,
) -> None:
    async def run() -> list[float]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(0.6)
            assert api.attention == "watching"
            await api.play_emotion("sad")
            return _tracking_weights(api)

    weights = asyncio.run(run())
    watch = api_module.ATTENTION_WATCH_WEIGHT
    assert weights == [1.0, 0.0, watch, 0.0, watch]


def test_attention_makes_no_transition_during_an_emotion(
    fast_attention: None,
) -> None:
    async def run() -> list[float]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            # Engaged; the grace period elapses *during* the emotion (the fake "sad"
            # move is short, so give it a long one from the offline library if
            # needed — see tests/test_api.py's emotion helpers) — the transition must
            # wait for the restore, so the dip and the restore bracket nothing else.
            await asyncio.sleep(0.2)
            await api.play_emotion("sad")
            return _tracking_weights(api)

    weights = asyncio.run(run())
    assert weights[:3] == [1.0, 0.0, 1.0]


def test_stop_head_tracking_stops_the_attention_loop(fast_attention: None) -> None:
    async def run() -> tuple[str | None, int, int]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.stop_head_tracking()
            state = api.attention
            before = len(_fake(api).commands)
            await asyncio.sleep(0.6)
            return state, before, len(_fake(api).commands)

    state, before, after = asyncio.run(run())
    assert state is None
    assert after == before  # no tracking sends after the stop
```

For `test_attention_makes_no_transition_during_an_emotion`: the fake emotion is 0.3 s and the grace is 0.3 s, so tighten the timing rather than lengthen the move — sleep `0.25` before the emotion (grace elapses mid-move) and assert the first three weights are `[1.0, 0.0, 1.0]` (restore to the engaged weight, no `0.0`/watch pair in between); the watching transition may then follow after the restore, which is fine.

**Check:** the check command — green. Run `uv run pytest tests/test_api.py -k attention -p no:randomly -x` a few times to make sure the timings are stable; if a test flakes, widen its sleeps (never its assertions).

### Step 5 — Live tier and statuses

- Run `uv run pytest tests-e2e -rs` once on the headless sim. The sim has no camera, so tracking never sees a face and every session goes `watching` after 3 s; nothing should fail. Record the outcome below.
- Set this plan's `**Status:**` to `Done` and change its row in [_index.md](_index.md). `specs/api.md` and `specs/robot.md` stay `Updated` (their promotion to `Implemented` is gated by plan 202609162000's on-robot checklist); `specs/motion.md` stays `Stable`.
- Append to plan [202609162000](202609162000_motion-loop-presence-and-breathing.md) step 8 checklist: `- [ ] Face tracking: stand in front of the robot (it follows), leave the frame — after ~3 s the head breathes again; come back — it follows again with no jump either way.` (do it in this plan's change, one line).

## Verification

- `uv run ruff check . && uv run ruff format . && uv run pyright && uv run pytest` — all green, attention tests stable across a few runs.
- `uv run pytest tests-e2e -rs` on the headless sim — no failures (record skips here).
- The on-robot check is the added step 8 item of plan 202609162000 and gates that plan, not this one.

### Outcome (2026-09-17, Apple Silicon Mac, macOS 26.5.2)

- Fast tier: 272 passed; the attention/tracking/emotion tests passed 5 runs out of 5 (`-k "attention or tracking or emotion"`).
- Headless sim: 11 passed, 3 skipped (`camera`, `gravity_compensation`, "a simulation ignores motor modes").
- Deviations from the code above (the spec wins):
  - **Step 2 tests:** writing `robot.set_target(head=…)` from the test raced the loop — its next tick overwrote the fake's "present pose" before the re-anchor landed (flaky first-sample assertion). Both tests now patch `get_current_head_pose` (`_head_held_elsewhere`) so the present pose stays elsewhere, and the resume test allows up to two ticks of the old idle move before the blend starts.
  - **`_start_tracking_now`:** cancels any running attention task and starts a fresh one, rather than keeping a running task. The spec says `start_head_tracking` "(re)starts engaged"; a kept task carries a stale last-seen time and would hand the head back on its next poll. Pinned by `test_restarting_tracking_while_watching_reengages_for_a_full_grace_period`.
  - **Formatting:** `ruff format src tests tests-e2e` rather than `ruff format .`, which also rewrites Python blocks inside Markdown files (docs and plans not part of this change).
