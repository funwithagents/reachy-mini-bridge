"""Connection seam to the upstream ``reachy_mini`` SDK.

Specified by [specs/core/robot.md](../../specs/core/robot.md). ``real``/``sim`` drive the
upstream ``reachy_mini.ReachyMini`` directly; ``fake`` drives the first-party
``FakeReachyMini`` (in [fake_reachy_mini.py](fake_reachy_mini.py)), which imports no
``reachy_mini`` and records the commands it receives. ``AnyReachyMini`` is a union
type alias over the two so pyright keeps the fake in lockstep with the surface the
layers above call. ``build_robot`` selects a backend; ``fetch_daemon_json`` reads the
daemon's own HTTP API, which serves what the SDK exposes no getter for.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from reachy_mini import ReachyMini

from .fake_reachy_mini import FakeReachyMini

__all__ = ["AnyReachyMini", "build_robot", "fetch_daemon_json"]

# A readable name for "any Reachy Mini implementation" — the real SDK object or our
# fake. The union keeps the fake honest against the real surface under pyright.
type AnyReachyMini = ReachyMini | FakeReachyMini

_BACKENDS = ("real", "sim", "fake")
DAEMON_HTTP_TIMEOUT_S = 2.0


def build_robot(backend: str = "real", **opts: Any) -> AnyReachyMini:
    """Build (and connect) the robot for ``backend``.

    ``fake`` returns a ``FakeReachyMini``; ``real``/``sim`` construct the upstream
    ``reachy_mini.ReachyMini`` (``sim`` sets ``use_sim=True``).
    ``opts`` forwards upstream connection options (``host``, ``port``, ``timeout``, …).
    """
    if backend not in _BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; expected one of {_BACKENDS}")
    if backend == "fake":
        return FakeReachyMini()
    return ReachyMini(use_sim=(backend == "sim"), **opts)


def fetch_daemon_json(robot: ReachyMini, path: str) -> Any:
    """GET ``path`` (``/api/...``) from the robot's daemon over HTTP and decode the JSON body.

    Blocking; the host and port are the ones the SDK client connected to. Raises on an
    unreachable daemon or an HTTP error status. Callers reach it through this module
    (``robot.fetch_daemon_json``), so tests patch it here.
    """
    client = robot.client
    url = f"http://{client.host}:{client.port}{path}"
    with urllib.request.urlopen(url, timeout=DAEMON_HTTP_TIMEOUT_S) as response:
        return json.load(response)
