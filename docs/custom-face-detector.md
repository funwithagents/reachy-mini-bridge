# Your own face detector — the `custom` detection source

The bridge ships **no face detector**. It ships the plumbing around one: the camera feed
(`api.camera`, the one reader of the robot's camera), a runner that hands each new frame
to a detector you register, the selection of the target face among the ones it returns,
and the head tracker that turns that face into a look-at aim. Vision code — models,
runtimes, their weights and their dependencies — stays out of the bridge's core, so a
user of the daemon's own detector (the default `faces.detector: "daemon"`) installs
nothing for it. Design: [specs/user_perception.md](../specs/user_perception.md) "Custom
detectors" and [specs/camera.md](../specs/camera.md).

## The contract

A detector is any object with a `detect(frame_bgr, ts)` method returning the faces it
sees, in pixels of the frame it was given:

```python
from reachy_mini_bridge import FaceDetector, PixelFace   # a Protocol and a frozen dataclass

PixelFace(
    bbox=(x, y, width, height),     # pixels of the frame given
    nose=(u, v),                    # the point the head aims at; the bbox centre when None
    eyes=((ur, vr), (ul, vl)),      # right, left: gives the roll; None when unknown
)
```

What the runner guarantees, and what it asks of you:

- `detect` runs **once per new camera frame**, on a worker thread, whatever the poll rate
  (10 frames a second on a local daemon). It receives the feed's frame — **read-only**,
  shared with every other consumer of the feed; copy before drawing on it.
- Return within a frame period. A slower detector skips frames; they are never queued.
- Return faces in any order; the bridge picks the target (the largest face above a
  minimum size, then the nearest to the previous target, dropped after a run of misses)
  and puts it first in `api.faces.value.faces`. Do not smooth positions across frames:
  frames come from different head poses, and the tracker smooths the aim in the world.
- Never touch the robot or the api from a detector.
- A detector that raises is one skipped frame; one that raises on every frame flips
  `api.faces.value.active` to `False` after a few seconds, and back once it works.

You register a **factory** — a zero-argument callable returning a fresh detector (a class
is one) — so a detector holds per-run state (a model session) and every restart gets a
clean one. The factory is checked when registered: not callable, raising, or returning
something without a callable `detect` is a `ValueError`, and the registered one stays.

## Upstream's YuNet detector as a custom detector

`reachy_mini.vision.face_detector.FaceDetector` is YuNet on ONNX Runtime — the detector
the daemon itself runs. `onnxruntime` and the Hugging Face hub are base dependencies of
`reachy_mini`, so it needs nothing beyond what the bridge already installs (OpenCV is
not needed); the model is downloaded into the Hugging Face cache on first use. Wrapping
it is a few lines:

```python
import numpy as np
from reachy_mini.vision.face_detector import FaceDetector as YuNet

from reachy_mini_bridge import PixelFace, ReachyMiniApi, ReachyMiniConfig

DETECT_WIDTH = 320  # upstream's own tracker detects at 320 px wide


class YuNetDetector:
    def __init__(self) -> None:
        self._yunet = YuNet()  # downloads the model on first use

    def detect(self, frame_bgr, ts):
        # Subsample to about 320 px wide (no OpenCV needed) and scale the results back.
        step = max(1, frame_bgr.shape[1] // DETECT_WIDTH)
        small = np.ascontiguousarray(frame_bgr[::step, ::step])
        up = lambda p: (p[0] * step, p[1] * step)
        return [
            PixelFace(
                bbox=tuple(v * step for v in f.bbox),
                nose=up(f.nose),
                eyes=(up(f.right_eye), up(f.left_eye)),
            )
            for f in self._yunet.detect(small)
        ]


config = ReachyMiniConfig.from_json_file("robot.json")  # "faces": {"detector": "custom"}
config.faces.face_detector = YuNetDetector  # Python only: a JSON file names the source

async with ReachyMiniApi(config) as api:
    await api.set_motors_state("enabled")
    ...  # the head follows whoever YuNet sees; api.faces reports every face, the target first
```

`faces.detector` is `"custom"` in the config (a JSON file can say so); the detector
itself is code, set on the dataclass as above or at run time with
`await api.set_face_detector(YuNetDetector)`. Session entry refuses `"custom"` with
nothing registered (`ValueError`, before anything is started). Registering another
factory while the session runs swaps the detector between two frames.

The opt-in live test `tests-e2e/test_custom_faces.py` runs exactly this wrapper on the
viewer sim's camera and checks the head converges on the test scene's portrait:

```
REACHY_MINI_E2E_SIM_VIEWER=1 REACHY_MINI_E2E_FACE_DETECTOR=yunet uv run pytest tests-e2e -rs -k custom
```

## One detector, two homes

The same detector object also runs in a vision graph you build over the camera feed,
beside other perception. `api.camera` has the shape a latest-value vision runtime
samples — `latest()` returning items with `frame_id`, `ts` and `image` — so a stage plugs
onto it directly, with no adapter and no second reader of the camera:

```python
hands = HandStage(api.camera, target_fps=30)   # any latest-value graph with that shape
```

When the graph runs its own face detector, a thin registered detector that returns the
graph's latest faces for the frame's `frame_id` still feeds the head. The feed is what
lets the two coexist: every consumer samples the same frames and takes none from the
others, which two callers of upstream's one-shot `get_frame()` would.

## What the report carries

`api.faces.value` from the `custom` source is a `FaceReport` with `source="custom"`, every
face normalised into the tracker's coordinates (`x`, `y` in `[-1, 1]`, the nose when
given, else the bbox centre; `roll` from the eyes; `size` as the bbox height over the
frame height), the target first, and the frame's own `ts`. Its `head_pose` is the pose
the frame was captured from when the feed knows it — today only on the `fake` backend;
on a robot or the sim the frame's time is its arrival and the tracker estimates the
delay, as it does for the `daemon` source ([specs/camera.md](../specs/camera.md) "The
frame's time and the head pose").
