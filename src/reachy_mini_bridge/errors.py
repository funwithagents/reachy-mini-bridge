"""Exception types the bridge raises (specs/api.md, specs/robot.md).

One small hierarchy so callers and the tools layer catch a single named base rather
than guessing at ad-hoc types. State errors (e.g. a movement verb called while motors
are off) are distinct from ``ValueError``, which the api reserves for out-of-range
input validation.
"""

from __future__ import annotations

__all__ = ["BridgeError", "MotorsNotEnabledError"]


class BridgeError(RuntimeError):
    """Base for errors originating in the bridge's own logic (not upstream/validation)."""


class MotorsNotEnabledError(BridgeError):
    """A verb that moves the robot was called while motors were not ``enabled``.

    Raised fail-fast by movement verbs (``play_emotion``, ``start_head_tracking``) after
    reading the live motor state, rather than silently enabling torque or sending a
    command that does nothing. The caller enables motors via
    ``set_motors_state("enabled")`` first (see specs/api.md "Motors").
    """
