# Live tier shared fixtures.
#
# This tier is NOT collected by the default `uv run pytest` (testpaths = ["tests"]);
# run it explicitly with `uv run pytest tests-e2e`. Mirror any isolation fixture the
# fast tier uses here — tests-e2e/ isn't a package that can import from tests/, so the
# few lines are duplicated rather than shared. (The library holds no process-global
# state today, so no reset fixture is needed yet — see specs/testing.md.)
#
# `live_api` is the entry point: it resolves a *target* (`REACHY_MINI_E2E_TARGET` =
# `sim` default | `real`), brings up a daemon ("own it or borrow it": reuse one already
# reachable, else spawn a sim one and own its teardown; never spawn for `real`), builds
# a `ReachyMiniApi` over it, then *probes* capabilities against the live daemon and
# yields `(api, capabilities)`. Tests gate on the probed set via `requires_caps(...)`
# (in support.py). See ../specs/testing.md ("E2E targets & capabilities") for the
# strategy and ../docs/running-the-sim-daemon.md for the launch recipes.

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import pytest

from reachy_mini_bridge.api import ReachyMiniApi
from reachy_mini_bridge.robot import AnyReachyMini, build_robot

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_STARTUP_TIMEOUT = 45.0  # a headless MuJoCo daemon is ready in ~1s; be generous
_AUDIO_PROBE_TIMEOUT = 5.0
_CAMERA_PROBE_TIMEOUT = 5.0


# --- target / connection config (from the environment) ---


def _target() -> str:
    return os.environ.get("REACHY_MINI_E2E_TARGET", "sim").strip().lower()


def _backend() -> str:
    """The bridge backend string for the selected target.

    The `REACHY_MINI_E2E_TARGET` values map straight onto `build_robot`'s backends:
    `real` → `"real"`, anything else (the default `sim`) → `"sim"` — mirroring
    `_managed_daemon`'s own real-vs-sim branch. Crucially we always keep
    `spawn_daemon=False` (its default): the client never brings up its own daemon, it
    connects to the one this harness manages (own-it-or-borrow-it). With
    `spawn_daemon=False` the upstream `use_sim` flag is a **no-op** — it's read only when
    spawning (see `reachy_mini.daemon.utils.daemon_check`), so `"real"` and `"sim"` build
    the identical network client here. Passing the target's real backend therefore
    changes no behavior; it just keeps the label honest and exercises the bridge's `sim`
    construction path instead of hardcoding `"real"` for both.
    """
    return "real" if _target() == "real" else "sim"


def _address() -> tuple[str, int]:
    host = os.environ.get("REACHY_MINI_HOST", _DEFAULT_HOST)
    port = int(os.environ.get("REACHY_MINI_PORT", str(_DEFAULT_PORT)))
    return host, port


def _sim_viewer() -> bool:
    """Whether to launch the headfull MuJoCo viewer instead of headless."""
    return os.environ.get("REACHY_MINI_E2E_SIM_VIEWER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# --- readiness ---


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _backend_ready(host: str, port: int) -> bool:
    """True once the daemon's *backend* is actually up (status reports one).

    Stronger than a bare `/ws/sdk` check: the WebSocket is accepted before the MuJoCo
    backend has come up (and even if it fails), so readiness means `backend_status is
    not None`, reached by an actual client connection. Connects `no_media` so the poll
    stays cheap and never negotiates the media path.
    """
    try:
        # A plain network client (spawn_daemon=False) to whatever daemon is at the
        # address — see `_backend` for why the target's backend is safe to use here.
        with build_robot(
            _backend(),
            connection_mode="network",
            spawn_daemon=False,
            host=host,
            port=port,
            media_backend="no_media",
        ) as robot:
            return robot.client.get_status().backend_status is not None
    except Exception:  # noqa: BLE001  (not connectable yet)
        return False


# --- daemon lifecycle: own it or borrow it ---

# GStreamer-bundle env vars that `reachy_mini`'s `gstreamer_bundle.pth` *prepends* to at
# every Python startup. Once this process has imported `reachy_mini` (the readiness probe
# does), they're already set — and the spawned daemon's own `.pth` would prepend again,
# producing malformed doubled single-value paths (`scanner:scanner`) that GStreamer can't
# exec. That makes the external plugin scanner fail, forcing an in-process scan that
# segfaults on `libgstpython.dylib`. Scrub them so the child sets fresh, correct values —
# exactly what upstream's own app launcher does (reachy_mini/apps/manager.py).
_GST_BUNDLE_ENV = (
    "GST_PLUGIN_PATH_1_0",
    "GST_PLUGIN_SYSTEM_PATH_1_0",
    "GST_REGISTRY_1_0",
    "GST_PLUGIN_SCANNER_1_0",
    "GI_TYPELIB_PATH",
    "PYGI_DLL_DIRS",
    "XDG_DATA_DIRS",
    "XDG_CONFIG_DIRS",
)


def _daemon_env() -> dict[str, str]:
    """Spawn env with the inherited GStreamer-bundle vars scrubbed (see above)."""
    env = os.environ.copy()
    for key in _GST_BUNDLE_ENV:
        env.pop(key, None)
    return env


def _sim_cmd(viewer: bool) -> list[str] | None:
    """Launch command for a sim daemon, or None if no launcher is available.

    Headless (CI): `reachy-mini-daemon --sim --headless --no-preload-datasets` — media
    on (no `--no-media`) so audio/camera can be probed. Headfull (local viewer): the
    MuJoCo viewer under `mjpython`, which supplies the GL context the sim camera needs
    and lets you watch the sim as a robot stand-in. The viewer only opens inside a GUI
    session; from a non-GUI shell, launch it via `launchctl asuser` (see the doc).
    """
    if viewer:
        exe = shutil.which("mjpython")
        if exe is None:
            return None
        return [
            exe,
            "-m",
            "reachy_mini.daemon.app.main",
            "--sim",
            "--scene",
            "minimal",
            "--no-preload-datasets",
        ]
    exe = shutil.which("reachy-mini-daemon")
    if exe is None:
        return None
    return [exe, "--sim", "--headless", "--no-preload-datasets"]


def _managed_daemon(target: str) -> Iterator[tuple[str, int]]:
    """Yield a live daemon at (host, port) for `target`.

    Borrows a daemon already reachable at the address (never tears it down). Otherwise
    `real` skips (we never spawn a robot), and `sim` spawns a MuJoCo daemon and owns
    its teardown. Skips cleanly (never fails) when the sim extra / launcher is missing,
    the port is busy with something else, or the daemon can't become ready in time.
    """
    host, port = _address()

    if _backend_ready(host, port):
        # Already up (a viewer sim you started, or the robot) — borrow, don't own.
        yield host, port
        return

    if target == "real":
        pytest.skip(f"no reachable real Reachy Mini daemon at {host}:{port}")

    # sim target: spawn and own it.
    pytest.importorskip("mujoco", reason="sim extra (mujoco) not installed")
    if _port_open(host, port):
        pytest.skip(f"port {port} is busy but not a ready Reachy Mini daemon")
    cmd = _sim_cmd(_sim_viewer())
    if cmd is None:
        pytest.skip("no sim daemon launcher available (reachy-mini-daemon / mjpython)")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_daemon_env(),
    )
    # The headfull viewer needs an unlocked GUI session with a real display: mjpython's
    # MuJoCo viewer can't get a GL context from the window server when the screen is
    # locked (or over plain SSH), so it hangs (no output → startup timeout) or segfaults
    # (exit -11). This hint turns those otherwise-cryptic skips into an actionable one.
    viewer_hint = (
        " — the headfull viewer (REACHY_MINI_E2E_SIM_VIEWER) needs an unlocked GUI "
        "session with a display; a locked screen makes mjpython's viewer hang or crash"
        if _sim_viewer()
        else ""
    )
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.skip(
                    f"sim daemon exited during startup (exit {proc.returncode})"
                    f"{viewer_hint}"
                )
            if _backend_ready(host, port):
                break
            time.sleep(1.0)
        else:
            pytest.skip(f"sim daemon did not become ready in time{viewer_hint}")
        yield host, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


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

    media: Any = robot.media  # real MediaManager; typed loosely (fake lacks get_frame)
    if _probe_audio(media):
        caps.add("audio")
    if _probe_camera(media):
        caps.add("camera")
    return frozenset(caps)


# --- the fixtures ---


@pytest.fixture(scope="module")
def _live_daemon() -> Iterator[tuple[str, int]]:
    """A live daemon for the selected target (module-scoped: one per test file)."""
    yield from _managed_daemon(_target())


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
    # Build the api on the target's own backend (`sim`/`real`) with `spawn_daemon=False`,
    # so it connects as a plain network client to the daemon this harness already manages
    # rather than bringing up its own. See `_backend` for why that's safe (with
    # spawn_daemon False the two backends build the identical client).
    api = ReachyMiniApi(
        _backend(),
        connection_mode="network",
        spawn_daemon=False,
        host=host,
        port=port,
        media_backend="local",
    )
    asyncio.run(api.__aenter__())
    try:
        caps = _probe_capabilities(api.robot)
        if "audio" in caps:
            api.robot.media.start_recording()  # restore what the session needs
        yield api, caps
    finally:
        asyncio.run(api.__aexit__(None, None, None))
