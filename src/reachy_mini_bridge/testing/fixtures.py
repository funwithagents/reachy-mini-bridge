"""The importable pytest plugin: the ``live_bridge`` fixture and its capability probe,
the ``sim_scene`` client and the ``face_scene`` / ``emotions_library`` helpers.

A consumer opts in from their own (root) ``conftest.py``::

    pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]

then writes target-agnostic e2e tests that gate on probed capabilities::

    from reachy_mini_bridge.testing import requires_caps

    def test_it_speaks(live_bridge):
        requires_caps(live_bridge, "audio")
        bridge, _caps = live_bridge
        live_bridge.run(bridge.say("hello", my_synth))

``live_bridge`` resolves the target (``REACHY_MINI_E2E_TARGET`` = ``sim`` default | ``real``),
brings a daemon up under own-it-or-borrow-it (see ``_daemon``), builds a ``ReachyMiniBridge``
over it on one event loop that lives from ``start()`` to ``stop()`` (``BridgeLoop``), *probes*
capabilities against the live daemon, and yields a ``LiveBridge`` — ``(bridge, capabilities)``
plus ``run(coro)`` on that loop.
See ../../../specs/testing/testing_support.md for the strategy and
../../../docs/running-the-sim-daemon.md for the launch recipes.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from reachy_mini_bridge.bridge import ReachyMiniBridge, _daemon_kinematics_engine
from reachy_mini_bridge.config import (
    DaemonConfig,
    FaceDetectionSettings,
    MotionSettings,
    ReachyMiniConfig,
)
from reachy_mini_bridge.errors import SimSceneError
from reachy_mini_bridge.robot import AnyReachyMini
from reachy_mini_bridge.sim_displays import fetch_face_markers
from reachy_mini_bridge.testing import _daemon
from reachy_mini_bridge.testing.sim_scene import SimSceneClient
from reachy_mini_bridge.testing.support import BridgeLoop, LiveBridge, requires_caps

_AUDIO_PROBE_TIMEOUT = 5.0
# A fresh session's first frame usually arrives within a second on the viewer, but can take
# more than two on a daemon that has already served sessions (measured 2026-10-05: a 2 s
# wait lost the camera for one module in seven); the probe runs once per run, so 5 s is
# cheap. Headless no frame ever comes.
_CAMERA_PROBE_TIMEOUT = 5.0


# --- capability probing (against the live daemon, at setup) ---


def _probe_audio(media: Any) -> bool:
    """True if the open media session yields a real mic sample within the timeout.

    Runs after the bridge's ``MediaSession`` has started recording and playback, and never
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

    The sim ignores the mode (nothing to test), and any other engine makes the bridge refuse
    it — so the probe never sends the command. It reads the daemon status (not a
    simulation) and the engine through the bridge's own read.
    """
    try:
        status = robot.client.get_status()
        if status.simulation_enabled or status.mockup_sim_enabled:
            return False
        return _daemon_kinematics_engine(robot) == "Placo"
    except Exception:  # noqa: BLE001  (unreadable ⇒ capability absent)
        return False


def _probe_faces(host: str, port: int) -> bool:
    """True if the daemon serves the bridge's inject endpoint with a portrait (a body
    of kind `face`): it was launched on the bridge's test scene (every harness-spawned sim
    is, specs/testing/sim_scene.md), so tests can spawn, move and despawn faces in front of
    the eye camera. A daemon launched any other way lacks it."""
    try:
        bodies = SimSceneClient(host, port).bodies().values()
        return any(state.kind == "face" for state in bodies)
    except SimSceneError:
        return False


def _probe_face_markers(host: str, port: int) -> bool:
    """True if the daemon serves the face markers display: a viewer sim launched with
    ``--sim-display face_markers`` (every viewer sim the harness spawns is,
    specs/daemon/sim_displays.md), so a test can read back where the bridge places a face."""
    return fetch_face_markers(host, port) is not None


def _probe_capabilities(
    robot: AnyReachyMini, address: tuple[str, int] | None = None
) -> frozenset[str]:
    """Probe what the live daemon can actually do — never inferred from backend type.

    Environment quirks decide: audio needs a recording session (the bridge's), the sim camera
    needs a GL context, gravity compensation needs hardware on the Placo kinematics engine,
    etc. `faces` (probed at `address`, the daemon's HTTP port) means the daemon runs the
    bridge's generated face scene; `face_markers` that it draws the faces the bridge
    sends it. `doa` (mic-array direction of arrival) is robot-only
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
    if address is not None and _probe_face_markers(*address):
        caps.add("face_markers")
    return frozenset(caps)


_PROBED: dict[tuple[str, int], frozenset[str]] = {}


def probed_capabilities(
    robot: AnyReachyMini, address: tuple[str, int]
) -> frozenset[str]:
    """The capabilities of the daemon at ``address``, probed once per ``pytest`` run — on
    the first bridge session over it — and reused by every later session on it: they are
    the daemon's (its camera, its audio device, its kinematics engine, its scene and
    displays), not a session's, and probing them again on every module would only add
    the probes' waits to every file's setup (specs/testing/testing.md "The harness")."""
    if address not in _PROBED:
        _PROBED[address] = _probe_capabilities(robot, address)
    return _PROBED[address]


# --- the fixtures ---


@pytest.fixture(scope="session")
def _live_daemon() -> Iterator[tuple[str, int]]:
    """A live daemon for the selected target — session-scoped: one per ``pytest`` run,
    brought up by the first module that needs it and stopped when the run ends
    (specs/testing/testing.md "Daemon lifecycle"). The bridge session over it is each
    module's own (``live_bridge``)."""
    yield from _daemon.managed_daemon(_daemon.target())


@pytest.fixture(scope="module")
def sim_scene(_live_daemon: tuple[str, int]) -> SimSceneClient:
    """A ``SimSceneClient`` on the fixture-managed daemon: show, place, move and hide
    the scriptable bodies of a bridge scene (specs/testing/sim_scene.md). Only useful where
    ``live_bridge`` probed the ``faces`` capability — gate with ``requires_caps``."""
    host, port = _live_daemon
    return SimSceneClient(host, port)


def _bridge_daemon_config() -> DaemonConfig:
    """The bridge's side of the harness's sim displays: on the viewer sim the bridge
    sends its face markers to the daemon (specs/daemon/sim_displays.md). The daemon stays
    the harness's (``spawn`` is "never")."""
    displays = _daemon.sim_displays()
    if _daemon.backend() != "sim" or not displays.enabled():
        return DaemonConfig()
    return DaemonConfig(headless=False, sim_displays=displays)


@pytest.fixture(scope="module")
def live_bridge(
    _live_daemon: tuple[str, int],
) -> Iterator[LiveBridge]:
    """A connected ``ReachyMiniBridge`` + its probed capability set, for the selected target.

    Builds the bridge against the fixture-managed daemon (no robot injection — construction
    stays backend-string-only per specs/core/robot.md) and probes capabilities through
    ``bridge.robot``. The bridge's lifecycle runs on one event loop, on a background
    thread, from ``start()`` to ``stop()`` (the bridge is loop-bound: its detection loop
    is an asyncio task, its observables publish on the loop thread); a test runs its
    coroutines on that loop through ``live_bridge.run(...)``, never ``asyncio.run``.

    Probing happens after ``start()``, on the media pipeline the bridge's MediaSession
    already started, which the probes leave running (see ``_probe_audio``) — once per
    run: a later module's session reuses the set (``probed_capabilities``).
    """
    host, port = _live_daemon
    # Build the bridge on the target's own backend (`sim`/`real`) with the daemon left to
    # this harness (`daemon.spawn` stays "never"): the bridge connects as a plain network
    # client to the daemon `_live_daemon` already manages — the run's one daemon, this
    # module's own session over it. See `_daemon.backend` for why the label is safe here.
    # The live tier's subject is the robot that follows a face, so the config names the
    # shipped `yunet` detector with detection and tracking on (the defaults run no
    # detector — specs/vision/user_perception.md "Configuration"). The model downloads into the
    # Hugging Face cache on the first live run, as the emotions library does.
    bridge = ReachyMiniBridge(
        ReachyMiniConfig(
            backend=_daemon.backend(),
            robot={
                "connection_mode": "network",
                "host": host,
                "port": port,
                "media_backend": "local",
            },
            face_detection=FaceDetectionSettings(detector="yunet", enabled=True),
            motion=MotionSettings(tracking=True),
            daemon=_bridge_daemon_config(),
        )
    )
    with BridgeLoop() as loop:
        loop.run(bridge.start())
        try:
            caps = probed_capabilities(bridge.robot, (host, port))
            yield LiveBridge(bridge, caps, loop)
        finally:
            loop.run(bridge.stop())


@pytest.fixture
def face_scene(
    live_bridge: LiveBridge, sim_scene: SimSceneClient
) -> Iterator[SimSceneClient]:
    """The scene with nobody in view, for a test that puts faces in front of the robot:
    gated on ``camera`` and ``faces`` (it skips where ``live_bridge`` probed neither), the
    pool of portraits cleared before the test and again after it, so a test starts with an
    empty view whatever the previous one left and leaves none behind
    (specs/testing/sim_scene.md "A pool of portraits"). A test on a session of its own
    (not ``live_bridge``) writes the same four lines against that session."""
    requires_caps(live_bridge, "camera", "faces")
    sim_scene.clear()
    yield sim_scene
    sim_scene.clear()


@pytest.fixture
def emotions_library() -> None:
    """The client-side emotions library in the local Hugging Face cache, for a test that
    plays one: a cache hit, else a download (a one-time cost), else a skip (offline).

    The daemon preloads the datasets in the background, but ``play_emotion`` resolves the
    move on the client from the cache, so on a fresh machine a live test would fail on the
    miss; this fetches the library so the test genuinely exercises the move, skipping only
    when it truly cannot be fetched. It yields nothing: its value is the side effect."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    from reachy_mini.motion.recorded_move import DEFAULT_EMOTIONS_DATASET

    try:
        snapshot_download(
            DEFAULT_EMOTIONS_DATASET, repo_type="dataset", local_files_only=True
        )
    except LocalEntryNotFoundError:
        try:
            snapshot_download(DEFAULT_EMOTIONS_DATASET, repo_type="dataset")
        except Exception as exc:  # noqa: BLE001  (offline / fetch failure)
            pytest.skip(f"emotions dataset not cached and download failed: {exc}")
