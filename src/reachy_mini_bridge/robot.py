"""Connection seam to the upstream ``reachy_mini`` SDK.

Specified by [specs/robot.md](../../specs/robot.md). ``real``/``sim`` drive the
upstream ``reachy_mini.ReachyMini`` directly; ``fake`` drives the first-party
``FakeReachyMini`` (in [fake_reachy_mini.py](fake_reachy_mini.py)), which imports no
``reachy_mini`` and records the commands it receives. ``AnyReachyMini`` is a union
type alias over the two so pyright keeps the fake in lockstep with the surface the
layers above call. ``build_robot`` selects a backend.
"""

from __future__ import annotations

from typing import Any

from reachy_mini import ReachyMini

from .fake_reachy_mini import FakeReachyMini

__all__ = ["AnyReachyMini", "build_robot"]

# A readable name for "any Reachy Mini implementation" — the real SDK object or our
# fake. The union keeps the fake honest against the real surface under pyright.
type AnyReachyMini = ReachyMini | FakeReachyMini

_BACKENDS = ("real", "sim", "fake")


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
