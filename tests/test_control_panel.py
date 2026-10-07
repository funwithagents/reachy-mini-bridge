"""Functional tests for the control panel's controller on the fake backend
(specs/examples/control_panel.md "Tests").

Drives `ControlPanelController` from caller threads the way the Gradio app does and
asserts through the bridge's escape hatch (`bridge.robot`, the FakeReachyMini's recorded
commands). The Gradio layer is only smoke-tested (skipped when gradio is absent).
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import math
import time
import wave
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from examples.control_panel.controller import (
    FACE_MARKER_RGB,
    ControlPanelController,
    draw_faces,
)
from reachy_mini_bridge import (
    BridgeError,
    MotorsNotEnabledError,
    PixelFace,
    ReachyMiniConfig,
)
from reachy_mini_bridge.config import FaceDetectionSettings, MotionSettings
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


class _SlowSynth:
    """About one second of audio, streamed in ten chunks — a mid-flight to stop in."""

    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        for _ in range(10):
            yield np.full(1600, 0.2, dtype=np.float32)
            await asyncio.sleep(0.01)


class _Scene:
    """The fake's stand-in for a person (specs/vision/user_perception.md "`fake` backend
    support"): a stub detector's faces on the fake's 64x48 frame, shown and hidden."""

    def __init__(self) -> None:
        self.faces: list[PixelFace] = []

    def detector(self) -> _Scene:
        return self

    def detect(self, frame_bgr: npt.NDArray[np.uint8], ts: float) -> list[PixelFace]:
        return list(self.faces)

    def show(self, x: float = 0.0, y: float = 0.0) -> None:
        u = (x + 1.0) / 2.0 * 63
        v = (y + 1.0) / 2.0 * 47
        self.faces[:] = [PixelFace(bbox=(u - 5, v - 8, 10, 16), nose=(u, v))]

    def hide(self) -> None:
        self.faces.clear()


def _faces_config(scene: _Scene, *, tracking: bool = True) -> ReachyMiniConfig:
    """A fake config detecting through ``scene`` (detection on; tracking as asked)."""
    return ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="custom", enabled=True, face_detector=scene.detector
        ),
        motion=MotionSettings(tracking=tracking),
    )


def _fake(controller: ControlPanelController) -> FakeReachyMini:
    robot = controller.bridge.robot
    assert isinstance(robot, FakeReachyMini)
    return robot


def _commands(controller: ControlPanelController) -> list[str]:
    return [name for name, _ in _fake(controller).commands]


def _wav(directory: Path, seconds: float, name: str) -> Path:
    """Write ``seconds`` of 16 kHz mono silence as a WAV file."""
    path = directory / name
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


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
        assert controller.running and controller.bridge.running
        fake = _fake(controller)
        names = _commands(controller)
        assert "media.start_recording" in names
        assert "media.start_playing" in names
        assert controller.emotions == ["happy", "sad", "curious"]

    assert not controller.running and not controller.bridge.running
    after = [name for name, _ in fake.commands]
    assert after.index("media.stop_recording") > after.index("media.start_recording")
    assert "media.stop_playing" in after
    with pytest.raises(BridgeError):
        controller.get_motors_state()
    with pytest.raises(BridgeError):
        controller.say("hello")


def test_start_timeout_cancels_the_bring_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from reachy_mini_bridge import bridge as bridge_module

    async def slow_start(self: object) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(bridge_module.MediaSession, "start", slow_start)
    controller = ControlPanelController("fake")
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        controller.start(timeout=0.2)
    assert time.monotonic() - t0 < 2.0
    assert not controller.running
    # The bring-up unwound: the robot connection it had opened is closed again.
    with pytest.raises(BridgeError):
        _ = controller.bridge.robot


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

        assert controller.busy == []
        with pytest.raises(FileNotFoundError):
            controller.play_sound("no-such-sound.wav")


def test_play_sound_plays_to_its_end_and_stops_from_another_thread(
    tmp_path: Path,
) -> None:
    short, long = _wav(tmp_path, 0.2, "short.wav"), _wav(tmp_path, 3.0, "long.wav")
    with (
        ControlPanelController("fake") as controller,
        cf.ThreadPoolExecutor(max_workers=1) as pool,
    ):
        assert controller.play_sound(str(short)) is True
        started = pool.submit(controller.play_sound, str(long))
        _wait_until(lambda: _commands(controller).count("media.play_sound") == 2)
        assert controller.busy == ["sound"]
        assert controller.stop_sound() == 1
        assert started.result(timeout=2.0) is False
        assert "media.stop_sound" in _commands(controller)
        assert controller.busy == []


def test_a_new_sound_file_stops_the_one_playing(tmp_path: Path) -> None:
    first, second = _wav(tmp_path, 3.0, "first.wav"), _wav(tmp_path, 0.2, "second.wav")
    with (
        ControlPanelController("fake") as controller,
        cf.ThreadPoolExecutor(max_workers=2) as pool,
    ):
        started = pool.submit(controller.play_sound, str(first))
        _wait_until(lambda: "media.play_sound" in _commands(controller))
        assert controller.play_sound(str(second)) is True
        assert started.result(timeout=2.0) is False


def test_snapshot_reflects_the_modes_and_the_camera_is_rgb() -> None:
    scene = _Scene()
    with ControlPanelController(_faces_config(scene, tracking=False)) as controller:
        state = controller.snapshot()
        assert state.backend == "fake"
        assert (state.presence, state.idle, state.wobbling) == (True, "breathing", True)
        assert state.tracking is False
        assert state.attention is None
        assert state.voice == "none"
        assert state.mic_sample_rate == 16000
        assert state.busy == []

        controller.set_presence(False)
        controller.set_idle("hold")
        controller.set_wobbling(False)
        controller.set_head_tracking(True, focus=True)  # a mode: no motors needed
        state = controller.snapshot()
        assert (state.presence, state.idle, state.wobbling) == (False, "hold", False)
        assert state.tracking is True
        assert state.attention == "watching"  # nobody there yet
        scene.show(0.2, 0.0)
        _wait_until(lambda: controller.snapshot().attention == "engaged")
        scene.hide()
        assert controller.bridge.tracking_focus is True

        controller.set_head_tracking(False, focus=True)  # the Tracking box unticked
        state = controller.snapshot()
        assert controller.bridge.tracking_focus is False
        assert state.tracking is False
        assert state.attention is None

        assert state.face_detection is True
        _wait_until(lambda: controller.snapshot().faces == 0)  # active, nobody there
        scene.show()
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

    scene = _Scene()
    with ControlPanelController(_faces_config(scene)) as controller:
        demo = build_app(controller)
        assert isinstance(demo, gr.Blocks)
        scene.show(0.5, -0.5)
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
    scene = _Scene()
    with ControlPanelController(_faces_config(scene)) as controller:
        _wait_until(lambda: controller.snapshot().face_rate is not None)
        # The detector runs once per frame of the fake's camera (10 fps), so the meter
        # reads the feed's rate: about ten observations a second, face or not.
        scene.show(0.25, -0.1)
        time.sleep(2.5)
        state = controller.snapshot()
        assert state.faces == 1
        assert len(state.face_positions) == 1
        x, y = state.face_positions[0]
        assert (x, y) == (pytest.approx(0.25, abs=0.02), pytest.approx(-0.1, abs=0.03))
        assert state.face_rate is not None and 7.0 <= state.face_rate <= 13.0

        scene.hide()  # an empty room still reports at the detector's rate
        _wait_until(lambda: controller.snapshot().faces == 0)
        time.sleep(1.0)
        state = controller.snapshot()
        assert state.face_positions == []
        assert state.face_rate is not None and 7.0 <= state.face_rate <= 13.0

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


def test_snapshot_marks_the_tracked_face_by_track_id() -> None:
    """The report lists faces by track id; the target is the one the tracker follows —
    the biggest — found by its id, not the first (specs/examples/control_panel.md)."""
    scene = _Scene()
    with ControlPanelController(_faces_config(scene)) as controller:
        _wait_until(lambda: controller.snapshot().face_rate is not None)
        # a small face on the left first, a big one on the right second
        scene.faces[:] = [
            PixelFace(bbox=(13.0, 20.0, 6.0, 8.0), nose=(16.0, 24.0)),
            PixelFace(bbox=(42.0, 14.0, 12.0, 20.0), nose=(48.0, 24.0)),
        ]
        _wait_until(lambda: controller.snapshot().face_target is not None, timeout=5)
        state = controller.snapshot()
        report = controller.bridge.faces.value
        assert state.faces == 2 and state.face_target == 1
        assert state.face_positions[0][0] < 0 < state.face_positions[1][0]
        assert (
            report.faces[1].track_id == controller.bridge.head_tracking.value.track_id
        )
        view = controller.face_view()
        assert view.active and view.target == 1
        assert len(view.positions) == len(view.rolls) == 2

        controller.stop_head_tracking()
        _wait_until(lambda: controller.snapshot().face_target is None)
        assert controller.snapshot().faces == 2  # still reported, none followed


def test_draw_faces_thickens_the_target_only() -> None:
    frame = np.zeros((240, 480, 3), dtype=np.uint8)
    positions = [(-0.5, 0.0), (0.5, 0.0)]

    def green_per_side(marked: npt.NDArray[np.uint8]) -> tuple[int, int]:
        green = (marked == FACE_MARKER_RGB).all(axis=2)
        return int(green[:, :240].sum()), int(green[:, 240:].sum())

    left, right = green_per_side(draw_faces(frame, positions, target=1))
    assert right > left * 1.5  # the target's ring is twice as thick
    left, right = green_per_side(draw_faces(frame, positions, target=0))
    assert left > right * 1.5
    left, right = green_per_side(draw_faces(frame, positions))
    assert left == right  # no target: both thin
