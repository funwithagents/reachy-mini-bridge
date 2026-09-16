"""Functional tests for the connection seam (specs/robot.md).

These drive ``build_robot`` on the ``fake`` backend only — no daemon, no hardware.
The real/sim construction path is exercised end-to-end by the opt-in
``tests-e2e/test_api.py`` (which builds the api over a live daemon); the fake's own
behavior is covered by ``tests/test_fake_reachy_mini.py``, and its signatures are
checked against upstream here (that file stays ``reachy_mini``-free).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

import pytest
from reachy_mini import ReachyMini
from reachy_mini.media.audio_base import AudioBase
from reachy_mini.media.media_manager import MediaManager

from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.robot import build_robot


def test_build_robot_fake_returns_fake() -> None:
    robot = build_robot("fake")
    assert isinstance(robot, FakeReachyMini)


def test_build_robot_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        build_robot("bogus")


def test_context_manager_records_teardown() -> None:
    with build_robot("fake") as robot:
        assert isinstance(robot, FakeReachyMini)
        robot.enable_motors()
    assert robot.commands[-1][0] == "__exit__"


# The consumed slice (specs/robot.md): (path from the fake robot, upstream class, member).
# pyright checks call compatibility through the `AnyReachyMini` union, not parameter
# defaults, so the defaults are compared here. Lifecycle stays out: its parameter names
# differ by design and pyright covers it.
_CONSUMED_SLICE: list[tuple[str, type, str]] = [
    *(
        ("", ReachyMini, name)
        for name in (
            "set_target",
            "get_current_head_pose",
            "get_current_joint_positions",
            "start_head_tracking",
            "stop_head_tracking",
            "enable_wobbling",
            "disable_wobbling",
            "enable_motors",
            "disable_motors",
            "enable_gravity_compensation",
        )
    ),
    # `stop_sound` stays out: upstream `MediaManager` has no such member yet; the fake
    # models the proposed one (docs/upstream-play-move-cancellation.md).
    *(
        ("media", MediaManager, name)
        for name in (
            "start_recording",
            "stop_recording",
            "get_audio_sample",
            "get_input_audio_samplerate",
            "get_input_channels",
            "start_playing",
            "stop_playing",
            "push_audio_sample",
            "get_output_audio_samplerate",
            "get_output_channels",
            "play_sound",
            "get_frame",
        )
    ),
    *(
        ("media.audio", AudioBase, name)
        for name in ("apply_audio_config", "clear_player")
    ),
]


@pytest.mark.parametrize(
    ("path", "upstream", "name"),
    _CONSUMED_SLICE,
    ids=[f"{path or 'robot'}.{name}" for path, _, name in _CONSUMED_SLICE],
)
def test_fake_signatures_match_upstream(path: str, upstream: type, name: str) -> None:
    owner: object = FakeReachyMini()
    for attr in filter(None, path.split(".")):
        owner = getattr(owner, attr)

    def params(fn: Callable[..., object]) -> list[tuple[str, object]]:
        return [
            (p.name, p.default)
            for p in inspect.signature(fn).parameters.values()
            if p.name != "self"
        ]

    assert params(getattr(owner, name)) == params(getattr(upstream, name))


def test_local_audio_backend_keeps_the_playbin_the_bridge_stops() -> None:
    """The bridge's one reach into SDK internals (specs/audio.md "Stopping a sound
    file"): `GStreamerAudio` must keep the play_sound playbin as `_playbin`."""
    from reachy_mini.media.audio_gstreamer import GStreamerAudio

    assert "self._playbin = playbin" in inspect.getsource(GStreamerAudio.play_sound)
    assert "self._playbin" in inspect.getsource(GStreamerAudio.stop_playing)
