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


# --- the camera exposure: the parse -----------------------------------------------------


def _vc_header() -> bytes:
    """A VideoControl header descriptor, as a camera's descriptors open with."""
    return bytes([13, 0x24, 0x01, 0x10, 0x01, 0, 0, 0, 0, 0, 0, 1, 1])


def _input_terminal(terminal_id: int, terminal_type: int, controls: bytes) -> bytes:
    """An input terminal descriptor of the camera layout (the focal-length words and
    ``bControlSize`` precede ``bmControls``)."""
    body = bytes(
        [0x24, 0x02, terminal_id, *terminal_type.to_bytes(2, "little"), 0, 0]
        + [0] * 6
        + [len(controls)]
    )
    return bytes([len(body) + 1 + len(controls)]) + body + controls


_PRIORITY_ADVERTISED = b"\x0e\x00\x00"  # bits 1–3: AE mode, AE priority, exposure time
_PRIORITY_MISSING = b"\x0a\x00\x00"  # bits 1 and 3 only


def test_camera_terminal_reads_the_camera_terminals_id_and_priority_bit() -> None:
    descriptors = (
        _vc_header()
        + _input_terminal(1, 0x0201, _PRIORITY_ADVERTISED)
        + bytes([9, 0x24, 0x05, 2, 1, 0, 0, 0, 0])  # a processing unit after it
    )
    assert real_daemon.camera_terminal(descriptors) == real_daemon.CameraTerminal(
        terminal_id=1, ae_priority=True
    )
    without = _vc_header() + _input_terminal(3, 0x0201, _PRIORITY_MISSING)
    assert real_daemon.camera_terminal(without) == real_daemon.CameraTerminal(
        terminal_id=3, ae_priority=False
    )


@pytest.mark.parametrize(
    "descriptors",
    [
        b"",
        _vc_header(),
        _vc_header() + _input_terminal(1, 0x0101, _PRIORITY_ADVERTISED),
        bytes([0, 0x24]) + _input_terminal(1, 0x0201, _PRIORITY_ADVERTISED),
    ],
    ids=[
        "nothing",
        "a header alone",
        "a streaming terminal only",
        "a zero-length block",
    ],
)
def test_camera_terminal_is_none_without_a_camera_terminal(descriptors: bytes) -> None:
    assert real_daemon.camera_terminal(descriptors) is None


def test_lite_camera_usb_id_is_upstreams() -> None:
    constants = pytest.importorskip("reachy_mini.media.camera_constants")
    specs = constants.ReachyMiniLiteCamSpecs
    assert (specs.vid, specs.pid) == real_daemon.LITE_CAMERA_USB_ID


# --- the camera exposure: the decision --------------------------------------------------


_PRIORITY = 0x03
_GET_CUR = 0x81


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


_SUPPORTED = real_daemon.CameraTerminal(terminal_id=1, ae_priority=True)


def test_enable_ae_priority_sets_the_flag_and_reads_it_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = _Control([b"\x00", b"\x01"])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert real_daemon.enable_ae_priority(control, _SUPPORTED) == "enabled"
    assert control.requests == [
        ("get", _PRIORITY, _GET_CUR, 1),
        ("set", _PRIORITY, b"\x01"),
        ("get", _PRIORITY, _GET_CUR, 1),
    ]
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    assert "frame rate" in caplog.records[0].getMessage()


def test_enable_ae_priority_leaves_a_flag_already_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = _Control([b"\x01"])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert real_daemon.enable_ae_priority(control, _SUPPORTED) == "already"
    assert control.requests == [("get", _PRIORITY, _GET_CUR, 1)]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]


def test_enable_ae_priority_leaves_a_terminal_without_the_control(
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = _Control([])
    terminal = real_daemon.CameraTerminal(terminal_id=1, ae_priority=False)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert real_daemon.enable_ae_priority(control, terminal) == "unsupported"
    assert control.requests == []
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "1/60 s" in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    ("control", "said"),
    [
        (_Control([b"\x00"], refuse=True), "refused"),
        (_Control([b"\x00", b"\x00"]), "reads back"),
    ],
    ids=["the bus refuses the write", "the flag does not take"],
)
def test_enable_ae_priority_warns_when_the_camera_does_not_take_it(
    caplog: pytest.LogCaptureFixture, control: _Control, said: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert real_daemon.enable_ae_priority(control, _SUPPORTED) == "refused"
    assert control.requests[1] == ("set", _PRIORITY, b"\x01")
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert said in caplog.records[0].getMessage()


# --- the media-server wiring ------------------------------------------------------------


class _FakePipeline:
    def __init__(self) -> None:
        self.states: list[Any] = []

    def set_state(self, state: Any) -> None:
        self.states.append(state)


def _macos(
    monkeypatch: pytest.MonkeyPatch,
    names: list[str | None],
    count: int,
    camera: Any = None,
) -> None:
    """A macOS start: the device names each build opens, the device count — and the
    Lite camera the exposure step finds on USB (``None``: no Lite camera, so no test
    reaches the real bus; a callable: the finder itself)."""
    opened = iter(names)
    monkeypatch.setattr(real_daemon.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(real_daemon, "_video_device_count", lambda: count)
    monkeypatch.setattr(real_daemon, "_gst_null", lambda: "NULL")
    monkeypatch.setattr(
        real_daemon, "_opened_device_name", lambda pipeline: next(opened)
    )
    finder = camera if callable(camera) else (lambda: camera)
    monkeypatch.setattr(real_daemon, "_lite_camera", finder)


def test_checked_start_enables_the_exposure_priority_after_the_selection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    control = _Control([b"\x00", b"\x01"])
    starts: list[str] = []

    def finder() -> tuple[_Control, real_daemon.CameraTerminal]:
        assert starts == ["1", "0"]  # the camera is settled before the exposure step
        return control, _SUPPORTED

    _macos(monkeypatch, ["Caméra du MacBook Pro", "Reachy Mini Camera"], 2, finder)
    server = SimpleNamespace(_cam_path="1", _pipeline_sender=_FakePipeline())
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        real_daemon._checked_start(server, lambda s: starts.append(s._cam_path))
    assert [r[0] for r in control.requests] == ["get", "set", "get"]
    assert control.closed
    assert "auto-exposure priority enabled" in caplog.records[-1].getMessage()


def test_checked_start_runs_on_when_the_camera_cannot_be_reached_over_usb(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def finder() -> None:
        raise RuntimeError("No backend available")

    _macos(monkeypatch, ["Reachy Mini Camera"], 1, finder)
    server = SimpleNamespace(_cam_path="0", _pipeline_sender=_FakePipeline())
    starts: list[str] = []
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        real_daemon._checked_start(server, lambda s: starts.append(s._cam_path))
    assert starts == ["0"]
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "No backend available" in caplog.records[0].getMessage()


def test_checked_start_says_nothing_without_a_lite_camera_on_usb(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _macos(monkeypatch, ["Reachy Mini Camera"], 1, camera=None)
    server = SimpleNamespace(_cam_path="0", _pipeline_sender=_FakePipeline())
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        real_daemon._checked_start(server, lambda s: None)
    assert caplog.records == []


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
    monkeypatch.setattr(real_daemon, "_lite_camera", lambda: pytest.fail("USB"))
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
