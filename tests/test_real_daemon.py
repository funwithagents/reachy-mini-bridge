"""Functional tests for the real daemon launcher (specs/daemon/real_daemon.md) — no daemon, no
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

from reachy_mini_bridge import real_daemon, uvc

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


@pytest.mark.parametrize(
    "opened",
    ["Reachy Mini Camera", None],
    ids=["the robot camera", "a device that reports no name"],
)
def test_select_camera_keeps_a_first_build_it_cannot_fault(opened: str | None) -> None:
    builds = _Builds([opened])
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


# --- the media-server wiring ------------------------------------------------------------


class _Control:
    """Scripts what each read of the control returns, records every request, and can
    refuse the write the way the bus does."""

    def __init__(self, reads: list[bytes], *, refuse: bool = False) -> None:
        self._reads = iter(reads)
        self._refuse = refuse
        self.requests: list[tuple[Any, ...]] = []
        self.closed = False

    def get(self, selector: int, request: int, length: int) -> bytes:
        self.requests.append(("get", selector, request, length))
        return next(self._reads)

    def set(self, selector: int, data: bytes) -> None:
        self.requests.append(("set", selector, data))
        if self._refuse:
            raise OSError(32, "Pipe error")

    def close(self) -> None:
        self.closed = True


_SUPPORTED = uvc.CameraTerminal(terminal_id=1, ae_priority=True)


class _FakeServer:
    """Upstream's `GstMediaServer` as the check sees it: the camera path, the sender
    pipeline the opened device is read from, and `stop()`."""

    def __init__(self, cam_path: str) -> None:
        self._cam_path = cam_path
        self._pipeline_sender = object()
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


def _starts(log: list[str], results: list[bool] | None = None) -> Any:
    """Upstream's start as a recorder: each call logs the camera path it started on and
    returns the next scripted result (True when the script runs out)."""
    outcomes = iter(results or [])

    def start(server: _FakeServer) -> bool:
        log.append(server._cam_path)
        return next(outcomes, True)

    return start


def _macos(
    monkeypatch: pytest.MonkeyPatch,
    names: list[str | None],
    count: int,
    camera: Any = None,
) -> None:
    """A macOS start: the device names each build opens, the device count — and the
    robot camera the exposure step finds on USB (``None``: no camera, so no test
    reaches the real bus; a callable: the finder itself, given the names)."""
    opened = iter(names)
    monkeypatch.setattr(real_daemon.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(real_daemon, "_video_device_count", lambda: count)
    monkeypatch.setattr(
        real_daemon, "_opened_device_name", lambda pipeline: next(opened)
    )
    finder = camera if callable(camera) else (lambda names: camera)
    monkeypatch.setattr(real_daemon, "robot_camera", finder)


def test_checked_start_enables_the_exposure_priority_after_the_selection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    control = _Control([b"\x00", b"\x01"])
    starts: list[str] = []

    def finder(names: Any) -> tuple[_Control, uvc.CameraTerminal]:
        assert starts == ["1", "0"]  # the camera is settled before the exposure step
        assert (
            tuple(names) == real_daemon.ROBOT_CAMERA_NAMES
        )  # found as upstream finds it
        return control, _SUPPORTED

    _macos(monkeypatch, ["Caméra du MacBook Pro", "Reachy Mini Camera"], 2, finder)
    server = _FakeServer("1")
    # the decision's own line comes from uvc.py: capture the package
    with caplog.at_level(logging.INFO, logger="reachy_mini_bridge"):
        assert real_daemon._checked_start(server, _starts(starts))
    assert [r[0] for r in control.requests] == ["get", "set", "get"]
    assert control.closed
    assert "auto-exposure priority enabled" in caplog.records[-1].getMessage()


def test_checked_start_runs_on_when_the_camera_cannot_be_reached_over_usb(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def finder(names: Any) -> None:
        raise RuntimeError("No backend available")

    _macos(monkeypatch, ["Reachy Mini Camera"], 1, finder)
    server = _FakeServer("0")
    starts: list[str] = []
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert real_daemon._checked_start(server, _starts(starts))
    assert starts == ["0"]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "No backend available" in caplog.records[0].getMessage()


def test_checked_start_says_nothing_without_a_robot_camera_on_usb(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _macos(monkeypatch, ["Reachy Mini Camera"], 1, camera=None)
    server = _FakeServer("0")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert real_daemon._checked_start(server, _starts([]))
    assert caplog.records == []


def test_checked_start_rebuilds_the_media_server_on_the_next_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _macos(monkeypatch, ["Caméra du MacBook Pro", "Reachy Mini Camera"], count=2)
    server = _FakeServer("1")
    starts: list[str] = []
    assert real_daemon._checked_start(server, _starts(starts))
    assert starts == ["1", "0"]  # the detected index, then the next one
    assert server.stops == 1  # upstream's stop() before the rebuild, so it builds anew
    assert server._cam_path == "0"  # the index the daemon now runs on


def test_checked_start_is_satisfied_by_a_first_build_that_opened_the_robot_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _macos(monkeypatch, ["Reachy Mini Camera"], count=2)
    server = _FakeServer("0")
    starts: list[str] = []
    assert real_daemon._checked_start(server, _starts(starts))
    assert starts == ["0"]
    assert server.stops == 0


def test_checked_start_returns_a_refused_start_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream's start returning False left the pipeline stopped: the wrapper hands the
    False back for upstream's callers to retry, and reads no device and touches no camera
    (specs/daemon/real_daemon.md "The camera check")."""
    monkeypatch.setattr(real_daemon.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        real_daemon, "_opened_device_name", lambda pipeline: pytest.fail("probed")
    )
    monkeypatch.setattr(real_daemon, "robot_camera", lambda names: pytest.fail("USB"))
    server = _FakeServer("1")
    starts: list[str] = []
    assert not real_daemon._checked_start(server, _starts(starts, [False]))
    assert starts == ["1"] and server.stops == 0


def test_checked_start_returns_the_result_of_the_last_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild upstream refuses to start leaves the media stopped: the wrapper returns
    that build's False, not the first build's True, so upstream retries it."""
    # The refused rebuild opens nothing: no name, which the selection accepts.
    _macos(monkeypatch, ["Caméra du MacBook Pro", None], count=2)
    server = _FakeServer("1")
    starts: list[str] = []
    assert not real_daemon._checked_start(server, _starts(starts, [True, False]))
    assert starts == ["1", "0"] and server.stops == 1


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
    monkeypatch.setattr(real_daemon, "robot_camera", lambda names: pytest.fail("USB"))
    server = _FakeServer(cam_path)
    starts: list[str] = []
    assert real_daemon._checked_start(server, _starts(starts))
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
        lambda server, start: calls.append((server, start)) or False,
    )
    server = object()
    assert (
        wrapped(server) is False
    )  # upstream's callers read the wrapped start's result
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


@pytest.mark.parametrize(
    ("args", "forwarded"),
    [
        (
            ["--no-preload-datasets", "--kinematics-engine", "Placo"],
            ["--no-preload-datasets", "--kinematics-engine", "Placo"],
        ),
        ([], ["--preload-datasets"]),
    ],
    ids=["flags forwarded", "defaults"],
)
def test_run_real_daemon_rewrites_argv_and_installs_the_camera_check(
    monkeypatch: pytest.MonkeyPatch, args: list[str], forwarded: list[str]
) -> None:
    argv, installed = _run(args, monkeypatch)
    assert argv == ["reachy-mini-daemon", *forwarded]
    assert installed == 1


def test_the_launcher_reports_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /api/bridge/daemon` on the app upstream builds: the launcher's name."""
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from reachy_mini.daemon.app import main as upstream_main

    monkeypatch.setattr(
        upstream_main, "create_app", lambda *a, **kw: fastapi.FastAPI(title="upstream")
    )
    _run([], monkeypatch)
    upstream_args: Any = SimpleNamespace()
    app = upstream_main.create_app(upstream_args, None)
    answer = testclient.TestClient(app).get("/api/bridge/daemon").json()
    assert answer == {"launcher": "real"}
