"""Functional tests for the detection loop (specs/user_perception.md) on the fake.

The loop runs unchanged at real time, with its timing shortened: polls at 20 Hz, a
drop published after 0.15 s. A test drives the scene through the fake's show_face /
hide_face and observes the report (`value`) and what a `changes()` subscriber wakes on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from reachy_mini_bridge import face_detection as fd
from reachy_mini_bridge.face_detection import (
    DAEMON_DETECT_WEIGHT,
    Face,
    FaceDetection,
    FaceReport,
    report_from_daemon,
)
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.observable import Observable

POLL_S = 0.05


@pytest.fixture(autouse=True)
def fast_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fd, "FACE_POLL_HZ", 1.0 / POLL_S)
    monkeypatch.setattr(fd, "FACE_ABSENT_S", 0.15)


class _Loop:
    """A running detection loop on a fake, with a subscriber recording what it wakes on."""

    def __init__(self, robot: FakeReachyMini, faces: Observable[FaceReport]) -> None:
        self.robot = robot
        self.faces = faces
        self.woken: list[FaceReport] = []
        self.observed: list[FaceReport] = []


@asynccontextmanager
async def _running(*, owns_daemon_arming: bool = True) -> AsyncIterator[_Loop]:
    robot = FakeReachyMini()
    faces: Observable[FaceReport] = Observable(FaceReport.inactive("daemon"))
    loop = _Loop(robot, faces)
    detection = FaceDetection(
        robot,
        source="daemon",
        faces=faces,
        owns_daemon_arming=owns_daemon_arming,
        on_observation=loop.observed.append,
    )

    async def subscribe() -> None:
        async for report in faces.changes():
            loop.woken.append(report)

    subscriber = asyncio.create_task(subscribe())
    await asyncio.sleep(0)
    await detection.start()
    await asyncio.sleep(2 * POLL_S)  # the first poll has published `active`
    try:
        yield loop
    finally:
        await detection.stop()
        await asyncio.sleep(0)
        subscriber.cancel()


def _counts(reports: list[FaceReport]) -> list[int]:
    return [len(r.faces) for r in reports]


def _run[T](coro: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        return await coro()

    return asyncio.run(main())


def test_report_from_daemon_maps_the_payload() -> None:
    detected = {"detected": True, "x": 0.25, "y": -0.5, "roll": 0.1, "ts": 12.5}
    assert report_from_daemon(detected, active=True) == FaceReport(
        faces=(Face(x=0.25, y=-0.5, roll=0.1, size=None),),
        ts=12.5,
        source="daemon",
        active=True,
    )
    nobody = {"detected": False, "x": None, "y": None, "roll": None, "ts": None}
    assert report_from_daemon(nobody, active=True) == FaceReport(
        (), 0.0, "daemon", True
    )


def test_the_loop_publishes_active_then_a_face_appearing_once() -> None:
    async def run() -> tuple[list[FaceReport], FaceReport]:
        async with _running() as loop:
            loop.robot.show_face(0.2, 0.1)
            await asyncio.sleep(4 * POLL_S)
            return list(loop.woken), loop.faces.value

    woken, value = _run(run)
    assert [(r.active, len(r.faces)) for r in woken] == [(True, 0), (True, 1)]
    assert value.faces == (Face(x=0.2, y=0.1, roll=None, size=None),)


def test_a_gap_shorter_than_the_absence_window_wakes_nobody() -> None:
    async def run() -> tuple[list[int], list[int]]:
        async with _running() as loop:
            loop.robot.show_face()
            await asyncio.sleep(3 * POLL_S)
            loop.woken.clear()
            loop.robot.hide_face()
            await asyncio.sleep(0.08)  # < 0.15 s
            read_meanwhile = len(loop.faces.value.faces)
            loop.robot.show_face()
            await asyncio.sleep(0.3)
            return [read_meanwhile], _counts(loop.woken)

    read_meanwhile, woken = _run(run)
    assert read_meanwhile == [0]  # `value` did read empty during the gap
    assert woken == []


def test_a_face_gone_past_the_window_wakes_once_with_an_empty_report() -> None:
    async def run() -> list[FaceReport]:
        async with _running() as loop:
            loop.robot.show_face()
            await asyncio.sleep(3 * POLL_S)
            loop.woken.clear()
            loop.robot.hide_face()
            await asyncio.sleep(0.5)
            return list(loop.woken)

    woken = _run(run)
    assert [(r.active, r.faces) for r in woken] == [(True, ())]


def test_a_moving_face_updates_the_value_without_waking() -> None:
    async def run() -> tuple[float, list[int]]:
        async with _running() as loop:
            loop.robot.show_face(0.0, 0.0)
            await asyncio.sleep(3 * POLL_S)
            loop.woken.clear()
            loop.robot.show_face(0.5, 0.0)
            await asyncio.sleep(3 * POLL_S)
            return loop.faces.value.faces[0].x, _counts(loop.woken)

    x, woken = _run(run)
    assert x == 0.5
    assert woken == []


def test_stop_publishes_the_inactive_report() -> None:
    async def run() -> tuple[FaceReport, list[FaceReport]]:
        async with _running() as loop:
            loop.robot.show_face()
            await asyncio.sleep(3 * POLL_S)
        await asyncio.sleep(0)
        return loop.faces.value, loop.woken

    value, woken = _run(run)
    assert value == FaceReport.inactive("daemon")
    assert woken[-1] == FaceReport.inactive("daemon")


def test_the_loop_arms_and_disarms_the_daemon_when_it_owns_the_arming() -> None:
    async def run(owns: bool) -> list[tuple[str, dict[str, Any]]]:
        async with _running(owns_daemon_arming=owns) as loop:
            pass
        return [c for c in loop.robot.commands if "head_tracking" in c[0]]

    assert _run(lambda: run(True)) == [
        ("start_head_tracking", {"weight": DAEMON_DETECT_WEIGHT}),
        ("stop_head_tracking", {}),
    ]
    assert _run(lambda: run(False)) == []


def test_handing_the_arming_back_arms_at_once_and_away_skips_the_disarm() -> None:
    async def run() -> list[tuple[str, dict[str, Any]]]:
        robot = FakeReachyMini()
        faces: Observable[FaceReport] = Observable(FaceReport.inactive("daemon"))
        detection = FaceDetection(
            robot, source="daemon", faces=faces, owns_daemon_arming=False
        )
        await detection.start()
        await detection.set_owns_daemon_arming(True)  # the tracker stopped
        await detection.set_owns_daemon_arming(False)  # the tracker started again
        await detection.stop()
        return [c for c in robot.commands if "head_tracking" in c[0]]

    assert _run(run) == [("start_head_tracking", {"weight": DAEMON_DETECT_WEIGHT})]


def test_a_failing_source_reads_inactive_then_active_on_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fd, "FACE_SOURCE_DOWN_S", 0.2)
    real = fd.daemon_face_target
    failing = {"on": False}

    def flaky(robot: Any) -> dict[str, Any]:
        if failing["on"]:
            raise OSError("503 Service Unavailable")
        return real(robot)

    monkeypatch.setattr(fd, "daemon_face_target", flaky)

    async def run() -> tuple[list[bool], bool]:
        async with _running() as loop:
            loop.robot.show_face()
            await asyncio.sleep(3 * POLL_S)
            loop.woken.clear()
            failing["on"] = True
            await asyncio.sleep(0.4)
            down = loop.faces.value.active
            failing["on"] = False
            await asyncio.sleep(3 * POLL_S)
            return [r.active for r in loop.woken], down

    woken, down = _run(run)
    assert down is False
    assert woken == [False, True]


def test_on_observation_sees_every_poll() -> None:
    async def run() -> tuple[int, int]:
        async with _running() as loop:
            loop.observed.clear()
            loop.robot.show_face()
            await asyncio.sleep(0.5)
            return len(loop.observed), sum(1 for r in loop.observed if r.faces)

    polls, with_face = _run(run)
    assert polls >= 6  # ~10 polls in 0.5 s at 20 Hz
    assert with_face >= polls - 1


def test_the_custom_source_is_not_available_yet() -> None:
    async def run() -> None:
        robot = FakeReachyMini()
        faces: Observable[FaceReport] = Observable(FaceReport.inactive("custom"))
        detection = FaceDetection(
            robot, source="custom", faces=faces, owns_daemon_arming=True
        )
        with pytest.raises(ValueError, match="not available yet"):
            await detection.start()
        assert robot.commands == []

    _run(run)
