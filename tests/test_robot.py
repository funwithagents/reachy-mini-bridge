"""Functional tests for the connection seam (specs/robot.md).

These drive ``build_robot`` on the ``fake`` backend only — no daemon, no hardware.
The real/sim construction path is exercised end-to-end by the opt-in
``tests-e2e/test_api.py`` (which builds the api over a live daemon); the fake's own
behavior is covered by ``tests/test_fake_reachy_mini.py``.
"""

from __future__ import annotations

import pytest

from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.robot import build_robot


def test_build_robot_fake_returns_fake() -> None:
    robot = build_robot("fake")
    assert isinstance(robot, FakeReachyMini)


def test_build_robot_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        build_robot("bogus")


def test_context_manager_records_teardown() -> None:
    with build_robot("fake") as robot:
        assert isinstance(robot, FakeReachyMini)
        robot.enable_motors()
    assert robot.commands[-1][0] == "__exit__"
