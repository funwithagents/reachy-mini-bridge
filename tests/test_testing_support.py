"""Fast-tier tests for the shipped testing harness (`reachy_mini_bridge.testing`).

Deterministic and daemon-free: they exercise the skip gates, the plugin's fixture
registration, the target→backend resolution, the per-target daemon bring-up decisions
(library lifecycle scripted), and the gravity-compensation probe (daemon answers
scripted) — none of which needs a live daemon. The `live_bridge` fixture itself (which *does* need a daemon) is exercised by the
e2e tier, not here.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import daemon
from reachy_mini_bridge import face_detection as face_detection_module
from reachy_mini_bridge import robot as robot_module
from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.camera import CameraFeed
from reachy_mini_bridge.config import (
    KINEMATICS_ENGINES,
    DaemonConfig,
    FaceDetectionSettings,
    MotionSettings,
    ReachyMiniConfig,
)
from reachy_mini_bridge.errors import DaemonError, SimSceneError
from reachy_mini_bridge.face_detection import PixelFace
from reachy_mini_bridge.testing import (
    BridgeLoop,
    LiveBridge,
    _daemon,
    fixtures,
    gaze,
    require_env,
    requires_caps,
)
from reachy_mini_bridge.testing.sim_scene import BodyState

# --- requires_caps skip gate ---


def test_requires_caps_reports_every_missing_cap():
    live = (object(), frozenset({"motion"}))
    with pytest.raises(pytest.skip.Exception) as excinfo:
        requires_caps(live, "audio", "camera")
    message = str(excinfo.value)
    assert "audio" in message and "camera" in message


def test_requires_caps_does_not_skip_when_all_present():
    live = (object(), frozenset({"motion", "audio"}))
    try:
        requires_caps(live, "motion", "audio")
    except pytest.skip.Exception:  # pragma: no cover - a wrong skip is the failure
        pytest.fail("requires_caps skipped despite every capability being present")


# --- require_env skip gate ---


def test_require_env_returns_the_value_when_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RMB_TEST_TOKEN", "secret-123")
    assert require_env("RMB_TEST_TOKEN") == "secret-123"


@pytest.mark.parametrize("value", [None, ""])
def test_require_env_skips_when_unset_or_empty(
    monkeypatch: pytest.MonkeyPatch, value: str | None
):
    if value is None:
        monkeypatch.delenv("RMB_TEST_TOKEN", raising=False)
    else:
        monkeypatch.setenv("RMB_TEST_TOKEN", value)
    with pytest.raises(pytest.skip.Exception):
        require_env("RMB_TEST_TOKEN")


# --- plugin registers the fixtures (without spawning a daemon) ---


def test_the_daemon_is_the_runs_and_the_bridge_session_the_modules():
    # Importing the plugin module registers the fixtures without touching a daemon.
    # `@pytest.fixture` wraps each in a FixtureFunctionDefinition carrying its marker.
    # One daemon per pytest run, one bridge session (and scene client) per test file
    # (specs/testing/testing.md "Daemon lifecycle").
    scopes = {
        fixtures._live_daemon: "session",
        fixtures.live_bridge: "module",
        fixtures.sim_scene: "module",
        fixtures.face_scene: "function",
        fixtures.emotions_library: "function",
    }
    for fixture, scope in scopes.items():
        marker = getattr(fixture, "_fixture_function_marker", None)
        assert marker is not None, f"{fixture!r} is not a pytest fixture"
        assert marker.scope == scope


# --- the convergence kit's pure parts (specs/testing/testing_support.md "Public surface") ---


def _track(yaws: Sequence[float], start: float, expected: float) -> gaze.Track:
    track = gaze.Track("synthetic", start, expected, None)
    track.samples = [(y, 0.0) for y in yaws]
    track.times = [0.1 * i for i in range(len(yaws))]
    return track


def test_expected_yaw_is_the_heading_of_the_face_from_the_pivot():
    assert gaze.expected_yaw_deg(0.15) == pytest.approx(18.43, abs=0.01)
    assert gaze.expected_yaw_deg(-0.15) == pytest.approx(-18.43, abs=0.01)
    assert gaze.expected_yaw_deg(0.15, distance=0.35) == pytest.approx(23.2, abs=0.1)
    assert gaze.face_at(0.15) == (
        gaze.DEFAULT_FACE_POS[0],
        0.15,
        gaze.DEFAULT_FACE_POS[2],
    )


def test_yaw_pitch_read_back_a_rotated_pose():
    yaw, pitch = math.radians(20.0), math.radians(-10.0)
    rz = np.array(
        [
            [math.cos(yaw), -math.sin(yaw), 0],
            [math.sin(yaw), math.cos(yaw), 0],
            [0, 0, 1],
        ]
    )
    ry = np.array(
        [
            [math.cos(pitch), 0, math.sin(pitch)],
            [0, 1, 0],
            [-math.sin(pitch), 0, math.cos(pitch)],
        ]
    )
    pose = np.eye(4)
    pose[:3, :3] = rz @ ry
    got_yaw, got_pitch = gaze.yaw_pitch_deg(pose)
    assert got_yaw == pytest.approx(20.0, abs=1e-6)
    assert got_pitch == pytest.approx(-10.0, abs=1e-6)
    assert gaze.angle_from_neutral_deg(pose) > 20.0
    assert gaze.angle_from_neutral_deg(np.eye(4)) == pytest.approx(0.0)


def test_a_track_measures_overshoot_and_the_swing_back():
    # Turning from 0 to 18: past the face by 4 deg once, then creeping onto it.
    onto = _track([0, 6, 12, 18, 22, 21, 20, 19, 18, 18], start=0.0, expected=18.0)
    assert onto.overshoot_deg == pytest.approx(4.0)
    assert onto.swing_back_deg == pytest.approx(0.0)
    # Past the face by 4, then back 3 short of it: an oscillation.
    swung = _track([0, 6, 12, 18, 22, 18, 15, 16, 17, 18], start=0.0, expected=18.0)
    assert swung.overshoot_deg == pytest.approx(4.0)
    assert swung.swing_back_deg == pytest.approx(3.0)
    # The settled yaw is the mean over the last SETTLE_WINDOW_S only.
    settled = _track([0] * 30 + [18] * 21, start=0.0, expected=18.0)  # 0.1 s apart
    assert settled.yaw == pytest.approx(18.0)
    assert settled.settle_s == pytest.approx(5.0)
    # A face that did not move sideways has no overshoot to measure.
    still = _track([1, -1, 2, -2], start=0.0, expected=0.0)
    assert still.overshoot_deg == 0.0 and still.swing_back_deg == 0.0


# --- target / backend / address resolution (the consumer-facing env knobs) ---


def test_target_defaults_to_sim_and_normalizes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REACHY_MINI_E2E_TARGET", raising=False)
    assert _daemon.target() == "sim"
    monkeypatch.setenv("REACHY_MINI_E2E_TARGET", "  REAL ")
    assert _daemon.target() == "real"


def test_backend_is_real_only_for_the_real_target(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REACHY_MINI_E2E_TARGET", "real")
    assert _daemon.backend() == "real"
    monkeypatch.setenv("REACHY_MINI_E2E_TARGET", "sim")
    assert _daemon.backend() == "sim"
    monkeypatch.delenv("REACHY_MINI_E2E_TARGET", raising=False)
    assert _daemon.backend() == "sim"  # unknown/absent target ⇒ sim


def test_kinematics_engine_reads_env_with_the_analytical_default(
    monkeypatch: pytest.MonkeyPatch,
):
    """`REACHY_MINI_E2E_KINEMATICS` is the spawned daemon's `daemon.kinematics_engine`:
    unset or empty means upstream's analytical engine, each bridge word is taken whatever
    its case, and anything else *fails* the run — a typo must not run the suite on the
    wrong engine (specs/testing/testing_support.md "Configuration via the environment")."""
    monkeypatch.delenv("REACHY_MINI_E2E_KINEMATICS", raising=False)
    assert _daemon.kinematics_engine() == "analytical"
    monkeypatch.setenv("REACHY_MINI_E2E_KINEMATICS", "  ")
    assert _daemon.kinematics_engine() == "analytical"
    for engine in KINEMATICS_ENGINES:
        monkeypatch.setenv("REACHY_MINI_E2E_KINEMATICS", f" {engine.upper()} ")
        assert _daemon.kinematics_engine() == engine
    monkeypatch.setenv("REACHY_MINI_E2E_KINEMATICS", "AnalyticalKinematics")
    with pytest.raises(pytest.fail.Exception, match="analytical, placo, nn"):
        _daemon.kinematics_engine()


def test_address_reads_env_with_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REACHY_MINI_HOST", raising=False)
    monkeypatch.delenv("REACHY_MINI_PORT", raising=False)
    assert _daemon.address() == ("127.0.0.1", 8000)
    monkeypatch.setenv("REACHY_MINI_HOST", "192.168.1.5")
    monkeypatch.setenv("REACHY_MINI_PORT", "9100")
    assert _daemon.address() == ("192.168.1.5", 9100)


# --- daemon bring-up per target (library lifecycle scripted, no daemon) ---


class _RecordedSpawns:
    """Stands in for `daemon.managed_daemon`, recording each call's backend and address."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.configs: list[DaemonConfig] = []
        self.error = error

    @contextmanager
    def __call__(
        self, config: DaemonConfig, *, host: str, port: int, backend: str
    ) -> Iterator[daemon.DaemonHandle]:
        self.calls.append((backend, host, port))
        self.configs.append(config)
        if self.error is not None:
            raise self.error
        yield daemon.DaemonHandle(host, port, owned=True, pid=1)


def _patch_lifecycle(
    monkeypatch: pytest.MonkeyPatch, *, ready: bool, spawns: _RecordedSpawns
) -> None:
    monkeypatch.setattr(daemon, "is_daemon_ready", lambda host, port: ready)
    monkeypatch.setattr(daemon, "managed_daemon", spawns)


def test_real_target_spawns_a_real_daemon_on_loopback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REACHY_MINI_HOST", raising=False)
    monkeypatch.delenv("REACHY_MINI_PORT", raising=False)
    monkeypatch.setenv("REACHY_MINI_E2E_KINEMATICS", "placo")
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)
    assert next(_daemon.managed_daemon("real")) == ("127.0.0.1", 8000)
    assert spawns.calls == [("real", "127.0.0.1", 8000)]
    # The run's engine reaches the hardware daemon too: the gravity-compensation run.
    assert spawns.configs[0].kinematics_engine == "placo"


def test_real_target_borrows_a_ready_daemon(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REACHY_MINI_HOST", raising=False)
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=True, spawns=spawns)
    assert next(_daemon.managed_daemon("real")) == ("127.0.0.1", 8000)
    assert spawns.calls == []


def test_real_target_skips_a_remote_address_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("REACHY_MINI_E2E_REQUIRED_CAPS", raising=False)
    monkeypatch.setenv("REACHY_MINI_HOST", "192.168.1.5")
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)
    with pytest.raises(pytest.skip.Exception, match="192.168.1.5"):
        next(_daemon.managed_daemon("real"))
    assert spawns.calls == []


def test_a_remote_real_address_fails_when_capabilities_are_required(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("REACHY_MINI_E2E_REQUIRED_CAPS", "motion")
    monkeypatch.setenv("REACHY_MINI_HOST", "192.168.1.5")
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)
    with pytest.raises(pytest.fail.Exception, match="192.168.1.5.*requires motion"):
        next(_daemon.managed_daemon("real"))
    assert spawns.calls == []


@pytest.mark.parametrize("required", ["", "camera, audio"])
def test_a_daemon_that_cannot_come_up_skips_or_fails_as_required(
    monkeypatch: pytest.MonkeyPatch, required: str
):
    """An unavailable daemon provides no capability: a skip by default, a failure once
    the run requires any (the CI knob — specs/testing/ci.md)."""
    monkeypatch.setenv("REACHY_MINI_E2E_REQUIRED_CAPS", required)
    monkeypatch.delenv("REACHY_MINI_HOST", raising=False)
    spawns = _RecordedSpawns(error=DaemonError("real daemon exited during startup"))
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)
    outcome = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(outcome, match="exited during startup") as info:
        next(_daemon.managed_daemon("real"))
    assert spawns.calls == [("real", "127.0.0.1", 8000)]
    assert ("requires audio, camera" in str(info.value)) is bool(required)


def test_required_capabilities_parse_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REACHY_MINI_E2E_REQUIRED_CAPS", raising=False)
    assert _daemon.required_capabilities() == frozenset()
    monkeypatch.setenv("REACHY_MINI_E2E_REQUIRED_CAPS", " , ")
    assert _daemon.required_capabilities() == frozenset()
    monkeypatch.setenv("REACHY_MINI_E2E_REQUIRED_CAPS", "Motion, audio ,CAMERA,faces,")
    assert _daemon.required_capabilities() == frozenset(
        {"motion", "audio", "camera", "faces"}
    )


def test_check_required_capabilities_fails_naming_the_missing_ones(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("REACHY_MINI_E2E_REQUIRED_CAPS", raising=False)
    fixtures.check_required_capabilities(frozenset())  # nothing required: a no-op
    monkeypatch.setenv("REACHY_MINI_E2E_REQUIRED_CAPS", "motion,audio,camera,faces")
    fixtures.check_required_capabilities(
        frozenset({"motion", "audio", "camera", "faces", "face_markers"})
    )
    with pytest.raises(
        pytest.fail.Exception, match=r"camera, faces \(probed: audio, motion\)"
    ):
        fixtures.check_required_capabilities(frozenset({"motion", "audio"}))
    with pytest.raises(pytest.fail.Exception, match=r"probed: none"):
        fixtures.check_required_capabilities(frozenset())


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_robot_options_use_local_media_on_a_loopback_host(
    monkeypatch: pytest.MonkeyPatch, host: str
):
    monkeypatch.delenv("REACHY_MINI_E2E_MEDIA_BACKEND", raising=False)
    assert _daemon.robot_options(host, 8010) == {
        "connection_mode": "network",
        "host": host,
        "port": 8010,
        "media_backend": "local",
    }


def test_robot_options_leave_media_to_upstream_on_a_remote_host(
    monkeypatch: pytest.MonkeyPatch,
):
    """A wireless robot's daemon serves no local IPC media path: the harness hands
    upstream its `default`, which a network client auto-detects to WebRTC."""
    monkeypatch.delenv("REACHY_MINI_E2E_MEDIA_BACKEND", raising=False)
    options = _daemon.robot_options("192.168.1.5", 8000)
    assert options["media_backend"] == "default"
    assert (options["host"], options["port"]) == ("192.168.1.5", 8000)


def test_an_explicit_media_backend_overrides_the_locality_rule(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("REACHY_MINI_E2E_MEDIA_BACKEND", " no_media ")
    assert _daemon.robot_options("127.0.0.1", 8000)["media_backend"] == "no_media"
    assert _daemon.robot_options("192.168.1.5", 8000)["media_backend"] == "no_media"


@pytest.mark.parametrize("viewer", [False, True])
def test_a_spawned_sim_runs_the_test_scene_for_the_daemon_lifetime(
    monkeypatch: pytest.MonkeyPatch, viewer: bool
):
    """Every sim the harness spawns, headless or viewer, runs the bridge's test scene,
    written into a temporary directory that outlives the daemon (the file must exist
    while the daemon runs) and is removed afterwards."""
    pytest.importorskip("mujoco", reason="sim extra (mujoco) not installed")
    monkeypatch.delenv("REACHY_MINI_HOST", raising=False)
    if viewer:
        monkeypatch.setenv("REACHY_MINI_E2E_SIM_VIEWER", "1")
    else:
        monkeypatch.delenv("REACHY_MINI_E2E_SIM_VIEWER", raising=False)
    monkeypatch.setenv("REACHY_MINI_E2E_KINEMATICS", "nn" if viewer else "analytical")
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)

    lifecycle = _daemon.managed_daemon("sim")
    assert next(lifecycle) == ("127.0.0.1", 8000)
    (config,) = spawns.configs
    assert spawns.calls == [("sim", "127.0.0.1", 8000)]
    assert config.spawn == "auto" and config.headless is not viewer
    assert config.kinematics_engine == ("nn" if viewer else "analytical")
    # The viewer sim draws the faces the bridge sends it; headless has no viewer.
    assert config.sim_displays.enabled() == (["face_markers"] if viewer else [])
    assert config.scene is not None and config.scene.endswith("scene.xml")
    scene = Path(config.scene)
    assert scene.is_file()
    assert 'name="face_1"' in scene.read_text(encoding="utf-8")
    with pytest.raises(StopIteration):
        next(lifecycle)
    assert not scene.exists()


# --- camera capability probe: through the bridge's feed, never beside it ---


def test_camera_probe_reads_the_bridges_feed_and_never_get_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The feed is the one reader of upstream's one-shot `get_frame()`; a probe calling it
    beside the feed's thread would starve one or the other (specs/vision/camera.md). The
    probe reads a real `CameraFeed`, bound to a reader the way the bridge binds it."""
    monkeypatch.setattr(fixtures, "_CAMERA_PROBE_TIMEOUT", 1.0)
    monkeypatch.setattr(fixtures, "_AUDIO_PROBE_TIMEOUT", 0.05)

    class _Media:
        def get_frame(self) -> None:
            raise AssertionError("the probe must not read get_frame() beside the feed")

        def get_audio_sample(self) -> None:
            return None

    robot: Any = SimpleNamespace(
        client=SimpleNamespace(get_status=lambda: SimpleNamespace(backend_status={})),
        media=_Media(),
    )
    reads = {"n": 0}

    def late_reader() -> tuple[npt.NDArray[np.uint8], float | None] | None:
        reads["n"] += 1
        if reads["n"] < 3:
            return None  # nothing yet: the probe keeps waiting on the feed
        return np.zeros((4, 4, 3), dtype=np.uint8), None

    feed = CameraFeed(late_reader, None)
    asyncio.run(feed.start())
    try:
        caps = fixtures._probe_capabilities(robot, None, feed)
    finally:
        asyncio.run(feed.stop())
    assert "camera" in caps and "motion" in caps and "audio" not in caps

    silent = CameraFeed(lambda: None, None)
    asyncio.run(silent.start())
    try:
        assert "camera" not in fixtures._probe_capabilities(robot, None, silent)
    finally:
        asyncio.run(silent.stop())
    # No feed given (a caller probing a bare robot): the capability is not claimed.
    assert "camera" not in fixtures._probe_capabilities(robot, None)


# --- faces capability probe ---


def test_faces_probe_needs_a_face_body_on_the_sim_scene_endpoint(
    monkeypatch: pytest.MonkeyPatch,
):
    """The `faces` capability is a portrait in the scene: any body of kind `face`
    (specs/testing/sim_scene.md "The testing harness")."""

    class _Client:
        def __init__(self, host: str, port: int) -> None:
            self.address = (host, port)

        def bodies(self) -> dict[str, BodyState]:
            return answers[self.address]

    def body(name: str, kind: str) -> BodyState:
        return BodyState(name, (0, 0, 0), (1, 0, 0, 0), False, False, kind)

    answers: dict[tuple[str, int], dict[str, BodyState]] = {
        ("127.0.0.1", 8000): {"face_2": body("face_2", "face")},
        ("127.0.0.1", 8001): {"duck_1": body("duck_1", "duck")},
    }
    monkeypatch.setattr(fixtures, "SimSceneClient", _Client)
    assert fixtures._probe_faces("127.0.0.1", 8000)
    assert not fixtures._probe_faces("127.0.0.1", 8001)


def test_faces_probe_absent_without_the_endpoint(monkeypatch: pytest.MonkeyPatch):
    class _NoEndpoint:
        def __init__(self, host: str, port: int) -> None:
            pass

        def bodies(self) -> dict[str, object]:
            raise SimSceneError("no inject endpoint")

    monkeypatch.setattr(fixtures, "SimSceneClient", _NoEndpoint)
    assert not fixtures._probe_faces("127.0.0.1", 8000)


# --- face_markers capability probe, and the bridge's side of it ---


def test_face_markers_probe_is_the_displays_route(monkeypatch: pytest.MonkeyPatch):
    """The `face_markers` capability is the daemon answering the face markers route;
    a daemon launched without the display (or none at all) lacks it."""
    states: dict[int, object] = {8000: {"age_s": None, "markers": []}, 8001: None}
    monkeypatch.setattr(fixtures, "fetch_face_markers", lambda host, port: states[port])
    assert fixtures._probe_face_markers("127.0.0.1", 8000)
    assert not fixtures._probe_face_markers("127.0.0.1", 8001)


def test_the_bridge_sends_face_markers_on_the_viewer_sim_only(
    monkeypatch: pytest.MonkeyPatch,
):
    """`live_bridge`'s config turns the face markers on where the harness's daemon
    draws them — the viewer sim — and nowhere else."""
    monkeypatch.delenv("REACHY_MINI_E2E_TARGET", raising=False)
    monkeypatch.setenv("REACHY_MINI_E2E_SIM_VIEWER", "1")
    viewer = fixtures._bridge_daemon_config()
    assert viewer.spawn == "never" and not viewer.headless
    assert viewer.sim_displays.enabled() == ["face_markers"]
    monkeypatch.delenv("REACHY_MINI_E2E_SIM_VIEWER")
    assert fixtures._bridge_daemon_config().sim_displays.enabled() == []
    monkeypatch.setenv("REACHY_MINI_E2E_SIM_VIEWER", "1")
    monkeypatch.setenv("REACHY_MINI_E2E_TARGET", "real")
    assert fixtures._bridge_daemon_config().sim_displays.enabled() == []


# --- gravity_compensation capability probe ---


class _StatusRobot:
    """Just enough of a network robot for the probe: `client.get_status()`, host, port."""

    def __init__(self, *, sim: bool = False, mockup: bool = False) -> None:
        status = SimpleNamespace(simulation_enabled=sim, mockup_sim_enabled=mockup)
        self.client = SimpleNamespace(
            get_status=lambda: status, host="127.0.0.1", port=8000
        )


def _probe(robot: object) -> bool:
    return fixtures._probe_gravity_compensation(robot)  # pyright: ignore[reportArgumentType]


def _serve_engine(monkeypatch: pytest.MonkeyPatch, engine: str) -> list[str]:
    fetched: list[str] = []

    def fetch(robot: object, path: str) -> object:
        fetched.append(path)
        return {"info": {"engine": engine, "collision check": False}}

    monkeypatch.setattr(robot_module, "fetch_daemon_json", fetch)
    return fetched


def test_gravity_compensation_needs_hardware_on_placo(monkeypatch: pytest.MonkeyPatch):
    fetched = _serve_engine(monkeypatch, "Placo")
    assert _probe(_StatusRobot()) is True
    assert fetched == ["/api/kinematics/info"]


def test_gravity_compensation_absent_on_the_default_engine(
    monkeypatch: pytest.MonkeyPatch,
):
    _serve_engine(monkeypatch, "AnalyticalKinematics")
    assert _probe(_StatusRobot()) is False


def test_gravity_compensation_absent_on_a_simulation(monkeypatch: pytest.MonkeyPatch):
    fetched = _serve_engine(monkeypatch, "Placo")
    assert _probe(_StatusRobot(sim=True)) is False
    assert _probe(_StatusRobot(mockup=True)) is False
    assert fetched == []  # decided from the status alone


def test_gravity_compensation_absent_when_the_daemon_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail(robot: object, path: str) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(robot_module, "fetch_daemon_json", fail)
    assert _probe(_StatusRobot()) is False


# --- audio capability probe ---


class _SharedPipelineMedia:
    """Upstream's shared record+play pipeline as it behaves on macOS.

    Opened on the robot's card; once stopped, the next start reopens on the host default.
    Samples are tagged with the device they were captured from.
    """

    def __init__(self, *, yields_samples: bool = True) -> None:
        self.device = "robot"
        self.running = True  # the bridge's MediaSession already started it
        self.yields_samples = yields_samples

    def start_recording(self) -> None:
        if not self.running:
            self.device = "host-default"
        self.running = True

    def stop_recording(self) -> None:
        self.running = False

    def get_audio_sample(self) -> object:
        if not (self.running and self.yields_samples):
            return None
        return SimpleNamespace(size=320, device=self.device)


def test_audio_probe_reads_the_open_session_without_restarting_it():
    media = _SharedPipelineMedia()
    assert fixtures._probe_audio(media) is True
    assert media.running
    sample = media.get_audio_sample()
    assert getattr(sample, "device", None) == "robot"  # still the robot's mic


def test_audio_probe_absent_when_no_sample_arrives(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fixtures, "_AUDIO_PROBE_TIMEOUT", 0.2)
    media = _SharedPipelineMedia(yields_samples=False)
    assert fixtures._probe_audio(media) is False
    assert media.running and media.device == "robot"


def test_capabilities_are_probed_once_per_daemon_and_reused(
    monkeypatch: pytest.MonkeyPatch,
):
    """The probes run on the first session over a daemon; every later session on the same
    address gets the same set without probing again (they are the daemon's, not a
    session's); another address is probed on its own."""
    monkeypatch.setattr(fixtures, "_PROBED", {})
    probed: list[tuple[str, int]] = []

    def probe(
        robot: object, address: tuple[str, int] | None = None, camera: object = None
    ) -> frozenset[str]:
        assert address is not None
        probed.append(address)
        return frozenset({"motion", f"port-{address[1]}"})

    monkeypatch.setattr(fixtures, "_probe_capabilities", probe)
    first = fixtures.probed_capabilities(object(), ("127.0.0.1", 8000))  # pyright: ignore[reportArgumentType]
    again = fixtures.probed_capabilities(object(), ("127.0.0.1", 8000))  # pyright: ignore[reportArgumentType]
    other = fixtures.probed_capabilities(object(), ("127.0.0.1", 8010))  # pyright: ignore[reportArgumentType]
    assert first == again == {"motion", "port-8000"}
    assert other == {"motion", "port-8010"}
    assert probed == [("127.0.0.1", 8000), ("127.0.0.1", 8010)]


# --- the harness loop (specs/testing/testing_support.md "Public surface") -----------------
#
# The bridge is loop-bound: its detection loop is an asyncio task on the loop that ran
# `start()`. `BridgeLoop` keeps that loop alive across a module's synchronous tests, so a
# fixture-started bridge keeps detecting and tracking between the tests' own calls.


class _Blinking:
    """A stub detector: a face that moves a little on every call, so every observation
    is a fresh report (``faces.value.ts`` advances) and the tracker engages it."""

    calls = 0

    def detect(self, frame_bgr: object, ts: float) -> list[PixelFace]:
        _Blinking.calls += 1
        u = 31.5 + (_Blinking.calls % 3)
        return [PixelFace(bbox=(u - 5, 15.5, 10, 16), nose=(u, 23.5))]


def _tracking_fake() -> ReachyMiniBridge:
    return ReachyMiniBridge(
        ReachyMiniConfig(
            backend="fake",
            face_detection=FaceDetectionSettings(
                detector="custom", enabled=True, face_detector=_Blinking
            ),
            motion=MotionSettings(tracking=True),
        )
    )


def test_a_bridge_on_the_harness_loop_keeps_detecting_between_synchronous_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(face_detection_module, "FACE_POLL_HZ", 20.0)
    bridge = _tracking_fake()
    with BridgeLoop() as loop:
        loop.run(bridge.start())
        detection = bridge._detection
        assert detection is not None and detection.running
        time.sleep(0.3)  # a plain wait, no coroutine of ours running
        first = bridge.faces.value
        time.sleep(0.3)
        second = bridge.faces.value
        assert first.active and second.active
        assert second.ts > first.ts  # observations kept coming while nobody awaited
        assert bridge.head_tracking.value.attention == "engaged"
        loop.run(bridge.stop())
        assert not detection.running
        assert bridge.faces.value.active is False
    assert not loop.running


def test_bridge_loop_run_propagates_exceptions_and_refuses_once_stopped() -> None:
    async def boom() -> None:
        raise ValueError("from the loop")

    async def answer() -> int:
        await asyncio.sleep(0.01)
        return 42

    loop = BridgeLoop()
    loop.start()
    try:
        assert loop.run(answer()) == 42
        with pytest.raises(ValueError, match="from the loop"):
            loop.run(boom())
        with pytest.raises(TimeoutError):
            loop.run(asyncio.sleep(5.0), timeout=0.05)
    finally:
        loop.stop()
    with pytest.raises(RuntimeError, match="not running"):
        loop.run(answer())


def test_bridge_loop_stop_cancels_what_a_test_left_running() -> None:
    cancelled = threading.Event()

    async def forever() -> None:
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def leave_it() -> None:
        asyncio.get_running_loop().create_task(forever())

    loop = BridgeLoop()
    loop.start()
    loop.run(leave_it())
    loop.stop()
    assert cancelled.is_set()


def test_live_bridge_unpacks_and_gates_like_the_tuple_did() -> None:
    bridge = ReachyMiniBridge("fake")
    loop = BridgeLoop()
    live = LiveBridge(bridge, frozenset({"motion"}), loop)
    unpacked, caps = live
    assert unpacked is bridge and caps == frozenset({"motion"})
    requires_caps(live, "motion")  # no skip
    with pytest.raises(pytest.skip.Exception):
        requires_caps(live, "audio")
    with pytest.raises(RuntimeError, match="not running"):
        live.run(asyncio.sleep(0))
