"""Functional tests for FakeReachyMini (specs/robot.md).

These exercise the fake directly — the commands it records and the synthetic
perception/audio it returns — the behavior the deterministic ``tests/`` tier relies
on. No daemon, no hardware, no ``reachy_mini``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import numpy as np

from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


def test_motor_state_transitions() -> None:
    robot = FakeReachyMini()

    def mode() -> str:
        # Motor state is read the way the SDK exposes it: via the daemon client.
        return robot.client.get_status().backend_status.motor_control_mode

    # Starts resting.
    assert mode() == "disabled"

    robot.enable_motors()
    assert mode() == "enabled"

    robot.enable_gravity_compensation()
    assert mode() == "gravity_compensation"

    robot.disable_motors()
    assert mode() == "disabled"


def test_motion_commands_are_recorded() -> None:
    robot = FakeReachyMini()
    robot.start_head_tracking(weight=0.5)
    asyncio.run(robot.async_play_move("happy", initial_goto_duration=1.0))

    names = [name for name, _ in robot.commands]
    assert names == ["start_head_tracking", "async_play_move"]

    tracking_args = robot.commands[0][1]
    assert tracking_args["weight"] == 0.5
    assert robot.commands[1][1]["move"] == "happy"
    assert robot.commands[1][1]["initial_goto_duration"] == 1.0


def test_wobbling_toggles_are_recorded() -> None:
    robot = FakeReachyMini()
    robot.enable_wobbling()
    robot.disable_wobbling()

    assert [name for name, _ in robot.commands] == [
        "enable_wobbling",
        "disable_wobbling",
    ]


def test_media_commands_are_recorded() -> None:
    robot = FakeReachyMini()
    robot.media.start_recording()
    robot.media.push_audio_sample(np.zeros((320, 2), dtype=np.float32))
    robot.media.play_sound("wake_up.wav")
    robot.media.audio.clear_player()

    recorded = {name: args for name, args in robot.commands}
    assert "media.start_recording" in recorded
    assert recorded["media.push_audio_sample"]["frames"] == 320
    assert recorded["media.play_sound"]["sound_file"] == "wake_up.wav"
    assert "audio.clear_player" in recorded


def test_capture_format_agrees_with_getters() -> None:
    # Downstream conversion code reads these getters rather than hardcoding; the
    # synthetic sample must match what they report.
    media = FakeReachyMini().media
    sample = media.get_audio_sample()

    assert sample.dtype == np.float32
    assert sample.ndim == 2
    assert sample.shape[1] == media.get_input_channels()
    assert media.get_input_audio_samplerate() == 16000
    assert media.get_output_channels() == media.get_input_channels()


def test_play_move_sleeps_the_moves_duration() -> None:
    # The fake keeps a move's timing so a cancel has something in flight to interrupt.
    robot = FakeReachyMini()

    t0 = time.monotonic()
    asyncio.run(robot.async_play_move(SimpleNamespace(duration=0.1)))
    assert time.monotonic() - t0 >= 0.08

    t0 = time.monotonic()
    asyncio.run(robot.async_play_move("bare-name"))
    assert time.monotonic() - t0 < 0.05
