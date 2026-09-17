# Cancellable verbs — `play_emotion` stops motion and sound, cancel-safe bring-up

**Status:** Done

Implements the "Cancellation" contract in [specs/api.md](../specs/api.md) (every async verb is fully cancellable) and the `play_emotion` guarantee it adds, [specs/audio.md](../specs/audio.md) "Stopping a sound file" (`MediaSession.stop_sound()`), and the fake's move timing in [specs/robot.md](../specs/robot.md). It closes the gap measured on a Reachy Mini Lite: a cancelled `play_emotion` stopped commanding the trajectory, but its sound played on for 15 s with the head wobbler swaying the head to it ([docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Cancelling a move").

**Out of scope (do not build):** a stop for `play_sound` (api.md open question 4), `stop_talking()` (deferred), returning the head to its pre-move pose (the contract says no rewind), re-implementing upstream's playback loop, and any change to how or when the emotion's sound *starts*.

## Read first

1. [specs/api.md](../specs/api.md) — the "Cancellation" section and the `play_emotion` bullet under "Expression".
2. [specs/audio.md](../specs/audio.md) — the "Stopping a sound file" bullet under "Shared", and the lifecycle paragraph of "One media session".
3. [docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Cancelling a move" — why the SDK's own `cancel_move()` must **not** be used.
4. The code you will change: [`src/reachy_mini_bridge/audio.py`](../src/reachy_mini_bridge/audio.py), [`src/reachy_mini_bridge/api.py`](../src/reachy_mini_bridge/api.py), [`src/reachy_mini_bridge/fake_reachy_mini.py`](../src/reachy_mini_bridge/fake_reachy_mini.py), and their tests `tests/test_audio.py`, `tests/test_api.py`, `tests/test_robot.py`, `tests-e2e/test_api.py`.

## Facts the design rests on (already verified — do not re-investigate)

- Upstream `ReachyMini.async_play_move` starts the move's sound with a fire-and-forget `media_manager.play_sound(...)`, then loops with `await asyncio.sleep(...)`. Cancelling the awaiting task makes that loop exit at its next tick (≤ 10 ms). **The trajectory stops by itself. Only the sound keeps playing.**
- On the local GStreamer backend the sound plays on a `playbin` the SDK stores as `media.audio._playbin`. Setting it to `Gst.State.NULL` stops the sound and leaves the shared record/playback pipeline untouched (capture and pushes keep working). This was verified on the sim.
- `media.audio.clear_player()` does **not** stop that sound (it only flushes the push path) but it **does** reset the head wobbler. `stop_sound()` therefore does both: playbin to `NULL`, then `clear_player()`.
- Upstream `cancel_move()` calls `media_manager.stop_playing()`, which sets the shared pipeline to `NULL`: capture dies, pushes are dropped, a restart costs ~270 ms of mic and on macOS rebinds to the Mac's own devices. **Never call `cancel_move()` or `stop_playing()` from the bridge.**
- On the `webrtc` client backend (`GstWebRTCClient`) the sound plays on the daemon; the daemon exposes `POST {daemon_url}/api/media/stop_sound`.
- `asyncio.to_thread` cannot interrupt the thread it started; when the awaiting task is cancelled, the thread runs to completion in the background.

## Scope

- `src/reachy_mini_bridge/audio.py` — add `MediaSession.stop_sound()`, the module-level dispatch `_stop_sound_file(robot)` and `_post(url)`, and the `cancel_safe_step` helper; use the helper for `start_recording` / `start_playing` in `MediaSession.__aenter__`.
- `src/reachy_mini_bridge/api.py` — `play_emotion` stops the sound on any early exit; `_get_recorded_moves` keeps the library load in a shielded future; `__aenter__` uses `cancel_safe_step` for the daemon spawn and the robot connect; `_FakeRecordedMoves.get` returns a `_FakeRecordedMove` object.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — `async_play_move` sleeps the move's `duration`; `_FakeMedia.stop_sound()` records a command.
- `tests/test_audio.py`, `tests/test_api.py`, `tests/test_robot.py`, `tests/test_fake_reachy_mini.py` — new tests listed per step; one existing assertion updated.
- `tests-e2e/test_api.py` — one new live test, and the emotions-library download block factored into a helper.
- `README.md`, `specs/api.md`, `specs/audio.md`, `specs/robot.md`, `specs/_index.md`, `specs/_overview.md`, `plans/_index.md` — statuses and one README sentence (step 8).

Test helpers that already exist and you should reuse: in `tests/test_api.py` — `_fake(api)` (the `FakeReachyMini` behind an api), `_command_names(api)`, `_ToneSynth` (a synthesizer yielding one 400-frame chunk); in `tests/test_audio.py` — `_command_names(robot)`, `_pushed_frames(robot)`, `_ToneSynth(sample_rate, chunks=, block=)`. Every recorded command is a `(name, args)` tuple on `robot.commands`.

## Steps

Do the steps in order; run `uv run pytest` after each one.

### Step 1 — `stop_sound` on the fake and on `MediaSession`

**1a. `fake_reachy_mini.py`** — in `class _FakeMedia`, next to `play_sound`, add:

```python
    def stop_sound(self) -> None:
        """Stop the sound file `play_sound` started.

        Fake-only member: it models the `MediaManager.stop_sound()` upstream lacks
        (docs/reachy-mini-api.md "Cancelling a move"); the bridge stops the real backend's
        playbin itself meanwhile (specs/audio.md "Stopping a sound file").
        """
        self._commands.append(("media.stop_sound", {}))
```

**1b. `audio.py`** — add these imports at the top: `import logging`, `import urllib.request`, and `from .fake_reachy_mini import FakeReachyMini` (a plain import: `fake_reachy_mini.py` imports nothing from `audio.py`, so there is no cycle). Add `_logger = logging.getLogger(__name__)` and `_DAEMON_HTTP_TIMEOUT_S = 2.0` next to the other module constants. Then add, after `MediaSession.clear_player`:

```python
    def stop_sound(self) -> None:
        """Stop the sound file the SDK is playing, without touching the shared pipeline.

        An emotion's sidecar sound or a `play_sound` call. Then resets the head wobbler
        through :meth:`clear_player` (the stopped player never reaches the EOS that
        would reset it). A no-op when no sound plays. Works at any time, like
        :meth:`clear_player`. See specs/audio.md "Stopping a sound file".
        """
        _stop_sound_file(self._robot)
        self.clear_player()
```

and these module-level functions (below the `MediaSession` class):

```python
def _stop_sound_file(robot: AnyReachyMini) -> None:
    """Backend dispatch behind :meth:`MediaSession.stop_sound` (see specs/audio.md)."""
    if isinstance(robot, FakeReachyMini):
        robot.media.stop_sound()
        return
    audio: Any = robot.media.audio
    if audio is None:
        return
    # Lazy imports: only a real/sim robot reaches this branch.
    from reachy_mini.media.audio_gstreamer import GStreamerAudio
    from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient

    if isinstance(audio, GStreamerAudio):
        # The bridge's one reach into SDK internals: the exact body of the daemon-side
        # MediaServer.stop_sound(), pinned by tests/test_robot.py. Replace with
        # media.stop_sound() once upstream ships it.
        playbin = audio._playbin
        if playbin is not None:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            playbin.set_state(Gst.State.NULL)
            audio._playbin = None
    elif isinstance(audio, GstWebRTCClient):
        if audio.daemon_url:
            _post(f"{audio.daemon_url}/api/media/stop_sound")
    else:
        _logger.warning(
            "stop_sound: unsupported audio backend %s; the sound plays on",
            type(audio).__name__,
        )


def _post(url: str) -> None:
    """POST to the daemon's HTTP API with an empty body (patched by tests)."""
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=_DAEMON_HTTP_TIMEOUT_S):
        pass
```

`AnyReachyMini` is already imported under `TYPE_CHECKING` in `audio.py`; `isinstance(robot, FakeReachyMini)` needs the runtime import added above.

**1c. Tests in `tests/test_audio.py`** (add `from types import SimpleNamespace`, `import logging`, and `from reachy_mini_bridge import audio as audio_module`):

- `test_stop_sound_on_the_fake_records_the_stop_then_resets_the_wobbler`: `robot = FakeReachyMini(); MediaSession(robot).stop_sound()` (no `async with` needed) → `_command_names(robot) == ["media.stop_sound", "audio.clear_player"]`.
- `test_stop_sound_stops_the_local_playbin_and_clears_it`: build a stand-in without running the SDK constructor — `from reachy_mini.media.audio_gstreamer import GStreamerAudio; audio = GStreamerAudio.__new__(GStreamerAudio)`; give it `audio._playbin = _StubPlaybin()` where `_StubPlaybin.set_state(self, state)` appends `state` to `self.states`; call `audio_module._stop_sound_file(SimpleNamespace(media=SimpleNamespace(audio=audio)))` (cast the argument with `typing.cast("Any", ...)` to satisfy pyright); assert `playbin.states == [Gst.State.NULL]` (import `Gst` the same way as in 1b) and `audio._playbin is None`. Call it again: no error, `states` unchanged (no-op when nothing plays).
- `test_stop_sound_on_the_webrtc_backend_posts_to_the_daemon`: `from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient; audio = GstWebRTCClient.__new__(GstWebRTCClient); audio.daemon_url = "http://127.0.0.1:8000"`; `monkeypatch.setattr(audio_module, "_post", lambda url: posted.append(url))`; assert `posted == ["http://127.0.0.1:8000/api/media/stop_sound"]`.
- `test_stop_sound_on_an_unknown_backend_warns_and_returns`: `audio = object()`; with `caplog.at_level(logging.WARNING, logger="reachy_mini_bridge.audio")` the call returns and `"unsupported audio backend" in caplog.text`.

Importing `reachy_mini.media.*` in `tests/` is fine: `tests/test_robot.py` already imports `reachy_mini`. Both stand-ins skip the SDK constructor, so their `__del__` would print an "Exception ignored … has no attribute" traceback when garbage-collected (harmless, but noisy and confusing). In those two tests, first do `monkeypatch.setattr(GStreamerAudio, "__del__", lambda self: None)` (respectively on `GstWebRTCClient`) — verified to construct cleanly that way.

**1d. Parity guard in `tests/test_robot.py`** — add:

```python
def test_local_audio_backend_keeps_the_playbin_the_bridge_stops() -> None:
    """The bridge's one reach into SDK internals (specs/audio.md "Stopping a sound
    file"): `GStreamerAudio` must keep the play_sound playbin as `_playbin`."""
    from reachy_mini.media.audio_gstreamer import GStreamerAudio

    assert "self._playbin = playbin" in inspect.getsource(GStreamerAudio.play_sound)
    assert "self._playbin" in inspect.getsource(GStreamerAudio.stop_playing)
```

Do **not** add `stop_sound` to `_CONSUMED_SLICE` (upstream `MediaManager` has no such member yet); add a comment above the `media` entries saying so, pointing at `docs/reachy-mini-api.md` "Cancelling a move".

### Step 2 — the fake keeps a move's timing

**2a. `fake_reachy_mini.py`** — `import asyncio` at the top; in `FakeReachyMini.async_play_move`, after the `self.commands.append(...)`, add:

```python
        # The fake keeps the move's timing (specs/robot.md) so a cancel has something
        # in flight to interrupt. A move without a duration (a bare name) takes 0 s.
        await asyncio.sleep(float(getattr(move, "duration", 0.0)))
```

**2b. `api.py`** — add `from dataclasses import dataclass` and, replacing the `_FAKE_EMOTIONS` / `_FakeRecordedMoves` block:

```python
_FAKE_EMOTIONS = ("happy", "sad", "curious")
_FAKE_MOVE_DURATION_S = 0.3


@dataclass(frozen=True)
class _FakeRecordedMove:
    """Offline stand-in for an upstream ``RecordedMove``: the two attributes the bridge
    and the fake read. ``sad`` has no sound so both `play_emotion` paths are testable."""

    name: str
    duration: float = _FAKE_MOVE_DURATION_S
    sound_path: Path | None = None


class _FakeRecordedMoves:
    """Offline stand-in for the upstream ``RecordedMoves`` on the ``fake`` backend.

    ``get`` raises ``ValueError`` for an unknown name — mirroring the real
    ``RecordedMoves.get`` contract so callers see the same failure either way.
    """

    def list_moves(self) -> list[str]:
        return list(_FAKE_EMOTIONS)

    def get(self, move_name: str) -> _FakeRecordedMove:
        if move_name not in _FAKE_EMOTIONS:
            raise ValueError(f"Move {move_name} not found in emotions library")
        sound = None if move_name == "sad" else Path(f"{move_name}.ogg")
        return _FakeRecordedMove(move_name, sound_path=sound)
```

(`Path` is already imported in `api.py`. The fake never opens `sound_path`; it is only read for the "has a sound" decision in step 3.)

**2c. Tests** — in `tests/test_api.py::test_play_emotion_resolves_name_and_plays_it` change the final assertion to `assert asyncio.run(run()).name == "curious"` (the recorded `move` is now the object). Add:

- `tests/test_api.py::test_play_emotion_on_the_fake_takes_the_moves_duration`: time `await api.play_emotion("curious")` with `time.monotonic()`; assert `elapsed >= 0.25`.
- `tests/test_fake_reachy_mini.py::test_play_move_sleeps_the_moves_duration`: `asyncio.run(robot.async_play_move(SimpleNamespace(duration=0.1)))` takes ≥ 0.08 s, and `asyncio.run(robot.async_play_move("bare-name"))` takes < 0.05 s.

### Step 3 — `play_emotion` stops the sound on any early exit

**3a. `api.py`** — replace the body of `play_emotion` with:

```python
        await self._require_motors_enabled("play_emotion")
        moves = await self._get_recorded_moves()
        move = moves.get(name)  # ValueError on unknown name
        media = self._require_media()
        has_sound = getattr(move, "sound_path", None) is not None
        try:
            await self.robot.async_play_move(move)
        except BaseException:
            # Upstream's loop stops on cancel (or on its own error) but leaves the
            # move's sound playing (docs/reachy-mini-api.md "Cancelling a move"). Stop it
            # before propagating; the media session stays open.
            if has_sound:
                media.stop_sound()
            raise
```

Update the docstring: completes when the move has played; cancelling stops motion and sound; the head stays where the cancel caught it.

**3b. Tests in `tests/test_api.py`** (add `import time`):

- `test_cancelled_play_emotion_stops_the_sound_and_keeps_the_session`:
  ```python
  async def run() -> tuple[float, list[str]]:
      async with ReachyMiniApi("fake", synthesizer=_ToneSynth()) as api:
          await api.set_motors_state("enabled")
          task = asyncio.create_task(api.play_emotion("happy"))
          while "async_play_move" not in _command_names(api):
              await asyncio.sleep(0)
          t0 = time.monotonic()
          task.cancel()
          with pytest.raises(asyncio.CancelledError):
              await task
          elapsed = time.monotonic() - t0
          await api.say("still here")  # the session is usable right after
          return elapsed, _command_names(api)
  ```
  Assert `elapsed < 0.05`; with `i = names.index("async_play_move")`, `names[i + 1 : i + 3] == ["media.stop_sound", "audio.clear_player"]`; and `"media.push_audio_sample" in names[i + 3 :]`.
- `test_cancelled_soundless_emotion_does_not_stop_a_sound`: same as above with `"sad"`; assert `"media.stop_sound" not in names`.
- `test_play_emotion_failure_stops_the_sound_and_propagates` (uses `monkeypatch`): replace `FakeReachyMini.async_play_move` with an `async def boom(self, move, **kwargs)` that appends `("async_play_move", {"move": move})` then `raise RuntimeError("boom")`; `pytest.raises(RuntimeError, match="boom")` around `await api.play_emotion("happy")`; assert `"media.stop_sound"` follows `"async_play_move"` in the names.
- `test_completed_play_emotion_does_not_stop_the_sound`: `await api.play_emotion("happy")` to completion → `"media.stop_sound" not in _command_names(api)` (a completed move's sound plays to its natural end).

### Step 4 — the emotions-library load survives a cancel

**4a. `api.py`** — replace the `_recorded_moves` attribute by `self._recorded_moves_future: asyncio.Future[Any] | None = None` (in `__init__`; reset to `None` in `__aexit__` where `_recorded_moves` was reset), and replace `_get_recorded_moves` with:

```python
    async def _get_recorded_moves(self) -> Any:
        # One load per connection, shielded: a cancelled first caller does not
        # discard the load, and the next caller awaits the same in-flight future.
        future = self._recorded_moves_future
        if future is None:
            future = asyncio.ensure_future(asyncio.to_thread(self._load_recorded_moves))
            self._recorded_moves_future = future
        try:
            return await asyncio.shield(future)
        except Exception:
            # A failed load is not cached: the next call retries.
            if self._recorded_moves_future is future:
                self._recorded_moves_future = None
            raise
```

(`asyncio.CancelledError` is a `BaseException`, so `except Exception` leaves the future in place on a cancel — that is the point.)

**4b. Test `tests/test_api.py::test_cancelled_library_load_is_reused_by_the_next_call`** (uses `monkeypatch`, `import threading`): patch `ReachyMiniApi._load_recorded_moves` with a function that increments a counter, sets `started: threading.Event`, waits on `release: threading.Event`, then returns `api_module._FakeRecordedMoves()`. Inside `async with ReachyMiniApi("fake")`: `task = asyncio.create_task(api.list_emotions())`; poll `while not started.is_set(): await asyncio.sleep(0.01)`; `task.cancel()`; `pytest.raises(asyncio.CancelledError)` on `await task`; `release.set()`; `names = await api.list_emotions()`. Assert `names == ["happy", "sad", "curious"]` and the counter is `1`.

### Step 5 — cancel-safe bring-up

**5a. `audio.py`** — add a public helper (exported in `__all__`), with `T = TypeVar("T")` (`from typing import TypeVar`):

```python
async def cancel_safe_step(enter: Callable[[], T], undo: Callable[[T], object]) -> T:
    """Run the blocking ``enter`` off the loop; on a cancel, finish it, undo it, re-raise.

    ``asyncio.to_thread`` cannot interrupt its thread. If the awaiting task is
    cancelled while ``enter`` runs, this waits for ``enter`` to finish, runs ``undo`` on
    its result (also off the loop), and then re-raises the ``CancelledError`` — so a
    daemon spawn, a robot connect, or a media ``start_*`` is never leaked by an
    ``asyncio.timeout`` around the api's ``async with``. If ``enter`` itself fails
    during that wait there is nothing to undo and the cancel still propagates. A
    second cancel during the wait abandons the step (accepted, documented in
    specs/api.md "Lifecycle").
    """
    step = asyncio.ensure_future(asyncio.to_thread(enter))
    try:
        return await asyncio.shield(step)
    except asyncio.CancelledError as cancel:
        try:
            result = await step
        except BaseException as exc:  # noqa: BLE001 - the step failed: nothing to undo
            _logger.warning("bring-up step failed while being cancelled: %r", exc)
            raise cancel from exc
        await asyncio.to_thread(undo, result)
        raise
```

`Callable` is already imported under `TYPE_CHECKING` in `audio.py`; it is only used in annotations here, and the file has `from __future__ import annotations`, so that is enough.

Then in `MediaSession.__aenter__` replace the two `start_*` lines:

```python
            await cancel_safe_step(media.start_recording, lambda _: media.stop_recording())
            stack.push_async_callback(asyncio.to_thread, media.stop_recording)
            await cancel_safe_step(media.start_playing, lambda _: media.stop_playing())
            stack.push_async_callback(asyncio.to_thread, media.stop_playing)
```

(`apply_audio_config` stays on plain `asyncio.to_thread`: a cancel there has nothing to undo, and the stack already unwinds both starts.)

**5b. `api.py`** — import `cancel_safe_step` from `.audio`. In `__aenter__`:

- daemon: replace `await asyncio.to_thread(daemon_cm.__enter__)` with `await cancel_safe_step(daemon_cm.__enter__, lambda _: daemon_cm.__exit__(None, None, None))`; keep the following `stack.push_async_callback(...)` line as is.
- robot: build and enter are one step (upstream's `ReachyMini` connects in its constructor, so a built-but-dropped robot is already a leaked connection):
  ```python
  def connect() -> AnyReachyMini:
      built = build_robot(cfg.backend, **cfg.effective_robot_options())
      built.__enter__()
      return built


  robot = await cancel_safe_step(connect, lambda r: r.__exit__(None, None, None))
  stack.push_async_callback(asyncio.to_thread, lambda: robot.__exit__(None, None, None))
  ```
  `AnyReachyMini` is imported under `TYPE_CHECKING` in `api.py`; the annotation is fine because of `from __future__ import annotations`.

**5c. Tests.**

- `tests/test_audio.py::test_cancel_during_media_open_unwinds_the_started_capture` (`import threading`): `robot = FakeReachyMini()`; keep `original = robot.media.start_recording`; `monkeypatch.setattr(robot.media, "start_recording", slow)` where `slow()` sets `started`, waits on `release`, then calls `original()`. In `asyncio.run`: `session = MediaSession(robot)`; `task = asyncio.create_task(session.__aenter__())`; poll until `started.is_set()`; `task.cancel()`; `release.set()`; `pytest.raises(asyncio.CancelledError)` on `await task`. Assert `_command_names(robot) == ["media.start_recording", "media.stop_recording"]` and that `session.say("x", _ToneSynth(16000))` raises `BridgeError` (the session never opened).
- `tests/test_api.py::test_cancel_during_bring_up_exits_the_robot` (`monkeypatch`, `threading`): define `class _SlowEnter(FakeReachyMini)` whose `__enter__` sets `started`, waits on `release`, then returns `super().__enter__()`; `robot = _SlowEnter()`; `monkeypatch.setattr(api_module, "build_robot", lambda backend, **kw: robot)`. In `asyncio.run`: `api = ReachyMiniApi("fake")`; `task = asyncio.create_task(api.__aenter__())`; poll until `started.is_set()`; `task.cancel()`; `release.set()`; `pytest.raises(asyncio.CancelledError)` on `await task`. Assert `robot.commands[-1][0] == "__exit__"`, `"media.start_recording" not in [n for n, _ in robot.commands]`, and `api.robot` raises `BridgeError`.
- `tests/test_audio.py::test_cancel_safe_step_returns_the_result_when_not_cancelled`: `await cancel_safe_step(lambda: 42, lambda _: calls.append("undo"))` returns `42` and `calls == []`.
- `tests/test_audio.py::test_cancel_safe_step_propagates_a_failing_step_as_the_cancel`: a step that sets `started`, waits on `release`, then raises `RuntimeError`; cancel the awaiting task once started, release, and assert the task ends with `CancelledError` and the undo was never called.

### Step 6 — live test

In `tests-e2e/test_api.py`, factor the `snapshot_download` block of `test_play_emotion_plays_a_real_move` into `def _require_emotions_library() -> None` (same behavior: cache hit, else download, else `pytest.skip`) and call it from both tests. Add `from reachy_mini import ReachyMini` to the imports. Then:

```python
def test_cancelled_emotion_stops_motion_and_sound(
    live_api: tuple[ReachyMiniApi, frozenset[str]],
) -> None:
    """specs/api.md "Cancellation": cancelling `play_emotion` 1 s into `dance2` returns
    at once, the joints are still afterwards (no sound left driving the wobbler), and
    the local backend's playbin is cleared. Measured before the fix: the sound played
    its remaining 15 s and the head kept swaying 0.1–0.2 rad per half second."""
    requires_caps(live_api, "motion")
    api, _caps = live_api
    _require_emotions_library()
    robot = api.robot
    assert isinstance(robot, ReachyMini)

    async def scenario() -> tuple[float, float, object]:
        await api.set_motors_state("enabled")
        await api.set_wobbling(True)
        task = asyncio.create_task(api.play_emotion("dance2"))
        await asyncio.sleep(1.0)
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        latency = time.monotonic() - t0
        await asyncio.sleep(0.5)  # the head reaches its last target
        samples: list[npt.NDArray[np.float64]] = []
        t1 = time.monotonic()
        while time.monotonic() - t1 < 2.0:
            head, antennas = await asyncio.to_thread(robot.get_current_joint_positions)
            samples.append(np.array(list(head) + list(antennas), dtype=np.float64))
            await asyncio.sleep(0.05)
        stacked = np.stack(samples)
        travel = float((stacked.max(axis=0) - stacked.min(axis=0)).max())
        playbin = getattr(robot.media.audio, "_playbin", "not-local")
        return latency, travel, playbin

    latency, travel, playbin = asyncio.run(scenario())
    print(
        f"\n[e2e] cancel latency {latency * 1000:.0f} ms, joint travel after {travel:.4f} rad"
    )
    assert latency < 0.1
    assert travel < 0.02, f"joints still moving after the cancel: {travel:.4f} rad"
    if playbin != "not-local":
        assert playbin is None
```

If the sim turns out to need it, raise the settle sleep (0.5 s → 1.0 s); do not raise the 0.02 rad threshold (the hardware noise floor is 0.0015 rad and the sway it must catch is ≥ 0.08 rad).

### Step 7 — README

In `README.md`, after the sentence ending "cancel the task to stop it (queued audio is flushed).", add: "Cancelling the task is how you interrupt any verb: `play_emotion` stops the motion and the emotion's sound the same way, and the session stays usable for the next verb (see [specs/api.md](specs/api.md) \"Cancellation\")."

### Step 8 — statuses (only once everything in Verification passes)

- `specs/api.md`, `specs/audio.md`, `specs/robot.md`: the line `**Status:** Updated` → `**Status:** Implemented`.
- `specs/_index.md`: the `api.md`, `audio.md`, `robot.md` rows: `| Updated |` → `| Implemented |`.
- `specs/_overview.md`: delete the sentence "Those three are `Updated` while the cancellation contract (below) is built — see [../plans/_index.md](../plans/_index.md)."
- This plan and its row in `plans/_index.md`: `Todo` → `Done`.

## Do not

- Do not call `robot.cancel_move()` or `media.stop_playing()` anywhere in the bridge (see "Facts").
- Do not play the emotion's sound yourself (no `sound=False`, no decoding, no new dependency): the sound must keep starting exactly as upstream starts it.
- Do not re-implement upstream's playback loop.
- Do not add `stop_sound` to `_CONSUMED_SLICE` in `tests/test_robot.py`.
- Do not move the head after a cancel.
- Do not change `play_sound`.

## Verification

Run, in this order, and fix anything red before moving on:

```
uv run ruff check .
uv run ruff format .
uv run pyright
uv run pytest
```

Then the live tier, on the sim first, then the USB robot (the harness spawns and stops the daemon itself; do not start one by hand):

```
uv run pytest tests-e2e -rs -k cancelled_emotion
REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e -rs -k cancelled_emotion
```

Expected: the test passes on both (a `SKIPPED` is not a pass — read the reason printed by `-rs`), the printed cancel latency is well under 100 ms, and the printed joint travel is under 0.02 rad. Finally run the full `uv run pytest tests-e2e -rs` on the sim to make sure the existing emotion and audio tests still pass. Mark this plan `Done` (here and in [_index.md](_index.md)) only after all of the above.
