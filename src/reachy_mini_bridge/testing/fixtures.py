"""The importable pytest plugin: the ``requires`` marker and its gate, the
``live_bridge`` fixture and its capability probe, ``live_capabilities``, the
``sim_scene`` client, the ``face_scene`` / ``emotions_library`` helpers and the
``live target`` summary.

A consumer opts in from their own (root) ``conftest.py``::

    pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]

(a conftest below the rootdir registers it from its ``pytest_configure`` instead:
``config.pluginmanager.import_plugin("reachy_mini_bridge.testing.fixtures")``), then
writes target-agnostic e2e tests that name the capabilities they need::

    pytestmark = pytest.mark.requires("audio")  # every test of the file

    @pytest.mark.requires("motion")  # this one needs the head too
    def test_it_nods_while_speaking(live_bridge):
        bridge, _caps = live_bridge
        live_bridge.run(bridge.say("hello", my_synth))

The gate skips a test whose capabilities the target lacks — before any of its fixtures
once the run has probed, so a file the target cannot serve never starts its bridge.

``live_bridge`` resolves the target (``REACHY_MINI_E2E_TARGET`` = ``sim`` default | ``real``),
brings a daemon up under own-it-or-borrow-it (see ``_daemon``), builds a ``ReachyMiniBridge``
over it on one event loop that lives from ``start()`` to ``stop()`` (``BridgeLoop``), *probes*
capabilities against the live daemon, and yields a ``LiveBridge`` — ``(bridge, capabilities)``
plus ``run(coro)`` on that loop.
See ../../../specs/testing/testing_support.md for the strategy and
../../../docs/guides/running-daemons.md for the launch recipes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator

import pytest

from reachy_mini_bridge.bridge import ReachyMiniBridge, _daemon_kinematics_engine
from reachy_mini_bridge.camera import CameraFeed
from reachy_mini_bridge.config import (
    DaemonConfig,
    FaceDetectionSettings,
    ReachyMiniConfig,
)
from reachy_mini_bridge.errors import SimSceneError
from reachy_mini_bridge.microphone import MicFeed
from reachy_mini_bridge.robot import AnyReachyMini
from reachy_mini_bridge.sim_displays import fetch_face_markers
from reachy_mini_bridge.testing import _daemon
from reachy_mini_bridge.testing.sim_scene import SimSceneClient
from reachy_mini_bridge.testing.support import (
    BridgeLoop,
    LiveBridge,
    check_capability_names,
    missing_reason,
    requires_caps,
)

MARKER = "requires"

_AUDIO_PROBE_TIMEOUT = 5.0
# The audio probe reads the bridge's mic feed, the one reader of upstream's one-shot
# `get_audio_sample()` (specs/audio/microphone.md), for the same reason the camera probe
# reads the camera feed: a second reader beside it would split the capture.
# The camera probe reads the bridge's camera feed — the one reader of upstream's one-shot
# `get_frame()` (specs/vision/camera.md) — never `get_frame()` itself: the feed's thread
# loops that call from the moment the bridge starts, and two readers of it starve each
# other silently (measured 2026-10-05: probing `get_frame()` beside the feed lost the
# camera for one CI run in two, for the whole run — the probe runs once per run). A
# fresh session's first frame usually arrives within a second, more than two on a daemon
# that has served sessions before; 5 s is cheap once per run. Headless on macOS no frame
# ever comes.
_CAMERA_PROBE_TIMEOUT = 5.0


# --- capability probing (against the live daemon, at setup) ---


def _probe_audio(mic: MicFeed) -> bool:
    """True if the bridge's mic feed publishes a non-empty chunk within the timeout.

    Runs after the bridge's ``MediaSession`` has started recording and playback, and never
    starts or stops the pipeline itself: upstream records and plays through one shared
    pipeline whose device binding does not survive a restart on macOS — a stop/start
    reopens both on the system default speaker and mic (docs/internals/upstream-sdk-notes.md).
    Read on the feed, the one reader of ``get_audio_sample()`` — never beside it.
    """
    try:
        deadline = time.perf_counter() + _AUDIO_PROBE_TIMEOUT
        while time.perf_counter() < deadline:
            chunk = mic.latest()
            if chunk is not None and chunk.samples.size > 0:
                return True
            time.sleep(0.1)
        return False
    except Exception:  # noqa: BLE001
        return False


def _probe_camera(feed: CameraFeed) -> bool:
    """True if the bridge's camera feed publishes a frame within the timeout (the sim
    needs a GL context for one; a robot always has one). Read on the feed, the one reader
    of upstream's ``get_frame()`` — never on ``get_frame()`` beside it."""
    try:
        deadline = time.perf_counter() + _CAMERA_PROBE_TIMEOUT
        while time.perf_counter() < deadline:
            if feed.latest() is not None:
                return True
            time.sleep(0.1)
        return False
    except Exception:  # noqa: BLE001
        return False


def _is_simulation(robot: AnyReachyMini) -> bool:
    """True if the daemon status reports a simulation, MuJoCo or mockup."""
    status = robot.client.get_status()
    return bool(status.simulation_enabled or status.mockup_sim_enabled)


def _probe_motor_states(robot: AnyReachyMini) -> bool:
    """True if the daemon honors `set_motors_state("enabled" / "disabled")`: hardware, a
    USB Lite or a wireless robot. A simulation ignores every motor-state change and keeps
    reporting `enabled` (specs/core/bridge.md "Motors").

    Read from the daemon status, never by switching: a probe that turned torque off at
    setup would drop a head no test has lowered yet.
    """
    try:
        return not _is_simulation(robot)
    except Exception:  # noqa: BLE001  (unreadable ⇒ capability absent)
        return False


def _probe_gravity_compensation(robot: AnyReachyMini) -> bool:
    """True if the daemon holds gravity compensation: hardware on the Placo engine — a
    harness-spawned daemon runs it with `REACHY_MINI_E2E_KINEMATICS=placo`.

    The sim ignores the mode (nothing to test), and any other engine makes the bridge refuse
    it — so the probe never sends the command. It reads the daemon status (not a
    simulation) and the engine through the bridge's own read.
    """
    try:
        if _is_simulation(robot):
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
    robot: AnyReachyMini,
    address: tuple[str, int] | None = None,
    camera: CameraFeed | None = None,
    mic: MicFeed | None = None,
) -> frozenset[str]:
    """Probe what the live daemon can actually do — never inferred from backend type.

    Environment quirks decide: audio needs a recording session (the bridge's), the sim camera
    needs a GL context, honored motor states need hardware, gravity compensation needs hardware on the Placo kinematics engine,
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

    if mic is not None and _probe_audio(mic):
        caps.add("audio")
    if camera is not None and _probe_camera(camera):
        caps.add("camera")
    if _probe_motor_states(robot):
        caps.add("motor_states")
    if _probe_gravity_compensation(robot):
        caps.add("gravity_compensation")
    if address is not None and _probe_faces(*address):
        caps.add("faces")
    if address is not None and _probe_face_markers(*address):
        caps.add("face_markers")
    return frozenset(caps)


_PROBED: dict[tuple[str, int], frozenset[str]] = {}


def known_capabilities() -> frozenset[str] | None:
    """The capabilities probed on the run's daemon (at ``REACHY_MINI_HOST`` /
    ``REACHY_MINI_PORT``), or ``None`` while no session has probed it yet."""
    return _PROBED.get(_daemon.address())


def check_required_capabilities(caps: frozenset[str]) -> None:
    """Fail — not skip — when the run requires a capability (`REACHY_MINI_E2E_REQUIRED_CAPS`)
    the daemon did not probe; a no-op when none is required. Called by every harness
    fixture right after its probe, so a CI sim that comes up without its camera or its
    audio turns the job red instead of green-with-skips (specs/testing/ci.md)."""
    missing = sorted(_daemon.required_capabilities() - caps)
    if missing:
        pytest.fail(
            f"the target lacks required capability/ies: {', '.join(missing)} "
            f"(probed: {', '.join(sorted(caps)) or 'none'})"
        )


def probed_capabilities(
    robot: AnyReachyMini,
    address: tuple[str, int],
    camera: CameraFeed | None = None,
    mic: MicFeed | None = None,
) -> frozenset[str]:
    """The capabilities of the daemon at ``address``, probed once per ``pytest`` run — on
    the first bridge session over it — and reused by every later session on it: they are
    the daemon's (its camera, its audio device, its kinematics engine, its scene and
    displays), not a session's, and probing them again on every module would only add
    the probes' waits to every file's setup (specs/testing/testing.md "The harness")."""
    if address not in _PROBED:
        _PROBED[address] = _probe_capabilities(robot, address, camera, mic)
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


def _harness_config(host: str, port: int) -> ReachyMiniConfig:
    """The config of every bridge session the harness opens over the run's daemon."""
    # Build the bridge on the target's own backend (`sim`/`real`) with the daemon left to
    # this harness (`daemon.spawn` stays "never"): the bridge connects as a plain network
    # client to the daemon `_live_daemon` already manages — the run's one daemon, this
    # module's own session over it — its media backend by the host's locality
    # (`_daemon.robot_options`). See `_daemon.backend` for why the label is safe here.
    # The config names the shipped `yunet` detector (the defaults run none —
    # specs/vision/user_perception.md "Configuration") with detection and tracking off: a
    # face test turns on what it needs and `face_scene` turns both off after it, so every
    # other test keeps the head on its idle move whoever stands in front of a real robot.
    # The model downloads into the Hugging Face cache on the first live run, as the
    # emotions library does.
    return ReachyMiniConfig(
        backend=_daemon.backend(),
        robot=_daemon.robot_options(host, port),
        face_detection=FaceDetectionSettings(detector="yunet"),
        daemon=_bridge_daemon_config(),
    )


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
    bridge = ReachyMiniBridge(_harness_config(host, port))
    with BridgeLoop() as loop:
        loop.run(bridge.start())
        try:
            caps = probed_capabilities(
                bridge.robot, (host, port), bridge.camera, bridge.mic
            )
            check_required_capabilities(caps)
            yield LiveBridge(bridge, caps, loop)
        finally:
            loop.run(bridge.stop())


@pytest.fixture(scope="session")
def live_capabilities(request: pytest.FixtureRequest) -> frozenset[str]:
    """The run's probed capabilities: the set the first bridge session over the daemon
    probed, or — where none has yet — the set a short session of its own probes (the
    harness's config, started, probed, stopped) on the run's daemon, brought up then.
    What the ``requires`` gate reads, and a consumer's fixture that needs the set without
    a bridge session of its own."""
    known = known_capabilities()
    if known is not None:
        return known
    host, port = request.getfixturevalue("_live_daemon")
    bridge = ReachyMiniBridge(_harness_config(host, port))
    with BridgeLoop() as loop:
        loop.run(bridge.start())
        try:
            caps = probed_capabilities(
                bridge.robot, (host, port), bridge.camera, bridge.mic
            )
        finally:
            loop.run(bridge.stop())
    check_required_capabilities(caps)
    return caps


@pytest.fixture
def face_scene(
    live_bridge: LiveBridge, sim_scene: SimSceneClient
) -> Iterator[SimSceneClient]:
    """The scene with nobody in view, for a test that puts faces in front of the robot:
    gated on ``camera`` and ``faces`` (it skips where ``live_bridge`` probed neither), the
    pool of portraits cleared before the test and again after it, so a test starts with an
    empty view whatever the previous one left and leaves none behind
    (specs/testing/sim_scene.md "A pool of portraits"). After the test the bridge's
    detection and tracking are back off, the state ``live_bridge`` starts in, whatever the
    test turned on. A test on a session of its own (not ``live_bridge``) writes the same
    gate and clears against that session."""
    requires_caps(live_bridge, "camera", "faces")
    sim_scene.clear()
    try:
        yield sim_scene
    finally:
        sim_scene.clear()
        live_bridge.run(_seen_empty(live_bridge.bridge))
        live_bridge.run(live_bridge.bridge.stop_head_tracking())
        live_bridge.run(live_bridge.bridge.set_face_detection(False))


async def _seen_empty(bridge: ReachyMiniBridge, timeout: float = 3.0) -> None:
    """Until the running detection reports nobody, so the camera has shown the cleared
    scene before detection goes off: the next test's run starts from frames without the
    portraits this one spawned. A no-op with detection off; past ``timeout`` it returns
    (a portrait the sim failed to hide is the next test's ``clear()`` to fix)."""
    deadline = time.perf_counter() + timeout
    while bridge.faces.value.active and bridge.faces.value.faces:
        if time.perf_counter() > deadline:
            return
        await asyncio.sleep(0.05)


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


# --- the `requires` marker and its gate ---


def _needs(item: pytest.Item) -> list[str]:
    """The capabilities ``item``'s ``requires`` marks name — its own and its file's."""
    return [name for mark in item.iter_markers(MARKER) for name in mark.args]


def _gate(needs: list[str], caps: frozenset[str]) -> None:
    """Skip a test needing ``needs`` where ``caps`` lacks one — fail it instead where the
    missing one is required (``REACHY_MINI_E2E_REQUIRED_CAPS``), so every test of the run
    that needs it is red, not only the fixture that probed."""
    reason = missing_reason(caps, needs)
    if reason is None:
        return
    required = sorted(set(needs) & _daemon.required_capabilities() - caps)
    if required:
        pytest.fail(
            f"{reason} — and this run requires {', '.join(required)}", pytrace=False
        )
    pytest.skip(reason)


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker, and stop the run at once on an unknown capability in
    ``REACHY_MINI_E2E_REQUIRED_CAPS``."""
    config.addinivalue_line(
        "markers",
        f"{MARKER}(*capabilities): run the test only where the live target probed every "
        "capability; skip it elsewhere (specs/testing/testing_support.md)",
    )
    _daemon.required_capabilities()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """A typo in a mark stops the run instead of skipping its test on every target."""
    for item in items:
        for mark in item.iter_markers(MARKER):
            check_capability_names(mark.args, f"{item.nodeid}: @pytest.mark.{MARKER}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Gate a test ahead of its fixtures once the run has probed: a test that cannot run
    skips before its bridge session starts. Before the first probe, ``_requires_gate``
    gates it."""
    needs = _needs(item)
    caps = known_capabilities()
    if needs and caps is not None:
        _gate(needs, caps)


@pytest.fixture(autouse=True)
def _requires_gate(request: pytest.FixtureRequest) -> None:
    """The gate of a test that runs before the run's first probe: reads the capabilities
    through ``live_capabilities`` — probed by the test's own bridge session, set up
    before this fixture, or by a short session of its own. Once the run has probed, the
    hook has gated the test already; a test with no ``requires`` mark never asks for a
    daemon."""
    needs = _needs(request.node)
    if needs and known_capabilities() is None:
        _gate(needs, request.getfixturevalue("live_capabilities"))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """The ``live target`` section: what the target had, to read the run's skips against."""
    caps = known_capabilities()
    if caps is None:
        return
    host, port = _daemon.address()
    target = _daemon.target()
    if target == "sim":
        target = f"sim {'viewer' if _daemon._sim_viewer() else 'headless'}"
    required = _daemon.required_capabilities()
    lines = [
        ("target", f"{target} at {host}:{port}"),
        ("engine", _daemon.kinematics_engine()),
        ("probed", ", ".join(sorted(caps)) or "nothing"),
        ("required", ", ".join(sorted(required)) or "nothing"),
    ]
    terminalreporter.section("live target")
    for label, value in lines:
        terminalreporter.write_line(f"{label:<9} {value}")
