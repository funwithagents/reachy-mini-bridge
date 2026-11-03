"""Functional tests for the greeter example (examples/greeter/app.py) on the fake: a stub
detector stands in for a person, far, then too close, then back, then gone."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

from examples.greeter import app
from reachy_mini_bridge import PixelFace, ReachyMiniBridge, ReachyMiniConfig
from reachy_mini_bridge.config import (
    FaceDetectionSettings,
    MotionSettings,
    TrackingSettings,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

# Face heights on the fake's 48 px frame through its Lite camera model: 12 px reads about
# 0.6 m away, outside the default 0.40 m personal space; 40 px about 0.19 m, inside it.
FAR_PX = 12.0
NEAR_PX = 40.0


class _Person:
    def __init__(self) -> None:
        self.height_px: float | None = None

    def detector(self) -> _Person:
        return self

    def detect(self, frame_bgr: npt.NDArray[np.uint8], ts: float) -> list[PixelFace]:
        if self.height_px is None:
            return []
        h, u, v = self.height_px, 31.5, 23.5
        return [PixelFace(bbox=(u - h / 3, v - h / 2, 2 * h / 3, h), nose=(u, v))]


async def _said(lines: list[str], text: str, timeout: float) -> None:
    deadline = time.perf_counter() + timeout
    while text not in lines:
        assert time.perf_counter() < deadline, f"never said {text!r}; said {lines}"
        await asyncio.sleep(0.05)


def test_the_greeter_reacts_to_the_personal_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hello when someone shows up, a word when they come too close and another when they
    step back, good bye when they leave — each once, in that order."""
    lines: list[str] = []

    async def say(bridge: ReachyMiniBridge, text: str) -> None:
        lines.append(text)

    monkeypatch.setattr(app, "say_safely", say)
    monkeypatch.setattr(app, "HELLO_DELAY_S", 0.2)
    person = _Person()
    config = ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="custom", face_detector=person.detector
        ),
        motion=MotionSettings(idle="hold", tracking=TrackingSettings(enabled=True)),
    )

    async def scenario() -> None:
        greeter = asyncio.create_task(app.run(ReachyMiniBridge(config)))
        try:
            await asyncio.sleep(1.0)  # the session up, nobody there
            person.height_px = FAR_PX
            await _said(lines, "Hello there", 3.0)
            person.height_px = NEAR_PX
            await _said(lines, "Whoa, you're a bit close!", 3.0)
            person.height_px = FAR_PX
            await _said(lines, "Ah, that's better.", 3.0)
            person.height_px = None
            await _said(lines, "Good bye", 5.0)
        finally:
            greeter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await greeter

    asyncio.run(scenario())
    assert lines == [
        "Hello there",
        "Whoa, you're a bit close!",
        "Ah, that's better.",
        "Good bye",
    ]


def test_a_visitor_who_leaves_while_too_close_gets_no_relief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[str] = []

    async def say(bridge: ReachyMiniBridge, text: str) -> None:
        lines.append(text)

    monkeypatch.setattr(app, "say_safely", say)
    monkeypatch.setattr(app, "HELLO_DELAY_S", 0.2)
    person = _Person()
    config = ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="custom", face_detector=person.detector
        ),
        motion=MotionSettings(idle="hold", tracking=TrackingSettings(enabled=True)),
    )

    async def scenario() -> None:
        greeter = asyncio.create_task(app.run(ReachyMiniBridge(config)))
        try:
            await asyncio.sleep(1.0)
            person.height_px = NEAR_PX  # arrives already too close
            await _said(lines, "Whoa, you're a bit close!", 4.0)
            person.height_px = None
            await _said(lines, "Good bye", 5.0)
            await asyncio.sleep(0.5)
        finally:
            greeter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await greeter

    asyncio.run(scenario())
    assert lines == ["Hello there", "Whoa, you're a bit close!", "Good bye"]
