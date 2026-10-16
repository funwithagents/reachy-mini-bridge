# Camera feed followers: `frames()` and `wait_for` over an `Observable`

**Status:** Done

Implements the settled behavior in `specs/vision/camera.md` ("Followers — `frames()` and `wait_for`", the feed's surface) and `specs/core/observable.md` (`set_threadsafe`), with the edits they brought to `specs/core/bridge.md` ("Cancellation", the `camera` property). Delivers the camera feed's second door: an `Observable[CameraFrame | None]` the reader thread publishes into through the session's loop, `frames()` as a latest-wins follower that ends with the session, `wait_for(predicate)`, the checks at the call, and the observable's `set_threadsafe` for a producer on another thread — the surface `naoqi-bridge`'s camera feeds expose, over this bridge's thread reader. Leaves the reader a thread (upstream's `get_frame()` blocks inside the call), the detection loop a sampler on its own clock, the frame's shape unchanged, and the spec's two remaining open questions (the capture time, the camera model) open.

## Scope

- `src/reachy_mini_bridge/observable.py` — `set_threadsafe(loop, value)`: `loop.call_soon_threadsafe(self.set, value)`
- `src/reachy_mini_bridge/camera.py` — `CameraFeed` holds `_frames: Observable[CameraFrame | None]`, captures the loop and bumps a session counter in `start()`, publishes every frame to the observable from the thread after the lock write (`_wake`, a closed loop swallowed), publishes `None` in `stop()` after the join; `frames()`, `wait_for()`, `_check_running`, `_follow(session)`
- `src/reachy_mini_bridge/bridge.py` — the `camera` property's docstring names the followers
- `tests/test_camera.py` — the followers over the scripted reader (every frame to two followers, the newest to a slow one, `wait_for` both ways, ending at `stop()`, the checks at the call, a cancelled follower, no carry-over into a later session)
- `tests/test_observable.py` — `set_threadsafe` from a thread; on a closed loop
- `tests/test_bridge.py` — followers through a fake session with a cancel mid-flight; refused outside a session
- `specs/vision/camera.md`, `specs/core/observable.md`, `specs/core/bridge.md`, `specs/_index.md` — the design, open questions closed (camera 3, observable 2)
- `docs/reference/api.md`, `docs/getting-started.md`, `docs/guides/perception-and-tracking.md`, `AGENTS.md` — the two doors named where the feed is described

## Steps

1. **`Observable.set_threadsafe`.** One method queuing `set` on the given loop; `RuntimeError` from a closed loop propagates to the caller.
2. **The feed's channel.** `_frames = Observable(None)`, `_loop = None`, `_session = 0` at construction. `start()` records `asyncio.get_running_loop()` and increments `_session` before spawning the thread. The thread, after `self._latest = CameraFrame(...)` under the lock, calls `_wake(frame)`: `set_threadsafe` on the recorded loop, `RuntimeError` swallowed. `stop()` publishes `None` through `set` after the thread is joined — the join's completion callback runs after every frame callback the thread queued — and only when a thread was running.
3. **The followers.** `frames()` checks `running` (`BridgeError` naming the call) and returns `_follow(self._session)`, an async generator over `_frames.changes()` returning on `None` or a session change. `wait_for(predicate)` checks `running`, returns `latest()` when it matches, else the first matching frame of `_follow`, and raises `BridgeError` when the follow ends.
4. **The detection loop stays.** No change to `face_detection.py`: it samples `latest()` on its clock (camera.md "The detection loop samples").
5. **Tests**, as in Scope: the feed-level tests drive a loop of their own (`asyncio.run` around start, follow, stop), the reader scripted with three empty reads ahead of its frames so a follower subscribed right after `start()` is in place for the first one; the bridge-level test runs two followers on the fake session and cancels one.
6. **Docs and statuses**, as in Scope; both specs stay `Implemented` since the code lands with the design.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` — all green (741 passed on macOS). The live tier needs no new test: the followers are loop mechanics over the same reader the camera e2e test already exercises.
