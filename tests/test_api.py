"""Functional tests for ReachyMiniApi on the fake backend (specs/api.md).

Drives the public api the way a caller would and asserts through the escape hatch
(`api.robot`, the FakeReachyMini) and its recorded commands. No network, no daemon,
no hardware. Async runs via `asyncio.run` (fast-tier convention).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge.api import ReachyMiniApi
from reachy_mini_bridge.errors import BridgeError, MotorsNotEnabledError
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


class _ToneSynth:
    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        yield np.full(400, 0.2, dtype=np.float32)


def _fake(api: ReachyMiniApi) -> FakeReachyMini:
    robot = api.robot
    assert isinstance(robot, FakeReachyMini)
    return robot


def _command_names(api: ReachyMiniApi) -> list[str]:
    return [name for name, _ in _fake(api).commands]


# --- construction / escape hatch ---------------------------------------------------


def test_robot_escape_hatch_is_the_fake() -> None:
    api = ReachyMiniApi("fake")
    assert isinstance(api.robot, FakeReachyMini)
    assert api.raw is api.robot


# --- motors ------------------------------------------------------------------------


def test_motor_state_round_trips_through_the_daemon() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            seen = [await api.get_motors_state()]
            await api.set_motors_state("enabled")
            seen.append(await api.get_motors_state())
            await api.set_motors_state("gravity_compensation")
            seen.append(await api.get_motors_state())
            await api.set_motors_state("disabled")
            seen.append(await api.get_motors_state())
            return seen

    assert asyncio.run(run()) == [
        "disabled",
        "enabled",
        "gravity_compensation",
        "disabled",
    ]


def test_set_motors_state_dispatches_to_matching_primitive() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.set_motors_state("gravity_compensation")
            await api.set_motors_state("disabled")
            return _command_names(api)

    names = asyncio.run(run())
    assert "enable_motors" in names
    assert "enable_gravity_compensation" in names
    assert "disable_motors" in names


def test_set_motors_state_rejects_unknown_state() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            with pytest.raises(ValueError, match="unknown motor state"):
                await api.set_motors_state("asleep")

    asyncio.run(run())


# --- motor precondition on movement verbs ------------------------------------------


def test_play_emotion_requires_motors_enabled() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("disabled")
            with pytest.raises(MotorsNotEnabledError):
                await api.play_emotion("happy")
            # nothing was sent downstream
            assert "async_play_move" not in _command_names(api)

    asyncio.run(run())


def test_start_head_tracking_requires_motors_enabled() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("gravity_compensation")
            with pytest.raises(MotorsNotEnabledError):
                await api.start_head_tracking()
            assert "start_head_tracking" not in _command_names(api)

    asyncio.run(run())


def test_movement_verbs_run_once_motors_enabled() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.play_emotion("happy")
            await api.start_head_tracking(weight=0.5)
            await api.stop_head_tracking()
            return _command_names(api)

    names = asyncio.run(run())
    assert "async_play_move" in names
    assert "start_head_tracking" in names
    assert "stop_head_tracking" in names


def test_start_head_tracking_forwards_weight() -> None:
    async def run() -> float:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.start_head_tracking(weight=0.25)
            args = next(a for n, a in _fake(api).commands if n == "start_head_tracking")
            return args["weight"]

    assert asyncio.run(run()) == 0.25


# --- expression --------------------------------------------------------------------


def test_list_emotions_returns_the_offline_library() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            return await api.list_emotions()

    assert asyncio.run(run()) == ["happy", "sad", "curious"]


def test_play_emotion_resolves_name_and_plays_it() -> None:
    async def run() -> object:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.play_emotion("curious")
            args = next(a for n, a in _fake(api).commands if n == "async_play_move")
            return args["move"]

    assert asyncio.run(run()) == "curious"


def test_play_emotion_unknown_name_raises() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            with pytest.raises(ValueError, match="not found"):
                await api.play_emotion("nonexistent")

    asyncio.run(run())


# --- audio ------------------------------------------------------------------------


def test_say_routes_through_the_media_pipeline() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake", synthesizer=_ToneSynth()) as api:
            await api.say("hello")
            return _command_names(api)

    names = asyncio.run(run())
    assert "media.push_audio_sample" in names


def test_say_accepts_a_per_call_synthesizer() -> None:
    async def run() -> int:
        async with ReachyMiniApi("fake") as api:  # no default synth configured
            await api.say("hello", _ToneSynth())
            return sum(1 for n in _command_names(api) if n == "media.push_audio_sample")

    assert asyncio.run(run()) >= 1


def test_say_without_any_synthesizer_raises_bridge_error() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            with pytest.raises(BridgeError, match="SpeechSynthesizer"):
                await api.say("hello")

    asyncio.run(run())


def test_play_sound_reaches_the_media_layer() -> None:
    async def run() -> dict[str, object]:
        async with ReachyMiniApi("fake") as api:
            await api.play_sound("wake_up.wav")
            return next(a for n, a in _fake(api).commands if n == "media.play_sound")

    assert asyncio.run(run())["sound_file"] == "wake_up.wav"


def test_audio_input_streams_mic_bytes_and_exposes_format() -> None:
    async def run() -> tuple[int, int, bytes]:
        async with ReachyMiniApi("fake") as api:
            chunk = b""
            async for c in api.audio_input():
                chunk = c
                break
            return api.mic_sample_rate, api.mic_channels, chunk

    sr, ch, chunk = asyncio.run(run())
    assert sr == 16000
    assert ch == 2
    assert len(chunk) > 0 and len(chunk) % 2 == 0  # whole int16 samples


# --- perception (camera) -----------------------------------------------------------


def test_get_camera_frame_returns_a_bgr_frame() -> None:
    async def run() -> npt.NDArray[np.uint8] | None:
        async with ReachyMiniApi("fake") as api:
            return await api.get_camera_frame()

    frame = asyncio.run(run())
    assert frame is not None  # the fake always has a frame ready
    assert frame.ndim == 3 and frame.shape[2] == 3  # HxWx3 BGR
    assert frame.dtype == np.uint8
    assert frame.size > 0
    # The synthetic frame carries real structure (a gradient), not a flat constant.
    assert frame.min() != frame.max()
