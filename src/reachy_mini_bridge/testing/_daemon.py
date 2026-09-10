"""Private daemon-lifecycle plumbing for the live tier — imported by `fixtures.py`.

Resolves the *target* from the environment (`REACHY_MINI_E2E_TARGET` = `sim` default |
`real`) and brings a daemon up under **own-it-or-borrow-it**: reuse one already reachable
at the address (never tear it down), else — for `sim` only — spawn a MuJoCo daemon and own
its teardown; never spawn for `real`. Skips cleanly (never fails) whenever the environment
can't provide one. See ../../../specs/testing_support.md and
../../../docs/running-the-sim-daemon.md for the launch recipes and the GStreamer gotcha.

Kept out of `fixtures.py` so the plugin module reads as the fixture surface, not the
process-management plumbing. The names used by `fixtures.py` (`target`, `backend`,
`address`, `managed_daemon`) are un-underscored; the rest stays module-private.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

from reachy_mini_bridge.robot import build_robot

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_STARTUP_TIMEOUT = 45.0  # a headless MuJoCo daemon is ready in ~1s; be generous


# --- target / connection config (from the environment) ---


def target() -> str:
    """The selected e2e target: `sim` (default) or `real`."""
    return os.environ.get("REACHY_MINI_E2E_TARGET", "sim").strip().lower()


def backend() -> str:
    """The bridge backend string for the selected target.

    The `REACHY_MINI_E2E_TARGET` values map straight onto `build_robot`'s backends:
    `real` → `"real"`, anything else (the default `sim`) → `"sim"` — mirroring
    `managed_daemon`'s own real-vs-sim branch. Crucially we always keep
    `spawn_daemon=False` (its default): the client never brings up its own daemon, it
    connects to the one this harness manages (own-it-or-borrow-it). With
    `spawn_daemon=False` the upstream `use_sim` flag is a **no-op** — it's read only when
    spawning (see `reachy_mini.daemon.utils.daemon_check`), so `"real"` and `"sim"` build
    the identical network client here. Passing the target's real backend therefore
    changes no behavior; it just keeps the label honest and exercises the bridge's `sim`
    construction path instead of hardcoding `"real"` for both.
    """
    return "real" if target() == "real" else "sim"


def address() -> tuple[str, int]:
    """The daemon address from `REACHY_MINI_HOST` / `REACHY_MINI_PORT`."""
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
        # address — see `backend` for why the target's backend is safe to use here.
        with build_robot(
            backend(),
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


def managed_daemon(target_: str) -> Iterator[tuple[str, int]]:
    """Yield a live daemon at (host, port) for `target_`.

    Borrows a daemon already reachable at the address (never tears it down). Otherwise
    `real` skips (we never spawn a robot), and `sim` spawns a MuJoCo daemon and owns
    its teardown. Skips cleanly (never fails) when the sim extra / launcher is missing,
    the port is busy with something else, or the daemon can't become ready in time.
    """
    host, port = address()

    if _backend_ready(host, port):
        # Already up (a viewer sim you started, or the robot) — borrow, don't own.
        yield host, port
        return

    if target_ == "real":
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
