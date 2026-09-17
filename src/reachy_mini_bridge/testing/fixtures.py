"""The importable pytest plugin: the ``live_api`` fixture and its capability probe.

A consumer opts in from their own (root) ``conftest.py``::

    pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]

then writes target-agnostic e2e tests that gate on probed capabilities::

    from reachy_mini_bridge.testing import requires_caps

    def test_it_speaks(live_api):
        requires_caps(live_api, "audio")
        api, _caps = live_api
        ...

``live_api`` resolves the target (``REACHY_MINI_E2E_TARGET`` = ``sim`` default | ``real``),
brings a daemon up under own-it-or-borrow-it (see ``_daemon``), builds a ``ReachyMiniApi``
over it, *probes* capabilities against the live daemon, and yields ``(api, capabilities)``.
See ../../../specs/testing_support.md for the strategy and
../../../docs/running-the-sim-daemon.md for the launch recipes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest

from reachy_mini_bridge.api import ReachyMiniApi, _daemon_kinematics_engine
from reachy_mini_bridge.config import ReachyMiniConfig
from reachy_mini_bridge.errors import SimSceneError
from reachy_mini_bridge.robot import AnyReachyMini
from reachy_mini_bridge.testing import _daemon
from reachy_mini_bridge.testing.sim_scene import SimSceneClient

_AUDIO_PROBE_TIMEOUT = 5.0
_CAMERA_PROBE_TIMEOUT = 5.0


# --- capability probing (against the live daemon, at setup) ---


def _probe_audio(media: Any) -> bool:
    """True if the open media session yields a real mic sample within the timeout.

    Runs after the api's ``MediaSession`` has started recording and playback, and never
    starts or stops the pipeline itself: upstream records and plays through one shared
    pipeline whose device binding does not survive a restart on macOS — a stop/start
    reopens both on the system default speaker and mic (docs/reachy-mini-api.md).
    """
    try:
        deadline = time.monotonic() + _AUDIO_PROBE_TIMEOUT
        while time.monotonic() < deadline:
            sample = media.get_audio_sample()
            if sample is not None and getattr(sample, "size", 1) > 0:
                return True
            time.sleep(0.1)
        return False
    except Exception:  # noqa: BLE001
        return False


def _probe_camera(media: Any) -> bool:
    """True if a camera frame comes back within the timeout (needs a GL context)."""
    try:
        deadline = time.monotonic() + _CAMERA_PROBE_TIMEOUT
        while time.monotonic() < deadline:
            frame = media.get_frame()
            if frame is not None and getattr(frame, "size", 1) > 0:
                return True
            time.sleep(0.1)
        return False
    except Exception:  # noqa: BLE001
        return False


def _probe_gravity_compensation(robot: AnyReachyMini) -> bool:
    """True if the daemon holds gravity compensation: hardware on the Placo engine.

    The sim ignores the mode (nothing to test), and any other engine makes the api refuse
    it — so the probe never sends the command. It reads the daemon status (not a
    simulation) and the engine through the api's own read.
    """
    try:
        status = robot.client.get_status()
        if status.simulation_enabled or status.mockup_sim_enabled:
            return False
        return _daemon_kinematics_engine(robot) == "Placo"
    except Exception:  # noqa: BLE001  (unreadable ⇒ capability absent)
        return False


def _probe_faces(host: str, port: int) -> bool:
    """True if the daemon serves the bridge's sim-scene endpoint with a `face` body:
    it was launched on the bridge's test scene (`REACHY_MINI_E2E_SIM_SCENE=test`,
    specs/sim_scene.md), so tests can show, move and hide a face in front of the eye
    camera. A daemon launched any other way — headless or not — lacks it."""
    try:
        return "face" in SimSceneClient(host, port).bodies()
    except SimSceneError:
        return False


def _probe_capabilities(
    robot: AnyReachyMini, address: tuple[str, int] | None = None
) -> frozenset[str]:
    """Probe what the live daemon can actually do — never inferred from backend type.

    Environment quirks decide: audio needs a recording session (the api's), the sim camera
    needs a GL context, gravity compensation needs hardware on the Placo kinematics engine,
    etc. `faces` (probed at `address`, the daemon's HTTP port) means the daemon runs the
    bridge's generated face scene. `doa` (mic-array direction of arrival) is robot-only
    and reserved — left unprobed, so `requires_caps("doa")` skips on sim.
    """
    caps: set[str] = set()
    try:
        if robot.client.get_status().backend_status is not None:
            caps.add("motion")
    except Exception:  # noqa: BLE001, S110  (no status ⇒ no motion cap)
        pass

    # Typed loosely: the probes call it defensively (any failure ⇒ capability absent).
    media: Any = robot.media
    if _probe_audio(media):
        caps.add("audio")
    if _probe_camera(media):
        caps.add("camera")
    if _probe_gravity_compensation(robot):
        caps.add("gravity_compensation")
    if address is not None and _probe_faces(*address):
        caps.add("faces")
    return frozenset(caps)


# --- the fixtures ---


@pytest.fixture(scope="module")
def _live_daemon() -> Iterator[tuple[str, int]]:
    """A live daemon for the selected target (module-scoped: one per test file)."""
    yield from _daemon.managed_daemon(_daemon.target())


@pytest.fixture(scope="module")
def sim_scene(_live_daemon: tuple[str, int]) -> SimSceneClient:
    """A ``SimSceneClient`` on the fixture-managed daemon: show, place, move and hide
    the scriptable bodies of a bridge scene (specs/sim_scene.md). Only useful where
    ``live_api`` probed the ``faces`` capability — gate with ``requires_caps``."""
    host, port = _live_daemon
    return SimSceneClient(host, port)


@pytest.fixture(scope="module")
def live_api(
    _live_daemon: tuple[str, int],
) -> Iterator[tuple[ReachyMiniApi, frozenset[str]]]:
    """A connected ``ReachyMiniApi`` + its probed capability set, for the selected target.

    Builds the api against the fixture-managed daemon (no robot injection — construction
    stays backend-string-only per specs/robot.md) and probes capabilities through
    ``api.robot``. The api's async lifecycle is driven on a throwaway loop; tests run
    their own coroutines via ``asyncio.run`` (nothing in the api binds to a loop).

    Probing happens after ``__aenter__``, on the media pipeline the api's MediaSession
    already started, which the probes leave running (see ``_probe_audio``).
    """
    host, port = _live_daemon
    # Build the api on the target's own backend (`sim`/`real`) with the daemon left to
    # this harness (`daemon.spawn` stays "never"): the api connects as a plain network
    # client to the daemon `_live_daemon` already manages, so one daemon serves the whole
    # test module. See `_daemon.backend` for why the backend label is safe here.
    api = ReachyMiniApi(
        ReachyMiniConfig(
            backend=_daemon.backend(),
            robot={
                "connection_mode": "network",
                "host": host,
                "port": port,
                "media_backend": "local",
            },
        )
    )
    asyncio.run(api.__aenter__())
    try:
        caps = _probe_capabilities(api.robot, (host, port))
        yield api, caps
    finally:
        asyncio.run(api.__aexit__(None, None, None))
