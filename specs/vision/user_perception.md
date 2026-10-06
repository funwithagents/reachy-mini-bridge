---
code:
  - src/reachy_mini_bridge/face_detection.py
  - src/reachy_mini_bridge/yunet.py
  - src/reachy_mini_bridge/bridge.py
  - src/reachy_mini_bridge/config.py
tests:
  - tests/test_face_detection.py
  - tests/test_yunet.py
  - tests/test_bridge.py
  - tests-e2e/test_perception.py
  - tests-e2e/test_custom_faces.py
---

# User perception — detecting the people in front of the robot (`face_detection.py`, `yunet.py`)

**Status:** Implemented

## Purpose

How the bridge perceives the **users** — the people in front of the robot: a **detection loop** that runs a face detector over the camera feed's frames and publishes the faces as an observable value ([observable.md](../core/observable.md)) — a caller reads the current report directly and subscribes to be woken when the number of people changes. The bridge's own head tracker ([head_tracking.md](../motion/head_tracking.md)) is the report's first consumer inside the bridge; a caller's code is the other.

Faces are today's means of perceiving a user, and the report is shaped around them; the concept is the perception itself, so later cues (a voice's direction of arrival, a person recognised) land here beside the faces rather than in a spec of their own.

Upstream detects inside the daemon, as one pipeline with its own head tracking that the client can only switch on and off ([../docs/reachy-mini-api.md](../../docs/reachy-mini-api.md) "Face tracking"): the client sees the selected face and nothing else. The bridge detects **on the host**, over its camera feed, with a **detector** it is configured with: the shipped one — upstream's own YuNet model, run by the bridge as a bridge `FaceDetector` (`yunet.py`) — or a developer's. Every detector sees every face, in a frame the feed has stamped, the bridge follows each face from frame to frame under a `track_id`, and its head tracker chooses whom to follow ([head_tracking.md](../motion/head_tracking.md) "Whom the head follows"). The daemon's tracking is left untouched — never armed, never stopped — so a caller who wants the daemon's own behaviour still reaches it through the robot escape hatch ([robot.md](../core/robot.md)).

**Detection is opt-in.** A config names the detector (`face_detection.detector`); with none named, nothing is detected and nothing tracks — the default `ReachyMiniConfig()` runs no detector, downloads no model and never touches the camera for faces.

## Core concepts / Decided

### The pipeline

```
   camera feed ──► detector ──► pixel faces ──► track ──► Face report ──► Observable[FaceReport]  (bridge.faces)
   (bridge.camera)  (yunet: upstream's model                                        │
                    custom: the developer's)                                        └──► head tracker ──► aim ──► motion loop's gaze layer
```

| Stage | Owner | What it does |
|---|---|---|
| camera feed | [camera.md](camera.md) | the one reader of the robot's camera; publishes the newest frame with its `frame_id`, its time and — when it can stand behind it — the head pose at that time |
| detector | `yunet.py` (shipped) or the developer's code | `detect(frame_bgr, ts)` → the faces in the frame, in pixels (`PixelFace`: bbox, nose, eyes, orientation). Run by the loop once per new frame — at most `target_fps` times a second — on a worker thread |
| track | `face_detection.py` | every face gets a **`track_id`**, carried from frame to frame by nearest matching and kept through a short run of misses — a few lines of geometry ("Tracks" below). No face is singled out: whom the head follows is the head tracker's choice. Centres are **not** smoothed: pixels from different frames were taken from different head poses, and the head tracker smooths the aim in the world frame instead |
| face report | `face_detection.py` | the faces normalised into the tracker's coordinates with their pixel boxes and track ids, in `track_id` order, with the frame itself, its `ts` and `head_pose`: the current `FaceReport`, published on `bridge.faces` |
| head tracker | [head_tracking.md](../motion/head_tracking.md) | the face it chooses to follow ([head_tracking.md](../motion/head_tracking.md) "Whom the head follows") → a look-at **aim** (a head pose), handed to the motion loop — fed every observation, not only the published changes; publishes which face it follows on `bridge.head_tracking` |
| gaze layer | [motion.md](../motion/motion.md) "The gaze layer" | the aim composed into the idle move's pose by the tracking weight |

### The face report

```python
@dataclass(frozen=True)
class Face:
    x: float             # normalised image coordinates of the face (its nose when known,
    y: float             # else the bbox centre) in [-1, 1]: x right, y down, (0, 0) the centre
    roll: float | None   # head roll in radians: the detector's fitted orientation when it gives one,
                         # else from the eye line; None when it gives neither
    size: float          # bbox height as a fraction of the frame height
    track_id: int = 0    # the face's track: the same positive integer for the same person from
                         # frame to frame, never reused (0 only on a Face built outside the loop)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # x, y, width, height in pixels
                         # of the report's frame
    pitch: float | None = None  # head pitch / yaw in radians, from a detector that fits a head
    yaw: float | None = None    # (PixelFace.orientation); None otherwise

@dataclass(frozen=True)
class FaceReport:
    faces: tuple[Face, ...]  # every face the detector reports, by track_id (oldest first)
    ts: float                # the frame's time (the bridge's monotonic clock, camera.md)
    source: str | None       # the detector's name: "yunet" | "custom"; None when the config names none
    active: bool             # a detector is running. False means "unknown", not "nobody"
    head_pose: npt.NDArray[np.float64] | None = None  # the pose the frame was captured from, when known
    frame: CameraFrame | None = None  # the camera frame the faces were found in; None while inactive

    @property
    def frame_id(self) -> int: ...        # frame.frame_id; 0 without a frame
```

`head_pose` and `frame` are left out of equality (`compare=False`): two reports are equal on what was seen, not on the arrays they reference.

- **Coordinates are the tracker's**: the normalised image position upstream's tracker works in, resolution-independent, so a report reads the same from every detector. A bearing in degrees (yaw / pitch of the ray, [bridge.md](../core/bridge.md)'s human units) is a later **additive** field: it needs the intrinsics the tracker holds, and nothing above changes when it lands.
- **A list, in a stable order.** Every face the detector sees, by `track_id`, oldest track first — so a face keeps its place while the people in view stay the same. The count is the number of people in view, and a count change is *someone appeared* / *someone left*.
- **`track_id`** names a person across frames: the loop carries each face's track to the next frame by nearest matching and keeps a track through a short run of misses ("Tracks" below), so a face the detector drops for a frame or two comes back under the same id. Ids count up from 1 and are never reused within a bridge object — the counter lives across loop restarts and sessions, as the camera feed's `frame_id` does ([camera.md](camera.md)). A client joins its own per-person results (a recognised identity, an expression) to the bridge's faces by `track_id`.
- **The report is about detection.** It singles no face out: whom the head follows — and how long it waits for a face that disappeared before turning to another — is the head tracker's choice, published with its `track_id` on `bridge.head_tracking` ([head_tracking.md](../motion/head_tracking.md) "Whom the head follows", "The head tracking report").
- **`bbox`** is the face's box in pixels of `frame.image` — the full camera frame, whatever width the detector worked at (a shipped detector scales its boxes back). A client crops a face from the frame with it (clamping to the image, copying before it writes).
- **`frame`** is the `CameraFrame` the faces were found in — a reference, not a copy: the feed's frames are shared read-only ([camera.md](camera.md) "The frame"), so it costs one frame kept alive until the next observation. It is what a client crops from, taken with the faces, where `bridge.camera.latest()` has usually moved on. `frame_id` delegates to it, so the report has the shape of a `vision-modules` result item (`frame_id`, `ts`): a graph node samples the bridge's faces through a `latest()` returning `bridge.faces.value`, stale-skipped on `frame_id` like any other upstream.
- **Orientation.** `roll`, `pitch` and `yaw` are the head's angles as seen from the camera, in radians: `roll` positive when the face's eye line turns clockwise in the image (the sign `atan2` gives on the eye line with y down, so both sources agree), `pitch` positive when the face tilts down, `yaw` positive when the face turns toward the image's right. A detector that fits a head (`PixelFace.orientation`) gives all three and its roll is preferred; one that gives eyes only gives the eye-line roll and leaves `pitch` / `yaw` `None`.
- **Identity is the client's.** A client that recognises people keeps its own map from `track_id` to who they are and enriches its own copy of the face list; the bridge holds no identity, no gallery, no model for it. The report carries what that enrichment needs — the frame, the boxes, the ids — and the head tracking report's `track_id` links a recognised person to the one the head follows.
- **`head_pose`** is the head pose the frame was captured from, copied from the camera feed's frame ([camera.md](camera.md) "The frame's time and the head pose"), so the head tracker aims a report against the pose its frame was taken from, exactly. A frame the feed could not stamp leaves it `None`, and the tracker estimates the delay instead ([head_tracking.md](../motion/head_tracking.md) "The aim") — today's case on the live backends, whose frames carry no capture time (camera.md open question 1); the fake stamps every frame.
- **`active` is tri-state by construction.** `faces=()` with `active=True` is *nobody there*; `active=False` is *no detector is looking* (detection off, or no camera frame reaching the detector) — a caller must not read it as an empty room. An inactive report always carries `faces=()`, so a caller waiting on `r.faces` never gets a stale face from before the detector stopped looking. Before the session is entered, after it exits, and while the detector is down ("The detection loop"), the value is `FaceReport((), ts=0.0, source=<config's detector>, active=False)` — no frame.

### The report is an observable

`bridge.faces` is an `Observable[FaceReport]` ([observable.md](../core/observable.md)): `value` is the current report, `changes()` wakes a subscriber on every *published* value, `wait_for(predicate)` waits for one that matches. The detection loop is its producer and decides what counts as a change: it `update`s the report on every observation (fresh coordinates for anyone reading `value`) and `set`s it only when the face **count** changes (debounced, below) or `active` flips — so `async for report in bridge.faces.changes()` wakes on *someone appeared*, *someone left*, *detection started / stopped*, never on a face moving. *The head now follows someone else* is the head tracker's event, on `bridge.head_tracking`.

### The detection loop

One asyncio task owned by the bridge, running while **anyone needs faces**: the caller's detection switch is on, or head tracking is on (the tracker is a client of the loop like any subscriber). It samples the camera feed at `FACE_POLL_HZ = 30` — three polls per frame of a local daemon's feed, which upstream caps at 10 fps (`media_server.IPC_FPS`, [../docs/reachy-mini-api.md](../../docs/reachy-mini-api.md) "Face tracking"), so a new frame is picked up within a third of a frame period rather than up to a whole one late. A poll that finds no frame yet, or a `frame_id` it has already processed, is skipped, so the detector runs **once per new frame** whatever the poll rate; a new frame runs the detector off the event loop (`asyncio.to_thread`), the bridge normalises the pixel faces it gets back against the frame's size, carries the tracks, gives the report the frame, its `ts` and `head_pose`, `update`s `bridge.faces`, feeds the tracker, and publishes changes through a **debounce**:

- a **rise** in the count is published on the first report that shows it;
- a **drop** is published once the lower count has held for `FACE_ABSENT_S = 0.3` s — a detector misses a face on a single frame now and then, and the debounce is what keeps *left* from firing on a blink of the detector. (The tracker's loss timeout is a longer, separate window: one serves the event, the other the head.)
- `active` flips are published at once: when the loop starts and its detector is built, when it stops, and when the detector cannot look (below).

**Every poll reaches the observer.** The loop's observer — the bridge, feeding the head tracker — hears every poll: `on_observation(report)` for an observation, `on_observation(None)` for a poll that produced none (no new frame yet, a failed `detect`), so the tracker keeps time while the camera is silent ([head_tracking.md](../motion/head_tracking.md) "Easing, loss, focus").

**A ceiling on the rate: `target_fps`.** The config's `face_detection.target_fps` (default `null`) caps how many frames a second the detector runs on: a new frame arriving less than `1 / target_fps` after the previous detection started is skipped (marked seen, not processed), so the detector runs on the newest frame once the period has passed. `null` runs it once per new frame. It is a ceiling, not a rate the loop produces: the detector never runs faster than the feed delivers frames (10 fps on a local daemon, a nominal 30 fps on the wireless robot's WebRTC stream) or than it can process them. The loop enforces it, so it applies to every detector, shipped or custom. It is the wireless robot's lever: at 30 fps, detecting on every frame triples the cost for aims the gaze layer eases anyway.

### Tracks

The loop keeps a short list of **tracks**, each a `track_id`, the pixel centre and the size (the larger side of its box) of its face on its last observation, and a miss count. On every observation:

1. **Match.** Every pair of (track, face) whose centres lie within `TRACK_MAX_JUMP_FACES` face sizes of each other — a size is a box's larger side in pixels, and the smaller of the track's last size and the face's counts, so a small face near where a large one was is held to its own size — is a candidate match; pairs are taken nearest first, each track and each face used at most once. A matched face takes its track's id and the track its face's centre and size, its misses reset.
2. **New tracks.** Every unmatched face opens a track with the next id.
3. **Misses.** Every unmatched track counts a miss and is dropped after `TRACK_MAX_MISSES` consecutive misses.

This is upstream's association rule — keep the nearest, hold through misses — applied to every face (`TRACK_MAX_JUMP_FACES = 1.5`, `TRACK_MAX_MISSES = 20` observations, upstream's miss window), and the only frame-to-frame tracker the desk scene needs: a few people, at desk range, moving less than one and a half of their own face between two observations. The gate is in the face's own size rather than a fraction of the frame, so it does not depend on the camera's resolution or field of view and is tight for a near, large face and lenient for a far, small one: upstream's quarter-of-the-frame gate let a track whose face had gone continue onto a neighbour — two portraits at desk range sit 2.4 face sizes apart, 0.51 of that frame-relative gate — which the head, following by `track_id`, would then swing onto without a published change. The gates are counted in observations, so at a lowered `target_fps` the miss window lasts proportionally longer in seconds. The tracks are per detection run (a restart starts with none), the id counter is not.

**Building the detector.** The loop builds its detector from the configured factory when it starts — the shipped detector's own (`yunet`, handed the config's `face_detection.width`), or the registered custom one — on a worker thread, because a build may load a model (the shipped detector downloads its weights on first use and opens an inference session). A build that raises fails the start: at session entry, bring-up fails with the cause (a `BridgeError` chaining it, the session unwound as for any other step); from `start_head_tracking` / `set_face_detection(True)` mid-session, the verb raises it and the switches are left as they were. A build that returns something without a callable `detect` fails the start the same way, with a `ValueError` naming the type (the object released through its `close()` if it has one). The factory is called nowhere else — not at registration — so nothing is built while nobody needs faces, and a model loads once per detection start.

**Releasing the detector.** A detector with a callable `close()` has it called, on a worker thread, whenever the loop lets go of it — when the loop stops, and when a new custom factory replaces it — so a detector holding a native session (MediaPipe's landmarker) releases it. `close()` is optional; a detector without one is simply dropped. A `close()` that raises is logged at `DEBUG` and ignored.

A frame the detector raises on is logged at `DEBUG` and skipped, never fatal (the frame is marked processed, so it is not retried); a detector that produces no observation for `FACE_SOURCE_DOWN_S = 5` s — or for two periods of `target_fps`, when that is longer — (no new frame arriving: the headless sim's absent camera, a daemon with no camera; or raising on every frame) publishes the inactive report (`active=False`, no faces) with one `WARNING`, and an active one again on the next good observation. The timing values are module constants of `face_detection.py`, not config (a knob would land in the `face_detection` block beside `width` and `target_fps`).

**The detector's cost, logged.** `FACE_COST_LOG_S = 10` s after the first observation of a detection run, the loop logs one `INFO` line: the detector's name, the configured `width` and `target_fps`, the mean time of its `detect` calls and the observations per second it achieved over that window. These are the numbers a client tunes `width` and `target_fps` against — on a laptop, on the robot's own compute — and the line is written once per run.

### Detectors

| `face_detection.detector` | Detector | Needs | Notes |
|---|---|---|---|
| `null` *(default)* | none | — | No detection, no tracking: `face_detection.enabled: true` or `motion.tracking: true` with no detector is a `ConfigError` ([config.md](../core/config.md)); `set_face_detection(True)` / `start_head_tracking()` raise `ValueError`. `bridge.faces` stays at its inactive value |
| `yunet` | the bridge's `YuNetDetector` (`yunet.py`): upstream's `reachy_mini.vision.face_detector.FaceDetector` — YuNet on ONNX Runtime, the model the daemon itself runs — wrapped as a bridge `FaceDetector` | a camera; the model's weights (a Hugging Face download on first use, cached after) | Nothing to install: `onnxruntime` and the Hugging Face hub are base dependencies of `reachy_mini`, and OpenCV is not needed. Five landmarks: roll from the eye line. The floor on the robot's own compute. Details below |
| `custom` | the developer's `FaceDetector`, registered as a factory | a camera; `FaceDetectionSettings.face_detector` or `set_face_detector(factory)` | The contract below. With nothing registered, session entry raises `ValueError` (as a bad `idle_move` does) |

### The shipped detector — `yunet.py`

`YuNetDetector` is upstream's face detector as a bridge `FaceDetector`, and the reference implementation of the contract below:

- **Construction** imports `reachy_mini.vision.face_detector` and builds its `FaceDetector` with upstream's default thresholds — the import is inside the constructor (the two modules import each other; the runtime itself is already loaded with `reachy_mini`, whose package imports its vision module). The constructor is where the weights are fetched (`hf_hub_download`, pinned to the revision upstream pins) and the session opened — which is why the loop builds detectors on a worker thread, and why a config without the detector never pays for it.
- **Detection width.** `YuNetDetector(width=...)` takes the config's `face_detection.width`, default `DETECT_WIDTH = 320` — upstream's own tracker detects at 320. `detect` subsamples the frame by the integer stride `max(1, frame_width // width)` to about that width and scales every bbox, nose and eye it gets back by the stride, so at 320 a 1280×720 stream (the sim, the wireless robot) is detected at 320×180 and the Lite's 1920×1080 at 320×180 too, and the detector keeps up with the feed on one CPU thread; `null` detects on the full frame (stride 1). The subsample is a strided view made contiguous, no OpenCV. The width is the trade between cost and precision: two eyes about fifteen pixels apart at 320 make one pixel of landmark error several degrees of roll, and 640 quadruples the cost and halves that error.
- **Output.** One `PixelFace` per upstream `Face`: its bbox, its nose, its eyes (right, left) — so every report from it carries a roll and a size.
- The module is one file the project map lists ([../AGENTS.md](../../AGENTS.md)).

**One shipped detector, others plugged in.** The bridge ships YuNet alone and adds no vision dependency for detection, to stay lightweight: other detectors — a vision library's (`funwithagents/vision-modules`' MediaPipe landmarker, say), a developer's own model — are registered through `custom` and bring their dependencies with them.

### Custom detectors — the `FaceDetector` protocol

```python
@dataclass(frozen=True)
class PixelFace:
    bbox: tuple[float, float, float, float]        # x, y, width, height in pixels of the frame given
    nose: tuple[float, float] | None = None        # the point the head aims at; the bbox centre when None
    eyes: tuple[tuple[float, float], tuple[float, float]] | None = None  # right, left: gives roll
    orientation: tuple[float, float, float] | None = None  # roll, pitch, yaw in radians, from a
                                                           # detector that fits a head

class FaceDetector(Protocol):
    def detect(self, frame_bgr: npt.NDArray[np.uint8], ts: float) -> Sequence[PixelFace]: ...
    # optional: def close(self) -> None — called when the loop lets go of the detector

FaceDetectorFactory = Callable[[], FaceDetector]
```

- **A factory, called once per detection start**, as the idle move's is called per idle entry ([motion.md](../motion/motion.md) "Custom idle moves"): the registered value is a zero-argument callable returning a fresh detector, so a detector holds per-run state (a model session) and a restart gets a clean one. Registered through `FaceDetectionSettings.face_detector` (a Python-only config field) or `set_face_detector(factory)`; `None` clears it.
- **`orientation`** follows the report's convention ("The face report": `roll` the eye line's sign, `pitch` positive down, `yaw` positive toward the image's right); given, it is preferred over the eye-line roll and fills the report's `pitch` / `yaw`. **`close()`** is optional, called on a worker thread when the loop stops or replaces the detector ("The detection loop").
- **The two cost knobs.** `face_detection.target_fps` applies to a custom detector as to a shipped one — the loop skips the frames. `face_detection.width` is the shipped detector's knob: it is handed to their constructors, and a custom detector's working width is its author's (the factory takes no argument; a detector that subsamples scales its faces back to the frame it was given, as `YuNetDetector` does).
- **Checked at registration**: the registered value must be callable — `ValueError` otherwise, the registered factory left as it was — and nothing is constructed until the loop starts ("Building the detector": a factory that raises, or returns something without a callable `detect`, fails that start). With `face_detection.detector` `"custom"` and nothing registered, session entry raises `ValueError`; registering while the loop runs in `custom` mode restarts the detector with the new factory; **clearing it** (`set_face_detector(None)`) while the loop runs in `custom` mode — detection or tracking on — raises `ValueError` and changes nothing: stop both first.
- **What the author guarantees.** `detect` runs on a worker thread once per **new** camera frame — polling faster than the feed runs the detector at the feed's rate (10 fps on a local daemon). It treats the frame as read-only (the array is the feed's, shared with every other consumer; a detector that draws on it copies first). It returns within a frame period or the next frame is skipped (frames are dropped, never queued); it may return faces in any order; it never touches the robot or the bridge. Its dependencies are its own — the bridge adds none.
- **The frame's time and the head pose it was taken from.** Both come with the frame: the camera feed stamps every frame it publishes with `ts` and, when it can stand behind it, the head pose at that time ([camera.md](camera.md) "The frame's time and the head pose" — a pose is attached only to a capture time, never to an arrival time). The detection loop hands the detector `frame.image` with `frame.ts` and copies `frame.head_pose` onto the report — image, time and pose taken together. This is also why a detector returns faces unsmoothed: smoothing across frames mixes pixels taken from different head poses, and the tracker's easing smooths the aim in the world frame instead.
- **A detector that raises while running** is one skipped frame and a `DEBUG` line; raising on every frame trips the detector-down rule above.
- **The shipped detector is the worked example.** `YuNetDetector` is a custom detector in every respect but its name in the config: the documentation shows it as the wrapper a developer writes around a model of their own, and the live tier registers it through the `custom` path to test that path ([testing.md](../testing/testing.md)).
- **The bridge drives the detector; a graph reads the report.** The protocol is the shape a latest-value vision library's detector has too — `detect(image_bgr, ts)` returning what was found in pixels — so a detector object from such a library is registered with the bridge through `custom` and the bridge's loop runs it, once per frame, where the head follows its faces. A graph the developer runs over the same camera feed beside other perception (hands, gestures, a recogniser) takes the faces from `bridge.faces`: each report carries its frame, the boxes and the track ids ("The face report"), so a graph node crops, analyses and joins its results to the bridge's faces by `track_id`, and `bridge.head_tracking` names the track the head follows. The feed is what makes the two coexist ([camera.md](camera.md) "A valid upstream for a vision graph").

### Configuration

Five fields, in two blocks ([config.md](../core/config.md)):

```json
"face_detection": { "detector": "yunet", "enabled": true, "width": 320, "target_fps": null },
"motion":         { "tracking": true }
```

The block is named for the module and the verb (`face_detection.py`, `set_face_detection`); `bridge.faces` is its output.

- `face_detection.detector` ∈ `null` / `yunet` / `custom`, default `null` — the detector; `custom` needs the Python-only `FaceDetectionSettings.face_detector` factory.
- `face_detection.enabled` (default `false`) — whether the detection loop runs from session entry for the caller's sake (the face report). It needs **no motors**: nothing moves. Its verb is `set_face_detection(enabled)`, its property `face_detection`.
- `face_detection.width` (default `320`, `null` for the full frame) — the width the shipped detector works at: its cost per frame against its precision ("The shipped detector — `yunet.py`"). A custom detector's width is its author's.
- `face_detection.target_fps` (default `null`, once per new frame) — the ceiling on detections per second, enforced by the loop for every detector ("The detection loop"); the same name as a vision-modules stage's knob for the same thing.
- `motion.tracking` (default `false`) — whether the head tracker runs from session entry ([head_tracking.md](../motion/head_tracking.md)). It **implies detection**: the loop runs while either switch is on, so `enabled: false` with `tracking: true` is valid and means faces are detected for the head only (the report is still published — the loop has it anyway). `set_face_detection(False)` while tracking is on leaves the loop running; `faces.value.active` says what is actually happening.
- Either switch on with `detector` `null` is a `ConfigError` at `from_dict`; on a `ReachyMiniConfig` built directly in code the same contradiction is a `ValueError` at session entry.
- `width` and `target_fps` are read when the loop starts; they have no verb. Where the bridge runs decides what they cost: on a laptop talking to the wireless robot, the WebRTC decode plus the detector on the host; on the robot, its own CPU, where `yunet` at 320 — what the robot's daemon runs — is the floor.

The example config, the README and the control panel name `yunet` with both switches on: the robot that follows the person in front of it is one line of config away, not the default of a bare object.

### Lifecycle

`ReachyMiniBridge.start()` ([bridge.md](../core/bridge.md) "Lifecycle") starts the detection loop — a `FaceDetection` with the `async start()` / `async stop()` pair every session of the bridge has — after the media session, the camera feed and the wobbling setup and before the motion session, since the tracker hands its aim to the loop; it stops it right after the motion session on exit. Bring-up cancel / failure unwinds it like every other step. The report resets to its inactive value at exit, published through `set`, so a subscriber of `changes()` learns that detection stopped; the observable itself outlives the session (a caller may keep iterating across sessions of the same bridge object).

### `fake` backend support

The fake has no detector of its own and needs none: the default config runs no loop on it, and a test that needs faces registers a **stub detector** through the `custom` path — a `FaceDetector` returning the `PixelFace`s of a scriptable scene on the fake's synthetic frames ([camera.md](camera.md) "`fake` backend support": the gradient frame, paced at the fake's frame rate, each frame stamped with its capture time and the fake's head pose). The detection loop and the debounce run unchanged on the fake, at real time, so `tests/` observe: a report that flips when the scene shows / hides a face, with the debounce holding a sub-threshold gap; a `changes()` subscriber woken on count changes only, and ending cleanly when cancelled mid-wait; the default config's `faces` staying inactive with no detector, and the switches refusing without one; a detector whose factory raises failing bring-up. With the stub scripting several faces across frames, they also observe the tracks: a face keeping its `track_id` while it moves and through a gap shorter than the miss window, a new id after a longer one, two faces crossing without trading ids, the faces in id order; the report's `frame` being the frame the stub was handed and its boxes in that frame's pixels; `target_fps` capping the stub's calls per second below the fake's frame rate; `close()` called at stop and on a factory swap; a stub's `orientation` preferred over its eyes' roll. The shipped wrappers are tested on stubs of their upstreams (no model): the width's stride and the scaling back, the conversion to `PixelFace`. Every report on the fake carries the frame's `head_pose`, so what the head does with it — [head_tracking.md](../motion/head_tracking.md)'s to test — runs through the tracker's exact-pose path.

## Relationship to the other specs

- **[camera.md](camera.md):** the detection loop samples the camera feed — the one reader of the robot's camera — and puts each frame, with its `ts` and `head_pose`, on its report; a vision graph over the same feed reads the report beside its other perception.
- **[observable.md](../core/observable.md):** `bridge.faces` is an `Observable[FaceReport]`; the detection loop is its producer and defines what it publishes.
- **[head_tracking.md](../motion/head_tracking.md):** the tracker consumes every observation's report, steers the head toward the face it chose — the biggest eligible one when it follows none, held by `track_id` while it is seen — and publishes which face it follows on `bridge.head_tracking`; tracking implies detection, and needs a detector.
- **[bridge.md](../core/bridge.md):** `faces`, `set_face_detection` / `face_detection`, `set_face_detector` / `face_detector`; the switches' behaviour without a detector.
- **[config.md](../core/config.md):** the `face_detection` block (`detector`, `enabled`, `width`, `target_fps`, the Python-only `face_detector`); `motion.tracking` implies detection; the cross-block rule.
- **vision-modules:** a face detector from it (its MediaPipe landmarker) runs in the bridge as a `custom` detector; a recogniser or other face module a client runs over `bridge.faces`.
- **[robot.md](../core/robot.md):** the consumed slice reaches the camera through the feed alone (`media.get_frame`) and the daemon's tracking not at all.
- **[sim_daemon.md](../daemon/sim_daemon.md):** the sim's faces are detected by the bridge from the camera stream the launcher feeds — the rendered eye camera under the viewer or, on Linux, headless through the launcher's offscreen render, or a host webcam headless or not; a macOS headless sim without a webcam has no camera and its detector reads inactive.
- **[sim_scene.md](../testing/sim_scene.md) / [testing.md](../testing/testing.md) / [testing_support.md](../testing/testing_support.md):** a show / hide of the portrait on the viewer sim asserts *appeared* / *left* on `faces.changes()`; the live harness configures `yunet`.

## Open questions

1. **A daemon-side detector for the wireless robot.** On a wireless robot the host path decodes the camera's WebRTC stream and runs the detector on the host; the daemon's own detector on the robot would spare both, read over `GET /api/media/tracking/face` ([../docs/reachy-mini-api.md](../../docs/reachy-mini-api.md) "Face tracking"). It returns as a `daemon` value of `face_detection.detector` only if a measurement on a wireless robot shows the stream or the host CPU to be a problem — and it needs upstream to run its detector without steering the head (today the detector runs only while tracking is armed at a weight above zero, and the daemon blends its own aim in by that weight), and the MuJoCo daemon to step its tracking (upstream's sim loop never does), before it is worth a second source with its own report semantics (one face, no size, smoothed in the image, on the daemon's clock).
2. **A bearing in the report.** Yaw / pitch of the face in degrees, added to `Face` once the tracker's camera model ([head_tracking.md](../motion/head_tracking.md)) is shared with the report; deferred until a caller wants angles rather than image coordinates.
3. **Debounce numbers.** `FACE_ABSENT_S` is a starting value, to be tuned on the viewer sim against the flicker rate of the detector. The poll rate follows the local feed's 10 fps cap — the shipped detector reports 10.0 observations/s on the viewer sim's rendered camera (2026-09-29), the feed's rate; still to measure: the wireless robot's WebRTC frame rate reaching the host (30 fps nominal) and the detector's cost there — the cost line the loop logs is the measurement, and `width` / `target_fps` the answer's knobs.
4. **Other cues of a user.** The direction a voice comes from (the daemon's DoA snapshot, [bridge.md](../core/bridge.md) deferred perception) would enrich the report — a `Face` gaining a `voice` field, or the report gaining `voices` beside `faces`. Deferred until a caller needs more than faces; the shape above is designed to take it additively. A recognised identity is the client's by design ("The face report"): the report carries the frame, the boxes and the track ids a client enriches.
5. **The frame's capture time** is the camera feed's question ([camera.md](camera.md) "The frame's time and the head pose", open question 1): today only the `fake` stamps a capture time, so a report carries a pose there and none on the sim or a robot, where the tracker estimates the delay. Nothing in this spec changes with the answer.
6. **A face stand-in for downstream tests.** The stub detector the bridge's own tests register on the fake could ship in `reachy_mini_bridge.testing`, so a consumer testing on the fake shows and hides a face in one line ([testing_support.md](../testing/testing_support.md)); deferred until a consumer asks.
7. **A preferred track.** A client that knows who is who (a recogniser keyed by `track_id`) will want to say whom the head follows: a `track_id` argument on the tracking verbs, preferred by the tracker's choice while its track lives, the automatic rule taking over after. What a lost preferred track does — hold the head on its last aim, or hand over to the automatic rule — is the policy to settle with it. Deferred to its own pass, after the report carries the ids.
8. **A graph driving the detector.** A client running a full vision-modules face stage over the camera feed, with the bridge's head following that stage's faces, would need a second kind of source beside the factory: a sampled `latest()` upstream whose items carry `frame_id`, `ts` and pixel faces, stale-skipped on `frame_id`, the loop keeping the last few frames' poses by `frame_id`. Deferred until such a client exists; enrichment over `bridge.faces` covers the graphs in view.
