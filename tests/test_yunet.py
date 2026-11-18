"""Functional tests for the shipped YuNet wrapper (specs/vision/user_perception.md "The shipped
detector"), on a stub in place of upstream's model: no weights, no network."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from reachy_mini_bridge.face_detection import PixelFace
from reachy_mini_bridge.yunet import YuNetDetector


@dataclass(frozen=True)
class _UpstreamFace:
    """The shape of ``reachy_mini.vision.face_detector.Face``."""

    bbox: tuple[float, float, float, float]
    right_eye: tuple[float, float]
    left_eye: tuple[float, float]
    nose: tuple[float, float]


class _UpstreamStub:
    """Records the frames it is given and returns fixed faces, in pixels of that frame."""

    def __init__(self, faces: list[_UpstreamFace]) -> None:
        self.faces = faces
        self.frames: list[Any] = []

    def detect(self, frame_bgr: Any) -> list[_UpstreamFace]:
        self.frames.append(frame_bgr)
        return list(self.faces)


def _frame(width: int, height: int) -> Any:
    return np.zeros((height, width, 3), dtype=np.uint8)


def test_a_wide_frame_is_detected_on_a_subsample_and_the_faces_scaled_back() -> None:
    stub = _UpstreamStub(
        [
            _UpstreamFace(
                bbox=(10.0, 20.0, 30.0, 40.0),
                right_eye=(15.0, 30.0),
                left_eye=(35.0, 31.0),
                nose=(25.0, 40.0),
            )
        ]
    )
    detector = YuNetDetector(upstream=lambda: stub)

    faces = detector.detect(_frame(1280, 720), ts=1.0)

    # 1280 // 640 = 2 (the default width, DETECT_WIDTH): the model saw 640 x 360 and a
    # contiguous array.
    (seen,) = stub.frames
    assert seen.shape == (360, 640, 3)
    assert seen.flags["C_CONTIGUOUS"]
    assert faces == [
        PixelFace(
            bbox=(20.0, 40.0, 60.0, 80.0),
            nose=(50.0, 80.0),
            eyes=((30.0, 60.0), (70.0, 62.0)),
        )
    ]


def test_the_configured_width_sets_the_stride() -> None:
    """`face_detection.width`: 640 on a 1280 stream is stride 2; None detects the full
    frame."""
    wide = _UpstreamStub([])
    YuNetDetector(upstream=lambda: wide, width=640).detect(_frame(1280, 720), 0.0)
    assert wide.frames[0].shape == (360, 640, 3)
    full = _UpstreamStub([])
    frame = _frame(1920, 1080)
    YuNetDetector(upstream=lambda: full, width=None).detect(frame, 0.0)
    assert full.frames[0] is frame  # no subsample, no copy


def test_a_small_frame_is_detected_whole() -> None:
    stub = _UpstreamStub(
        [
            _UpstreamFace(
                bbox=(1.0, 2.0, 3.0, 4.0),
                right_eye=(1.5, 3.0),
                left_eye=(3.5, 3.0),
                nose=(2.5, 4.0),
            )
        ]
    )
    frame = _frame(64, 48)  # the fake's frame: narrower than DETECT_WIDTH
    faces = YuNetDetector(upstream=lambda: stub).detect(frame, ts=0.0)
    assert stub.frames[0] is frame  # no copy, no subsample
    assert faces == [
        PixelFace(
            bbox=(1.0, 2.0, 3.0, 4.0), nose=(2.5, 4.0), eyes=((1.5, 3.0), (3.5, 3.0))
        )
    ]


@pytest.mark.parametrize(
    ("score_threshold", "kwargs"),
    [(None, {}), (0.8, {"score_threshold": 0.8})],
)
def test_the_score_threshold_reaches_upstreams_detector(
    monkeypatch: pytest.MonkeyPatch,
    score_threshold: float | None,
    kwargs: dict[str, float],
) -> None:
    """`face_detection.score_threshold` is upstream's `score_threshold`; `None` leaves
    upstream's own default in place. Upstream's class is replaced by a recorder: no
    weights load."""
    import reachy_mini.vision.face_detector as upstream

    calls: list[dict[str, Any]] = []

    class _Recorder(_UpstreamStub):
        def __init__(self, **given: Any) -> None:
            calls.append(given)
            super().__init__([])

    monkeypatch.setattr(upstream, "FaceDetector", _Recorder)
    YuNetDetector(score_threshold=score_threshold)
    assert calls == [kwargs]
