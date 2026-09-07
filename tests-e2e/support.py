"""Helpers for the opt-in live tier.

Two skip gates for tests that can't run in the current environment:

- `require_env(...)` — a live test needs real credentials; rather than fail when
  they're absent, it *skips* cleanly (you only exercise services you hold keys for).
- `requires_caps(...)` — a test needs a robot capability (`motion`, `audio`, `camera`,
  …); it *skips* when the current target didn't probe that capability. The probed set
  rides along in the `live_robot` fixture value, so a test passes that value in:
  `requires_caps(live_robot, "audio")` — see specs/testing.md.
"""

import os

import pytest


def require_env(name: str) -> str:
    """Return env var `name`, or skip the calling test if it's unset/empty."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set; skipping live test")
    return value


def requires_caps(live_robot: tuple[object, frozenset[str]], *caps: str) -> None:
    """Skip the calling test unless the live target probed every capability in `caps`.

    Pass the `live_robot` fixture value (`(robot, capabilities)`); the probed set
    travels in it, so no ambient state is needed.
    """
    _robot, available = live_robot
    missing = sorted(set(caps) - available)
    if missing:
        pytest.skip(f"target lacks required capability/ies: {', '.join(missing)}")
