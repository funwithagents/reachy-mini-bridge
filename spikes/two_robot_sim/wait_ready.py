"""Wait until both daemons of the two-robot host report ``running`` (exit 1 on timeout)."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request


def state(port: int) -> str:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/daemon/status", timeout=3
        ) as reply:
            return str(json.load(reply)["state"])
    except Exception as error:  # noqa: BLE001 - not up yet
        return f"unreachable ({type(error).__name__})"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", type=int, nargs=2, default=[8011, 8012])
    parser.add_argument("--timeout", type=float, default=180.0)
    ns = parser.parse_args()
    deadline = time.monotonic() + ns.timeout
    states: list[str] = []
    while time.monotonic() < deadline:
        states = [state(port) for port in ns.ports]
        if all(s == "running" for s in states):
            print(f"both daemons running on ports {ns.ports}")
            return
        time.sleep(2.0)
    raise SystemExit(f"daemons not ready after {ns.timeout:.0f} s: {states}")


if __name__ == "__main__":
    main()
