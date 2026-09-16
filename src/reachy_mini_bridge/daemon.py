"""Daemon lifecycle: bring up / tear down a local ``reachy-mini-daemon`` (specs/daemon.md).

Upstream's ``ReachyMini`` is a client that connects to a separately running daemon in
its constructor. ``managed_daemon`` sits between "a config that says ``sim``" (or a
USB-attached ``real`` robot) and "a daemon ready to accept that client": own it or borrow
it, wait for *readiness* (the backend is up, not merely the port), launch headless by
default, scrub the GStreamer
environment the child inherits, and stop exactly what was started. Shared by
``ReachyMiniApi`` and the testing harness (``reachy_mini_bridge.testing``).

The process-spawning and readiness-probing steps are module-private callables
(``_spawn``, ``_ready``, ``_sleep``, ``_port_open``, ``_placo_available``) resolved at
call time, so the deterministic tests script them without a daemon.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from .config import DaemonConfig
from .errors import DaemonError
from .robot import build_robot

__all__ = [
    "DaemonHandle",
    "is_daemon_ready",
    "launch_command",
    "managed_daemon",
    "scrubbed_env",
]

DAEMON_BACKENDS = ("sim", "real")

_POLL_INTERVAL_S = 1.0
_TERMINATE_GRACE_S = 10.0

# GStreamer-bundle env vars that `reachy_mini`'s `gstreamer_bundle.pth` *prepends* to at
# every Python startup. A parent that has imported `reachy_mini` (every bridge process)
# already has them set; the child's own `.pth` would prepend again, producing doubled
# single-value paths (`scanner:scanner`) GStreamer can't exec, so the external plugin
# scanner fails and the in-process fallback segfaults on `libgstpython.dylib`. Scrubbing
# lets the child set fresh, correct values — what upstream's own app launcher does.
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

_VIEWER_HINT = (
    " — the viewer (daemon.headless = false) needs an unlocked GUI session with a "
    "display; a locked screen makes mjpython's viewer hang or crash"
)


@dataclass(frozen=True)
class DaemonHandle:
    """What ``managed_daemon`` yields: where the daemon is and whether we own it."""

    host: str
    port: int
    # True when this context spawned the process and will stop it
    owned: bool
    # the spawned process, when owned
    pid: int | None


class _Process(Protocol):
    """The slice of ``subprocess.Popen`` the lifecycle uses (tests script a stand-in)."""

    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


# --- pure pieces -------------------------------------------------------------------


def scrubbed_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child's environment: ``base`` (default ``os.environ``) minus the GStreamer-bundle
    vars (see the module note)."""
    env = dict(os.environ if base is None else base)
    for key in _GST_BUNDLE_ENV:
        env.pop(key, None)
    return env


def launch_command(config: DaemonConfig, *, backend: str = "sim") -> list[str]:
    """The argv for a ``backend`` daemon per ``config``.

    ``sim`` (docs/running-the-sim-daemon.md) — headless: ``reachy-mini-daemon --sim
    --headless [--no-preload-datasets] [--scene S]``; viewer: ``mjpython -m
    reachy_mini.daemon.app.main --sim [...]`` (supplies the camera's GL context; needs a
    GUI session). ``real`` — a USB-attached robot: ``reachy-mini-daemon [--kinematics-engine
    Placo] [--no-preload-datasets]``, Placo whenever it is importable (gravity compensation
    needs it). Media stays on. Raises ``DaemonError`` when the launcher is not on ``PATH``.
    """
    _check_backend(backend)
    if backend == "real":
        exe = shutil.which("reachy-mini-daemon")
        if exe is None:
            raise DaemonError(
                "no 'reachy-mini-daemon' launcher on PATH — it ships with reachy-mini, "
                "the bridge's base dependency"
            )
        cmd = [exe]
        if _placo_available():
            cmd += ["--kinematics-engine", "Placo"]
        if not config.preload_datasets:
            cmd.append("--no-preload-datasets")
        return cmd
    if config.headless:
        exe = shutil.which("reachy-mini-daemon")
        if exe is None:
            raise DaemonError(
                "no 'reachy-mini-daemon' launcher on PATH — install the sim extra "
                "(reachy-mini-bridge[sim])"
            )
        cmd = [exe, "--sim", "--headless"]
    else:
        exe = shutil.which("mjpython")
        if exe is None:
            raise DaemonError(
                "no 'mjpython' launcher on PATH for the viewer daemon — install the sim "
                "extra (reachy-mini-bridge[sim])"
            )
        cmd = [exe, "-m", "reachy_mini.daemon.app.main", "--sim"]
    if not config.preload_datasets:
        cmd.append("--no-preload-datasets")
    if config.scene is not None:
        cmd += ["--scene", config.scene]
    return cmd


# --- probes and process seams (patched by tests) -----------------------------------


def is_daemon_ready(host: str, port: int) -> bool:
    """True once the daemon's *backend* is up (``get_status().backend_status`` is set).

    Stronger than an open port or an accepted WebSocket: ``/ws/sdk`` answers 403 until
    the daemon has woken, and accepts the socket even when its MuJoCo backend failed to
    start. Probes with a plain network client, media off (cheap). Any failure ⇒ False.
    """
    try:
        with build_robot(
            "real",
            connection_mode="network",
            spawn_daemon=False,
            host=host,
            port=port,
            media_backend="no_media",
        ) as robot:
            return robot.client.get_status().backend_status is not None
    except Exception:  # noqa: BLE001  (not connectable yet)
        return False


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


def _check_backend(backend: str) -> None:
    if backend not in DAEMON_BACKENDS:
        raise ValueError(
            f"daemon backend must be one of {DAEMON_BACKENDS}, got {backend!r}"
        )


def _placo_available() -> bool:
    return importlib.util.find_spec("placo") is not None


def _spawn(cmd: list[str], env: dict[str, str]) -> _Process:
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
    )


def _ready(host: str, port: int) -> bool:
    return is_daemon_ready(host, port)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# --- the lifecycle ------------------------------------------------------------------


@contextmanager
def managed_daemon(
    config: DaemonConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    backend: str = "sim",
) -> Iterator[DaemonHandle]:
    """Yield a ready daemon at ``host:port`` per ``config.spawn`` (``auto`` | ``always``).

    ``backend`` picks the launch recipe: ``sim`` (MuJoCo) or ``real`` (a USB-attached robot).

    ``auto``: borrow a daemon already ready (or still booting) at the address, else
    spawn one and own it. ``always``: spawn and own; the port already open is an error.
    An owned daemon is stopped on exit (terminate, grace, kill) on every exit path; a
    borrowed one is left running. Synchronous — the api enters it off the event loop.
    """
    if config.spawn not in ("auto", "always"):
        raise ValueError(
            f"managed_daemon needs spawn 'auto' or 'always', got {config.spawn!r}"
        )
    _check_backend(backend)
    if _port_open(host, port):
        if config.spawn == "always":
            raise DaemonError(
                f"port {port} on {host} is already in use, and daemon.spawn is 'always'"
            )
        _wait_until_ready(host, port, config, backend, proc=None, cmd=None)
        yield DaemonHandle(host=host, port=port, owned=False, pid=None)
        return

    cmd = launch_command(config, backend=backend)
    proc = _spawn(cmd, scrubbed_env())
    try:
        _wait_until_ready(host, port, config, backend, proc=proc, cmd=cmd)
        yield DaemonHandle(host=host, port=port, owned=True, pid=proc.pid)
    finally:
        _stop(proc)


def _wait_until_ready(
    host: str,
    port: int,
    config: DaemonConfig,
    backend: str,
    *,
    proc: _Process | None,
    cmd: list[str] | None,
) -> None:
    hint = _VIEWER_HINT if backend == "sim" and not config.headless else ""
    deadline = time.monotonic() + config.startup_timeout
    while True:
        if proc is not None and (code := proc.poll()) is not None:
            raise DaemonError(
                f"{backend} daemon exited during startup (exit {code}); command: "
                f"{' '.join(cmd or [])}{hint}"
            )
        if _ready(host, port):
            return
        if time.monotonic() >= deadline:
            what = (
                f"spawned {backend} daemon did not become ready"
                if proc is not None
                else f"port {port} on {host} is open but no ready Reachy Mini daemon answers"
            )
            raise DaemonError(f"{what} within {config.startup_timeout:g}s{hint}")
        _sleep(_POLL_INTERVAL_S)


def _stop(proc: _Process) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
