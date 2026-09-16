"""Fast-tier tests for the shipped testing harness (`reachy_mini_bridge.testing`).

Deterministic and daemon-free: they exercise the skip gates, the public re-exports, the
plugin's fixture registration, the target→backend resolution, the per-target daemon
bring-up decisions (library lifecycle scripted), and the gravity-compensation probe (daemon
answers scripted) — none of which needs a live daemon. The `live_api` fixture itself (which *does* need a daemon) is exercised by the
e2e tier, not here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from reachy_mini_bridge import api as api_module
from reachy_mini_bridge import daemon, testing
from reachy_mini_bridge.config import DaemonConfig
from reachy_mini_bridge.errors import DaemonError
from reachy_mini_bridge.testing import _daemon, fixtures, require_env, requires_caps
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
    assert set(testing.__all__) == {"require_env", "requires_caps"}


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


def test_live_api_and_daemon_are_module_scoped_fixtures():
    # Importing the plugin module registers the fixtures without touching a daemon.
    # `@pytest.fixture` wraps each in a FixtureFunctionDefinition carrying its marker;
    # assert both are fixtures *and* module-scoped (one daemon per test file, per spec).
    for fixture in (fixtures.live_api, fixtures._live_daemon):
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
        self.error = error

    @contextmanager
    def __call__(
        self, config: DaemonConfig, *, host: str, port: int, backend: str
    ) -> Iterator[daemon.DaemonHandle]:
        self.calls.append((backend, host, port))
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


def test_a_daemon_that_cannot_start_skips(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REACHY_MINI_HOST", raising=False)
    spawns = _RecordedSpawns(error=DaemonError("real daemon exited during startup"))
    _patch_lifecycle(monkeypatch, ready=False, spawns=spawns)
    with pytest.raises(pytest.skip.Exception, match="exited during startup"):
        next(_daemon.managed_daemon("real"))


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

    def fetch(url: str) -> object:
        fetched.append(url)
        return {"info": {"engine": engine, "collision check": False}}

    monkeypatch.setattr(api_module, "_fetch_json", fetch)
    return fetched


def test_gravity_compensation_needs_hardware_on_placo(monkeypatch: pytest.MonkeyPatch):
    fetched = _serve_engine(monkeypatch, "Placo")
    assert _probe(_StatusRobot()) is True
    assert fetched == ["http://127.0.0.1:8000/api/kinematics/info"]


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
    def fail(url: str) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(api_module, "_fetch_json", fail)
    assert _probe(_StatusRobot()) is False


# --- audio capability probe ---


class _SharedPipelineMedia:
    """Upstream's shared record+play pipeline as it behaves on macOS.

    Opened on the robot's card; once stopped, the next start reopens on the host default.
    Samples are tagged with the device they were captured from.
    """

    def __init__(self, *, yields_samples: bool = True) -> None:
        self.device = "robot"
        self.running = True  # the api's MediaSession already started it
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
