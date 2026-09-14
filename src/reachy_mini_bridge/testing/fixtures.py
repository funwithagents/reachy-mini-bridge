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

from reachy_mini_bridge.api import ReachyMiniApi
from reachy_mini_bridge.config import ReachyMiniConfig
from reachy_mini_bridge.robot import AnyReachyMini
from reachy_mini_bridge.testing import _daemon

_AUDIO_PROBE_TIMEOUT = 5.0
_CAMERA_PROBE_TIMEOUT = 5.0


# --- capability probing (against the live daemon, at setup) ---


def _probe_audio(media: Any) -> bool:
    """True if recording yields a real mic sample within the timeout.

    Restores the connect-time state (not recording) afterwards, so a later audio test
    starts from a clean slate and manages its own recording.
    """
    try:
        media.start_recording()
    except Exception:  # noqa: BLE001
        return False
    try:
        deadline = time.monotonic() + _AUDIO_PROBE_TIMEOUT
        while time.monotonic() < deadline:
            sample = media.get_audio_sample()
            if sample is not None and getattr(sample, "size", 1) > 0:
                return True
            time.sleep(0.1)
        return False
    finally:
        try:
            media.stop_recording()
        except Exception:  # noqa: BLE001, S110  (best-effort restore)
            pass


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


def _probe_capabilities(robot: AnyReachyMini) -> frozenset[str]:
    """Probe what the live daemon can actually do — never inferred from backend type.

    Environment quirks decide: audio needs `start_recording()` first, the sim camera
    needs a GL context, etc. `doa` (mic-array direction of arrival) is robot-only and
    reserved — left unprobed, so `requires_caps("doa")` skips on sim.
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
    return frozenset(caps)


# --- the fixtures ---


@pytest.fixture(scope="module")
def _live_daemon() -> Iterator[tuple[str, int]]:
    """A live daemon for the selected target (module-scoped: one per test file)."""
    yield from _daemon.managed_daemon(_daemon.target())


@pytest.fixture(scope="module")
def live_api(
    _live_daemon: tuple[str, int],
) -> Iterator[tuple[ReachyMiniApi, frozenset[str]]]:
    """A connected ``ReachyMiniApi`` + its probed capability set, for the selected target.

    Builds the api against the fixture-managed daemon (no robot injection — construction
    stays backend-string-only per specs/robot.md) and probes capabilities through
    ``api.robot``. The api's async lifecycle is driven on a throwaway loop; tests run
    their own coroutines via ``asyncio.run`` (nothing in the api binds to a loop).

    The capability probe's audio check stops recording in its cleanup, but the api's
    MediaSession opened it on ``__aenter__`` — so recording is restored before yielding,
    or the mic tap would see no samples (the conflict flagged in the plan).
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
        caps = _probe_capabilities(api.robot)
        if "audio" in caps:
            api.robot.media.start_recording()  # restore what the session needs
        yield api, caps
    finally:
        asyncio.run(api.__aexit__(None, None, None))
