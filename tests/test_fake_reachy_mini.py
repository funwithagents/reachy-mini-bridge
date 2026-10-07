"""Functional tests for FakeReachyMini (specs/core/robot.md).

These exercise the fake directly — the commands it records and the synthetic
perception/audio it returns — the behavior the deterministic ``tests/`` tier relies
on. No daemon, no hardware, no ``reachy_mini``.
"""

from __future__ import annotations

import numpy as np
import pytest

from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


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


def test_set_target_records_and_updates_the_present_pose() -> None:
    robot = FakeReachyMini()
    head = np.eye(4)
    head[2, 3] = 0.01
    robot.set_target(head=head, antennas=[0.1, -0.1], body_yaw=0.2)

    assert len(robot.targets) == 1
    assert robot.get_current_head_pose()[2, 3] == pytest.approx(0.01)
    joints, antennas = robot.get_current_joint_positions()
    assert joints == [0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert antennas == pytest.approx([0.1, -0.1])
    assert robot.commands == []

    # A partial target keeps the other components.
    robot.set_target(antennas=[0.3, -0.3])
    assert robot.get_current_head_pose()[2, 3] == pytest.approx(0.01)
    assert robot.get_current_joint_positions()[0][0] == pytest.approx(0.2)
    assert robot.get_current_joint_positions()[1] == pytest.approx([0.3, -0.3])


def test_set_target_rejects_bad_input() -> None:
    robot = FakeReachyMini()
    with pytest.raises(ValueError, match="At least one"):
        robot.set_target()
    with pytest.raises(ValueError, match="4x4"):
        robot.set_target(head=np.eye(3))
    with pytest.raises(ValueError, match="two elements"):
        robot.set_target(antennas=[0.1, 0.2, 0.3])
