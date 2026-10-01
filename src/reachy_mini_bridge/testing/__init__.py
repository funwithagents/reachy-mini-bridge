"""Consumer testing support: the shipped e2e harness for the bridge's backends.

A project that depends on ``reachy-mini-bridge`` tests its own code against the three
backends ([specs/core/robot.md]) the same way the bridge does:

- **Unit tests → ``fake``.** Construct ``ReachyMiniBridge("fake")`` directly (no daemon, no
  extra) and assert through the ``bridge.robot`` escape hatch. Needs nothing from this
  package.
- **E2E tests → ``sim`` / ``real``.** Opt into the pytest plugin from a root
  ``conftest.py`` — ``pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]`` — to get
  the ``live_bridge`` fixture (own-it-or-borrow-it daemon lifecycle, GStreamer env scrub,
  capability probing, and one event loop for the bridge's lifetime — run a test's
  coroutines through ``live_bridge.run(...)``), then gate each test with ``requires_caps``.

See ../../../specs/testing/testing_support.md and ../../../docs/testing-with-the-bridge.md.
"""

from reachy_mini_bridge.testing.support import (
    BridgeLoop,
    LiveBridge,
    require_env,
    requires_caps,
)

__all__ = ["BridgeLoop", "LiveBridge", "require_env", "requires_caps"]
