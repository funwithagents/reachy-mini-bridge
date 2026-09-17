"""Daemon lifecycle: bring up / tear down a local ``reachy-mini-daemon`` (specs/daemon.md).

Upstream's ``ReachyMini`` is a client that connects to a separately running daemon in
its constructor. ``managed_daemon`` sits between "a config that says ``sim``" (or a
USB-attached ``real`` robot) and "a daemon ready to accept that client": own it or borrow
it, wait for *readiness* (the backend is up, not merely the port), launch headless by
default, scrub the GStreamer environment the child inherits, forward the child's own log
lines into the bridge's, and stop exactly what was started. Shared by
``ReachyMiniApi`` and the testing harness (``reachy_mini_bridge.testing``).

The process-spawning and readiness-probing steps are module-private callables
(``_spawn``, ``_ready``, ``_sleep``, ``_port_open``, ``_placo_available``) resolved at
call time, so the deterministic tests script them without a daemon.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO, Protocol

from .config import DAEMON_BACKENDS, DaemonConfig
from .errors import DaemonError
from .robot import build_robot

__all__ = [
    "DaemonHandle",
    "is_daemon_ready",
    "launch_command",
    "managed_daemon",
    "scrubbed_env",
]

_POLL_INTERVAL_S = 1.0
_TERMINATE_GRACE_S = 10.0

# The child's own log lines (specs/daemon.md "The child's output reaches the bridge's log"):
# re-emitted under this logger, at the level the line carries when it carries one — the
# daemon logs `name - LEVEL - message` — and at DEBUG otherwise, so a working daemon is
# quiet while a problem inside it reaches whoever is running the bridge. The last lines ride
# along on the DaemonError of a launch that never becomes ready.
_child_logger = logging.getLogger(f"{__name__}.child")
_CHILD_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
}
_CHILD_TAIL_LINES = 12
# How long a reader gets to finish draining a pipe that has ended before its lines are
# quoted or its thread let go: a stopped daemon's pipe is at EOF, so this is slack, not a
# wait — a daemon still running (the readiness timeout) spends it once, on the error path.
_CHILD_DRAIN_S = 0.5

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
    @property
    def stdout(self) -> IO[bytes] | None: ...
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


def child_line_level(line: str) -> int:
    """The level a child's log line carries — the daemon logs ``name - LEVEL - message`` —
    or ``DEBUG``, which is where an unrecognised line belongs until it proves otherwise."""
    for name, level in _CHILD_LEVELS.items():
        if f" - {name} - " in line:
            return level
    return logging.DEBUG


class _ChildOutput:
    """Drains a spawned daemon's merged stdout/stderr into the bridge's log, in its own
    thread, keeping the last lines for the error of a launch that fails.

    A ``None`` stream (a scripted process in the tests) forwards nothing.
    """

    def __init__(self, stream: IO[bytes] | None) -> None:
        self._stream = stream
        self._lines: deque[str] = deque(maxlen=_CHILD_TAIL_LINES)
        self._thread: threading.Thread | None = None
        if stream is not None:
            self._thread = threading.Thread(
                target=self._drain, name="daemon-output", daemon=True
            )
            self._thread.start()

    def _drain(self) -> None:
        stream = self._stream
        assert stream is not None
        try:
            for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._lines.append(line)
                _child_logger.log(child_line_level(line), "%s", line)
        except (OSError, ValueError):
            pass  # the pipe closed under the read: the daemon was stopped

    def tail(self) -> str:
        """The daemon's last lines, ready to append to a ``DaemonError`` (empty if none).

        A daemon that has just died still has words in flight: give the reader the moment
        it needs to finish the pipe before quoting it.
        """
        self._settle(_CHILD_DRAIN_S)
        lines = list(self._lines)
        if not lines:
            return ""
        return "; the daemon's last output:\n  " + "\n  ".join(lines)

    def _settle(self, timeout: float) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def stop(self) -> None:
        """Let the reader finish the pipe, then close it if it never ends.

        Draining before closing is what keeps a dying daemon's last lines — closing first
        cuts the read off mid-stream and loses exactly the words that explain the failure.
        """
        self._settle(_CHILD_DRAIN_S)
        alive = self._thread is not None and self._thread.is_alive()
        if alive and self._stream is not None:
            try:
                self._stream.close()  # unblock a read on a pipe that will not end
            except OSError:
                pass
            self._settle(1.0)


def launch_command(config: DaemonConfig, *, backend: str = "sim") -> list[str]:
    """The argv for a ``backend`` daemon per ``config``.

    ``sim`` (docs/running-the-sim-daemon.md) — every recipe runs the bridge's sim daemon
    launcher (specs/sim_daemon.md: upstream's daemon with its face-tracking corrections and
    the ``config.camera`` source): headless ``<this interpreter> -m
    reachy_mini_bridge.sim_daemon --headless --[no-]preload-datasets [--scene S] [camera
    flags]``; viewer ``mjpython -m reachy_mini_bridge.sim_daemon [...]`` (the render's GL
    context; needs a GUI session). A ``config.scene`` ending in ``.xml`` is a scene *file*,
    run by the test scene's launcher (``reachy_mini_bridge.testing.sim_scene``,
    specs/sim_scene.md) built on it. ``real`` — a USB-attached robot:
    ``reachy-mini-daemon [--kinematics-engine Placo] --[no-]preload-datasets``, Placo
    whenever it is importable (gravity compensation needs it). Media stays on. Raises
    ``DaemonError`` when a launcher is not on ``PATH``.
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
        cmd.append(_preload_flag(config))
        return cmd
    if shutil.which("reachy-mini-daemon") is None:
        raise DaemonError(
            "no 'reachy-mini-daemon' launcher on PATH — install the sim extra "
            "(reachy-mini-bridge[sim])"
        )
    if config.headless:
        exe = sys.executable
    else:
        exe = shutil.which("mjpython")
        if exe is None:
            raise DaemonError(
                "no 'mjpython' launcher on PATH for the viewer daemon — install the sim "
                "extra (reachy-mini-bridge[sim])"
            )
    if config.scene is not None and _scene_is_path(config.scene):
        cmd = [exe, "-m", "reachy_mini_bridge.testing.sim_scene", "--scene-path"]
        cmd.append(os.path.abspath(config.scene))
    else:
        cmd = [exe, "-m", "reachy_mini_bridge.sim_daemon"]
        if config.scene is not None:
            cmd += ["--scene", config.scene]
    if config.headless:
        cmd.append("--headless")
    cmd.append(_preload_flag(config))
    cmd += _camera_flags(config)
    return cmd


def _preload_flag(config: DaemonConfig) -> str:
    # Always explicit: the daemon's own default is not to preload.
    return "--preload-datasets" if config.preload_datasets else "--no-preload-datasets"


def _camera_flags(config: DaemonConfig) -> list[str]:
    """The sim daemon launcher's camera flags: none for the rendered eye camera."""
    camera = config.camera
    if camera.source != "webcam":
        return []
    flags = ["--camera", "webcam"]
    if camera.device is not None:
        flags += ["--webcam-device", str(camera.device)]
    return [*flags, "--webcam-hfov", f"{camera.hfov_deg:g}"]


def _scene_is_path(scene: str) -> bool:
    """A ``DaemonConfig.scene`` ending in ``.xml`` is a scene *file* the bridge's own
    launcher loads (specs/sim_scene.md); anything else is an upstream scene name."""
    return scene.endswith(".xml")


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
        cmd,
        stdin=subprocess.DEVNULL,
        # One pipe for both, drained by `_ChildOutput` into the bridge's log: a daemon that
        # cannot open a camera or a port says so, and someone hears it.
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        # Own session => own process group (specs/daemon.md "The child runs in its own
        # session"): a terminal's Ctrl+C is a SIGINT to the whole foreground group, and a
        # daemon sharing it would die *with* the bridge instead of last — the api's
        # teardown would then run against a dead server (a warning per motion tick,
        # wobbler tracebacks, the robot left wherever it dropped). Detached, the daemon
        # sees no terminal signal; only `_stop` ends it, after the robot session closed.
        start_new_session=True,
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
    output = _ChildOutput(proc.stdout)
    try:
        _wait_until_ready(
            host, port, config, backend, proc=proc, cmd=cmd, output=output
        )
        yield DaemonHandle(host=host, port=port, owned=True, pid=proc.pid)
    finally:
        _stop(proc)
        output.stop()


def _wait_until_ready(
    host: str,
    port: int,
    config: DaemonConfig,
    backend: str,
    *,
    proc: _Process | None,
    cmd: list[str] | None,
    output: _ChildOutput | None = None,
) -> None:
    hint = _VIEWER_HINT if backend == "sim" and not config.headless else ""
    tail = output.tail if output is not None else str
    deadline = time.monotonic() + config.startup_timeout
    while True:
        if proc is not None and (code := proc.poll()) is not None:
            raise DaemonError(
                f"{backend} daemon exited during startup (exit {code}); command: "
                f"{' '.join(cmd or [])}{hint}{tail()}"
            )
        if _ready(host, port):
            return
        if time.monotonic() >= deadline:
            what = (
                f"spawned {backend} daemon did not become ready"
                if proc is not None
                else f"port {port} on {host} is open but no ready Reachy Mini daemon answers"
            )
            raise DaemonError(
                f"{what} within {config.startup_timeout:g}s{hint}{tail()}"
            )
        _sleep(_POLL_INTERVAL_S)


def _stop(proc: _Process) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
