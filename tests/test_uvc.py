"""Functional tests for the Lite camera's UVC controls (specs/daemon/real_daemon.md "The
camera exposure") — no robot, no USB: the descriptor parse on hand-built bytes, the
auto-exposure priority decision driven with a scripted control.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from reachy_mini_bridge import uvc

_LOGGER = "reachy_mini_bridge.uvc"


# --- the camera exposure: the parse -----------------------------------------------------


def _vc_header() -> bytes:
    """A VideoControl header descriptor, as a camera's descriptors open with."""
    return bytes([13, 0x24, 0x01, 0x10, 0x01, 0, 0, 0, 0, 0, 0, 1, 1])


def _input_terminal(terminal_id: int, terminal_type: int, controls: bytes) -> bytes:
    """An input terminal descriptor of the camera layout (the focal-length words and
    ``bControlSize`` precede ``bmControls``)."""
    body = bytes(
        [0x24, 0x02, terminal_id, *terminal_type.to_bytes(2, "little"), 0, 0]
        + [0] * 6
        + [len(controls)]
    )
    return bytes([len(body) + 1 + len(controls)]) + body + controls


_PRIORITY_ADVERTISED = b"\x0e\x00\x00"  # bits 1–3: AE mode, AE priority, exposure time
_PRIORITY_MISSING = b"\x0a\x00\x00"  # bits 1 and 3 only


def test_camera_terminal_reads_the_camera_terminals_id_and_priority_bit() -> None:
    descriptors = (
        _vc_header()
        + _input_terminal(1, 0x0201, _PRIORITY_ADVERTISED)
        + bytes([9, 0x24, 0x05, 2, 1, 0, 0, 0, 0])  # a processing unit after it
    )
    assert uvc.camera_terminal(descriptors) == uvc.CameraTerminal(
        terminal_id=1, ae_priority=True
    )
    without = _vc_header() + _input_terminal(3, 0x0201, _PRIORITY_MISSING)
    assert uvc.camera_terminal(without) == uvc.CameraTerminal(
        terminal_id=3, ae_priority=False
    )


@pytest.mark.parametrize(
    "descriptors",
    [
        b"",
        _vc_header(),
        _vc_header() + _input_terminal(1, 0x0101, _PRIORITY_ADVERTISED),
        bytes([0, 0x24]) + _input_terminal(1, 0x0201, _PRIORITY_ADVERTISED),
    ],
    ids=[
        "nothing",
        "a header alone",
        "a streaming terminal only",
        "a zero-length block",
    ],
)
def test_camera_terminal_is_none_without_a_camera_terminal(descriptors: bytes) -> None:
    assert uvc.camera_terminal(descriptors) is None


@pytest.mark.parametrize(
    ("cameras", "picked"),
    [
        ([("Caméra du MacBook Pro", 0), ("Reachy Mini Camera", 1)], 1),
        ([("Arducam_12MP Camera", 0), ("Reachy Mini Camera", 1)], 1),
        ([("Arducam_12MP Camera", 0), ("Caméra du MacBook Pro", 1)], 0),
        ([("Caméra du MacBook Pro", 0), ("", 1)], None),
        ([], None),
    ],
    ids=["the robot's", "Reachy before Arducam", "Arducam", "none named", "no camera"],
)
def test_pick_camera_matches_names_in_upstreams_priority_order(
    cameras: list[tuple[str, int]], picked: int | None
) -> None:
    assert uvc.pick_camera(cameras, ("Reachy", "Arducam_12MP", "imx708")) == picked


# --- the camera exposure: the decision --------------------------------------------------


_PRIORITY = 0x03
_GET_CUR = 0x81


class _Control:
    """Scripts what each read of the control returns, records every request, and can
    refuse the write the way the bus does."""

    def __init__(self, reads: list[bytes], *, refuse: bool = False) -> None:
        self._reads = iter(reads)
        self._refuse = refuse
        self.requests: list[tuple[Any, ...]] = []
        self.closed = False

    def get(self, selector: int, request: int, length: int) -> bytes:
        self.requests.append(("get", selector, request, length))
        return next(self._reads)

    def set(self, selector: int, data: bytes) -> None:
        self.requests.append(("set", selector, data))
        if self._refuse:
            raise OSError(32, "Pipe error")

    def close(self) -> None:
        self.closed = True


_SUPPORTED = uvc.CameraTerminal(terminal_id=1, ae_priority=True)


def test_enable_ae_priority_sets_the_flag_and_reads_it_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = _Control([b"\x00", b"\x01"])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert uvc.enable_ae_priority(control, _SUPPORTED) == "enabled"
    assert control.requests == [
        ("get", _PRIORITY, _GET_CUR, 1),
        ("set", _PRIORITY, b"\x01"),
        ("get", _PRIORITY, _GET_CUR, 1),
    ]
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    assert "frame rate" in caplog.records[0].getMessage()


def test_enable_ae_priority_leaves_a_flag_already_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = _Control([b"\x01"])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert uvc.enable_ae_priority(control, _SUPPORTED) == "already"
    assert control.requests == [("get", _PRIORITY, _GET_CUR, 1)]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]


def test_enable_ae_priority_leaves_a_terminal_without_the_control(
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = _Control([])
    terminal = uvc.CameraTerminal(terminal_id=1, ae_priority=False)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert uvc.enable_ae_priority(control, terminal) == "unsupported"
    assert control.requests == []
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "1/60 s" in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    ("control", "said"),
    [
        (_Control([b"\x00"], refuse=True), "refused"),
        (_Control([b"\x00", b"\x00"]), "reads back"),
    ],
    ids=["the bus refuses the write", "the flag does not take"],
)
def test_enable_ae_priority_warns_when_the_camera_does_not_take_it(
    caplog: pytest.LogCaptureFixture, control: _Control, said: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert uvc.enable_ae_priority(control, _SUPPORTED) == "refused"
    assert control.requests[1] == ("set", _PRIORITY, b"\x01")
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert said in caplog.records[0].getMessage()
