# Fast tier shared fixtures.
#
# If the package has process-global or singleton state (a module-level registry,
# a cached client, a configured logger), add an `autouse=True` fixture here that
# resets it before and after each test so state can't leak between tests. Keep
# this tier deterministic and network-free — anything that hits a real service
# belongs in tests-e2e/ instead.

import urllib.error
from email.message import Message

import pytest

from reachy_mini_bridge import robot as robot_module


@pytest.fixture(autouse=True)
def no_daemon_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake robot behind a `real` or `sim` config has no daemon: every read of its HTTP
    port answers 404, as upstream's daemon does on a route of the bridge's launcher. A
    test scripts a daemon by patching `fetch_daemon_json` over this."""

    def fetch(robot: object, path: str) -> object:
        raise urllib.error.HTTPError(path, 404, "Not Found", Message(), None)

    monkeypatch.setattr(robot_module, "fetch_daemon_json", fetch)
