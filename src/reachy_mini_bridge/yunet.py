"""The shipped face detector: upstream's YuNet as a bridge ``FaceDetector``
(specs/vision/user_perception.md "The shipped detector — ``yunet.py``").

``face_detection.detector: "yunet"`` runs :class:`YuNetDetector` on the camera feed's frames —
``reachy_mini.vision.face_detector.FaceDetector`` (YuNet on ONNX Runtime, the model the
daemon itself runs) wrapped into ``PixelFace``s. Nothing to install: ``onnxruntime`` and
the Hugging Face hub are base dependencies of ``reachy_mini``, and OpenCV is not needed.
The upstream import is inside the constructor (``yunet.py`` and ``face_detection.py``
import each other), and the constructor is where the weights are fetched (into the Hugging
Face cache, once) and the session opened — which is why the detection loop builds
detectors on a worker thread, and why a config without the detector never pays for it.

It is also the reference implementation of the ``FaceDetector`` contract — the wrapper a
developer writes around a model of their own (docs/guides/custom-face-detector.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from .face_detection import DETECT_WIDTH, PixelFace

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import numpy.typing as npt

__all__ = ["DETECT_WIDTH", "YuNetDetector"]


def _upstream_detector() -> Any:
    from reachy_mini.vision.face_detector import FaceDetector as UpstreamYuNet

    return UpstreamYuNet()


class YuNetDetector:
    """Upstream's YuNet detector as a bridge ``FaceDetector``.

    ``upstream`` builds the detector object to wrap — anything with
    ``detect(frame_bgr) -> faces`` whose faces carry ``bbox``, ``nose``, ``right_eye``
    and ``left_eye`` in pixels; the default builds upstream's ``FaceDetector`` with its
    default thresholds (tests inject a stub so no model loads). ``width`` is the width it
    detects at (``face_detection.width``), ``DETECT_WIDTH`` by default; ``None`` detects on
    the full frame.
    """

    def __init__(
        self,
        upstream: Callable[[], Any] | None = None,
        *,
        width: int | None = DETECT_WIDTH,
    ) -> None:
        self._width = width
        self._detector = (_upstream_detector if upstream is None else upstream)()

    def detect(
        self, frame_bgr: npt.NDArray[np.uint8], ts: float
    ) -> Sequence[PixelFace]:
        """Every face in the frame, in pixels of the frame given (specs/vision/user_perception.md
        "Custom detectors"): detected on a strided subsample about the configured width
        wide, every point scaled back by the stride."""
        step = 1 if self._width is None else max(1, frame_bgr.shape[1] // self._width)
        small = (
            np.ascontiguousarray(frame_bgr[::step, ::step]) if step > 1 else frame_bgr
        )

        def up(point: tuple[float, float]) -> tuple[float, float]:
            return (float(point[0]) * step, float(point[1]) * step)

        return [
            PixelFace(
                bbox=(
                    float(face.bbox[0]) * step,
                    float(face.bbox[1]) * step,
                    float(face.bbox[2]) * step,
                    float(face.bbox[3]) * step,
                ),
                nose=up(face.nose),
                eyes=(up(face.right_eye), up(face.left_eye)),
            )
            for face in self._detector.detect(small)
        ]
