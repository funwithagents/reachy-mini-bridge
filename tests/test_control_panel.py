"""Functional tests for the control panel's controller on the fake backend
(specs/examples/control_panel.md "Tests").

Drives `ControlPanelController` from caller threads the way the Gradio app does and
asserts through the bridge's escape hatch (`bridge.robot`, the FakeReachyMini's recorded
commands). The Gradio layer is only smoke-tested (skipped when gradio is absent).
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import json
import math
import time
import wave
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from app_setup.app import AppModel
from app_setup.autostart import starts_at_launch
from app_setup.folder import ConfigFolder
from app_setup.secrets import SecretStore

from examples.control_panel.controller import (
    FACE_MARKER_RGB,
    ControlPanelController,
    describe_tracking_change,
    describe_users_change,
    draw_faces,
)
from examples.control_panel.session import RobotSession
from reachy_mini_bridge import (
    BridgeError,
    HeadTrackingReport,
    MotorsNotEnabledError,
    PixelFace,
    ReachyMiniConfig,
    UserDeparture,
    UsersReport,
)
from reachy_mini_bridge.config import (
    FaceDetectionSettings,
    MotionSettings,
    TrackingSettings,
)
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


class _SlowSynth:
    """About one second of audio, streamed in ten chunks — a mid-flight to stop in."""

    sample_rate = 16000

    async def synthesize(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
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
        motion=MotionSettings(tracking=TrackingSettings(enabled=tracking)),
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
    deadline = time.perf_counter() + timeout
    while not predicate():
        assert time.perf_counter() < deadline, "condition not met in time"
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
    t0 = time.perf_counter()
    with pytest.raises(TimeoutError):
        controller.start(timeout=0.2)
    assert time.perf_counter() - t0 < 2.0
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

        t0 = time.perf_counter()
        assert controller.stop_saying() == 1
        assert started.result(timeout=2.0) is False
        assert time.perf_counter() - t0 < 0.5
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


def test_the_page_builds_over_a_session_and_its_ticks_read_the_state(
    tmp_path: Path,
) -> None:
    gr = pytest.importorskip("gradio")
    from examples.control_panel.app import (
        build_page,
        refresh,
        refresh_camera,
        refresh_session,
        refresh_users,
    )

    scene = _Scene()
    session = RobotSession()
    with ControlPanelController(_faces_config(scene)) as controller:
        # A session on a Python-only detector, which no saved config can name.
        session.controller = controller
        # The session already runs: the config is locked, so nothing is saved.
        demo = build_page(_model(tmp_path, session, saved=None), session)
        assert isinstance(demo, gr.Blocks)
        assert refresh_session(session, [])[0] == refresh(controller, [])[0]
        scene.show(0.5, -0.5)
        _wait_until(lambda: controller.snapshot().faces == 1)
        _wait_until(lambda: controller.face_rate is not None)  # the meter's first look
        table, mic_level, log_text = refresh(controller, [])
        assert "fake" in table and "presence" in table
        assert "faces" not in table  # under the camera instead
        assert mic_level == 0.0
        assert log_text == ""
        frame, faces_text = refresh_camera(controller, [])
        tid = controller.bridge.faces.value.faces[0].track_id
        # the ● joins the id once the head engages the face: either is right here
        assert any(
            f"| **#{tid}**{mark} | (+0.50, -0.50) |" in faces_text
            for mark in ("", " ●")
        )
        assert faces_text.startswith("### Faces (")
        assert "updates/s)" in faces_text.splitlines()[0]
        assert " m |" in faces_text  # the face's distance, from the fake's camera model
        assert frame is not None and frame.shape == (48, 64, 3)
        # Mirrored for display: the fake's left-to-right ramp (RGB channel 2) now falls,
        # and the face on the right of the image (x=+0.5) is marked on the left.
        assert frame[40, 0, 2] > frame[40, -1, 2]
        marked_cols = np.nonzero((frame == FACE_MARKER_RGB).all(axis=2))[1]
        assert marked_cols.size and marked_cols.max() < 32
        _wait_until(lambda: controller.users_view().track_id == tid, timeout=5)
        users_text, head_text, events_text = refresh_users(controller)
        assert f"| **#{tid}** ● | yes | yes |" in users_text
        assert head_text == f"### Head tracking: following #{tid}"
        assert f"#{tid} arrived" in events_text


# The offline fake: what the tests run a session on (the app's default is the full
# example config, the headless sim).
FAKE: dict[str, object] = {"reachy": {"backend": "fake"}}


def _model(
    folder: Path,
    session: RobotSession | None = None,
    *,
    saved: dict[str, object] | None = FAKE,
) -> AppModel:
    """The panel's model over a folder of its own, its secrets kept off os.environ, the
    fake saved as its config (`saved=None` keeps the app's default)."""
    from examples.control_panel.app import APP

    config_folder = ConfigFolder(folder, APP.default_config)
    model = AppModel(
        APP,
        config_folder,
        SecretStore(config_folder.env_path, {}),
        locked=session.locked if session is not None else None,
    )
    if saved is not None:
        model.save(saved)
    return model


def test_the_session_reads_relative_paths_against_the_apps_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder module-setup passes to the setup functions as `base_dir` is the one
    the session's config is loaded with: a voice file next to config.json is found."""
    with wave.open(str(tmp_path / "hi.wav"), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0\0" * 160)
    monkeypatch.chdir(Path(__file__).parent)  # the working directory has no hi.wav
    session = RobotSession()
    model = _model(tmp_path, session)
    voice = {
        "module": {"type": "audiofile", "files": [{"text": "hi", "file": "hi.wav"}]}
    }
    model.save({"reachy": {"backend": "fake", "tts": voice}})

    assert session.start(model) == []
    try:
        bridge = session.require().bridge
        assert bridge.synthesizer_error is None
        assert bridge.config.tts is not None
        assert bridge.config.tts["module"]["base_dir"] == str(model.path.resolve())
    finally:
        session.stop()


def test_a_session_starts_from_the_saved_config_and_locks_it_until_stopped(
    tmp_path: Path,
) -> None:
    session = RobotSession()
    model = _model(tmp_path, session)
    model.save({"reachy": {"backend": "fake", "motion": {"idle": "hold"}}})

    assert session.start(model) == []
    try:
        controller = session.require()
        assert controller.running
        assert controller.bridge.config.motion.idle == "hold"
        assert session.start(model) == [] and session.require() is controller
        with pytest.raises(RuntimeError):
            model.save({"reachy": {"backend": "fake"}})  # in use: refused
    finally:
        assert session.stop()
    assert not controller.running and not session.locked()
    model.save({"reachy": {"backend": "fake"}})  # free again
    assert not session.stop()
    with pytest.raises(BridgeError, match="press Start"):
        session.require()


def test_a_config_with_blockers_starts_nothing_and_says_why(tmp_path: Path) -> None:
    session = RobotSession()
    model = _model(tmp_path, session)
    model.save({"reachy": {"backend": "moon"}})

    reasons = session.start(model)

    assert reasons and "/reachy/backend" in reasons[0]
    assert session.reasons == reasons
    assert session.controller is None and not session.locked()


def test_a_bring_up_that_fails_is_a_reason_and_leaves_nothing_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reachy_mini_bridge import bridge as bridge_module

    def unreachable(backend: str, **opts: object) -> FakeReachyMini:
        raise ConnectionError("no robot here")

    monkeypatch.setattr(bridge_module, "build_robot", unreachable)
    session = RobotSession()
    model = _model(tmp_path, session)

    [reason] = session.start(model)

    assert reason.startswith("The session could not start:")
    assert "no robot here" in reason
    assert session.controller is None and not session.locked()


def test_the_default_config_is_the_full_example_valid_and_started_at_launch(
    tmp_path: Path,
) -> None:
    from examples.control_panel.app import APP, DEFAULT_CONFIG, DEFAULT_CONFIG_FILE

    model = _model(
        tmp_path, saved=None
    )  # an empty folder: app-setup writes the default

    # The repo's full example config, under the app's section.
    assert DEFAULT_CONFIG_FILE.name == "full.json"
    assert DEFAULT_CONFIG == {
        "reachy": json.loads(DEFAULT_CONFIG_FILE.read_text("utf-8"))
    }
    assert APP.default_config == DEFAULT_CONFIG
    assert model.saved_config() == DEFAULT_CONFIG
    # What the machine needs for it (the sim, the voice, the datasets) depends on the
    # machine; the config itself is valid.
    assert model.check_config(DEFAULT_CONFIG) == []
    assert starts_at_launch(model, None)
    assert not starts_at_launch(model, False)
    assert [section.name for section in APP.sections] == ["reachy"]


def test_the_page_puts_robot_first_opens_on_setup_and_ticks_without_a_session(
    tmp_path: Path,
) -> None:
    gr = pytest.importorskip("gradio")
    from examples.control_panel.app import (
        NO_SESSION,
        build_page,
        refresh_session,
        refresh_session_camera,
        refresh_session_users,
        session_markdown,
    )

    session = RobotSession()
    page = build_page(_model(tmp_path, session), session)

    blocks = list(page.blocks.values())
    tabs = [getattr(b, "label", None) for b in blocks if isinstance(b, gr.Tab)]
    selected = [getattr(b, "selected", None) for b in blocks if isinstance(b, gr.Tabs)]
    assert [t for t in tabs if t in ("Robot", "Setup", "Secrets")] == [
        "Robot",
        "Setup",
        "Secrets",
    ]
    assert tabs[0] == "Robot"
    assert "setup" in selected and "robot" not in selected
    assert refresh_session(session, [])[0] == NO_SESSION
    assert refresh_session_camera(session, [])[0] is None
    assert refresh_session_users(session)[0] == NO_SESSION
    assert "press Start" in session_markdown(session)


def test_the_page_opens_on_robot_when_the_session_started_at_launch(
    tmp_path: Path,
) -> None:
    gr = pytest.importorskip("gradio")
    from examples.control_panel.app import build_page

    session = RobotSession()
    page = build_page(_model(tmp_path, session), session, started_at_launch=True)

    selected = [
        getattr(b, "selected", None)
        for b in page.blocks.values()
        if isinstance(b, gr.Tabs)
    ]
    assert "robot" in selected and "setup" not in selected


def _interactive(update: dict[str, object]) -> object:
    return update["interactive"]


def test_start_and_stop_are_greyed_by_the_sessions_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("gradio")
    from examples.control_panel import session as session_module
    from examples.control_panel.app import session_buttons, session_markdown

    session = RobotSession()
    model = _model(tmp_path, session)
    seen: dict[str, object] = {}
    real_start = session_module.ControlPanelController.start

    def start(self: ControlPanelController, *, timeout: float | None = None) -> None:
        # Mid-start: what the page shows while the session comes up.
        seen["starting"] = (
            session.status,
            *map(_interactive, session_buttons(session)),
        )
        seen["line"] = session_markdown(session)
        real_start(self, timeout=timeout)

    monkeypatch.setattr(session_module.ControlPanelController, "start", start)
    assert (session.status, *map(_interactive, session_buttons(session))) == (
        "stopped",
        True,
        False,
    )

    assert session.start(model) == []
    try:
        assert seen["starting"] == ("starting", False, False)
        assert seen["line"] == "**Session:** starting…"
        assert (session.status, *map(_interactive, session_buttons(session))) == (
            "running",
            False,
            True,
        )
    finally:
        session.stop()
    assert (session.status, *map(_interactive, session_buttons(session))) == (
        "stopped",
        True,
        False,
    )


def test_a_start_that_starts_nothing_leaves_start_enabled(tmp_path: Path) -> None:
    pytest.importorskip("gradio")
    from examples.control_panel.app import session_buttons

    session = RobotSession()
    model = _model(tmp_path, session)
    model.save({"reachy": {"backend": "moon"}})

    assert session.start(model)
    assert session.status == "stopped"
    assert [_interactive(u) for u in session_buttons(session)] == [True, False]


# --- faces ----------------------------------------------------------------------------


@pytest.mark.realtime  # the meter reads the fake camera's 10 fps
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


def test_the_faces_table_shows_each_face_by_track_id() -> None:
    pytest.importorskip("gradio")
    from examples.control_panel.app import faces_markdown
    from examples.control_panel.controller import FaceView

    view = FaceView(
        active=True,
        positions=[(0.25, -0.1), (-0.5, 0.2)],
        rolls=[math.radians(-12.4), None],
        target=1,
        distances=[0.456, None],
        track_ids=[3, 4],
        sizes=[0.31, 0.2],
    )
    rows = faces_markdown(view, 10.0).splitlines()
    # one row per face: id (● on the followed one), position, size, roll, distance
    assert "| **#3** | (+0.25, -0.10) | 0.31 | -12° | 0.46 m |" in rows
    assert "| **#4** ● | (-0.50, +0.20) | 0.20 | — | — |" in rows
    assert rows[0] == "### Faces (10.0 updates/s)"
    empty = FaceView(active=True, positions=[], rolls=[], target=None)
    assert faces_markdown(empty, 9.5) == "### Faces (9.5 updates/s)\n\nnobody in view"
    off = FaceView(active=False, positions=[], rolls=[], target=None)
    assert faces_markdown(off, None) == "### Faces\n\n— (no detector is looking)"


def test_the_posture_readout_says_when_the_person_is_in_the_personal_space() -> None:
    pytest.importorskip("gradio")
    from examples.control_panel.app import _posture

    assert _posture(None) == "—"
    assert _posture((4.6, 12.0, False)) == "roll +5°, lean +12 mm"
    assert _posture((0.0, -13.0, True)) == "roll +0°, lean -13 mm, in personal space"


def test_the_posture_boxes_switch_each_behaviour_and_the_state_shows_it() -> None:
    """The panel's posture checkboxes go through `set_tracking`, keeping the rest of the
    settings; the state table reads the posture while the head follows someone."""
    pytest.importorskip("gradio")
    from examples.control_panel.app import state_table

    scene = _Scene()
    with ControlPanelController(_faces_config(scene)) as controller:
        controller.set_posture(mirroring=False, approach=True, personal_space=False)
        settings = controller.bridge.tracking
        assert settings.enabled  # still tracking
        assert (
            settings.mirroring.enabled,
            settings.approach.enabled,
            settings.personal_space.enabled,
        ) == (False, True, False)
        assert controller.snapshot().posture is None  # nobody followed yet
        scene.show()
        _wait_until(lambda: controller.snapshot().attention == "engaged")
        _wait_until(lambda: (controller.snapshot().posture or (0, 0))[1] > 5.0)
        state = controller.snapshot()
        assert state.posture is not None and state.posture[0] == 0.0  # no mirror
        assert "lean +" in state_table(state)


def test_the_base_following_box_switches_it_and_the_state_shows_the_body_yaw() -> None:
    """The panel's Base following checkbox goes through `set_base_following`, keeping
    the rest of the settings; the state table shows the switch and where the base
    points."""
    pytest.importorskip("gradio")
    from examples.control_panel.app import state_table

    with ControlPanelController(_faces_config(_Scene())) as controller:
        assert controller.snapshot().base_following is True  # the config's default
        controller.set_base_following(False)
        settings = controller.bridge.base_following
        assert settings.enabled is False
        assert settings.deadband_deg == 20.0  # the rest kept
        state = controller.snapshot()
        assert state.base_following is False
        assert state.body_yaw_deg == 0.0
        table = state_table(state)
        assert "**following** | off" in table
        assert "**body yaw** | +0°" in table


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


def _green(marked: npt.NDArray[np.uint8]) -> npt.NDArray[np.bool_]:
    return (marked == FACE_MARKER_RGB).all(axis=2)


def _crop(mask: npt.NDArray[np.bool_]) -> npt.NDArray[np.bool_]:
    rows, cols = np.nonzero(mask)
    return mask[rows.min() : rows.max() + 1, cols.min() : cols.max() + 1]


def test_draw_faces_stamps_each_label_upright_on_a_mirrored_frame() -> None:
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    # one face in the middle: its square's top edge is row 42, its label above it
    plain = _green(draw_faces(frame, [(0.0, 0.0)], labels=["#7"]))
    mirrored = _green(draw_faces(frame, [(0.0, 0.0)], labels=["#7"], mirror=True))
    label, label_mirrored = _crop(plain[:41]), _crop(mirrored[:41])
    assert label.shape == (5, 7)  # "#" and "7", 3x5 each, one column apart
    # the label reads the same way round on the mirrored frame — not flipped with it
    assert np.array_equal(label, label_mirrored)
    assert not np.array_equal(label, label[:, ::-1])  # "#7" is not symmetric
    assert not plain[45:55, 95:105].any()  # nothing stamped inside the square

    # a face on the right of the image: its label over it, on the left once mirrored
    right = _green(draw_faces(frame, [(0.5, 0.0)], labels=["#12"]))
    left = _green(draw_faces(frame, [(0.5, 0.0)], labels=["#12"], mirror=True))
    assert np.nonzero(right[:41])[1].min() > 100
    assert np.nonzero(left[:41])[1].max() < 100
    assert np.array_equal(_crop(right[:41]), _crop(left[:41]))

    # a face at the top edge: the label goes below its square, inside the frame
    top = _green(draw_faces(frame, [(0.0, -1.0)], labels=["#3"]))
    assert top[9:20, 90:110].any()


def test_describe_tracking_change_names_each_transition() -> None:
    off = HeadTrackingReport.inactive()
    watching = HeadTrackingReport(
        active=True, focus=False, attention="watching", track_id=None, ts=1.0
    )
    on_3 = HeadTrackingReport(
        active=True, focus=False, attention="engaged", track_id=3, ts=2.0
    )
    on_4 = HeadTrackingReport(
        active=True, focus=True, attention="engaged", track_id=4, ts=3.0
    )
    near_4 = HeadTrackingReport(
        active=True,
        focus=True,
        attention="engaged",
        track_id=4,
        ts=4.0,
        in_personal_space=True,
    )
    pinned_lost = HeadTrackingReport(
        active=True,
        focus=True,
        attention="watching",
        track_id=None,
        ts=5.0,
        pinned_track_id=4,
    )

    assert describe_tracking_change(off, watching) == ["tracking on"]
    assert describe_tracking_change(watching, on_3) == ["head follows #3"]
    assert describe_tracking_change(on_3, on_4) == ["focus on", "head #3 → #4"]
    assert describe_tracking_change(on_4, near_4) == ["#4 entered personal space"]
    assert describe_tracking_change(near_4, on_4) == ["#4 left personal space"]
    assert describe_tracking_change(on_4, pinned_lost) == [
        "pinned #4",
        "head lets go of #4 (watching)",
    ]
    assert describe_tracking_change(on_3, off) == ["tracking off"]
    assert describe_tracking_change(on_3, on_3) == []


def test_describe_users_change_names_arrivals_and_departures() -> None:
    idle = UsersReport.inactive()
    started = UsersReport(users=(), arrived=(), left=(), ts=1.0, active=True)
    swap = UsersReport(
        users=(),
        arrived=(5,),
        left=(UserDeparture(3, "left"),),
        ts=2.0,
        active=True,
    )
    stopped = UsersReport(
        users=(), arrived=(), left=(UserDeparture(5, "stopped"),), ts=0.0, active=False
    )
    assert describe_users_change(idle, started) == ["detection on"]
    assert describe_users_change(started, swap) == ["#5 arrived", "#3 left (left)"]
    assert describe_users_change(swap, stopped) == [
        "#5 left (stopped)",
        "detection off",
    ]


def test_the_users_view_and_the_tracking_events_follow_a_face() -> None:
    """A stub face arrives, the head engages it, and a stop lets it go: the users view
    shows it present and followed, the events record each step by its track id
    (specs/examples/control_panel.md "The tracking events")."""
    scene = _Scene()
    with ControlPanelController(_faces_config(scene)) as controller:
        view = controller.users_view()
        assert view.tracking and view.track_id is None
        scene.show(0.2, 0.0)
        _wait_until(lambda: controller.users_view().track_id is not None, timeout=5)
        view = controller.users_view()
        assert view.active and view.attention == "engaged"
        (user,) = view.users
        assert user.track_id == view.track_id and user.seen and user.in_view
        tid = user.track_id
        _wait_until(lambda: f"head follows #{tid}" in "\n".join(_events(controller)))
        assert any(line.endswith(f"#{tid} arrived") for line in _events(controller))

        controller.stop_head_tracking()
        controller.set_face_detection(False)
        _wait_until(lambda: any("detection off" in e for e in _events(controller)))
        events = _events(controller)
        assert any(line.endswith("tracking off") for line in events)
        assert any(line.endswith(f"#{tid} left (stopped)") for line in events)
        view = controller.users_view()
        assert (view.active, view.users, view.tracking) == (False, (), False)
        # newest first, each line stamped to the millisecond
        assert events[0].endswith("detection off")
        assert all(line[8] == "." and line[12:14] == "  " for line in events)


def _events(controller: ControlPanelController) -> list[str]:
    return controller.tracking_events()


def test_the_users_table_marks_the_followed_user_and_who_is_out_of_view() -> None:
    pytest.importorskip("gradio")
    from examples.control_panel.app import head_tracking_markdown, users_markdown
    from examples.control_panel.controller import UsersView
    from reachy_mini_bridge import Face, User

    def user(track_id: int, *, in_view: bool, missing_s: float = 0.0) -> User:
        face = Face(x=0.1, y=-0.2, roll=None, size=0.3, track_id=track_id)
        return User(
            track_id=track_id,
            seen=in_view and not missing_s,
            in_view=in_view,
            last_seen_ts=1.0,
            first_seen_ts=0.0,
            missing_s=missing_s,
            face=face,
        )

    view = UsersView(
        active=True,
        users=(user(3, in_view=False), user(4, in_view=True, missing_s=0.6)),
        tracking=True,
        focus=False,
        attention="engaged",
        track_id=3,
        pinned_track_id=None,
    )
    table = users_markdown(view)
    rows = [line for line in table.splitlines() if line.startswith("| **#")]
    assert rows[0].startswith("| **#3** ● | **no** | **no** | — |")
    assert rows[1].startswith("| **#4** | **no** | yes | 0.6 s | (+0.10, -0.20) |")

    lost = UsersView(
        active=True,
        users=(),
        tracking=True,
        focus=True,
        attention="watching",
        track_id=None,
        pinned_track_id=3,
    )
    assert users_markdown(lost) == "nobody present"
    off = UsersView(False, (), False, False, None, None, None)
    assert users_markdown(off) == "— (no detector is looking)"

    # the head tracking: whom it follows and that user's state, a lost pin, off
    assert head_tracking_markdown(view) == "### Head tracking: following #3"
    assert head_tracking_markdown(lost) == (
        "### Head tracking: following nobody (watching, pinned #3 lost) · focus"
    )
    assert head_tracking_markdown(off) == "### Head tracking: off"
