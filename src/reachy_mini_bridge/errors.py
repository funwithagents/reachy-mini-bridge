"""Exception types the bridge raises (specs/api.md, specs/robot.md, specs/config.md,
specs/daemon.md).

One small hierarchy so callers and the tools layer catch a single named base rather
than guessing at ad-hoc types. State errors (e.g. a movement verb called while motors
are off) are distinct from ``ValueError``, which the api reserves for out-of-range
input validation — and which ``ConfigError`` extends, since a malformed config is
invalid input data.
"""

from __future__ import annotations

__all__ = [
    "BridgeError",
    "ConfigError",
    "DaemonError",
    "GravityCompensationUnsupportedError",
    "MotorsNotEnabledError",
    "SimSceneError",
]


class BridgeError(RuntimeError):
    """Base for errors originating in the bridge's own logic (not upstream/validation)."""


class MotorsNotEnabledError(BridgeError):
    """A verb that moves the robot was called while motors were not ``enabled``.

    Raised fail-fast by movement verbs (``play_emotion``, ``start_head_tracking``) after
    reading the live motor state, rather than silently enabling torque or sending a
    command that does nothing. The caller enables motors via
    ``set_motors_state("enabled")`` first (see specs/api.md "Motors").
    """


class GravityCompensationUnsupportedError(BridgeError):
    """``set_motors_state("gravity_compensation")`` on a daemon that cannot hold the mode.

    Raised before anything is sent, when the robot daemon's kinematics engine is not
    Placo (or cannot be read): upstream sends the mode fire-and-forget, and such a daemon
    rejects it by closing the client connection. The motor state and the connection are
    left as they were (see specs/api.md "Motors").
    """


class DaemonError(BridgeError):
    """The bridge could not bring up, find, or stop a ``reachy-mini-daemon``.

    Raised by ``daemon.managed_daemon`` (see specs/daemon.md): a missing launcher, a
    busy port under ``spawn="always"``, a child that exits or never becomes ready
    within ``startup_timeout``.
    """


class ConfigError(ValueError):
    """A malformed ``ReachyMiniConfig`` (see specs/config.md).

    A ``ValueError`` — the same taxonomy tts-engine uses — so a caller can catch either
    ``ConfigError`` for the specific type or ``ValueError`` for any bad-config surface.
    """


class SimSceneError(BridgeError):
    """A sim-scene request failed: the daemon at the address does not serve the bridge's
    sim-scene endpoint (it was not launched through ``reachy_mini_bridge.testing.sim_scene``), it
    is unreachable, or it refused the request (an unknown body, a malformed pose). See
    specs/sim_scene.md.
    """
