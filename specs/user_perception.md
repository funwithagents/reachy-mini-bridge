---
code:
  - src/reachy_mini_bridge/face_detection.py
  - src/reachy_mini_bridge/api.py
  - src/reachy_mini_bridge/config.py
  - src/reachy_mini_bridge/fake_reachy_mini.py
tests:
  - tests/test_face_detection.py
  - tests/test_api.py
  - tests-e2e/test_api.py
---

# User perception — detecting the people in front of the robot (`face_detection.py`)

**Status:** Stable

## Purpose

How the bridge perceives the **users** — the people in front of the robot: a **detection loop** that reads faces from a pluggable source and publishes them as an observable value ([observable.md](observable.md)) — a caller reads the current report directly and subscribes to be woken when the number of people changes. The bridge's own head tracker ([head_tracking.md](head_tracking.md)) is the report's first consumer inside the bridge; a caller's code is the other.

Faces are today's means of perceiving a user, and the report is shaped around them; the concept is the perception itself, so later cues (a voice's direction of arrival, a person recognised) land here beside the faces rather than in a spec of their own.

Upstream detects inside the daemon, as one pipeline with its own head tracking that the client can only switch on and off ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking"): the client sees the selected face and nothing else. This concept makes the daemon's detector one **detection source** among others — a developer's own detector is another — and the bridge ships **no vision code** of its own.

## Core concepts / Decided

### The pipeline

```
   frames ──► detector ──► pixel faces ──► select ──► Face report ──► Observable[FaceReport]  (api.faces)
             (custom:                                              │
              developer's)                                         └──► head tracker ──► aim ──► motion loop's gaze layer
   daemon: REST poll ───────────────────────────────────────────────┘  (the daemon already selected one face)
```

| Stage | Owner | What it does |
|---|---|---|
| detection source | `face_detection.py` | yields one raw **observation** per poll: the faces seen at `ts`, or none (`daemon` and `custom` sources below) |
| select | `face_detection.py` (`custom` only) | one face is the **target**: acquire the largest above a minimum size, then the nearest to the previous target; drop the association after a run of misses — upstream's rule, re-implemented as a few lines of geometry. Its centre is **not** smoothed: pixels from different frames were taken from different head poses, and the head tracker smooths the aim in the world frame instead. The `daemon` source arrives already selected (and smoothed in the image, which the tracker's delay estimate absorbs) |
| face report | `face_detection.py` | the current `FaceReport`, published on `api.faces` |
| head tracker | [head_tracking.md](head_tracking.md) | the target face → a look-at **aim** (a head pose), handed to the motion loop — fed every poll, not only the published changes |
| gaze layer | [motion.md](motion.md) "The gaze layer" | the aim composed into the idle move's pose by the tracking weight |

### The face report

```python
@dataclass(frozen=True)
class Face:
    x: float             # normalised image coordinates of the face (its nose when known,
    y: float             # else the bbox centre) in [-1, 1]: x right, y down, (0, 0) the centre
    roll: float | None   # head roll in radians from the eye line; None when the source has none
    size: float | None   # bbox height as a fraction of the frame height; None from the daemon

@dataclass(frozen=True)
class FaceReport:
    faces: tuple[Face, ...]  # every face the source reports; the target face first
    ts: float                # when the observation was made (the source's monotonic clock)
    source: str              # "daemon" | "custom"
    active: bool             # a detector is running. False means "unknown", not "nobody"
    head_pose: npt.NDArray[np.float64] | None = None  # the pose the frame was captured from, when known
```

- **Coordinates are the tracker's**: the normalised image position the current daemon tracker works in and reports, resolution-independent, so a report reads the same from either source. A bearing in degrees (yaw / pitch of the ray, [api.md](api.md)'s human units) is a later **additive** field: it needs the intrinsics the tracker holds (below), and nothing above changes when it lands.
- **A list, even from the daemon.** The daemon reports at most one face — its selector keeps one and publishes no count — so from that source the count is 0 or 1 and a count change is exactly *appeared* / *left*. A custom detector reports every face it sees; the target face is first.
- **`head_pose`** is the head pose the frame was captured from, when the source knows it (a custom detector's frame, "Custom detectors" below); `None` from the daemon, whose report says only when it detected. The head tracker aims a report with a pose against it directly, and estimates the delay for one without ([head_tracking.md](head_tracking.md) "The aim").
- **`active` is tri-state by construction.** `faces=()` with `active=True` is *nobody there*; `active=False` is *no detector is looking* (detection off, or the daemon has no camera) — a caller must not read it as an empty room. Before the session is entered, and after it exits, the value is `FaceReport((), ts=0.0, source=<config>, active=False)`.

### The report is an observable

`api.faces` is an `Observable[FaceReport]` ([observable.md](observable.md)): `value` is the current report, `changes()` wakes a subscriber on every *published* value, `wait_for(predicate)` waits for one that matches. The detection loop is its producer and decides what counts as a change: it `update`s the report on every poll (fresh coordinates for anyone reading `value`) and `set`s it only when the face **count** changes (debounced, below) or `active` flips — so `async for report in api.faces.changes()` wakes on *someone appeared*, *someone left*, *detection started / stopped*, never on a face moving.

### The detection loop

One asyncio task owned by the api, running while **anyone needs faces**: the caller's detection switch is on, or head tracking is on (the tracker is a client of the loop like any subscriber). It polls the configured source at `FACE_POLL_HZ = 30` — three polls per frame of the daemon's local camera feed, which upstream caps at 10 fps (`media_server.IPC_FPS`, [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking"), so each observation is picked up within a third of a frame rather than up to a whole one late — turns each observation into a `FaceReport`, `update`s `api.faces`, feeds the tracker, and publishes count changes through a **debounce**:

- a **rise** in the count is published on the first report that shows it;
- a **drop** is published once the lower count has held for `FACE_ABSENT_S = 0.3` s — upstream's detector reports no face on a single missed frame, and the debounce is what keeps *left* from firing on a blink of the detector. (The tracker's loss timeout, below, is a longer, separate window: one serves the event, the other the head.)
- `active` flips are published at once: when the loop starts and its source is up, when it stops, and when the source reports it cannot look (the daemon without a camera).

A poll that fails (an HTTP error, a frame that is `None`, a detector that raises) is logged at `DEBUG` and skipped, never fatal; a source that keeps failing for `FACE_SOURCE_DOWN_S = 5` s flips `active` to `False` with one `WARNING`, and back to `True` on the next good poll. The three values are module constants of `face_detection.py`, not config (a knob would land in the `faces` block).

### Detection sources

| `faces.detector` | Reads | Needs | Notes |
|---|---|---|---|
| `daemon` *(default)* | `GET http://{host}:{port}/api/media/tracking/face` on the daemon's HTTP port → `{"status": "ok", "face_target": {detected, x, y, roll, ts}}`; `503` while the backend is not ready | the daemon's tracker **armed at a weight above zero** — the daemon runs its detector only then, and clears the face target at weight 0. The loop arms it with `robot.start_head_tracking(DAEMON_DETECT_WEIGHT)`, `DAEMON_DETECT_WEIGHT = 0.001`, when it starts and `stop_head_tracking()` when it stops | The daemon blends its own aim into the head by that weight — one part in a thousand, invisible — and its lost-face recentre by the same. That lean on documented daemon behaviour is pinned by the live tier, and is what the `custom` source does without. The same fields as the 1 Hz status stream (`get_tracked_face()`), read on demand instead: a poll is one HTTP round trip on the host and port the bridge already uses for readiness and the kinematics engine. In the sim, the launcher's first correction — tracking stepped each control tick — is what makes the daemon update its face target at all ([sim_daemon.md](sim_daemon.md)) |
| `custom` | `robot.media.get_frame()` (the frame `get_camera_frame()` returns) at the poll rate, handed to the registered detector | a registered `FaceDetector` factory (below); a camera | The daemon's tracking is left **off**. The detector runs off the event loop (`asyncio.to_thread`) on the frame the poll fetched; a frame that is `None` (no camera yet, the headless sim) is a skipped poll. The bridge normalises the pixel faces it gets back against that frame's size, then selects the target |

### Custom detectors — the `FaceDetector` protocol

```python
@dataclass(frozen=True)
class PixelFace:
    bbox: tuple[float, float, float, float]        # x, y, width, height in pixels of the frame given
    nose: tuple[float, float] | None = None        # the point the head aims at; the bbox centre when None
    eyes: tuple[tuple[float, float], tuple[float, float]] | None = None  # right, left: gives roll

class FaceDetector(Protocol):
    def detect(self, frame_bgr: npt.NDArray[np.uint8], ts: float) -> Sequence[PixelFace]: ...

FaceDetectorFactory = Callable[[], FaceDetector]
```

- **A factory, called once per detection start**, as the idle move's is called per idle entry ([motion.md](motion.md) "Custom idle moves"): the registered value is a zero-argument callable returning a fresh detector, so a detector holds per-run state (a model session) and a restart gets a clean one. Registered through `FaceSettings.face_detector` (a Python-only config field) or `set_face_detector(factory)`; `None` clears it.
- **Checked at registration**: not callable, a call that raises, or a result without a callable `detect` raises `ValueError` and leaves the registered factory as it was. With `faces.detector` `"custom"` and nothing registered, session entry raises `ValueError` (as a bad `idle_move` does); registering while the loop runs in `custom` mode restarts the source with the new detector.
- **What the author guarantees.** `detect` runs on a worker thread once per **new** camera frame: upstream's `get_frame()` hands each frame out once and returns `None` until the next arrives, so polling faster than the feed runs the detector at the feed's rate (10 fps on a local daemon), the polls in between skipped as `None` frames. It returns within a frame period or the next frame is skipped (frames are dropped, never queued); it may return faces in any order; it never touches the robot or the api. Its dependencies are its own — the bridge adds none.
- **The frame's time and the head pose it was taken from.** The detection loop hands the detector its frame with `ts`, the frame's **capture** time on the bridge's monotonic clock, and attaches to the report the head pose at that time (`FaceReport.head_pose`, the motion loop's `head_pose_at(ts)` — the robot's reported pose at the capture time, [motion.md](motion.md)) — image, time and pose taken together, so the head tracker aims each face against the pose its frame was captured from, exactly, with no delay to estimate ([head_tracking.md](head_tracking.md) "The aim"). This is also why a custom detector returns faces unsmoothed: smoothing across frames mixes pixels taken from different head poses, and the tracker's easing smooths the aim in the world frame instead. Where the capture time comes from is open question 6; until it is known, `ts` is when `get_frame()` returned the frame, the report carries no pose, and the tracker estimates the delay as it does for the `daemon` source.
- **A detector that raises while running** is one skipped poll and a `DEBUG` line; raising on every poll trips the source-down rule above.
- **Upstream's detector is a valid custom detector.** `reachy_mini.vision.face_detector.FaceDetector` (YuNet on ONNX Runtime; onnxruntime and the Hugging Face hub are base dependencies of `reachy_mini`, OpenCV is not needed) returns faces with bbox, eyes and nose; wrapping it into `PixelFace`s is a few lines a developer writes — the bridge's documentation shows them, and the bridge does not ship them. A shipped `local` mode that would is deliberately deferred (open question 1).

### Configuration

Three fields, in two blocks ([config.md](config.md)):

```json
"faces":  { "detector": "daemon", "detection": true },
"motion": { "tracking": true }
```

- `faces.detector` ∈ `daemon` / `custom` — the detection source; `custom` needs the Python-only `FaceSettings.face_detector` factory.
- `faces.detection` — whether the detection loop runs from session entry for the caller's sake (the face report). It needs **no motors**: nothing moves. Its verb is `set_face_detection(enabled)`, its property `face_detection`.
- `motion.tracking` — whether the head tracker runs from session entry ([head_tracking.md](head_tracking.md)). It **implies detection**: the loop runs while either switch is on, so `detection: false` with `tracking: true` is valid and means faces are detected for the head only (the report is still published — the loop has it anyway). `set_face_detection(False)` while tracking is on leaves the loop running; `faces.value.active` says what is actually happening.

### Lifecycle

`ReachyMiniApi.__aenter__` ([api.md](api.md) "Lifecycle") starts the detection loop after the media session and the wobbling setup and before the motion session, since the tracker hands its aim to the loop; it stops it right after the motion session on exit, so the daemon's detector (in `daemon` mode) is disarmed before the robot disconnects and never left running for the next app. Bring-up cancel / failure unwinds it like every other step. The report resets to its inactive value at exit, published through `set`, so a subscriber of `changes()` learns that detection stopped; the observable itself outlives the session (a caller may keep iterating across sessions of the same api object).

### `fake` backend support

`FakeReachyMini` ([robot.md](robot.md)) reports a face target through its daemon-client stand-in — `client.face_target`, the REST payload's dict — driven by `show_face(x=0.0, y=0.0, roll=None)` / `hide_face()`, replacing the boolean `face_detected`. `start_head_tracking` / `stop_head_tracking` stay recorded commands, so a test asserts the `daemon` source arms and disarms the daemon at `DAEMON_DETECT_WEIGHT`. `media.get_frame()` (the gradient frame) feeds a stub detector for the `custom` source. The detection loop and the debounce run unchanged on the fake, at real time, so `tests/` observe: a report that flips on `show_face` / `hide_face` with the debounce holding a sub-threshold gap; a `changes()` subscriber woken on count changes only, and ending cleanly when cancelled mid-wait; the `daemon` source's arming commands. What the head does with the report is [head_tracking.md](head_tracking.md)'s to test.

## Relationship to the other specs

- **[observable.md](observable.md):** `api.faces` is an `Observable[FaceReport]`; the detection loop is its producer and defines what it publishes.
- **[head_tracking.md](head_tracking.md):** the tracker consumes every poll's report and steers the head; tracking implies detection.
- **[api.md](api.md):** `faces`, `set_face_detection` / `face_detection`, `set_face_detector` / `face_detector`; the attention loop that used to poll the daemon's face target is gone.
- **[config.md](config.md):** the `faces` block (`detector`, `detection`, the Python-only `face_detector`); `motion.tracking` implies detection.
- **[robot.md](robot.md):** the consumed slice trades `get_tracked_face` for the REST read, keeps `start_head_tracking` / `stop_head_tracking` for the `daemon` source, and adds `media.get_frame` (already consumed by `get_camera_frame`); the fake's face target.
- **[sim_daemon.md](sim_daemon.md):** the stepping correction stays — the `daemon` source depends on it.
- **[sim_scene.md](sim_scene.md) / [testing.md](testing.md):** a show / hide of the portrait on the viewer sim asserts *appeared* / *left* on `faces.changes()`.
- **[tools.md](tools.md):** nothing yet — an agent cannot subscribe; a snapshot or wait-for-a-face tool is deferred with the rest of that layer.

## Open questions

1. **A shipped `local` mode.** Running upstream's YuNet detector in the bridge process would need no daemon-side tracking at all and no new dependency, but puts vision code in this repo; deferred, the developer's wrapper being the path meanwhile.
2. **A bearing in the report.** Yaw / pitch of the face in degrees, added to `Face` once the tracker's camera model ([head_tracking.md](head_tracking.md)) is shared with the report; deferred until a caller wants angles rather than image coordinates.
3. **The daemon's ε-weight lean.** An upstream "detect without steering" mode would remove `DAEMON_DETECT_WEIGHT`; to be filed with the draft in [../docs/upstream-head-tracking-after-face-loss.md](../docs/upstream-head-tracking-after-face-loss.md).
4. **Debounce numbers.** `FACE_ABSENT_S` is a starting value, to be tuned on the viewer sim against the flicker rate of upstream's detector. The poll rate is settled by measurement: on the viewer sim (rendered camera and webcam, weight 1.0 and 0.001 alike) the daemon's detector produced 10.0–10.2 observations/s — the `IPC_FPS` cap, not the detector's speed — and a poll took 0.4 ms (p99 1.1 ms) on loopback, hence 30 Hz. Still to measure: the wireless robot's detector rate on its Raspberry Pi, and the cost of 30 polls/s over Wi-Fi.
5. **Other cues of a user.** The direction a voice comes from (the daemon's DoA snapshot, [api.md](api.md) deferred perception) and a recognised identity would enrich the report — a `Face` gaining a `voice` or `name` field, or the report gaining `voices` beside `faces`. Deferred until a caller needs more than faces; the shape above is designed to take them additively.
6. **The frame's capture time.** `media.get_frame()` returns the image alone. The capture time is the GStreamer buffer's timestamp in upstream's media pipeline (the camera's on a robot, the render's in the sim), mapped to the monotonic clock — to be checked: whether upstream's reader keeps it, and whether it survives the daemon's IPC feed, or needs an upstream accessor (`get_frame_with_timestamp`). It is what makes the `custom` source's aim exact; to be settled within the custom-detectors plan.
