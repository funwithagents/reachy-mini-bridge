"""Private glue between the environment and the bridge's daemon lifecycle — imported by
`fixtures.py`.

Resolves the *target* from the environment (`REACHY_MINI_E2E_TARGET` = `sim` default |
`real`) and the daemon address (`REACHY_MINI_HOST` / `REACHY_MINI_PORT`), then hands the
daemon work to the library's [daemon.py](../daemon.py) (specs/daemon.md): `real` borrows
a ready daemon or skips; `sim` runs `managed_daemon(spawn="auto")` — reuse one already
ready at the address (never torn down), else spawn a MuJoCo daemon and own its teardown —
translating a `DaemonError` into a `pytest.skip` so the live tier skips, never fails,
when the environment can't provide one.

Kept out of `fixtures.py` so the plugin module reads as the fixture surface. The names
used by `fixtures.py` (`target`, `backend`, `address`, `managed_daemon`) are
un-underscored; the rest stays module-private.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from reachy_mini_bridge import daemon
from reachy_mini_bridge.config import DaemonConfig
from reachy_mini_bridge.errors import DaemonError

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


# --- target / connection config (from the environment) ---


def target() -> str:
    """The selected e2e target: `sim` (default) or `real`."""
    return os.environ.get("REACHY_MINI_E2E_TARGET", "sim").strip().lower()


def backend() -> str:
    """The bridge backend string for the selected target.

    `real` → `"real"`, anything else (the default `sim`) → `"sim"`. The fixture connects
    as a plain network client to the daemon this module manages (`daemon.spawn` stays
    `"never"` on the api side), so both backends build the identical client here; passing
    the target's real backend keeps the label honest and exercises the bridge's `sim`
    construction path.
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


# --- daemon lifecycle: own it or borrow it (via the library) ---


def managed_daemon(target_: str) -> Iterator[tuple[str, int]]:
    """Yield a live daemon at (host, port) for `target_`.

    Borrows a daemon already ready at the address (never tears it down). Otherwise
    `real` skips (we never spawn a robot), and `sim` spawns a MuJoCo daemon through
    `reachy_mini_bridge.daemon.managed_daemon` and owns its teardown. Skips cleanly
    (never fails) when the sim extra / launcher is missing, the port is busy with
    something else, or the daemon can't become ready in time.
    """
    host, port = address()

    if daemon.is_daemon_ready(host, port):
        # Already up (a viewer sim you started, or the robot) — borrow, don't own.
        yield host, port
        return

    if target_ == "real":
        pytest.skip(f"no reachable real Reachy Mini daemon at {host}:{port}")

    # sim target: spawn and own it.
    pytest.importorskip("mujoco", reason="sim extra (mujoco) not installed")
    config = DaemonConfig(spawn="auto", headless=not _sim_viewer())
    try:
        with daemon.managed_daemon(config, host=host, port=port) as handle:
            yield handle.host, handle.port
    except DaemonError as e:
        pytest.skip(str(e))
