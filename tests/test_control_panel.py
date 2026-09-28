"""Functional tests for the control panel's controller on the fake backend
(specs/control_panel.md "Tests").

Drives `ControlPanelController` from caller threads the way the Gradio app does and
asserts through the api's escape hatch (`api.robot`, the FakeReachyMini's recorded
commands). The Gradio layer is only smoke-tested (skipped when gradio is absent).
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import math
import time
from collections.abc import AsyncIterator, Callable

import numpy as np
import numpy.typing as npt
import pytest

from examples.control_panel.controller import (
    FACE_MARKER_RGB,
    ControlPanelController,
    PanelState,
    draw_faces,
)
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
        assert (state.presence, state.idle, state.wobbling) == (True, "breathing", True)
        assert state.tracking is False
        assert state.attention is None
        assert state.voice == "none"
        assert state.mic_sample_rate == 16000
        assert state.busy == []
        assert state.emotions == ["happy", "sad", "curious"]

        controller.set_presence(False)
        controller.set_idle("hold")
        controller.set_wobbling(False)
        controller.set_head_tracking(True, focus=True)  # a mode: no motors needed
        state = controller.snapshot()
        assert (state.presence, state.idle, state.wobbling) == (False, "hold", False)
        assert state.tracking is True
        assert state.attention == "watching"  # nobody there yet
        _fake(controller).show_face(0.2, 0.0)
        _wait_until(lambda: controller.snapshot().attention == "engaged")
        _fake(controller).hide_face()
        assert controller.api.tracking_focus is True

        controller.set_head_tracking(False, focus=True)  # the Tracking box unticked
        state = controller.snapshot()
        assert controller.api.tracking_focus is False
        assert state.tracking is False
        assert state.attention is None

        assert state.face_detection is True
        _wait_until(lambda: controller.snapshot().faces == 0)  # active, nobody there
        _fake(controller).show_face()
        _wait_until(lambda: controller.snapshot().faces == 1)
        controller.set_face_detection(False)
        state = controller.snapshot()
        assert (state.face_detection, state.faces) == (False, -1)  # nobody looking

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
    from examples.control_panel.app import build_app, refresh, refresh_camera

    with ControlPanelController("fake") as controller:
        demo = build_app(controller)
        assert isinstance(demo, gr.Blocks)
        _fake(controller).show_face(0.5, -0.5)
        _wait_until(lambda: controller.snapshot().faces == 1)
        table, mic_level, log_text = refresh(controller, [])
        assert "fake" in table and "presence" in table
        assert "faces" not in table  # under the camera instead
        assert mic_level == 0.0
        assert log_text == ""
        frame, faces_text = refresh_camera(controller, [])
        assert "1 at (+0.50, -0.50)" in faces_text and "updates" in faces_text
        assert frame is not None and frame.shape == (48, 64, 3)
        # Mirrored for display: the fake's left-to-right ramp (RGB channel 2) now falls,
        # and the face on the right of the image (x=+0.5) is marked on the left.
        assert frame[40, 0, 2] > frame[40, -1, 2]
        marked_cols = np.nonzero((frame == FACE_MARKER_RGB).all(axis=2))[1]
        assert marked_cols.size and marked_cols.max() < 32


# --- faces ----------------------------------------------------------------------------


def test_snapshot_reports_face_positions_and_the_update_rate() -> None:
    with ControlPanelController("fake") as controller:
        _wait_until(lambda: controller.snapshot().face_rate is not None)
        fake = _fake(controller)
        # A detector reporting at ~20 Hz: each show_face is a new observation (new ts).
        # The detection loop polls at 30 Hz, so the bridge sees about 20 a second.
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            fake.show_face(0.25, -0.1)
            time.sleep(0.05)
        state = controller.snapshot()
        assert state.faces == 1
        assert state.face_positions == [(0.25, -0.1)]
        assert state.face_rate is not None and 12.0 <= state.face_rate <= 21.0

        fake.hide_face()  # one last observation, then nothing new
        time.sleep(2.5)
        assert controller.snapshot().face_rate == 0.0

        controller.stop_head_tracking()
        controller.set_face_detection(False)
        state = controller.snapshot()
        assert (state.faces, state.face_positions, state.face_rate) == (-1, [], None)


def test_the_faces_line_shows_each_faces_roll_in_degrees() -> None:
    pytest.importorskip("gradio")
    from examples.control_panel.app import faces_line

    line = faces_line([(0.25, -0.1), (-0.5, 0.2)], 10.0, [math.radians(-12.4), None])
    assert "2 at (+0.25, -0.10, roll -12°), (-0.50, +0.20)" in line
    assert faces_line([(0.25, -0.1)], None) == (
        "**Faces:** 1 at (+0.25, -0.10) · **updates:** —"
    )


def test_draw_faces_tilts_the_square_with_the_faces_roll() -> None:
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    upright = (draw_faces(frame, [(0.0, 0.0)], [0.0]) == FACE_MARKER_RGB).all(axis=2)
    tilted = draw_faces(frame, [(0.0, 0.0)], [math.radians(45.0)])
    green = (tilted == FACE_MARKER_RGB).all(axis=2)

    # centre pixel (100, 50), half-side 8 px: upright, the top edge's middle is marked
    # and the square's corner too; tilted 45 deg, the corner turns to straight above
    # the centre (8 * sqrt(2) ~ 11 px), and the upright edge falls inside the square
    assert upright[42, 100] and upright[42, 108]
    assert green[39, 100] and green[61, 100] and green[50, 89] and green[50, 111]
    assert not green[42, 100] and not green[42, 108]
    # an unknown roll draws it upright
    unknown = (draw_faces(frame, [(0.0, 0.0)], [None]) == FACE_MARKER_RGB).all(axis=2)
    assert np.array_equal(unknown, upright)


def test_draw_faces_outlines_each_face_where_it_is() -> None:
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    marked = draw_faces(frame, [(0.0, 0.0), (1.0, -1.0)])
    green = (marked == FACE_MARKER_RGB).all(axis=2)

    assert not frame.any()  # the input is left alone
    # centre face: a square of half-side 8 px (15 % of 100 px, rounded) around pixel
    # (100, 50) — its outline is marked, its middle is not
    assert green[42, 100] and green[58, 100] and green[50, 92] and green[50, 108]
    assert not green[50, 100]
    # a face at the top-right corner is clipped to the frame, not dropped: the part of
    # its outline inside the frame shows (here its bottom edge)
    assert green[8, 191:200].all()
    # nothing far from either face
    assert not green[90, 20:60].any()
