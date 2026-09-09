"""Functional tests for the client seam and FakeReachyMini (specs/client.md).

These drive the public surface on the ``fake`` backend only — no ``reachy_mini``
import, no daemon, no hardware. The real/sim construction path is covered by the
opt-in ``tests-e2e/test_client.py``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import numpy as np
import pytest

from reachy_mini_bridge.client import FakeReachyMini, build_robot


def test_build_robot_fake_returns_fake() -> None:
    robot = build_robot("fake")
    assert isinstance(robot, FakeReachyMini)


def test_build_robot_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        build_robot("bogus")


def test_importing_client_does_not_import_reachy_mini() -> None:
    # Run in a fresh interpreter: importing the seam + building the fake must not
    # drag in the heavy upstream package (that's what keeps tests/ daemon-free).
    code = (
        "import sys\n"
        "import reachy_mini_bridge.client as c\n"
        "c.build_robot('fake')\n"
        "leaked = sorted(m for m in sys.modules if m == 'reachy_mini' or m.startswith('reachy_mini.'))\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_motor_state_transitions() -> None:
    robot = build_robot("fake")
    assert isinstance(robot, FakeReachyMini)

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
    robot.goto_target(duration=1.5)
    asyncio.run(robot.async_play_move("happy", initial_goto_duration=1.0))

    names = [name for name, _ in robot.commands]
    assert names == ["start_head_tracking", "goto_target", "async_play_move"]

    tracking_args = robot.commands[0][1]
    assert tracking_args["weight"] == 0.5
    assert robot.commands[1][1]["duration"] == 1.5
    assert robot.commands[2][1]["move"] == "happy"


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


def test_context_manager_records_teardown() -> None:
    with build_robot("fake") as robot:
        assert isinstance(robot, FakeReachyMini)
        robot.enable_motors()
    assert robot.commands[-1][0] == "__exit__"
