"""Fast-tier tests for the shipped testing harness (`reachy_mini_bridge.testing`).

Deterministic and daemon-free: they exercise the skip gates, the public re-exports, the
plugin's fixture registration, the target→backend resolution, the per-target daemon
bring-up decisions (library lifecycle scripted), and the gravity-compensation probe (daemon
answers scripted) — none of which needs a live daemon. The `live_bridge` fixture itself (which *does* need a daemon) is exercised by the
e2e tier, not here.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from reachy_mini_bridge import daemon, testing
from reachy_mini_bridge import face_detection as face_detection_module
from reachy_mini_bridge import robot as robot_module
from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.config import (
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
    require_env,
    requires_caps,
)
from reachy_mini_bridge.testing.sim_scene import BodyState
from reachy_mini_bridge.testing.support import (
    require_env as support_require_env,
)
from reachy_mini_bridge.testing.support import (
    requires_caps as support_requires_caps,
)

# --- public re-exports ---


def test_package_reexports_the_public_names():
    assert testing.require_env is support_require_env
    assert testing.requires_caps is support_requires_caps
    assert set(testing.__all__) == {
        "BridgeLoop",
        "LiveBridge",
        "require_env",
        "requires_caps",
    }


# --- requires_caps skip gate ---


def test_requires_caps_skips_when_a_needed_cap_is_absent():
    live = (object(), frozenset({"motion"}))
    with pytest.raises(pytest.skip.Exception) as excinfo:
        requires_caps(live, "audio")
    assert "audio" in str(excinfo.value)


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


def test_require_env_skips_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RMB_TEST_TOKEN", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_env("RMB_TEST_TOKEN")


def test_require_env_skips_when_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RMB_TEST_TOKEN", "")
    with pytest.raises(pytest.skip.Exception):
        require_env("RMB_TEST_TOKEN")


# --- plugin registers the fixtures (without spawning a daemon) ---


def test_live_bridge_and_daemon_are_module_scoped_fixtures():
    # Importing the plugin module registers the fixtures without touching a daemon.
    # `@pytest.fixture` wraps each in a FixtureFunctionDefinition carrying its marker;
    # assert both are fixtures *and* module-scoped (one daemon per test file, per spec).
    for fixture in (fixtures.live_bridge, fixtures._live_daemon, fixtures.sim_scene):
        marker = getattr(fixture, "_fixture_function_marker", None)
        assert marker is not None, f"{fixture!r} is not a pytest fixture"
        assert marker.scope == "module"


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
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)
    assert next(_daemon.managed_daemon("real")) == ("127.0.0.1", 8000)
    assert spawns.calls == [("real", "127.0.0.1", 8000)]


def test_real_target_borrows_a_ready_daemon(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REACHY_MINI_HOST", raising=False)
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=True, spawns=spawns)
    assert next(_daemon.managed_daemon("real")) == ("127.0.0.1", 8000)
    assert spawns.calls == []


def test_real_target_skips_a_remote_address_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("REACHY_MINI_HOST", "192.168.1.5")
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)
    with pytest.raises(pytest.skip.Exception, match="192.168.1.5"):
        next(_daemon.managed_daemon("real"))
    assert spawns.calls == []


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
    spawns = _RecordedSpawns()
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)

    lifecycle = _daemon.managed_daemon("sim")
    assert next(lifecycle) == ("127.0.0.1", 8000)
    (config,) = spawns.configs
    assert spawns.calls == [("sim", "127.0.0.1", 8000)]
    assert config.spawn == "auto" and config.headless is not viewer
    # The viewer sim draws the faces the bridge sends it; headless has no viewer.
    assert config.sim_displays.enabled() == (["face_markers"] if viewer else [])
    assert config.scene is not None and config.scene.endswith("scene.xml")
    scene = Path(config.scene)
    assert scene.is_file()
    assert 'name="face_1"' in scene.read_text(encoding="utf-8")
    with pytest.raises(StopIteration):
        next(lifecycle)
    assert not scene.exists()


def test_a_daemon_that_cannot_start_skips(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REACHY_MINI_HOST", raising=False)
    spawns = _RecordedSpawns(error=DaemonError("real daemon exited during startup"))
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)
    with pytest.raises(pytest.skip.Exception, match="exited during startup"):
        next(_daemon.managed_daemon("real"))


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
