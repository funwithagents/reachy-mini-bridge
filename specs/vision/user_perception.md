---
code:
  - src/reachy_mini_bridge/face_detection.py
  - src/reachy_mini_bridge/yunet.py
  - src/reachy_mini_bridge/api.py
  - src/reachy_mini_bridge/config.py
tests:
  - tests/test_face_detection.py
  - tests/test_yunet.py
  - tests/test_api.py
  - tests-e2e/test_api.py
  - tests-e2e/test_custom_faces.py
---

# User perception — detecting the people in front of the robot (`face_detection.py`, `yunet.py`)

**Status:** Implemented

## Purpose

How the bridge perceives the **users** — the people in front of the robot: a **detection loop** that runs a face detector over the camera feed's frames and publishes the faces as an observable value ([observable.md](../core/observable.md)) — a caller reads the current report directly and subscribes to be woken when the number of people changes. The bridge's own head tracker ([head_tracking.md](../motion/head_tracking.md)) is the report's first consumer inside the bridge; a caller's code is the other.

Faces are today's means of perceiving a user, and the report is shaped around them; the concept is the perception itself, so later cues (a voice's direction of arrival, a person recognised) land here beside the faces rather than in a spec of their own.

Upstream detects inside the daemon, as one pipeline with its own head tracking that the client can only switch on and off ([../docs/reachy-mini-api.md](../../docs/reachy-mini-api.md) "Face tracking"): the client sees the selected face and nothing else. The bridge detects **on the host**, over its camera feed, with a **detector** it is configured with: the shipped one — upstream's own YuNet model, run by the bridge as a bridge `FaceDetector` (`yunet.py`) — or a developer's. Every detector sees every face, in a frame the feed has stamped, and the bridge selects the target itself. The daemon's tracking is left untouched — never armed, never stopped — so a caller who wants the daemon's own behaviour still reaches it through the robot escape hatch ([robot.md](../core/robot.md)).

**Detection is opt-in.** A config names the detector (`faces.detector`); with none named, nothing is detected and nothing tracks — the default `ReachyMiniConfig()` runs no detector, downloads no model and never touches the camera for faces.

## Core concepts / Decided

### The pipeline

```
   camera feed ──► detector ──► pixel faces ──► select ──► Face report ──► Observable[FaceReport]  (api.faces)
   (api.camera)    (yunet: upstream's model                                │
                    custom: the developer's)                               └──► head tracker ──► aim ──► motion loop's gaze layer
```

| Stage | Owner | What it does |
|---|---|---|
| camera feed | [camera.md](camera.md) | the one reader of the robot's camera; publishes the newest frame with its `frame_id`, its time and — when it can stand behind it — the head pose at that time |
| detector | `yunet.py` (shipped) or the developer's code | `detect(frame_bgr, ts)` → the faces in the frame, in pixels (`PixelFace`: bbox, nose, eyes). Run by the loop once per new frame, on a worker thread |
| select | `face_detection.py` | one face is the **target**: acquire the largest above a minimum size, then the nearest to the previous target; drop the association after a run of misses — upstream's rule, re-implemented as a few lines of geometry. Its centre is **not** smoothed: pixels from different frames were taken from different head poses, and the head tracker smooths the aim in the world frame instead |
| face report | `face_detection.py` | the faces normalised into the tracker's coordinates, the target first, with the frame's `ts` and `head_pose`: the current `FaceReport`, published on `api.faces` |
| head tracker | [head_tracking.md](../motion/head_tracking.md) | the target face → a look-at **aim** (a head pose), handed to the motion loop — fed every observation, not only the published changes |
| gaze layer | [motion.md](../motion/motion.md) "The gaze layer" | the aim composed into the idle move's pose by the tracking weight |

### The face report

```python
@dataclass(frozen=True)
class Face:
    x: float             # normalised image coordinates of the face (its nose when known,
    y: float             # else the bbox centre) in [-1, 1]: x right, y down, (0, 0) the centre
    roll: float | None   # head roll in radians from the eye line; None when the detector gives no eyes
    size: float          # bbox height as a fraction of the frame height

@dataclass(frozen=True)
class FaceReport:
    faces: tuple[Face, ...]  # every face the detector reports; the target face first
    ts: float                # the frame's time (the bridge's monotonic clock, camera.md)
    source: str | None       # the detector's name: "yunet" | "custom"; None when the config names none
    active: bool             # a detector is running. False means "unknown", not "nobody"
    head_pose: npt.NDArray[np.float64] | None = None  # the pose the frame was captured from, when known
```

- **Coordinates are the tracker's**: the normalised image position upstream's tracker works in, resolution-independent, so a report reads the same from either detector. A bearing in degrees (yaw / pitch of the ray, [api.md](../core/api.md)'s human units) is a later **additive** field: it needs the intrinsics the tracker holds, and nothing above changes when it lands.
- **A list.** Every face the detector sees, the target face first; the count is the number of people in view, and a count change is *someone appeared* / *someone left*.
- **`head_pose`** is the head pose the frame was captured from, copied from the camera feed's frame ([camera.md](camera.md) "The frame's time and the head pose"), so the head tracker aims a report against the pose its frame was taken from, exactly. A frame the feed could not stamp leaves it `None`, and the tracker estimates the delay instead ([head_tracking.md](../motion/head_tracking.md) "The aim") — today's case on the live backends, whose frames carry no capture time (camera.md open question 1); the fake stamps every frame.
- **`active` is tri-state by construction.** `faces=()` with `active=True` is *nobody there*; `active=False` is *no detector is looking* (detection off, or no camera frame reaching the detector) — a caller must not read it as an empty room. Before the session is entered, and after it exits, the value is `FaceReport((), ts=0.0, source=<config's detector>, active=False)`.

### The report is an observable

`api.faces` is an `Observable[FaceReport]` ([observable.md](../core/observable.md)): `value` is the current report, `changes()` wakes a subscriber on every *published* value, `wait_for(predicate)` waits for one that matches. The detection loop is its producer and decides what counts as a change: it `update`s the report on every observation (fresh coordinates for anyone reading `value`) and `set`s it only when the face **count** changes (debounced, below) or `active` flips — so `async for report in api.faces.changes()` wakes on *someone appeared*, *someone left*, *detection started / stopped*, never on a face moving.

### The detection loop

One asyncio task owned by the api, running while **anyone needs faces**: the caller's detection switch is on, or head tracking is on (the tracker is a client of the loop like any subscriber). It samples the camera feed at `FACE_POLL_HZ = 30` — three polls per frame of a local daemon's feed, which upstream caps at 10 fps (`media_server.IPC_FPS`, [../docs/reachy-mini-api.md](../../docs/reachy-mini-api.md) "Face tracking"), so a new frame is picked up within a third of a frame period rather than up to a whole one late. A poll that finds no frame yet, or a `frame_id` it has already processed, is skipped, so the detector runs **once per new frame** whatever the poll rate; a new frame runs the detector off the event loop (`asyncio.to_thread`), the bridge normalises the pixel faces it gets back against the frame's size, selects the target, gives the report the frame's `ts` and `head_pose`, `update`s `api.faces`, feeds the tracker, and publishes count changes through a **debounce**:

- a **rise** in the count is published on the first report that shows it;
- a **drop** is published once the lower count has held for `FACE_ABSENT_S = 0.3` s — a detector misses a face on a single frame now and then, and the debounce is what keeps *left* from firing on a blink of the detector. (The tracker's loss timeout is a longer, separate window: one serves the event, the other the head.)
- `active` flips are published at once: when the loop starts and its detector is built, when it stops, and when the detector cannot look (below).

**Building the detector.** The loop builds its detector from the configured factory when it starts — `yunet`'s own, or the registered custom one — on a worker thread, because a build may load a model (the shipped detector downloads its weights into the Hugging Face cache on first use and opens an ONNX Runtime session). A build that raises fails the start: at session entry, bring-up fails with the cause (a `BridgeError` chaining it, the session unwound as for any other step); from `start_head_tracking` / `set_face_detection(True)` mid-session, the verb raises it and the switches are left as they were. Nothing is built while nobody needs faces.

A frame the detector raises on is logged at `DEBUG` and skipped, never fatal (the frame is marked processed, so it is not retried); a detector that produces no observation for `FACE_SOURCE_DOWN_S = 5` s — no new frame arriving (the headless sim's absent camera, a daemon with no camera) or raising on every frame — flips `active` to `False` with one `WARNING`, and back to `True` on the next good observation. The three values are module constants of `face_detection.py`, not config (a knob would land in the `faces` block).

### Detectors

| `faces.detector` | Detector | Needs | Notes |
|---|---|---|---|
| `null` *(default)* | none | — | No detection, no tracking: `faces.detection: true` or `motion.tracking: true` with no detector is a `ConfigError` ([config.md](../core/config.md)); `set_face_detection(True)` / `start_head_tracking()` raise `ValueError`. `api.faces` stays at its inactive value |
| `yunet` | the bridge's `YuNetDetector` (`yunet.py`): upstream's `reachy_mini.vision.face_detector.FaceDetector` — YuNet on ONNX Runtime, the model the daemon itself runs — wrapped as a bridge `FaceDetector` | a camera; the model's weights (a Hugging Face download on first use, cached after) | Nothing to install: `onnxruntime` and the Hugging Face hub are base dependencies of `reachy_mini`, and OpenCV is not needed. Details below |
| `custom` | the developer's `FaceDetector`, registered as a factory | a camera; `FaceSettings.face_detector` or `set_face_detector(factory)` | The contract below. With nothing registered, session entry raises `ValueError` (as a bad `idle_move` does) |

### The shipped detector — `yunet.py`

`YuNetDetector` is upstream's face detector as a bridge `FaceDetector`, and the reference implementation of the contract below:

- **Construction** imports `reachy_mini.vision.face_detector` and builds its `FaceDetector` with upstream's default thresholds — the import is inside the constructor (the two modules import each other; the runtime itself is already loaded with `reachy_mini`, whose package imports its vision module). The constructor is where the weights are fetched (`hf_hub_download`, pinned to the revision upstream pins) and the session opened — which is why the loop builds detectors on a worker thread, and why a config without the detector never pays for it.
- **Detection size.** `detect` subsamples the frame by an integer stride to about `DETECT_WIDTH = 320` px wide — upstream's own tracker detects at 320 — and scales every bbox, nose and eye it gets back by the stride, so a 1280×720 stream (the sim, the wireless robot) is detected at 320×180 and the Lite's 1920×1080 at 320×180 too, and the detector keeps up with the feed on one CPU thread. The subsample is a strided view made contiguous, no OpenCV.
- **Output.** One `PixelFace` per upstream `Face`: its bbox, its nose, its eyes (right, left) — so every report from it carries a roll and a size.
- The module is one file the project map lists ([../AGENTS.md](../../AGENTS.md)); a second shipped detector would be a second module and a second name in `FACE_DETECTORS`, nowhere else.

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

- **A factory, called once per detection start**, as the idle move's is called per idle entry ([motion.md](../motion/motion.md) "Custom idle moves"): the registered value is a zero-argument callable returning a fresh detector, so a detector holds per-run state (a model session) and a restart gets a clean one. Registered through `FaceSettings.face_detector` (a Python-only config field) or `set_face_detector(factory)`; `None` clears it.
- **Checked at registration**: not callable, a call that raises, or a result without a callable `detect` raises `ValueError` and leaves the registered factory as it was. With `faces.detector` `"custom"` and nothing registered, session entry raises `ValueError`; registering while the loop runs in `custom` mode restarts the detector with the new factory.
- **What the author guarantees.** `detect` runs on a worker thread once per **new** camera frame — polling faster than the feed runs the detector at the feed's rate (10 fps on a local daemon). It treats the frame as read-only (the array is the feed's, shared with every other consumer; a detector that draws on it copies first). It returns within a frame period or the next frame is skipped (frames are dropped, never queued); it may return faces in any order; it never touches the robot or the api. Its dependencies are its own — the bridge adds none.
- **The frame's time and the head pose it was taken from.** Both come with the frame: the camera feed stamps every frame it publishes with `ts` and, when it can stand behind it, the head pose at that time ([camera.md](camera.md) "The frame's time and the head pose" — a pose is attached only to a capture time, never to an arrival time). The detection loop hands the detector `frame.image` with `frame.ts` and copies `frame.head_pose` onto the report — image, time and pose taken together. This is also why a detector returns faces unsmoothed: smoothing across frames mixes pixels taken from different head poses, and the tracker's easing smooths the aim in the world frame instead.
- **A detector that raises while running** is one skipped frame and a `DEBUG` line; raising on every frame trips the detector-down rule above.
- **The shipped detector is the worked example.** `YuNetDetector` is a custom detector in every respect but its name in the config: the documentation shows it as the wrapper a developer writes around a model of their own, and the live tier registers it through the `custom` path to test that path ([testing.md](../testing/testing.md)).
- **One detector, two homes.** The protocol is the shape a latest-value vision library's detector has too — `detect(image_bgr, ts)` returning what was found in pixels — so the same detector object runs in the bridge's detection loop, where the bridge drives it and the head follows its faces, or in a graph the developer runs over the same camera feed beside other perception (hands, gestures), where a thin registered detector returning that graph's latest faces for the frame's `frame_id` still feeds the head. The feed is what makes the two coexist ([camera.md](camera.md) "A valid upstream for a vision graph").

### Configuration

Three fields, in two blocks ([config.md](../core/config.md)):

```json
"faces":  { "detector": "yunet", "detection": true },
"motion": { "tracking": true }
```

- `faces.detector` ∈ `null` / `yunet` / `custom`, default `null` — the detector; `custom` needs the Python-only `FaceSettings.face_detector` factory.
- `faces.detection` (default `false`) — whether the detection loop runs from session entry for the caller's sake (the face report). It needs **no motors**: nothing moves. Its verb is `set_face_detection(enabled)`, its property `face_detection`.
- `motion.tracking` (default `false`) — whether the head tracker runs from session entry ([head_tracking.md](../motion/head_tracking.md)). It **implies detection**: the loop runs while either switch is on, so `detection: false` with `tracking: true` is valid and means faces are detected for the head only (the report is still published — the loop has it anyway). `set_face_detection(False)` while tracking is on leaves the loop running; `faces.value.active` says what is actually happening.
- Either switch on with `detector` `null` is a `ConfigError` at `from_dict`; on a `ReachyMiniConfig` built directly in code the same contradiction is a `ValueError` at session entry.

The example config, the README and the control panel name `yunet` with both switches on: the robot that follows the person in front of it is one line of config away, not the default of a bare object.

### Lifecycle

`ReachyMiniApi.__aenter__` ([api.md](../core/api.md) "Lifecycle") starts the detection loop after the media session, the camera feed and the wobbling setup and before the motion session, since the tracker hands its aim to the loop; it stops it right after the motion session on exit. Bring-up cancel / failure unwinds it like every other step. The report resets to its inactive value at exit, published through `set`, so a subscriber of `changes()` learns that detection stopped; the observable itself outlives the session (a caller may keep iterating across sessions of the same api object).

### `fake` backend support

The fake has no detector of its own and needs none: the default config runs no loop on it, and a test that needs faces registers a **stub detector** through the `custom` path — a `FaceDetector` returning the `PixelFace`s of a scriptable scene on the fake's synthetic frames ([camera.md](camera.md) "`fake` backend support": the gradient frame, paced at the fake's frame rate, each frame stamped with its capture time and the fake's head pose). The detection loop and the debounce run unchanged on the fake, at real time, so `tests/` observe: a report that flips when the scene shows / hides a face, with the debounce holding a sub-threshold gap; a `changes()` subscriber woken on count changes only, and ending cleanly when cancelled mid-wait; the default config's `faces` staying inactive with no detector, and the switches refusing without one; a detector whose factory raises failing bring-up. Every report on the fake carries the frame's `head_pose`, so what the head does with it — [head_tracking.md](../motion/head_tracking.md)'s to test — runs through the tracker's exact-pose path.

## Relationship to the other specs

- **[camera.md](camera.md):** the detection loop samples the camera feed — the one reader of the robot's camera — and copies each frame's `ts` and `head_pose` onto its report; a vision graph over the same feed is where a detector runs beside other perception.
- **[observable.md](../core/observable.md):** `api.faces` is an `Observable[FaceReport]`; the detection loop is its producer and defines what it publishes.
- **[head_tracking.md](../motion/head_tracking.md):** the tracker consumes every observation's report and steers the head; tracking implies detection, and needs a detector.
- **[api.md](../core/api.md):** `faces`, `set_face_detection` / `face_detection`, `set_face_detector` / `face_detector`; the switches' behaviour without a detector.
- **[config.md](../core/config.md):** the `faces` block (`detector`, `detection`, the Python-only `face_detector`); `motion.tracking` implies detection; the cross-block rule.
- **[robot.md](../core/robot.md):** the consumed slice reaches the camera through the feed alone (`media.get_frame`) and the daemon's tracking not at all.
- **[sim_daemon.md](../daemon/sim_daemon.md):** the sim's faces are detected by the bridge from the camera stream the launcher feeds — the rendered eye camera under the viewer, or a host webcam headless or not; a headless sim without a webcam has no camera and its detector reads inactive.
- **[sim_scene.md](../testing/sim_scene.md) / [testing.md](../testing/testing.md):** a show / hide of the portrait on the viewer sim asserts *appeared* / *left* on `faces.changes()`; the live harness configures `yunet`.

## Open questions

1. **A daemon-side detector for the wireless robot.** On a wireless robot the host path decodes the camera's WebRTC stream and runs the detector on the host; the daemon's own detector on the robot would spare both, read over `GET /api/media/tracking/face` ([../docs/reachy-mini-api.md](../../docs/reachy-mini-api.md) "Face tracking"). It returns as a `daemon` value of `faces.detector` only if a measurement on a wireless robot shows the stream or the host CPU to be a problem — and it needs upstream to run its detector without steering the head (today the detector runs only while tracking is armed at a weight above zero, and the daemon blends its own aim in by that weight), and the MuJoCo daemon to step its tracking (upstream's sim loop never does), before it is worth a second source with its own report semantics (one face, no size, smoothed in the image, on the daemon's clock).
2. **A bearing in the report.** Yaw / pitch of the face in degrees, added to `Face` once the tracker's camera model ([head_tracking.md](../motion/head_tracking.md)) is shared with the report; deferred until a caller wants angles rather than image coordinates.
3. **Debounce numbers.** `FACE_ABSENT_S` is a starting value, to be tuned on the viewer sim against the flicker rate of the detector. The poll rate follows the local feed's 10 fps cap — the shipped detector reports 10.0 observations/s on the viewer sim's rendered camera (2026-09-29), the feed's rate; still to measure: the wireless robot's WebRTC frame rate reaching the host (30 fps nominal) and the detector's cost there.
4. **Other cues of a user.** The direction a voice comes from (the daemon's DoA snapshot, [api.md](../core/api.md) deferred perception) and a recognised identity would enrich the report — a `Face` gaining a `voice` or `name` field, or the report gaining `voices` beside `faces`. Deferred until a caller needs more than faces; the shape above is designed to take them additively.
5. **The frame's capture time** is the camera feed's question ([camera.md](camera.md) "The frame's time and the head pose", open question 1): today only the `fake` stamps a capture time, so a report carries a pose there and none on the sim or a robot, where the tracker estimates the delay. Nothing in this spec changes with the answer.
6. **A face stand-in for downstream tests.** The stub detector the bridge's own tests register on the fake could ship in `reachy_mini_bridge.testing`, so a consumer testing on the fake shows and hides a face in one line ([testing_support.md](../testing/testing_support.md)); deferred until a consumer asks.
7. **Detector families from a vision library.** Further shipped detectors — named in `faces.detector`, each behind a `faces-<name>` extra resolving to `funwithagents/vision-modules`, the glue converting their output to `PixelFace`s — stay an additive path beside `yunet`; deferred until a family other than YuNet is wanted.
