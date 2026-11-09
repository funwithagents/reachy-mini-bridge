"""The robot's audio devices, opened by name (specs/audio/audio.md "The robot's audio
devices", specs/daemon/sim_daemon.md "Audio devices").

Upstream opens the robot's sound card by name ("Reachy Mini Audio") and falls back to the
system defaults; nothing lets a caller name another device. Its matcher already takes the
name to look for — ``find_audio_device(..., target_name=...)`` in
``reachy_mini.media.device_detection`` — but ``get_audio_device``, which every upstream
module calls, always passes the default. This module closes that gap without rewriting
upstream's matching:

- ``resolve(name, direction)`` lists the host's devices through upstream's own
  ``Gst.DeviceMonitor`` listing and keeps those upstream's matcher accepts with ``name``
  as its target — exactly one, or an ``AudioDeviceError`` naming the devices of that
  direction — and returns upstream's per-platform id for it.
- ``substitute(module, source=..., sink=...)`` replaces ``get_audio_device`` in the
  namespace of the upstream module that calls it — ``reachy_mini.media.media_server`` in
  the sim daemon, ``reachy_mini.media.audio_gstreamer`` in the bridge's process — so every
  lookup of a named direction returns the chosen id and the other keeps upstream's
  answer. Counted: nested holders of the same ids share it, other ids are refused while it
  is held, and upstream's function comes back when the last holder leaves.

The upstream option that would retire this (``ReachyMini(audio_input_device=...)`` and a
daemon flag) is specs/audio/audio.md's.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

from .errors import BridgeError

__all__ = [
    "AudioDeviceError",
    "AudioDevices",
    "resolve",
    "resolve_devices",
    "substitute",
]

Direction = Literal["Source", "Sink"]

_DIRECTION_WORDS: dict[str, str] = {"Source": "input", "Sink": "output"}


class AudioDeviceError(BridgeError):
    """A named audio device matches no device of the host, or more than one, or other
    devices are already in force in this process."""


@dataclass(frozen=True)
class AudioDevices:
    """The platform ids a process opens for the robot's microphone (``source``) and
    speaker (``sink``); ``None`` for a direction left to upstream."""

    source: str | None = None
    sink: str | None = None

    def __bool__(self) -> bool:
        return self.source is not None or self.sink is not None


def _list_devices(direction: Direction) -> Sequence[Any]:
    """The host's devices of ``direction``, as upstream lists them (a seam for tests)."""
    # upstream's module pins the GStreamer version before anything imports it
    from reachy_mini.media.device_detection import gst_monitor_devices  # noqa: I001
    from gi.repository import Gst  # pyright: ignore[reportMissingImports]

    if not Gst.is_initialized():
        Gst.init(None)
    return gst_monitor_devices(f"Audio/{direction}")


def _platform_id(device: Any, direction: Direction, name: str) -> str | None:
    """Upstream's id for ``device`` when upstream's matcher accepts it for ``name``."""
    from reachy_mini.media.device_detection import find_audio_device

    return find_audio_device([device], direction, target_name=name)


def resolve(name: str, direction: Direction) -> str:
    """The platform id of the one ``direction`` device whose name contains ``name``,
    matched as upstream matches the robot's card. ``AudioDeviceError`` naming the
    devices of that direction when none or several match."""
    devices = list(_list_devices(direction))
    matches = [
        (device, device_id)
        for device in devices
        if (device_id := _platform_id(device, direction, name)) is not None
    ]
    word = _DIRECTION_WORDS[direction]
    names = ", ".join(repr(d.display_name) for d in devices) or "none"
    if not matches:
        raise AudioDeviceError(
            f"no audio {word} device matches {name!r}; the {word} devices are: {names}"
        )
    if len(matches) > 1:
        found = ", ".join(repr(d.display_name) for d, _ in matches)
        raise AudioDeviceError(
            f"the audio {word} device {name!r} is ambiguous: it matches {found}"
        )
    return matches[0][1]


def resolve_devices(
    input_device: str | None, output_device: str | None
) -> AudioDevices:
    """The ids of the named input and output devices (``None`` stays ``None``)."""
    return AudioDevices(
        source=None if input_device is None else resolve(input_device, "Source"),
        sink=None if output_device is None else resolve(output_device, "Sink"),
    )


@dataclass
class _Held:
    devices: AudioDevices
    original: Callable[..., Any]
    count: int


_lock = threading.Lock()
_held: dict[str, _Held] = {}


def _upstream_module(module: str | ModuleType) -> ModuleType:
    return importlib.import_module(module) if isinstance(module, str) else module


@contextmanager
def substitute(module: str | ModuleType, devices: AudioDevices) -> Iterator[None]:
    """While held, ``module.get_audio_device`` returns ``devices``' id for a named
    direction and upstream's answer for the other. Nothing to substitute (no direction
    named) leaves upstream's function alone. ``AudioDeviceError`` when the module already
    holds other devices in this process."""
    if not devices:
        yield
        return
    target = _upstream_module(module)
    key = target.__name__
    with _lock:
        held = _held.get(key)
        if held is None:
            original = target.get_audio_device

            def get_audio_device(device_type: str = "Source") -> str | None:
                chosen = devices.source if device_type == "Source" else devices.sink
                return chosen if chosen is not None else original(device_type)

            setattr(target, "get_audio_device", get_audio_device)  # noqa: B010
            _held[key] = _Held(devices, original, 1)
        elif held.devices != devices:
            raise AudioDeviceError(
                f"{key} already opens other audio devices in this process "
                f"({held.devices}); one process opens one robot's devices"
            )
        else:
            held.count += 1
    try:
        yield
    finally:
        with _lock:
            held = _held[key]
            held.count -= 1
            if held.count == 0:
                setattr(target, "get_audio_device", held.original)  # noqa: B010
                del _held[key]
