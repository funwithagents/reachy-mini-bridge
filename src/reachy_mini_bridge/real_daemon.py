"""The real daemon launcher: every hardware daemon the bridge starts (specs/daemon/real_daemon.md).

``python -m reachy_mini_bridge.real_daemon [--[no-]preload-datasets] [upstream flags...]``
runs upstream's ``reachy-mini-daemon`` for a robot on this machine's USB, in this
interpreter, with one correction. On macOS the daemon opens its camera through
``avfvideosrc device-index=N``, and the device order that index reads — AVFoundation's —
is not stable: it moves between two opens in one process. Every build of the media
pipeline is therefore a draw between the robot's camera and the computer's own, and the
wrong pick fails the robot camera's caps (``not-negotiated``), leaving the daemon
without video: audio keeps working, the IPC socket exists, no frame ever comes. The
launcher reads which device each build actually opened (``avfvideosrc``'s read-only
``device-name``) and rebuilds on the next index until it is the robot's camera.

Importing this module pulls in neither the daemon nor GStreamer; the daemon-side pieces
import them when they run.
"""

from __future__ import annotations

import argparse
import logging
import platform
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

__all__ = [
    "install_macos_camera_check",
    "is_robot_camera",
    "run_real_daemon",
    "select_camera",
]

_logger = logging.getLogger(__name__)

# Pipeline builds to spend before settling for whatever camera opened. With the computer's
# own camera and the robot's in the draw, six wrong picks in a row are unlikely.
_ATTEMPTS = 6
# How long a freshly started pipeline gets to open its device and report its name.
_OPEN_TIMEOUT_S = 3.0
_POLL_S = 0.05
# Substrings naming the robot's camera — the names upstream's detection matches
# (`device_detection.DEFAULT_CAM_NAMES`; a test pins the parity).
ROBOT_CAMERA_NAMES = ("Reachy", "Arducam_12MP", "imx708")


# --- the selection --------------------------------------------------------------------


def is_robot_camera(name: str, expected: Sequence[str] = ROBOT_CAMERA_NAMES) -> bool:
    """True when ``name`` (a device's display name) is one of the robot's cameras."""
    return any(part in name for part in expected)


def select_camera(
    opened: Callable[[], str | None],
    restart: Callable[[int], None],
    *,
    first: int,
    count: int,
    expected: Sequence[str] = ROBOT_CAMERA_NAMES,
    attempts: int = _ATTEMPTS,
) -> bool:
    """Keep the pipeline that opened the robot's camera; rebuild on the next index otherwise.

    ``opened()`` names the device the running pipeline opened — ``None`` when it cannot
    tell, which is accepted, so a camera that reports no name is never retried into the
    ground. ``restart(index)`` tears the pipeline down and builds it again on ``index``.
    Indices go round-robin over ``count`` devices after ``first``, for at most ``attempts``
    builds in all, the one already made included. True once the robot's camera is open;
    False when the last build is left running on another camera (audio still works).
    """
    index = first
    for attempt in range(1, attempts + 1):
        name = opened()
        if name is None or is_robot_camera(name, expected):
            if attempt > 1:
                _logger.info(
                    "robot camera open on avfvideosrc device-index %d after %d builds",
                    index,
                    attempt,
                )
            return True
        if attempt == attempts:
            _logger.error(
                "avfvideosrc device-index %d opened %r, not the robot's camera; giving up "
                "after %d builds — the daemon runs without video",
                index,
                name,
                attempts,
            )
            return False
        index = (index + 1) % max(count, 1)
        _logger.warning(
            "avfvideosrc device-index %d opened %r, not the robot's camera; rebuilding "
            "the media pipeline on device-index %d",
            (index - 1) % max(count, 1),
            name,
            index,
        )
        restart(index)
    return False  # pragma: no cover - the loop returns


# --- the GStreamer glue (runs in the daemon process) ----------------------------------


def _avfvideosrc(pipeline: Any) -> Any | None:
    """The ``avfvideosrc`` element of ``pipeline``, or ``None`` when it has none."""
    from gi.repository import (  # pyright: ignore[reportMissingImports]
        Gst,  # pyright: ignore[reportAttributeAccessIssue]
    )

    iterator = pipeline.iterate_recurse()
    while True:
        result, element = iterator.next()
        if result == Gst.IteratorResult.RESYNC:
            iterator.resync()
            continue
        if result != Gst.IteratorResult.OK:
            return None
        factory = element.get_factory()
        if factory is not None and factory.get_name() == "avfvideosrc":
            return element


def _opened_device_name(
    pipeline: Any, timeout_s: float = _OPEN_TIMEOUT_S
) -> str | None:
    """The name of the device ``pipeline``'s ``avfvideosrc`` opened, once it has (``None``
    without such a source, or when it names nothing within ``timeout_s``)."""
    source = _avfvideosrc(pipeline)
    if source is None:
        return None
    deadline = time.monotonic() + timeout_s
    while True:
        name = source.get_property("device-name")
        if name:
            return str(name)
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_S)


def _video_device_count() -> int:
    """How many video sources GStreamer's device monitor lists (0 when it cannot say)."""
    try:
        from reachy_mini.media.device_detection import gst_monitor_devices

        return len(gst_monitor_devices("Video/Source"))
    except Exception:  # noqa: BLE001  (a failed enumeration only shortens the rotation)
        return 0


def _gst_null() -> Any:
    from gi.repository import (  # pyright: ignore[reportMissingImports]
        Gst,  # pyright: ignore[reportAttributeAccessIssue]
    )

    return Gst.State.NULL


def _checked_start(server: Any, original_start: Callable[[Any], None]) -> None:
    """``GstMediaServer.start`` with the camera check: start, then keep rebuilding on the
    next index while the device that opened is not the robot's camera."""
    original_start(server)
    if platform.system() != "Darwin" or not str(server._cam_path).isdigit():
        return  # avfvideosrc is macOS-only; a non-numeric path is the sim or a webcam
    first = int(server._cam_path)

    def opened() -> str | None:
        return _opened_device_name(server._pipeline_sender)

    def restart(index: int) -> None:
        server._pipeline_sender.set_state(_gst_null())
        server._cam_path = str(index)
        original_start(server)

    select_camera(
        opened, restart, first=first, count=max(_video_device_count(), first + 1)
    )


def install_macos_camera_check() -> None:
    """Wrap upstream's ``GstMediaServer.start`` with the camera check (idempotent)."""
    from reachy_mini.media import media_server as upstream

    original_start = upstream.GstMediaServer.start
    if getattr(original_start, "_bridge_camera_check", False):
        return

    def start(self: Any) -> None:
        _checked_start(self, original_start)

    start._bridge_camera_check = True  # type: ignore[attr-defined]
    upstream.GstMediaServer.start = start


# --- the launcher -----------------------------------------------------------------------


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run the Reachy Mini hardware daemon for a robot on this machine's "
        "USB, with the bridge's macOS camera check (specs/daemon/real_daemon.md). Unrecognised "
        "flags go to upstream's daemon.",
    )
    parser.add_argument(
        "--preload-datasets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="pre-download the recorded-move datasets in the background",
    )
    return parser


def run_real_daemon(
    argv: Sequence[str] | None = None,
    *,
    prog: str = "python -m reachy_mini_bridge.real_daemon",
) -> None:
    """Run upstream's hardware daemon with the macOS camera check installed.

    Upstream's ``main()`` parses ``sys.argv``; this rewrites it to
    ``--[no-]preload-datasets`` plus anything unrecognised (``--kinematics-engine Placo``,
    say), installs the check, and calls ``main()``.
    """
    args, passthrough = _parser(prog).parse_known_args(argv)

    from reachy_mini.daemon.app import main as upstream_main

    install_macos_camera_check()
    sys.argv = [
        "reachy-mini-daemon",
        "--preload-datasets" if args.preload_datasets else "--no-preload-datasets",
        *passthrough,
    ]
    upstream_main.main()


if __name__ == "__main__":  # pragma: no cover - the daemon-side entry point
    run_real_daemon()
