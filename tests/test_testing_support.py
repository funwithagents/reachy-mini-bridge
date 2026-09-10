"""Fast-tier tests for the shipped testing harness (`reachy_mini_bridge.testing`).

Deterministic and daemon-free: they exercise the skip gates, the public re-exports, the
plugin's fixture registration, and the target→backend resolution — none of which needs a
live daemon. The `live_api` fixture itself (which *does* need a daemon) is exercised by the
e2e tier, not here.
"""

from __future__ import annotations

import pytest

from reachy_mini_bridge import testing
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
