"""Functional tests for the real daemon launcher (specs/real_daemon.md) — no daemon, no
robot, no GStreamer pipeline: the selection is driven with scripted device names, the
media-server wiring with a stand-in server, and the launcher with upstream's ``main()``
replaced by a recorder.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from reachy_mini_bridge import real_daemon

_LOGGER = "reachy_mini_bridge.real_daemon"


class _Builds:
    """Scripts what each pipeline build opened, and records the rebuilds asked for."""

    def __init__(self, names: list[str | None]) -> None:
        self._names = iter(names)
        self.restarts: list[int] = []

    def opened(self) -> str | None:
        return next(self._names)

    def restart(self, index: int) -> None:
        self.restarts.append(index)


# --- the selection --------------------------------------------------------------------


def test_is_robot_camera_matches_upstreams_camera_names() -> None:
    for name in ("Reachy Mini Camera", "Arducam_12MP", "imx708"):
        assert real_daemon.is_robot_camera(name)
    assert not real_daemon.is_robot_camera("Caméra du MacBook Pro")
    assert not real_daemon.is_robot_camera("")
    detection = pytest.importorskip("reachy_mini.media.device_detection")
    assert tuple(detection.DEFAULT_CAM_NAMES) == real_daemon.ROBOT_CAMERA_NAMES


def test_select_camera_keeps_a_build_that_opened_the_robot_camera() -> None:
    builds = _Builds(["Reachy Mini Camera"])
    assert real_daemon.select_camera(builds.opened, builds.restart, first=1, count=2)
    assert builds.restarts == []


def test_select_camera_rebuilds_round_robin_until_the_robot_camera_opens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    builds = _Builds(
        ["Caméra du MacBook Pro", "Caméra du MacBook Pro", "Reachy Mini Camera"]
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert real_daemon.select_camera(
            builds.opened, builds.restart, first=1, count=2
        )
    # after index 1 comes 0, then 1 again: the order avfvideosrc reads moves between opens
    assert builds.restarts == [0, 1]
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.WARNING, logging.WARNING, logging.INFO]
    assert "MacBook" in caplog.records[0].getMessage()
    assert "device-index 0" in caplog.records[0].getMessage()
    assert "after 3 builds" in caplog.records[-1].getMessage()


def test_select_camera_gives_up_after_the_attempts_and_leaves_the_last_build(
    caplog: pytest.LogCaptureFixture,
) -> None:
    builds = _Builds(["Caméra du MacBook Pro"] * 3)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert not real_daemon.select_camera(
            builds.opened, builds.restart, first=0, count=2, attempts=3
        )
    assert builds.restarts == [1, 0]  # three builds: the first plus two rebuilds
    assert caplog.records[-1].levelno == logging.ERROR
    assert "without video" in caplog.records[-1].getMessage()


def test_select_camera_accepts_a_device_that_reports_no_name() -> None:
    builds = _Builds([None])
    assert real_daemon.select_camera(builds.opened, builds.restart, first=0, count=1)
    assert builds.restarts == []


# --- the media-server wiring ------------------------------------------------------------


class _FakePipeline:
    def __init__(self) -> None:
        self.states: list[Any] = []

    def set_state(self, state: Any) -> None:
        self.states.append(state)


def _macos(
    monkeypatch: pytest.MonkeyPatch, names: list[str | None], count: int
) -> None:
    opened = iter(names)
    monkeypatch.setattr(real_daemon.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(real_daemon, "_video_device_count", lambda: count)
    monkeypatch.setattr(real_daemon, "_gst_null", lambda: "NULL")
    monkeypatch.setattr(
        real_daemon, "_opened_device_name", lambda pipeline: next(opened)
    )


def test_checked_start_rebuilds_the_media_server_on_the_next_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _macos(monkeypatch, ["Caméra du MacBook Pro", "Reachy Mini Camera"], count=2)
    server = SimpleNamespace(_cam_path="1", _pipeline_sender=_FakePipeline())
    starts: list[str] = []
    real_daemon._checked_start(server, lambda s: starts.append(s._cam_path))
    assert starts == ["1", "0"]  # the detected index, then the next one
    assert server._pipeline_sender.states == ["NULL"]  # torn down before the rebuild
    assert server._cam_path == "0"  # the index the daemon now runs on


def test_checked_start_is_satisfied_by_a_first_build_that_opened_the_robot_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _macos(monkeypatch, ["Reachy Mini Camera"], count=2)
    server = SimpleNamespace(_cam_path="0", _pipeline_sender=_FakePipeline())
    starts: list[str] = []
    real_daemon._checked_start(server, lambda s: starts.append(s._cam_path))
    assert starts == ["0"]
    assert server._pipeline_sender.states == []


@pytest.mark.parametrize(
    ("system", "cam_path"),
    [("Linux", "/dev/video0"), ("Darwin", "use_sim"), ("Darwin", "")],
)
def test_checked_start_leaves_other_platforms_and_sources_alone(
    monkeypatch: pytest.MonkeyPatch, system: str, cam_path: str
) -> None:
    monkeypatch.setattr(real_daemon.platform, "system", lambda: system)
    monkeypatch.setattr(
        real_daemon, "_opened_device_name", lambda pipeline: pytest.fail("probed")
    )
    server = SimpleNamespace(_cam_path=cam_path, _pipeline_sender=_FakePipeline())
    starts: list[str] = []
    real_daemon._checked_start(server, lambda s: starts.append(s._cam_path))
    assert starts == [cam_path]


def test_install_macos_camera_check_wraps_upstreams_start_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = pytest.importorskip("reachy_mini.media.media_server")
    original = upstream.GstMediaServer.start
    monkeypatch.setattr(upstream.GstMediaServer, "start", original)  # restored after
    real_daemon.install_macos_camera_check()
    wrapped = upstream.GstMediaServer.start
    assert wrapped is not original
    real_daemon.install_macos_camera_check()
    assert upstream.GstMediaServer.start is wrapped  # idempotent
    calls: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        real_daemon,
        "_checked_start",
        lambda server, start: calls.append((server, start)),
    )
    server = object()
    wrapped(server)
    assert calls == [(server, original)]


# --- the launcher -----------------------------------------------------------------------


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], int]:
    """Run the launcher with upstream's ``main()`` recording the argv it would parse and
    the camera check counted instead of installed."""
    upstream_main = pytest.importorskip("reachy_mini.daemon.app.main")
    seen: list[list[str]] = []
    installed: list[bool] = []
    monkeypatch.setattr(upstream_main, "main", lambda: seen.append(list(sys.argv)))
    monkeypatch.setattr(
        real_daemon, "install_macos_camera_check", lambda: installed.append(True)
    )
    monkeypatch.setattr(sys, "argv", ["untouched"])
    real_daemon.run_real_daemon(argv)
    assert len(seen) == 1
    return seen[0], len(installed)


def test_run_real_daemon_rewrites_argv_and_installs_the_camera_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv, installed = _run(
        [
            "--no-preload-datasets",
            "--kinematics-engine",
            "Placo",
            "--log-level",
            "DEBUG",
        ],
        monkeypatch,
    )
    assert argv == [
        "reachy-mini-daemon",
        "--no-preload-datasets",
        "--kinematics-engine",
        "Placo",
        "--log-level",
        "DEBUG",
    ]
    assert installed == 1


def test_run_real_daemon_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    argv, installed = _run([], monkeypatch)
    assert argv == ["reachy-mini-daemon", "--preload-datasets"]
    assert installed == 1
