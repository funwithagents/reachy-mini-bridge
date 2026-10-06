# Your own face detector — `face_detection.detector: "custom"`

How to run a face detector of your own inside the bridge: the head follows the faces it
finds and `bridge.faces` reports them, exactly as with the shipped detector. Design:
[specs/vision/user_perception.md](../../specs/vision/user_perception.md) "Custom detectors".

You need this only when the shipped detector is not what you want. `"face_detection":
{"detector": "yunet"}` names upstream's own YuNet model, wrapped by the bridge — nothing to
install, the weights downloaded into the Hugging Face cache on first use. The name alone
starts nothing: `"enabled": true` beside it runs the detector so `bridge.faces` reports who
is there, and `"motion": {"tracking": true}` makes the robot look at the person in front of
it (the head moves once its motors are `enabled`) — the [configuration
reference](../reference/configuration.md#face_detection--who-is-in-front-of-the-robot) has the fields. The
bridge ships that one detector
and no other, to stay lightweight: a model of your own, a vision library's detector
(vision-modules' MediaPipe face landmarker, with a fitted head orientation), or a stand-in
for tests plug in here, their dependencies yours.

## 1. Write the detector

A detector is any object with a `detect(frame_bgr, ts)` method returning the faces it sees,
in pixels of the frame it was given:

```python
from reachy_mini_bridge import FaceDetector, PixelFace   # a Protocol and a frozen dataclass

PixelFace(
    bbox=(x, y, width, height),     # pixels of the frame given
    nose=(u, v),                    # the point the head aims at; the bbox centre when None
    eyes=((ur, vr), (ul, vl)),      # right, left: gives the roll; None when unknown
    orientation=(roll, pitch, yaw), # radians, from a detector that fits a head; optional
)
```

`orientation` follows the report's convention — roll positive when the eye line turns
clockwise in the image, pitch positive when the face tilts down, yaw positive when it
turns toward the image's right — and is preferred over the eye-line roll when given.

The shipped detector is the worked example —
[src/reachy_mini_bridge/yunet.py](../../src/reachy_mini_bridge/yunet.py) is a class whose
constructor builds the model and whose `detect` subsamples the frame, runs the model and
scales its boxes, noses and eyes back into `PixelFace`s. A wrapper around a model of your
own has the same shape:

```python
import numpy as np

from reachy_mini_bridge import PixelFace

DETECT_WIDTH = 320  # upstream's own tracker detects at 320 px wide


class MyDetector:
    def __init__(self) -> None:
        self._model = load_my_model()  # per-run state: built once per detection start

    def detect(self, frame_bgr, ts):
        # Subsample to about 320 px wide (no OpenCV needed) and scale the results back.
        step = max(1, frame_bgr.shape[1] // DETECT_WIDTH)
        small = np.ascontiguousarray(frame_bgr[::step, ::step])
        return [
            PixelFace(
                bbox=tuple(v * step for v in f.bbox),
                nose=(f.nose[0] * step, f.nose[1] * step),
            )
            for f in self._model.detect(small)
        ]
```

What the bridge asks of `detect`:

- It runs **once per new camera frame**, on a worker thread, whatever the poll rate (10
  frames a second on a local daemon). Return within a frame period: a slower detector
  skips frames, they are never queued.
- The frame is the camera feed's, **read-only** and shared with every other consumer;
  copy before drawing on it.
- Return faces in any order and **unsmoothed**: frames come from different head poses,
  and the tracker smooths the aim in the world, not the pixels.
- Never touch the robot or the bridge from a detector. Its dependencies are its own.
- A detector that raises is one skipped frame; one that raises on every frame flips
  `bridge.faces.value.active` to `False` after a few seconds, and back once it works.
- `detect` is called by one caller at a time, never concurrently, possibly from a
  different pool thread each call — what a stateful model such as MediaPipe's needs.
- An optional `close()` releases what the detector holds (a model session): the bridge
  calls it on a worker thread when it lets go of the detector — at session exit, and when
  another factory replaces it — once any `detect` in flight has returned.
- `face_detection.target_fps` caps how often the bridge calls `detect`, for your detector
  too; `face_detection.width` is the shipped detector's knob — your detector's working
  width is yours (subsample as above and scale back to the frame you were given).

## 2. Register it

The config names the detector; code supplies it. You register a **factory** — a
zero-argument callable returning a fresh detector (a class is one) — so a detector holds
per-run state and every restart gets a clean one:

```python
from reachy_mini_bridge import ReachyMiniBridge, ReachyMiniConfig

config = ReachyMiniConfig.from_json_file("robot.json")
# robot.json: "face_detection": {"detector": "custom", "enabled": true},
#             "motion": {"tracking": true}
config.face_detection.face_detector = MyDetector  # Python only: a JSON file cannot carry code

async with ReachyMiniBridge(config) as bridge:
    await bridge.set_motors_state("enabled")
    ...  # the head follows whoever it chooses; bridge.faces reports every face
```

Or at run time, `await bridge.set_face_detector(MyDetector)`; registering another factory
while the session runs swaps the detector between two frames. The factory is checked when
registered — not callable is a `ValueError`, and the registered one stays — and session
entry refuses `"custom"` with nothing registered, before anything is started. Nothing is
built at registration: the loop builds the detector from the factory when it starts, on a
worker thread, so a constructor may load a model; a build that raises fails session entry
with a `BridgeError` naming the cause, and one that returns something without a callable
`detect` fails it with a `ValueError`. Clearing the factory (`set_face_detector(None)`)
while the loop runs it is refused — stop head tracking and face detection first.

## 3. What happens to your faces

The bridge runs the detector on each new frame of `bridge.camera`, gives every face it
returns a **`track_id`** carried from frame to frame by nearest matching (kept through a
short run of misses, never reused), and publishes a `FaceReport` on `bridge.faces`: every
face in normalised image coordinates with its pixel box and head angles, in `track_id`
order, with the frame they were found in, its time and — when the feed knows it — the head
pose it was taken from, so the tracker aims each face against the pose it was actually seen
from. `source` reads `"custom"`. The head tracker then chooses whom to follow — the biggest
face, kept while seen, a vanished face waited for before switching — and publishes that
choice, by `track_id`, on `bridge.head_tracking`. The fields are in
[specs/vision/user_perception.md](../../specs/vision/user_perception.md) "The face report" and
[specs/motion/head_tracking.md](../../specs/motion/head_tracking.md) "Whom the head follows".

The live test [tests-e2e/test_custom_faces.py](../../tests-e2e/test_custom_faces.py) registers
the shipped `YuNetDetector` through this path on the viewer sim's camera and checks the
head converges on the test scene's portrait with the same convergence kit the config-named
detector is tested with (`reachy_mini_bridge.testing.gaze`) — the registration and the
runner under test, not the model:

```
REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -k custom
```

## Beside a vision graph

A vision graph you build over the camera feed — hands, gestures, a face recogniser — runs
beside the bridge's detection. `bridge.camera` has the shape a latest-value vision runtime
samples — `latest()` returning items with `frame_id`, `ts` and `image` — so a stage plugs
onto it directly, with no adapter and no second reader of the camera:

```python
hands = HandStage(bridge.camera, target_fps=30)   # any latest-value graph with that shape
```

The faces reach the graph through `bridge.faces`, not through a second detector: each
report carries its `frame`, every face's pixel `bbox` and its `track_id`, and has a
`frame_id`, so a graph node crops the faces from the very frame they were found in,
analyses them (who is it, are they talking), and joins its results to the bridge's faces
by `track_id`. `bridge.head_tracking.value.track_id` says which of them the head follows.
A face detector you want the head to follow is registered with the bridge as above — the
bridge drives it. The feed is what lets all of this coexist — every consumer samples the
same frames and takes none from the others, which two callers of upstream's one-shot
`get_frame()` would ([specs/vision/camera.md](../../specs/vision/camera.md) "A valid upstream
for a vision graph").
