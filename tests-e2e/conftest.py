# Live tier shared fixtures.
#
# This tier is NOT collected by the default `uv run pytest` (testpaths = ["tests"]);
# run it explicitly with `uv run pytest tests-e2e`. Mirror any isolation fixture the
# fast tier uses here — tests-e2e/ isn't a package that can import from tests/, so the
# few lines are duplicated rather than shared.
#
# The `sim_daemon` fixture provides a live daemon (module-scoped: started once per
# test file, stopped at the end — never per test). It runs the real MuJoCo backend
# headless (`--sim --headless`), so it needs no display and runs anywhere, incl. CI.
# See ../docs/running-the-sim-daemon.md.

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator

import pytest

_HOST = "127.0.0.1"
_PORT = 8000


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
    not None`, reached by an actual client connection.
    """
    from reachy_mini_bridge.client import build_robot

    try:
        with build_robot(
            "real", connection_mode="network", host=host, port=port
        ) as robot:
            return robot.client.get_status().backend_status is not None
    except Exception:  # noqa: BLE001  (not connectable yet)
        return False


def _managed_daemon(
    cmd: list[str] | None,
    *,
    startup_timeout: float,
    ready_check: Callable[[str, int], bool],
) -> Iterator[tuple[str, int]]:
    """Yield a live daemon at (host, port): reuse a ready one, else spawn `cmd`.

    Skips (never fails) when the `sim` extra is missing, a launcher couldn't be built
    (`cmd is None`), the port is busy with something else, or the daemon can't become
    ready in time.
    """
    pytest.importorskip("mujoco", reason="sim extra (mujoco) not installed")

    if ready_check(_HOST, _PORT):
        # Already up (e.g. started manually) — use it as-is; not ours to tear down.
        yield (_HOST, _PORT)
        return
    if _port_open(_HOST, _PORT):
        pytest.skip(f"port {_PORT} is busy but not a ready Reachy Mini daemon")
    if cmd is None:
        pytest.skip("no daemon launcher available (reachy-mini-daemon / mjpython)")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.skip(f"daemon exited during startup (exit {proc.returncode})")
            if ready_check(_HOST, _PORT):
                break
            time.sleep(1.0)
        else:
            pytest.skip("daemon did not become ready in time")
        yield (_HOST, _PORT)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _daemon_cmd(flags: list[str]) -> list[str] | None:
    exe = shutil.which("reachy-mini-daemon")
    return [exe, *flags] if exe is not None else None


@pytest.fixture(scope="module")
def sim_daemon() -> Iterator[tuple[str, int]]:
    """Headless MuJoCo sim daemon — real physics via `--headless` (no viewer).

    Started once per test module, stopped at the end. `--headless` runs the MuJoCo
    backend without the interactive viewer, so it needs no display / `mjpython` and
    runs anywhere (the viewer, for *watching* the sim, is a dev convenience — see
    ../docs/running-the-sim-daemon.md). Readiness requires the MuJoCo backend to
    actually come up (not just the WebSocket).
    """
    yield from _managed_daemon(
        _daemon_cmd(["--sim", "--headless", "--no-media", "--no-preload-datasets"]),
        startup_timeout=45.0,
        ready_check=_backend_ready,
    )
