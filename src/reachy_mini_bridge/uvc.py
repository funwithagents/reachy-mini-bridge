"""The robot camera's UVC controls over USB (specs/daemon/real_daemon.md "The camera exposure").

The camera found by its product name, the way upstream's detection finds it
(``robot_camera``), its camera input terminal parsed from the VideoControl descriptors
(``camera_terminal``), and one control acted on: the auto-exposure priority
(``enable_ae_priority``), which lets the camera lower its frame rate to expose longer —
upstream's documented cure for the dark image its 1920x1080@60 mode gives. The requests
go through pyusb on the libusb that ``libusb_package`` bundles, both upstream
dependencies already, on the device's endpoint zero with no interface claim, so they
work beside a pipeline that has the camera open. The real daemon launcher
(``real_daemon.py``) is the caller; nothing here imports pyusb until it runs.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

__all__ = [
    "AePriorityOutcome",
    "CameraTerminal",
    "LibusbControl",
    "UvcControl",
    "camera_terminal",
    "enable_ae_priority",
    "pick_camera",
    "robot_camera",
]

_logger = logging.getLogger(__name__)

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


# --- the camera terminal and the decision ---------------------------------------------


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
    """A camera terminal's controls over USB (``LibusbControl``; tests script it):
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


class LibusbControl:
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


_T = TypeVar("_T")


def pick_camera(cameras: Sequence[tuple[str, _T]], names: Sequence[str]) -> _T | None:
    """The camera upstream's detection would pick among ``cameras`` (product name,
    camera): the first whose name contains a wanted name, ``names`` tried in priority
    order — the first name that any camera matches wins, as upstream's
    ``find_video_device`` does. ``None`` when no name matches."""
    for wanted in names:
        for name, camera in cameras:
            if wanted in name:
                return camera
    return None


def _video_control(device: Any) -> tuple[int, bytes] | None:
    """The VideoControl interface of ``device`` — its number and class-specific
    descriptors — or ``None`` for a device that is not a UVC camera."""
    for interface in device.get_active_configuration():
        if (
            interface.bInterfaceClass == _UVC_VIDEO_CLASS
            and interface.bInterfaceSubClass == _UVC_VIDEOCONTROL_SUBCLASS
        ):
            return int(interface.bInterfaceNumber), bytes(interface.extra_descriptors)
    return None


def robot_camera(names: Sequence[str]) -> tuple[LibusbControl, CameraTerminal] | None:
    """The robot's camera on USB with its camera terminal, found the way upstream's
    detection finds it: among the UVC devices on the bus — those with a VideoControl
    interface — the first whose product name contains one of ``names``, tried in
    priority order (``pick_camera``). ``None`` without one, or when the camera's
    VideoControl interface names no camera terminal. A device whose descriptors or
    name cannot be read — one the system will not open — is skipped."""
    import usb.core
    import usb.util
    from libusb_package import get_libusb1_backend

    devices: Any = usb.core.find(find_all=True, backend=get_libusb1_backend())
    cameras: list[tuple[str, tuple[Any, int, bytes]]] = []
    for device in devices or ():
        try:
            video_control = _video_control(device)
            if video_control is None:
                continue
            name = str(device.product or "")
        except Exception:  # noqa: BLE001 - a device this process cannot read
            continue
        cameras.append((name, (device, *video_control)))
    picked = pick_camera(cameras, names)
    for _name, (device, _interface, _descriptors) in cameras:
        if picked is None or device is not picked[0]:
            usb.util.dispose_resources(device)
    if picked is None:
        return None
    device, interface, descriptors = picked
    terminal = camera_terminal(descriptors)
    if terminal is None:
        usb.util.dispose_resources(device)
        return None
    return LibusbControl(device, interface, terminal.terminal_id), terminal
