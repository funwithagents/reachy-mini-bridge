"""What a client process does to reach one robot of the two-robot host, and no other.

The REST / WebSocket side needs only the port. The camera does not: upstream's local
client reads frames from a Unix socket whose path is a module constant
(``reachy_mini.daemon.utils.CAMERA_SOCKET_PATH``), so a process that wants robot B's
camera points that constant at robot B's socket before it builds its ``ReachyMini``.
One process therefore reads one robot's camera — which is the end use (one app per robot).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any


def robot_report(port: int, host: str = "127.0.0.1") -> dict[str, Any]:
    with urllib.request.urlopen(
        f"http://{host}:{port}/api/spike/robot", timeout=5
    ) as reply:
        return json.load(reply)


def bind_camera_to(port: int, host: str = "127.0.0.1") -> dict[str, Any]:
    """Point upstream's local camera reader, in this process, at the camera socket the
    daemon on ``port`` reports. Call before constructing ``ReachyMini`` / the bridge."""
    import reachy_mini.daemon.utils as daemon_utils
    from reachy_mini.media import camera_gstreamer

    report = robot_report(port, host)
    socket_path = report["camera_socket"]
    if socket_path is not None:
        # is_local_camera_available() reads the first; GStreamerCamera imported the second.
        daemon_utils.CAMERA_SOCKET_PATH = socket_path
        camera_gstreamer.CAMERA_SOCKET_PATH = socket_path
    return report
