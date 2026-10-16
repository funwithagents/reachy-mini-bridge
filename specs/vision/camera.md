---
code:
  - src/reachy_mini_bridge/camera.py
  - src/reachy_mini_bridge/bridge.py
  - src/reachy_mini_bridge/fake_reachy_mini.py
tests:
  - tests/test_camera.py
  - tests/test_bridge.py
  - tests-e2e/test_perception.py
---

# Camera feed — the one reader of the robot's camera (`camera.py`)

**Status:** Implemented

## Purpose

The bridge's video feed: **one reader** of the robot's camera, running for the whole session, publishing the newest frame — with the time it was taken and the head pose it was taken from — for any number of consumers to sample at their own rate: the detection loop ([user_perception.md](user_perception.md)), a caller's own code (a display, an agent tool reading a picture), and a vision graph built with another library — or to follow, frame by frame, from the event loop ("Followers" below). `bridge.camera` is that feed.

Upstream hands frames out one at a time: the client's camera pipeline ends in a GStreamer `appsink` holding one buffer (`drop=True`, `max-buffers=1`), and `media.get_frame()` pulls it — each frame returned once, then `None` (after a wait of up to 20 ms) until the next arrives ([../docs/internals/upstream-sdk-notes.md](../../docs/internals/upstream-sdk-notes.md) "Perception"). Two readers in one process therefore steal frames from each other, silently: a display refreshing at 5 Hz takes half the frames a detector polling at 30 Hz would have seen, and the head tracker's input rate drops with no error anywhere. The feed owns the pull, so nothing else in the process calls `get_frame()`.

The feed also stamps every frame **once**, in one place, with what a geometric consumer needs beyond the pixels: the frame's time on the bridge's monotonic clock and the head's pose at that time, so a face found in the frame is aimed against the pose the frame was taken from ([head_tracking.md](../motion/head_tracking.md) "The aim") and every consumer of the same frame agrees on both.

## Core concepts / Decided

### The frame

```python
@dataclass(frozen=True)
class CameraFrame:
    frame_id: int                                       # 1, 2, 3, … per feed — the key a result is matched on
    ts: float                                           # the frame's time on the monotonic clock (below)
    image: npt.NDArray[np.uint8]                        # (H, W, 3) BGR — the array get_frame() returned, read-only by convention
    head_pose: npt.NDArray[np.float64] | None = None    # the head's pose at ts, when the feed knows it
```

- **`frame_id`** increases by one per published frame, from 1, and keeps counting across sessions of the same bridge object (the feed is per api, like `faces`), so a result from an earlier session can never be mistaken for a new one. `CameraFeed.published_count` equals the last `frame_id` — the exact achieved rate over any window is `Δpublished_count / Δt`.
- **`ts`** is the frame's time. It is the frame's **capture** time when the feed can read it, and otherwise the time the reader received it ("The frame's time and the head pose" below says which is which and what follows from the difference).
- **`image`** is the array upstream returned — no copy, no conversion, BGR as the camera delivers it. Frames are shared by reference between every consumer, so **a consumer never writes into `image`**: anything that draws on a frame (the control panel's face markers) or publishes a sub-region copies first. This is the rule of every latest-value design, and the reason the feed can hand the same array to a display, a detector and a caller at no cost.
- **`head_pose`** is the 4×4 head pose the robot reported at `ts` (the motion loop's `head_pose_at(ts)`, [motion.md](../motion/motion.md) "A history of head poses"), or `None` when the feed cannot stand behind it (below). A consumer that needs it reads it from the frame; a consumer that does not (a hands detector, a display) ignores it.

### The feed

```
CameraFeed(read_frame, pose_at)
  async .start() / async .stop()  # the reader thread; the bridge's to call, with the session (below)
  .latest() -> CameraFrame | None # the newest frame, or None before the first / after the session
  .published_count -> int         # frames published, ever, on this feed
  .frames() -> AsyncIterator[CameraFrame]    # follow the feed from the event loop: latest-wins; ends with the session
  async .wait_for(predicate) -> CameraFrame  # the newest frame if it matches, else the first published one that does
```

- **One thread, one reader.** `start()` spawns the reader thread, which loops `read_frame()` — `robot.media.get_frame`, the only call site of it in the bridge — and, for every frame it gets back, stamps it (`ts`, then `head_pose = pose_at(ts)`) and rebinds the published frame under a lock; the lock only guards a consistent frame and is never held during the read. A `None` from `get_frame()` — no frame yet, the headless sim's absent camera — is one more pass, paced to `CAMERA_EMPTY_S = 0.02` s: a read that waited at least that long inside the call (upstream's GStreamer reader waits up to 20 ms for a sample) runs again at once, a read that returned sooner (upstream's `get_frame()` returns `None` immediately, with a warning, when the client was built without a camera) waits out the difference on the stop event — so an empty feed costs at most fifty passes a second whatever the reader does, and no CPU. A read that raises is logged at `DEBUG`, retried after `CAMERA_RETRY_S = 0.1` s, and a read that keeps raising for `CAMERA_DOWN_S = 5` s logs one `WARNING` (and one `INFO` when frames return). `stop()` sets the stop event and joins the thread off the event loop (`asyncio.to_thread`); a `stop()` on a feed that is not running is a no-op; the thread is a daemon thread, so a process exiting mid-read never hangs on it.
- **Latest value, no queue.** A consumer calls `latest()` whenever it wants a frame and gets the newest; nothing is buffered for it. A consumer slower than the feed sees fewer frames and never falls behind; a consumer faster than the feed sees the same frame again and tells by `frame_id` (the detection loop skips a poll whose `frame_id` it has processed; a display just redraws). No consumer can slow the reader or another consumer.
- **`bridge.camera`** is the feed, available from construction — a caller wires a consumer to it before entering the session, as it may subscribe to `faces` — with `latest()` `None` until the session's first frame. The pair is the `async start()` / `async stop()` every session of the bridge has, and the bridge's alone to call — a consumer samples the feed and never starts or stops it: the bridge starts the reader right after the media session is up and stops it right before the media session is torn down ([bridge.md](../core/bridge.md) "Lifecycle"); at exit the published frame resets to `None`, so a consumer cannot keep reading a frame from a camera that is gone. The daemon's camera feed itself is capped at 10 fps ([../docs/internals/upstream-sdk-notes.md](../../docs/internals/upstream-sdk-notes.md) "Face tracking": `IPC_FPS`), so that is the feed's rate on a local daemon; the feed does not pace itself.
- **`latest()` is a read, `frames()` a verb.** Sampling the feed is an instant, thread-safe call from any thread (the reader thread publishes; consumers only read), so there is no async verb around `latest()` and nothing for the cancellation contract to cover in it; `frames()` is the feed's one spanning verb ("Followers" below). `get_camera_frame()` is retired: `bridge.camera.latest()` returns what it returned, plus the frame's identity, time and pose, and `None` when it returned `None`.

### Followers — `frames()` and `wait_for`

Two doors into the same frames, the consumer's loop choosing: `latest()` for a **sampler** — a display at its refresh rate, an agent tool asked for a picture, the detection loop on its own clock (below) — and `frames()` for a **follower**, a consumer that reacts to each frame as it lands: a recorder, a server streaming the camera to a client, a test waiting for the first frame.

- **An `Observable` behind the thread.** The feed holds an `Observable[CameraFrame | None]` ([observable.md](../core/observable.md)). The reader thread, having rebound the published frame under its lock, publishes the same frame into the observable through the session's event loop — `set_threadsafe` with the loop `start()` captured — so followers wake on the loop, in publication order, one loop hop behind `latest()`. `latest()` stays the thread-side slot: an instant read from any thread whatever the loop is doing, always at or ahead of what the followers have been handed. The observable is the followers' channel and is never read for `latest()`.
- **`frames()`** returns an async iterator of every frame published from its first `async for` on. A follower that keeps up sees every frame; one that falls behind gets the newest, never a backlog (the observable's latest-wins); nothing blocks the reader or another follower. It ends on its own when the feed stops: `stop()` publishes `None` once the thread is joined — every frame the thread queued on the loop ran before the join's completion did, so `None` is the last value a follower of the session sees — and a follower never carries over into a later session: it remembers the session it was made in and ends when another one runs.
- **`wait_for(predicate)`** returns `latest()` at once when the predicate holds for it, else the first published frame that does; it raises `BridgeError` when the feed stops first. The check-then-follow never misses a frame, since `latest()` is never behind the observable.
- **Checks at the call.** `frames()` and `wait_for` on a feed that is not running raise `BridgeError` where they are called, not at the first `async for` — the rule of `audio_input()` ([microphone.md](../audio/microphone.md)). `latest()` never raises: `None` before the first frame and outside a session.
- **Cancellation.** `frames()` is a spanning verb ([bridge.md](../core/bridge.md) "Cancellation"): a cancel of the consuming task, a `break`, or the iterator's `aclose()` ends that follower and detaches it; the reader, every other follower and the session go on untouched.
- **The detection loop samples.** It polls `latest()` on its own clock ([user_perception.md](user_perception.md) "The detection loop"): under a `target_fps` ceiling it takes one frame per period and skips the rest, so a wake-up per frame would serve it nothing; without a ceiling it picks a new frame up within a third of a frame period, against a pipeline 200 ms long. It stays a sampler, and the feed's followers cost it nothing: a `set` with no follower is one loop callback per frame.

### The frame's time and the head pose

The one rule: **a head pose is attached only to a capture time.** A face reported on a frame is aimed against `head_pose` directly, with no delay to estimate ([head_tracking.md](../motion/head_tracking.md) "Observations that carry their own pose"); a pose taken at the wrong instant would be trusted just the same, and while the head turns toward a face — exactly when the pose matters — the pose at arrival differs from the pose at capture by the pipeline's latency (0.2 s in the sim). An exact-looking wrong pose is worse than none, so the feed never attaches one to an arrival time.

- **Capture time, when the backend gives one.** The reader the feed loops (`frame_reader(robot)`, the bridge's one call site of `get_frame`) returns each frame with its capture time when the backend knows it, or with none. The `fake` knows it: it synthesises the frame inside the call, so the instant `get_frame()` returns is the capture, and every fake frame carries `head_pose`. **Upstream's live backends give none — measured on the sim and on a robot.** The appsink's samples would carry the buffer's `pts` on the pipeline clock (one offset from the bridge's clock, read at start), but every buffer the client appsink delivers has `pts = 0` — on the viewer sim (rendered eye camera and webcam relay) and on a Reachy Mini Lite over USB, head still or turning (SDK 1.10). The cause is the daemon's, not the transport's: its media pipeline holds the microphone and speaker, so GStreamer selects the **audio device's clock** for it (base time 0), `unixfdsink` sends timestamps in that clock's domain, and the client's `unixfdsrc`, on the system clock, converts them to a negative running time and clamps them to zero. Reproduced in isolation: the same sender chain with an audio source zeroes every `pts`; forced onto `GstSystemClock` it delivers increasing `pts` 3–24 ms behind arrival. So no live backend reads a capture time today, and the bridge does not pull the pinned appsink internal (`camera._appsink_video`) for a timestamp that is not there. The fix and the ask — the daemon's sender pipeline on the system clock (one line), then `get_frame_with_timestamp()` — are upstream's to make, written up as a report to file there; once frames carry a `pts`, the reader returns the capture time and every rule below applies unchanged.
- **Arrival time, otherwise.** `ts` is `time.perf_counter()` taken when `get_frame()` returned the frame, and `head_pose` is `None`. This is the live backends' path today (`sim` and `real`): the tracker estimates the delay from its pose history ([head_tracking.md](../motion/head_tracking.md) "The aim") — correct, not exact.
- **The bridge's clock is `time.perf_counter()`**, everywhere the bridge stamps or times anything — frames, mic chunks, the motion loop's ticks and head-pose history, the detection loop's windows. It is monotonic and system-wide; on macOS and Linux it reads the same clock as `time.monotonic()`, and on Windows it is the high-resolution counter, which `time.monotonic()` uses only from Python 3.13 (before, its 15.6 ms ticks would step a 60 Hz loop unevenly).
- **Never the middle:** an arrival time with a pose looked up at it.

The pose comes from the motion session's `head_pose_at`, which is why the bridge constructs the `MotionSession` object before the feed (construction starts no thread; the reader's first frames get the pose read from the robot until the loop's thread records its own, [motion.md](../motion/motion.md)). A feed built without a `pose_at` (no motion session) publishes `head_pose=None` throughout.

### A valid upstream for a vision graph

A vision library built on latest-value sampling consumes any object with a `latest()` returning items that carry a `frame_id`, a `ts` and an `image` — the shape of the `funwithagents/vision-modules` runtime (`Upstream[FrameLike]`), and of any other host feed. `CameraFeed` and `CameraFrame` **have exactly that shape**, so a graph plugs onto `bridge.camera` directly:

```python
hands = HandStage(bridge.camera, target_fps=30)   # vision-modules; no adapter, no second reader
```

The compatibility is **structural, not a dependency**: the bridge's core imports no vision library ([project.md](../project.md)), and the library's protocols are satisfied by shape. The feed's tests in `tests/` read the frame's three members — `frame_id`, `ts`, `image` — and the feed's `latest()` by these names, so a rename on the bridge's side is caught here, and the library's side pins the same shape from its end. The frame-ownership rule above (read-only, copy before drawing) is the library's too.

Detectors follow the same principle: a detector written against the bridge's `FaceDetector` protocol ([user_perception.md](user_perception.md) "Custom detectors") runs in the bridge's detection loop or in a vision graph over the same feed; the bridge's shipped detector wraps the model upstream already depends on, and further families would arrive behind per-feature extras ([user_perception.md](user_perception.md) open question 7) — lazily, never in the core.

### `fake` backend support

`FakeReachyMini.media.get_frame()` ([robot.md](../core/robot.md)) is paced like the real backend's: the first call returns a frame at once (the fake is "always ready"), and each later call returns the next frame once `1 / FAKE_FRAME_HZ` (10 fps) has elapsed since the previous one, blocking until then — the real `get_frame()` blocks up to 20 ms for the next frame; the fake blocks up to a frame period, on the feed's thread, where it costs nothing. Every frame is the same 64×48 gradient (tests assert structure, not motion). The fake motion session answers `head_pose_at`, so a fast test asserts that a frame published while the fake's head is at pose P carries P, and that a face reported on it is aimed against P rather than against the pose at report time ([head_tracking.md](../motion/head_tracking.md)). `tests/` observe on the fake: the feed's `frame_id` advancing at the fake's rate with `published_count` equal to it; `latest()` `None` before entry and after exit; two consumers sampling the feed both seeing every frame (the property the single reader exists for); a `get_frame` that raises leaving the last frame in place and the feed recovering when frames return; a frame's `head_pose` matching the fake's pose at its `ts`. On the followers, over a scripted reader and through a fake session: two followers each receiving every frame at the reader's pace and a slow one the newest with no backlog; `wait_for` handing back the newest frame when it matches and the first published match otherwise; every follower ending on its own when the feed stops, a pending `wait_for` raising then, and a follower never yielding a later session's frame; a follower cancelled mid-wait ending promptly while the other follower, the reader and the session go on; `frames()` refused outside a session.

## Relationship to the other specs

- **[bridge.md](../core/bridge.md):** `bridge.camera`; the feed's place in the session lifecycle; `frames()` among the spanning verbs of the cancellation contract; `get_camera_frame()` retired; `CameraFrame` exported from the front door.
- **[user_perception.md](user_perception.md):** the detection loop samples the feed and copies the frame's `ts` and `head_pose` onto its report, whichever detector runs.
- **[head_tracking.md](../motion/head_tracking.md):** aims a report carrying a pose against it directly; estimates the delay for one without.
- **[motion.md](../motion/motion.md):** `head_pose_at`, the feed's source of a frame's pose.
- **[observable.md](../core/observable.md):** the followers' channel — an `Observable` the reader thread publishes into through `set_threadsafe`.
- **[microphone.md](../audio/microphone.md):** the sibling feed — a ring with a cursor per subscriber for a signal every subscriber needs whole, an observable for a state where the newest frame is the one that matters; the same rules at the call and at the session's end.
- **[robot.md](../core/robot.md):** `media.get_frame` is consumed by the feed alone; the fake's paced frames.
- **[control_panel.md](../examples/control_panel.md):** the panel's camera image is the feed's latest frame.
- **[testing.md](../testing/testing.md):** the `camera` capability probe reads the running bridge's camera feed — never `get_frame()` beside it, which would steal the feed's frames; the live camera test reads the feed too.

## Open questions

1. **The capture time waits for upstream.** The client's appsink `pts` is zero on every frame, sim and robot alike, because the daemon's media pipeline runs on its audio clock ("The frame's time and the head pose" above). The fix belongs in the daemon's media server (an upstream report is drafted, to be filed), **not** in the bridge's launchers: the bridge must behave the same on a daemon it did not start — a borrowed one, and the wireless robot's own, where it only ever borrows — so a correction that lives in the launchers would make the feed's frames carry a pose on some daemons and not others. Once upstream stamps frames, `frame_reader` gains a guarded appsink path (pull the sample, map `pts + base_time` onto the bridge's clock, fall back to `get_frame()` and arrival time when the `pts` is zero or the attribute is missing) and live frames over the local IPC carry their pose. The wireless robot's client reads the camera over WebRTC (`webrtc_client_gstreamer`, RTP timestamps), a separate path to measure then.
2. **The camera model on the feed.** The head tracker holds the active camera's model (size, intrinsics, and for a webcam the fixed mount, [head_tracking.md](../motion/head_tracking.md)); a consumer projecting the feed's frames wants it too. A read-only `bridge.camera.model` is the natural place, once a consumer outside the tracker needs it.
