# Your own face detector — `faces.detector: "custom"`

How to run a face detector of your own inside the bridge: the head follows the faces it
finds and `api.faces` reports them, exactly as with the shipped detector. Design:
[specs/vision/user_perception.md](../specs/vision/user_perception.md) "Custom detectors".

You need this only when the shipped detector is not what you want. `"faces": {"detector":
"yunet"}` runs upstream's own YuNet model, wrapped by the bridge — nothing to install, the
weights downloaded into the Hugging Face cache on first use — and is enough for a robot
that looks at the person in front of it. A model of your own, a detector shared with a
vision graph you already run, or a stand-in for tests are the reasons to read on.

## 1. Write the detector

A detector is any object with a `detect(frame_bgr, ts)` method returning the faces it sees,
in pixels of the frame it was given:

```python
from reachy_mini_bridge import FaceDetector, PixelFace   # a Protocol and a frozen dataclass

PixelFace(
    bbox=(x, y, width, height),     # pixels of the frame given
    nose=(u, v),                    # the point the head aims at; the bbox centre when None
    eyes=((ur, vr), (ul, vl)),      # right, left: gives the roll; None when unknown
)
```

The shipped detector is the worked example —
[src/reachy_mini_bridge/yunet.py](../src/reachy_mini_bridge/yunet.py) is a class whose
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
- Never touch the robot or the api from a detector. Its dependencies are its own.
- A detector that raises is one skipped frame; one that raises on every frame flips
  `api.faces.value.active` to `False` after a few seconds, and back once it works.

## 2. Register it

The config names the detector; code supplies it. You register a **factory** — a
zero-argument callable returning a fresh detector (a class is one) — so a detector holds
per-run state and every restart gets a clean one:

```python
from reachy_mini_bridge import ReachyMiniApi, ReachyMiniConfig

config = ReachyMiniConfig.from_json_file("robot.json")
# robot.json: "faces": {"detector": "custom", "detection": true}, "motion": {"tracking": true}
config.faces.face_detector = MyDetector  # Python only: a JSON file cannot carry code

async with ReachyMiniApi(config) as api:
    await api.set_motors_state("enabled")
    ...  # the head follows whoever it sees; api.faces reports every face, the target first
```

Or at run time, `await api.set_face_detector(MyDetector)`; registering another factory
while the session runs swaps the detector between two frames. The factory is checked when
registered — not callable, raising, or returning something without a callable `detect` is
a `ValueError`, and the registered one stays — and session entry refuses `"custom"` with
nothing registered, before anything is started. The loop builds the detector from the
factory when it starts, on a worker thread, so a constructor may load a model; a build
that fails fails session entry with a `BridgeError` naming the cause.

## 3. What happens to your faces

The bridge runs the detector on each new frame of `api.camera`, picks the **target** among
the faces it returns (the largest above a minimum size, then the nearest to the previous
target, dropped after a run of misses), and publishes a `FaceReport` on `api.faces`: every
face in normalised image coordinates, the target first, with the frame's time and — when
the feed knows it — the head pose the frame was taken from, so the tracker aims each face
against the pose it was actually seen from. `source` reads `"custom"`. The report's fields
are in [specs/vision/user_perception.md](../specs/vision/user_perception.md) "The face report".

The live test [tests-e2e/test_custom_faces.py](../tests-e2e/test_custom_faces.py) registers
the shipped `YuNetDetector` through this path on the viewer sim's camera and checks the
head converges on the test scene's portrait — the registration and the runner under test,
not the model:

```
REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs -k custom
```

## Sharing the detector with a vision graph

The same detector object also runs in a vision graph you build over the camera feed,
beside other perception. `api.camera` has the shape a latest-value vision runtime
samples — `latest()` returning items with `frame_id`, `ts` and `image` — so a stage plugs
onto it directly, with no adapter and no second reader of the camera:

```python
hands = HandStage(api.camera, target_fps=30)   # any latest-value graph with that shape
```

When the graph runs its own face detector, register a thin detector that returns the
graph's latest faces for the frame's `frame_id`: it still feeds the head. The feed is what
lets the two coexist — every consumer samples the same frames and takes none from the
others, which two callers of upstream's one-shot `get_frame()` would
([specs/vision/camera.md](../specs/vision/camera.md) "A valid upstream for a vision graph").
