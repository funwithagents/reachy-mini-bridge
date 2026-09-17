"""Functional tests for the control panel's controller on the fake backend
(specs/control_panel.md "Tests").

Drives `ControlPanelController` from caller threads the way the Gradio app does and
asserts through the api's escape hatch (`api.robot`, the FakeReachyMini's recorded
commands). The Gradio layer is only smoke-tested (skipped when gradio is absent).
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import time
from collections.abc import AsyncIterator, Callable

import numpy as np
import numpy.typing as npt
import pytest

from examples.control_panel.controller import ControlPanelController, PanelState
from reachy_mini_bridge import BridgeError, MotorsNotEnabledError, ReachyMiniConfig
from reachy_mini_bridge.config import MotionSettings
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


class _SlowSynth:
    """About one second of audio, streamed in ten chunks — a mid-flight to stop in."""

    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        for _ in range(10):
            yield np.full(1600, 0.2, dtype=np.float32)
            await asyncio.sleep(0.01)


def _fake(controller: ControlPanelController) -> FakeReachyMini:
    robot = controller.api.robot
    assert isinstance(robot, FakeReachyMini)
    return robot


def _commands(controller: ControlPanelController) -> list[str]:
    return [name for name, _ in _fake(controller).commands]


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


# --- lifecycle ------------------------------------------------------------------------


def test_start_enters_the_session_and_stop_exits_it() -> None:
    controller = ControlPanelController("fake")
    assert not controller.running
    with pytest.raises(BridgeError):
        controller.get_motors_state()

    with controller:
        assert controller.running
        fake = _fake(controller)
        names = _commands(controller)
        assert "media.start_recording" in names
        assert "media.start_playing" in names
        assert controller.emotions == ["happy", "sad", "curious"]

    assert not controller.running
    after = [name for name, _ in fake.commands]
    assert after.index("media.stop_recording") > after.index("media.start_recording")
    assert "media.stop_playing" in after
    with pytest.raises(BridgeError):
        controller.get_motors_state()
    with pytest.raises(BridgeError):
        controller.say("hello")


def test_start_timeout_cancels_the_bring_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from reachy_mini_bridge import api as api_module

    async def slow_enter(self: object) -> object:
        await asyncio.sleep(10)
        return self

    monkeypatch.setattr(api_module.MediaSession, "__aenter__", slow_enter)
    controller = ControlPanelController("fake")
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        controller.start(timeout=0.2)
    assert time.monotonic() - t0 < 2.0
    assert not controller.running
    # The bring-up unwound: the robot connection it had opened is closed again.
    with pytest.raises(BridgeError):
        _ = controller.api.robot


# --- instant verbs --------------------------------------------------------------------


def test_instant_verbs_dispatch_and_errors_propagate() -> None:
    with ControlPanelController("fake") as controller:
        controller.set_motors_state("disabled")
        assert controller.get_motors_state() == "disabled"
        with pytest.raises(MotorsNotEnabledError):
            controller.play_emotion("happy")
        with pytest.raises(ValueError):
            controller.set_motors_state("bogus")

        controller.set_motors_state("enabled")
        assert controller.get_motors_state() == "enabled"
        with pytest.raises(ValueError):
            controller.play_emotion("no-such-emotion")
        assert controller.play_emotion("sad") is True

        controller.play_sound("ding.wav")
        assert ("media.play_sound", {"sound_file": "ding.wav"}) in _fake(
            controller
        ).commands
        assert controller.busy == []


def test_snapshot_reflects_the_modes_and_the_camera_is_rgb() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(tracking=False))
    with ControlPanelController(config) as controller:
        state = controller.snapshot()
        assert isinstance(state, PanelState)
        assert state.backend == "fake"
        assert state.motors in ("enabled", "disabled", "gravity_compensation")
        assert (state.presence, state.breathing, state.wobbling) == (True, True, True)
        assert state.tracking is False
        assert state.attention is None
        assert state.voice == "none"
        assert state.mic_sample_rate == 16000
        assert state.busy == []
        assert state.emotions == ["happy", "sad", "curious"]

        controller.set_presence(False)
        controller.set_breathing(False)
        controller.set_wobbling(False)
        controller.set_motors_state("enabled")
        controller.start_head_tracking(0.5)
        state = controller.snapshot()
        assert (state.presence, state.breathing, state.wobbling) == (
            False,
            False,
            False,
        )
        assert state.tracking is True
        assert state.attention == "engaged"
        assert ("start_head_tracking", {"weight": 0.5}) in _fake(controller).commands

        controller.stop_head_tracking()
        state = controller.snapshot()
        assert state.tracking is False
        assert state.attention is None

        frame = controller.camera_frame_rgb()
        assert frame is not None
        assert frame.shape == (48, 64, 3)
        # The fake ramps its B channel left→right; flipped to RGB that is channel 2.
        assert frame[0, 0, 2] == 0 and frame[0, -1, 2] == 255
        assert frame[0, -1, 0] == 0


def test_voice_reports_a_synthesizer_or_its_absence() -> None:
    with ControlPanelController("fake", synthesizer=_SlowSynth()) as controller:
        assert controller.snapshot().voice == "ready"
    broken = ReachyMiniConfig.from_dict(
        {"backend": "fake", "tts": {"module": {"type": "no-such-tts-module"}}}
    )
    with ControlPanelController(broken) as controller:
        assert controller.snapshot().voice.startswith("unavailable: ")
        with pytest.raises(BridgeError):
            controller.say("hello")


# --- spanning verbs, stopped mid-flight from another thread ---------------------------


def test_say_stopped_mid_flight_flushes_the_speaker_and_the_session_stays_usable() -> (
    None
):
    with (
        ControlPanelController("fake", synthesizer=_SlowSynth()) as controller,
        cf.ThreadPoolExecutor(max_workers=1) as pool,
    ):
        started = pool.submit(controller.say, "a sentence that takes a second")
        _wait_until(lambda: "media.push_audio_sample" in _commands(controller))
        assert controller.busy == ["say"]

        t0 = time.monotonic()
        assert controller.stop_saying() == 1
        assert started.result(timeout=2.0) is False
        assert time.monotonic() - t0 < 0.5
        assert "audio.clear_player" in _commands(controller)
        assert controller.busy == []

        assert controller.stop_saying() == 0
        assert controller.say("again") is True


def test_a_new_say_stops_the_one_playing() -> None:
    with (
        ControlPanelController("fake", synthesizer=_SlowSynth()) as controller,
        cf.ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(controller.say, "first")
        _wait_until(lambda: controller.busy == ["say"])
        second = pool.submit(controller.say, "second")
        assert first.result(timeout=2.0) is False
        assert second.result(timeout=3.0) is True
        assert controller.busy == []


def test_emotion_stopped_mid_flight_stops_its_sound_and_the_next_one_plays() -> None:
    with (
        ControlPanelController("fake") as controller,
        cf.ThreadPoolExecutor(max_workers=1) as pool,
    ):
        controller.set_motors_state("enabled")
        started = pool.submit(controller.play_emotion, "happy")
        _wait_until(lambda: "media.play_sound" in _commands(controller))
        assert controller.busy == ["emotion"]

        assert controller.stop_emotion() == 1
        assert started.result(timeout=2.0) is False
        assert "media.stop_sound" in _commands(controller)
        assert controller.busy == []

        assert controller.play_emotion("happy") is True
        assert _commands(controller).count("media.play_sound") == 2


def test_stop_cancels_the_verbs_in_flight() -> None:
    controller = ControlPanelController("fake", synthesizer=_SlowSynth())
    controller.start()
    with cf.ThreadPoolExecutor(max_workers=1) as pool:
        started = pool.submit(controller.say, "cut short by the shutdown")
        _wait_until(lambda: controller.busy == ["say"])
        controller.stop()
        assert started.result(timeout=2.0) is False
    assert not controller.running


# --- the Gradio layer (smoke) ---------------------------------------------------------


def test_build_app_constructs_the_blocks_and_refresh_reads_the_state() -> None:
    gr = pytest.importorskip("gradio")
    from examples.control_panel.app import build_app, refresh

    with ControlPanelController("fake") as controller:
        demo = build_app(controller)
        assert isinstance(demo, gr.Blocks)
        table, mic_level, frame, log_text = refresh(controller, [])
        assert "fake" in table and "presence" in table
        assert mic_level == 0.0
        assert frame is not None and frame.shape == (48, 64, 3)
        assert log_text == ""
