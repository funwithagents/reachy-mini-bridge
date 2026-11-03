"""The real daemon launcher: every hardware daemon the bridge starts (specs/daemon/real_daemon.md).

``python -m reachy_mini_bridge.real_daemon [--[no-]preload-datasets] [upstream flags...]``
runs upstream's ``reachy-mini-daemon`` for a robot on this machine's USB, in this
interpreter, with two macOS corrections of the camera. On macOS the daemon opens its
camera through ``avfvideosrc device-index=N``, and the device order that index reads —
AVFoundation's — is not stable: it moves between two opens in one process. Every build
of the media pipeline is therefore a draw between the robot's camera and the computer's
own, and the wrong pick fails the robot camera's caps (``not-negotiated``), leaving the
daemon without video: audio keeps working, the IPC socket exists, no frame ever comes.
The launcher reads which device each build actually opened (``avfvideosrc``'s read-only
``device-name``) and rebuilds on the next index until it is the robot's camera.

And the camera it opens is dark: upstream pins the Lite camera to its 1920x1080@60 mode,
where the camera holds the frame rate and caps its exposure at 1/60 s, three to four
times too dark in room light. The launcher sets the camera's UVC auto-exposure priority
over USB — one control request beside the pipeline, through upstream's own pyusb and
libusb — so the camera may ease its frame rate to expose longer: upstream's documented
cure, applied by the daemon itself.

Importing this module pulls in neither the daemon, GStreamer nor pyusb; the daemon-side
pieces import them when they run.
"""

from __future__ import annotations

import argparse
import logging
import platform
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .config import DAEMON_INFO_ROUTE

__all__ = [
    "LITE_CAMERA_USB_ID",
    "CameraTerminal",
    "UvcControl",
    "camera_terminal",
    "enable_ae_priority",
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

# The Lite camera on USB: upstream's `ReachyMiniLiteCamSpecs.vid` / `.pid` (a test pins
# the parity).
LITE_CAMERA_USB_ID = (0x38FB, 0x1002)
# UVC 1.5 (the USB Video Class specification): the class-specific requests, the camera
# terminal's auto-exposure priority control, and the descriptor bytes naming the terminal
# — a VideoControl interface (video class, VideoControl subclass) carries one camera
# input terminal whose `bmControls` bit 2 says whether it has the control.
_UVC_SET_CUR, _UVC_GET_CUR = 0x01, 0x81
_UVC_CT_AE_PRIORITY_CONTROL = 0x03
_UVC_CS_INTERFACE, _UVC_VC_INPUT_TERMINAL, _UVC_ITT_CAMERA = 0x24, 0x02, 0x0201
_UVC_VIDEO_CLASS, _UVC_VIDEOCONTROL_SUBCLASS = 0x0E, 0x01
_UVC_AE_PRIORITY_BIT = 2
# The camera terminal descriptor: ..., bTerminalID at 3, wTerminalType at 4, bControlSize
# at 14, bmControls from 15.
_CT_TERMINAL_ID, _CT_TERMINAL_TYPE, _CT_CONTROL_SIZE, _CT_CONTROLS = 3, 4, 14, 15
_USB_TIMEOUT_MS = 1000


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


# --- the camera exposure (specs/daemon/real_daemon.md "The camera exposure") -----------


@dataclass(frozen=True)
class CameraTerminal:
    """A UVC camera input terminal as the VideoControl descriptors describe it: the id
    the requests address, and whether it advertises the auto-exposure priority control."""

    terminal_id: int
    ae_priority: bool


def camera_terminal(descriptors: bytes) -> CameraTerminal | None:
    """The camera input terminal among a VideoControl interface's class-specific
    descriptors, ``None`` when there is none or a block is malformed."""
    i = 0
    while i + 1 < len(descriptors):
        length, kind = descriptors[i], descriptors[i + 1]
        if length < 2:
            return None
        block = descriptors[i : i + length]
        if (
            kind == _UVC_CS_INTERFACE
            and len(block) > _CT_CONTROL_SIZE
            and block[2] == _UVC_VC_INPUT_TERMINAL
            and int.from_bytes(
                block[_CT_TERMINAL_TYPE : _CT_TERMINAL_TYPE + 2], "little"
            )
            == _UVC_ITT_CAMERA
        ):
            size = block[_CT_CONTROL_SIZE]
            controls = int.from_bytes(
                block[_CT_CONTROLS : _CT_CONTROLS + size], "little"
            )
            return CameraTerminal(
                terminal_id=block[_CT_TERMINAL_ID],
                ae_priority=bool(controls >> _UVC_AE_PRIORITY_BIT & 1),
            )
        i += length
    return None


class UvcControl(Protocol):
    """A camera terminal's controls over USB (``_LibusbControl``; tests script it):
    ``get`` a class request on a selector, ``set`` its current value."""

    def get(self, selector: int, request: int, length: int) -> bytes: ...
    def set(self, selector: int, data: bytes) -> None: ...


AePriorityOutcome = Literal["enabled", "already", "unsupported", "refused"]


def enable_ae_priority(
    control: UvcControl, terminal: CameraTerminal
) -> AePriorityOutcome:
    """Set the camera's auto-exposure priority to 1 — the camera may lower its frame rate
    to expose longer — and say what happened, one log line each.

    A terminal that does not advertise the control is left alone (``unsupported``); a
    flag already at 1 is not written (``already``); otherwise it is set and read back
    (``enabled``), and a request the camera or the bus refuses, or a read-back that is
    not 1, leaves the image as upstream has it (``refused``).
    """
    dark = "the image stays as upstream leaves it (1920x1080@60, exposure capped at 1/60 s)"
    if not terminal.ae_priority:
        _logger.warning(
            "robot camera: its camera terminal does not advertise the UVC auto-exposure "
            "priority control; %s",
            dark,
        )
        return "unsupported"
    try:
        current = control.get(_UVC_CT_AE_PRIORITY_CONTROL, _UVC_GET_CUR, 1)
        if current[:1] == b"\x01":
            _logger.debug("robot camera: UVC auto-exposure priority already enabled")
            return "already"
        control.set(_UVC_CT_AE_PRIORITY_CONTROL, b"\x01")
        read_back = control.get(_UVC_CT_AE_PRIORITY_CONTROL, _UVC_GET_CUR, 1)
    except OSError as e:  # pyusb's USBError is one: a stall, a bus error
        _logger.warning(
            "robot camera: the camera refused the UVC auto-exposure priority request "
            "(%s); %s",
            e,
            dark,
        )
        return "refused"
    if read_back[:1] != b"\x01":
        _logger.warning(
            "robot camera: UVC auto-exposure priority reads back %r after being set; %s",
            read_back,
            dark,
        )
        return "refused"
    _logger.info(
        "robot camera: UVC auto-exposure priority enabled — the camera may lower its "
        "frame rate below 60 to expose longer (specs/daemon/real_daemon.md)"
    )
    return "enabled"


# --- the USB glue (runs in the daemon process) ----------------------------------------


class _LibusbControl:
    """UVC class requests to one camera terminal over pyusb's libusb backend, sent on the
    device's endpoint zero with no interface claim: the requests need none, and macOS's
    camera driver holds the VideoControl interface while the camera streams, which is
    why ``Device.ctrl_transfer`` — it claims the interface of every interface-directed
    request — is refused there."""

    def __init__(self, device: Any, interface: int, terminal_id: int) -> None:
        self._device = device
        self._index = (terminal_id << 8) | interface

    def _transfer(
        self, request_type: int, request: int, selector: int, data: Any
    ) -> Any:
        context = self._device._ctx
        context.managed_open()
        return context.backend.ctrl_transfer(
            context.handle,
            request_type,
            request,
            selector << 8,
            self._index,
            data,
            _USB_TIMEOUT_MS,
        )

    def get(self, selector: int, request: int, length: int) -> bytes:
        import usb.util

        buffer = usb.util.create_buffer(length)
        # class request, interface recipient, device to host
        n = self._transfer(0xA1, request, selector, buffer)
        return bytes(buffer[:n])

    def set(self, selector: int, data: bytes) -> None:
        import array

        # class request, interface recipient, host to device
        self._transfer(0x21, _UVC_SET_CUR, selector, array.array("B", data))

    def close(self) -> None:
        import usb.util

        usb.util.dispose_resources(self._device)


def _lite_camera() -> tuple[_LibusbControl, CameraTerminal] | None:
    """The Lite camera on USB with its camera terminal — ``None`` without one attached,
    or when its VideoControl interface names no camera terminal."""
    import usb.core
    from libusb_package import get_libusb1_backend

    vid, pid = LITE_CAMERA_USB_ID
    device: Any = usb.core.find(
        idVendor=vid, idProduct=pid, backend=get_libusb1_backend()
    )
    if device is None:
        return None
    for interface in device.get_active_configuration():
        if (
            interface.bInterfaceClass == _UVC_VIDEO_CLASS
            and interface.bInterfaceSubClass == _UVC_VIDEOCONTROL_SUBCLASS
        ):
            terminal = camera_terminal(bytes(interface.extra_descriptors))
            if terminal is None:
                return None
            control = _LibusbControl(
                device, interface.bInterfaceNumber, terminal.terminal_id
            )
            return control, terminal
    return None


def _lite_ae_priority() -> None:
    """The exposure step of a macOS start: find the Lite camera and enable its
    auto-exposure priority; whatever goes wrong is one line and the daemon runs on."""
    try:
        found = _lite_camera()
    except Exception as e:  # noqa: BLE001 - no USB backend, a device that cannot open
        _logger.warning(
            "robot camera: cannot reach the camera over USB to enable its auto-exposure "
            "priority (%s); the image stays as upstream leaves it",
            e,
        )
        return
    if found is None:
        _logger.debug("robot camera: no Lite camera on USB; auto-exposure left alone")
        return
    control, terminal = found
    try:
        enable_ae_priority(control, terminal)
    finally:
        control.close()


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
    deadline = time.perf_counter() + timeout_s
    while True:
        name = source.get_property("device-name")
        if name:
            return str(name)
        if time.perf_counter() >= deadline:
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
    """``GstMediaServer.start`` with both corrections: start, keep rebuilding on the next
    index while the device that opened is not the robot's camera, then enable the Lite
    camera's auto-exposure priority."""
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
    _lite_ae_priority()


def install_macos_camera_check() -> None:
    """Wrap upstream's ``GstMediaServer.start`` with the macOS camera corrections — the
    check and the exposure (idempotent)."""
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


def _report() -> dict[str, Any]:
    """The answer of ``GET /api/bridge/daemon`` (specs/daemon/daemon.md "What the daemon
    reports"): the rest of the report is the sim's."""
    return {"launcher": "real"}


def run_real_daemon(
    argv: Sequence[str] | None = None,
    *,
    prog: str = "python -m reachy_mini_bridge.real_daemon",
) -> None:
    """Run upstream's hardware daemon with the macOS camera check installed.

    Upstream's ``main()`` parses ``sys.argv``; this rewrites it to
    ``--[no-]preload-datasets`` plus anything unrecognised (``--kinematics-engine Placo``,
    say), installs the check and the report's route, and calls ``main()``.
    """
    args, passthrough = _parser(prog).parse_known_args(argv)

    from reachy_mini.daemon.app import main as upstream_main

    install_macos_camera_check()
    original_create_app = upstream_main.create_app

    def create_app(*a: Any, **kw: Any) -> Any:
        app = original_create_app(*a, **kw)
        app.get(DAEMON_INFO_ROUTE, tags=["bridge-daemon"])(_report)
        return app

    upstream_main.create_app = create_app
    sys.argv = [
        "reachy-mini-daemon",
        "--preload-datasets" if args.preload_datasets else "--no-preload-datasets",
        *passthrough,
    ]
    upstream_main.main()


if __name__ == "__main__":  # pragma: no cover - the daemon-side entry point
    run_real_daemon()
