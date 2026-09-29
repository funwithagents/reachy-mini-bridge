---
code:
  - src/reachy_mini_bridge/camera.py
  - src/reachy_mini_bridge/api.py
  - src/reachy_mini_bridge/fake_reachy_mini.py
tests:
  - tests/test_camera.py
  - tests/test_api.py
  - tests-e2e/test_api.py
---

# Camera feed — the one reader of the robot's camera (`camera.py`)

**Status:** Implemented

## Purpose

The bridge's video feed: **one reader** of the robot's camera, running for the whole session, publishing the newest frame — with the time it was taken and the head pose it was taken from — for any number of consumers to sample at their own rate: the detection loop ([user_perception.md](user_perception.md)), a caller's own code (a display, an agent tool reading a picture), and a vision graph built with another library. `api.camera` is that feed.

Upstream hands frames out one at a time: the client's camera pipeline ends in a GStreamer `appsink` holding one buffer (`drop=True`, `max-buffers=1`), and `media.get_frame()` pulls it — each frame returned once, then `None` (after a wait of up to 20 ms) until the next arrives ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Perception"). Two readers in one process therefore steal frames from each other, silently: a display refreshing at 5 Hz takes half the frames a detector polling at 30 Hz would have seen, and the head tracker's input rate drops with no error anywhere. The feed owns the pull, so nothing else in the process calls `get_frame()`.

The feed also stamps every frame **once**, in one place, with what a geometric consumer needs beyond the pixels: the frame's time on the bridge's monotonic clock and the head's pose at that time, so a face found in the frame is aimed against the pose the frame was taken from ([head_tracking.md](head_tracking.md) "The aim") and every consumer of the same frame agrees on both.

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

- **`frame_id`** increases by one per published frame, from 1, and keeps counting across sessions of the same api object (the feed is per api, like `faces`), so a result from an earlier session can never be mistaken for a new one. `CameraFeed.published_count` equals the last `frame_id` — the exact achieved rate over any window is `Δpublished_count / Δt`.
- **`ts`** is the frame's time. It is the frame's **capture** time when the feed can read it, and otherwise the time the reader received it ("The frame's time and the head pose" below says which is which and what follows from the difference).
- **`image`** is the array upstream returned — no copy, no conversion, BGR as the camera delivers it. Frames are shared by reference between every consumer, so **a consumer never writes into `image`**: anything that draws on a frame (the control panel's face markers) or publishes a sub-region copies first. This is the rule of every latest-value design, and the reason the feed can hand the same array to a display, a detector and a caller at no cost.
- **`head_pose`** is the 4×4 head pose the robot reported at `ts` (the motion loop's `head_pose_at(ts)`, [motion.md](motion.md) "A history of head poses"), or `None` when the feed cannot stand behind it (below). A consumer that needs it reads it from the frame; a consumer that does not (a hands detector, a display) ignores it.

### The feed

```
CameraFeed(read_frame, pose_at)
  .start() / .stop()              # the reader thread; started with the session (below)
  .latest() -> CameraFrame | None # the newest frame, or None before the first / after the session
  .published_count -> int         # frames published, ever, on this feed
```

- **One thread, one reader.** `start()` spawns the reader thread, which loops `read_frame()` — `robot.media.get_frame`, the only call site of it in the bridge — and, for every frame it gets back, stamps it (`ts`, then `head_pose = pose_at(ts)`) and rebinds the published frame under a lock; the lock only guards a consistent frame and is never held during the read. A `None` from `get_frame()` — no frame yet, the headless sim's absent camera — is one more pass (the real backend already waits up to 20 ms inside the call, so an empty feed costs a few wake-ups a second and no CPU). A read that raises is logged at `DEBUG`, retried after `CAMERA_RETRY_S = 0.1` s, and a read that keeps raising for `CAMERA_DOWN_S = 5` s logs one `WARNING` (and one `INFO` when frames return). `stop()` sets the stop event and joins the thread; the thread is a daemon thread, so a process exiting mid-read never hangs on it.
- **Latest value, no queue.** A consumer calls `latest()` whenever it wants a frame and gets the newest; nothing is buffered for it. A consumer slower than the feed sees fewer frames and never falls behind; a consumer faster than the feed sees the same frame again and tells by `frame_id` (the detection loop skips a poll whose `frame_id` it has processed; a display just redraws). No consumer can slow the reader or another consumer.
- **`api.camera`** is the feed, available from construction — a caller wires a consumer to it before entering the session, as it may subscribe to `faces` — with `latest()` `None` until the session's first frame. The session starts the reader right after the media session is up and stops it right before the media session is torn down ([api.md](api.md) "Lifecycle"); at exit the published frame resets to `None`, so a consumer cannot keep reading a frame from a camera that is gone. The daemon's camera feed itself is capped at 10 fps ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking": `IPC_FPS`), so that is the feed's rate on a local daemon; the feed does not pace itself.
- **A property, not a verb.** Reading the feed is an instant, thread-safe call from any thread (the reader thread publishes; consumers only read), so there is no async verb around it and nothing for the cancellation contract to cover. `get_camera_frame()` is retired: `api.camera.latest()` returns what it returned, plus the frame's identity, time and pose, and `None` when it returned `None`.

### The frame's time and the head pose

The one rule: **a head pose is attached only to a capture time.** A face reported on a frame is aimed against `head_pose` directly, with no delay to estimate ([head_tracking.md](head_tracking.md) "Observations that carry their own pose"); a pose taken at the wrong instant would be trusted just the same, and while the head turns toward a face — exactly when the pose matters — the pose at arrival differs from the pose at capture by the pipeline's latency (0.2 s in the sim). An exact-looking wrong pose is worse than none, so the feed never attaches one to an arrival time.

- **Capture time, when the backend gives one.** The reader the feed loops (`frame_reader(robot)`, the bridge's one call site of `get_frame`) returns each frame with its capture time when the backend knows it, or with none. The `fake` knows it: it synthesises the frame inside the call, so the instant `get_frame()` returns is the capture, and every fake frame carries `head_pose`. **Upstream's live backends give none — measured on the sim and on a robot.** The appsink's samples would carry the buffer's `pts` on the pipeline clock (one offset from `time.monotonic()`, read at start), but every buffer the client appsink delivers has `pts = 0` — on the viewer sim (rendered eye camera and webcam relay) and on a Reachy Mini Lite over USB, head still or turning (SDK 1.10). The cause is the daemon's, not the transport's: its media pipeline holds the microphone and speaker, so GStreamer selects the **audio device's clock** for it (base time 0), `unixfdsink` sends timestamps in that clock's domain, and the client's `unixfdsrc`, on the system clock, converts them to a negative running time and clamps them to zero. Reproduced in isolation: the same sender chain with an audio source zeroes every `pts`; forced onto `GstSystemClock` it delivers increasing `pts` 3–24 ms behind arrival. So no live backend reads a capture time today, and the bridge does not pull the pinned appsink internal (`camera._appsink_video`) for a timestamp that is not there. The fix and the ask — the daemon's sender pipeline on the system clock (one line), then `get_frame_with_timestamp()` — are drafted in [../docs/upstream-camera-frame-timestamp.md](../docs/upstream-camera-frame-timestamp.md); once frames carry a `pts`, the reader returns the capture time and every rule below applies unchanged.
- **Arrival time, otherwise.** `ts` is `time.monotonic()` taken when `get_frame()` returned the frame, and `head_pose` is `None`. This is the live backends' path today (`sim` and `real`): the tracker estimates the delay from its pose history ([head_tracking.md](head_tracking.md) "The aim") — correct, not exact.
- **Never the middle:** an arrival time with a pose looked up at it.

The pose comes from the motion session's `head_pose_at`, which is why the api constructs the `MotionSession` object before the feed (construction starts no thread; the reader's first frames get the pose read from the robot until the loop's thread records its own, [motion.md](motion.md)). A feed built without a `pose_at` (no motion session) publishes `head_pose=None` throughout.

### A valid upstream for a vision graph

A vision library built on latest-value sampling consumes any object with a `latest()` returning items that carry a `frame_id`, a `ts` and an `image` — the shape of the `funwithagents/vision-modules` runtime (`Upstream[FrameLike]`), and of any other host feed. `CameraFeed` and `CameraFrame` **have exactly that shape**, so a graph plugs onto `api.camera` directly:

```python
hands = HandStage(api.camera, target_fps=30)   # vision-modules; no adapter, no second reader
```

The compatibility is **structural, not a dependency**: the bridge's core imports no vision library ([project.md](project.md)), and the library's protocols are satisfied by shape. A test in `tests/` pins the shape with the three-member protocol written out locally — `frame_id`, `ts`, `image` as read-only properties, and `latest()` — so a rename on the bridge's side is caught here, and the library's side pins the same shape from its end. The frame-ownership rule above (read-only, copy before drawing) is the library's too.

Detectors follow the same principle: a detector written against the bridge's `FaceDetector` protocol ([user_perception.md](user_perception.md) "Custom detectors") runs in the bridge's detection loop or in a vision graph over the same feed; the bridge's shipped detector wraps the model upstream already depends on, and further families would arrive behind per-feature extras ([user_perception.md](user_perception.md) open question 7) — lazily, never in the core.

### `fake` backend support

`FakeReachyMini.media.get_frame()` ([robot.md](robot.md)) is paced like the real backend's: the first call returns a frame at once (the fake is "always ready"), and each later call returns the next frame once `1 / FAKE_FRAME_HZ` (10 fps) has elapsed since the previous one, blocking until then — the real `get_frame()` blocks up to 20 ms for the next frame; the fake blocks up to a frame period, on the feed's thread, where it costs nothing. Every frame is the same 64×48 gradient (tests assert structure, not motion). The fake motion session answers `head_pose_at`, so a fast test asserts that a frame published while the fake's head is at pose P carries P, and that a face reported on it is aimed against P rather than against the pose at report time ([head_tracking.md](head_tracking.md)). `tests/` observe on the fake: the feed's `frame_id` advancing at the fake's rate with `published_count` equal to it; `latest()` `None` before entry and after exit; two consumers sampling the feed both seeing every frame (the property the single reader exists for); a `get_frame` that raises leaving the last frame in place and the feed recovering when frames return; a frame's `head_pose` matching the fake's pose at its `ts`.

## Relationship to the other specs

- **[api.md](api.md):** `api.camera`; the feed's place in the session lifecycle; `get_camera_frame()` retired; `CameraFrame` exported from the front door.
- **[user_perception.md](user_perception.md):** the detection loop samples the feed and copies the frame's `ts` and `head_pose` onto its report, whichever detector runs.
- **[head_tracking.md](head_tracking.md):** aims a report carrying a pose against it directly; estimates the delay for one without.
- **[motion.md](motion.md):** `head_pose_at`, the feed's source of a frame's pose.
- **[robot.md](robot.md):** `media.get_frame` is consumed by the feed alone; the fake's paced frames.
- **[control_panel.md](control_panel.md):** the panel's camera image is the feed's latest frame.
- **[testing.md](testing.md):** the `camera` capability probe reads `robot.media.get_frame` directly, before the api's session exists; the live camera test reads the feed.
- **[tools.md](tools.md):** the picture an agent tool returns is the feed's latest image, base64-encoded at that boundary.

## Open questions

1. **The capture time waits for upstream.** The client's appsink `pts` is zero on every frame, sim and robot alike, because the daemon's media pipeline runs on its audio clock ("The frame's time and the head pose" above). The fix belongs in the daemon's media server (the ask in [../docs/upstream-camera-frame-timestamp.md](../docs/upstream-camera-frame-timestamp.md)), **not** in the bridge's launchers: the bridge must behave the same on a daemon it did not start — a borrowed one, and the wireless robot's own, where it only ever borrows — so a correction that lives in the launchers would make the feed's frames carry a pose on some daemons and not others. Once upstream stamps frames, `frame_reader` gains a guarded appsink path (pull the sample, map `pts + base_time` onto `time.monotonic()`, fall back to `get_frame()` and arrival time when the `pts` is zero or the attribute is missing) and live frames over the local IPC carry their pose. The wireless robot's client reads the camera over WebRTC (`webrtc_client_gstreamer`, RTP timestamps), a separate path to measure then.
2. **The camera model on the feed.** The head tracker holds the active camera's model (size, intrinsics, and for a webcam the fixed mount, [head_tracking.md](head_tracking.md)); a consumer projecting the feed's frames wants it too. A read-only `api.camera.model` is the natural place, once a consumer outside the tracker needs it.
3. **A subscription.** The feed is sampled, not subscribed to: an `Observable` publishing every frame would wake an async consumer 10 times a second for a value it can read at will. An awaitable "next frame" (`await api.camera.next_frame()`) is cheap to add when a caller wants to react to frames rather than poll; deferred until one does.
