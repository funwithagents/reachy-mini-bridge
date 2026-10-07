"""Functional tests for ReachyMiniBridge on the fake backend (specs/core/bridge.md).

Drives the public bridge the way a caller would and asserts through the escape hatch
(`bridge.robot`, the FakeReachyMini) and its recorded commands. No network, no daemon,
no hardware. Async runs via `asyncio.run` (fast-tier convention).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import bridge as bridge_module
from reachy_mini_bridge import face_detection as face_detection_module
from reachy_mini_bridge import head_tracking as head_tracking_module
from reachy_mini_bridge import robot as robot_module
from reachy_mini_bridge.bridge import ReachyMiniBridge
from reachy_mini_bridge.config import (
    DaemonConfig,
    FaceDetectionSettings,
    MotionSettings,
    ReachyMiniConfig,
    SimDisplaySettings,
)
from reachy_mini_bridge.errors import (
    BridgeError,
    ConfigError,
    GravityCompensationUnsupportedError,
    MotorsNotEnabledError,
    SoundInterruptedError,
    SpeechInterruptedError,
)
from reachy_mini_bridge.face_detection import FaceReport, PixelFace
from reachy_mini_bridge.fake_reachy_mini import FAKE_FRAME_HZ, FakeReachyMini
from reachy_mini_bridge.head_tracking import HeadTrackingReport
from reachy_mini_bridge.motion import (
    ANTENNA_MIN_RAD,
    ANTENNA_OUTWARD,
    BLEND_S,
    BREATH_Z_M,
    NEUTRAL_ANTENNAS,
    NEUTRAL_HEAD,
    HoldMove,
    IdleMove,
    IdleOffsets,
)
from reachy_mini_bridge.sim_displays import FACE_BOX_HEIGHT_M


class _ToneSynth:
    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        yield np.full(400, 0.2, dtype=np.float32)


def _fake(bridge: ReachyMiniBridge) -> FakeReachyMini:
    robot = bridge.robot
    assert isinstance(robot, FakeReachyMini)
    return robot


def _command_names(bridge: ReachyMiniBridge) -> list[str]:
    return [name for name, _ in _fake(bridge).commands]


def _wav(directory: Path, seconds: float) -> Path:
    """Write ``seconds`` of 16 kHz mono silence as a WAV file."""
    path = directory / "sound.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def _yaw_deg(head: npt.NDArray[np.float64]) -> float:
    return float(np.degrees(np.arctan2(head[1, 0], head[0, 0])))


def _head_z(bridge: ReachyMiniBridge) -> list[float]:
    return [float(h[2, 3]) for h, _, _ in _fake(bridge).targets if h is not None]


# --- construction / escape hatch ---------------------------------------------------


def test_robot_escape_hatch_is_the_fake() -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            assert isinstance(bridge.robot, FakeReachyMini)
            assert bridge.raw is bridge.robot

    asyncio.run(run())


def test_string_shorthand_builds_a_config() -> None:
    bridge = ReachyMiniBridge("fake")
    assert bridge.config == ReachyMiniConfig(backend="fake")
    assert ReachyMiniBridge().config.backend == "real"


def test_from_dict_from_json_from_json_file(tmp_path: Path) -> None:
    data = {"backend": "fake"}
    text = json.dumps(data)
    path = tmp_path / "robot.json"
    path.write_text(text)
    synth = _ToneSynth()
    for bridge in (
        ReachyMiniBridge.from_dict(data, synthesizer=synth),
        ReachyMiniBridge.from_json(text, synthesizer=synth),
        ReachyMiniBridge.from_json_file(path, synthesizer=synth),
    ):
        assert bridge.config.backend == "fake"
        assert bridge._synthesizer is synth
    with pytest.raises(ConfigError):
        ReachyMiniBridge.from_json("{not json")


def test_robot_requires_entry() -> None:
    bridge = ReachyMiniBridge("fake")
    with pytest.raises(BridgeError):
        _ = bridge.robot
    with pytest.raises(BridgeError):
        _ = bridge.raw
    with pytest.raises(BridgeError):
        _ = bridge.mic_sample_rate

    async def run() -> None:
        async with bridge:
            assert isinstance(bridge.robot, FakeReachyMini)

    asyncio.run(run())
    with pytest.raises(BridgeError):
        _ = bridge.robot


def test_double_start_raises_and_the_session_still_works() -> None:
    async def run() -> str:
        async with ReachyMiniBridge("fake") as bridge:
            with pytest.raises(BridgeError):
                await bridge.start()
            await bridge.set_motors_state("enabled")
            return await bridge.get_motors_state()

    assert asyncio.run(run()) == "enabled"


def test_package_front_door_drives_the_fake() -> None:
    import reachy_mini_bridge as rmb

    assert set(rmb.__all__) == {
        "BridgeError",
        "CameraFrame",
        "ConfigError",
        "Face",
        "FaceDetector",
        "FaceReport",
        "GravityCompensationUnsupportedError",
        "HeadTrackingReport",
        "IdleMove",
        "IdleOffsets",
        "MotorsNotEnabledError",
        "Observable",
        "SoundInterruptedError",
        "SpeechInterruptedError",
        "PixelFace",
        "ReachyMiniBridge",
        "ReachyMiniConfig",
        "SpeechSynthesizer",
        "TTSEngineSynthesizer",
    }
    assert all(hasattr(rmb, name) for name in rmb.__all__)
    assert isinstance(_ToneSynth(), rmb.SpeechSynthesizer)

    # The front-door names are the ones a caller catches.
    async def run() -> None:
        async with rmb.ReachyMiniBridge("fake") as bridge:
            with pytest.raises(rmb.BridgeError):
                await bridge.say("hi")  # no synthesizer
            with pytest.raises(rmb.MotorsNotEnabledError):
                await bridge.play_emotion("happy")  # the fake boots disabled
            _fake(bridge).client.kinematics_engine = "AnalyticalKinematics"
            with pytest.raises(rmb.GravityCompensationUnsupportedError):
                await bridge.set_motors_state("gravity_compensation")

    asyncio.run(run())
    with pytest.raises(rmb.ConfigError):
        rmb.ReachyMiniConfig.from_json("{not json")


# --- default synthesizer from the config's `tts` block ------------------------------


def test_explicit_synthesizer_wins_over_tts_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_block: object) -> object:
        raise AssertionError("tts block must not be consumed")

    monkeypatch.setattr(bridge_module, "TTSEngineSynthesizer", boom)
    config = ReachyMiniConfig(backend="fake", tts={"module": {"type": "x"}})

    async def run() -> int:
        async with ReachyMiniBridge(config, synthesizer=_ToneSynth()) as bridge:
            await bridge.say("hi")
            return sum(
                1 for n in _command_names(bridge) if n == "media.push_audio_sample"
            )

    assert asyncio.run(run()) >= 1


def test_tts_block_builds_the_default_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[object] = []

    class _Adapter(_ToneSynth):
        def __init__(self, block: object) -> None:
            built.append(block)

    monkeypatch.setattr(bridge_module, "TTSEngineSynthesizer", _Adapter)
    block = {"module": {"type": "x"}}

    async def run() -> int:
        async with ReachyMiniBridge(
            ReachyMiniConfig(backend="fake", tts=block)
        ) as bridge:
            await bridge.say("hi")
            return sum(
                1 for n in _command_names(bridge) if n == "media.push_audio_sample"
            )

    assert asyncio.run(run()) >= 1
    assert built == [block]


def test_tts_block_for_an_uninstalled_provider_degrades_to_no_voice() -> None:
    """A real block for a provider whose extra is missing: tts-engine's ConfigError
    is recorded, the bridge comes up, and `say` names the extra to install."""
    from tts_engine.config import ConfigError as TTSEngineConfigError

    bridge = ReachyMiniBridge(
        ReachyMiniConfig(backend="fake", tts={"module": {"type": "no-such-provider"}})
    )
    assert isinstance(bridge.synthesizer_error, TTSEngineConfigError)

    async def run() -> None:
        async with bridge:
            with pytest.raises(BridgeError, match="no-such-provider"):
                await bridge.say("hi")

    asyncio.run(run())


def test_tts_block_build_failure_degrades_to_no_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cause = ValueError("environment variable 'X' is unset")

    def failing(_block: object) -> object:
        raise cause

    monkeypatch.setattr(bridge_module, "TTSEngineSynthesizer", failing)
    block = {"module": {"type": "x"}}
    bridge = ReachyMiniBridge(ReachyMiniConfig(backend="fake", tts=block))
    assert bridge.synthesizer_error is cause

    async def run() -> list[str]:
        async with bridge:
            with pytest.raises(BridgeError, match="X") as exc_info:
                await bridge.say("hi")
            assert exc_info.value.__cause__ is cause
            return await bridge.list_emotions()  # the robot is still up

    assert asyncio.run(run()) == ["happy", "sad", "curious"]


def test_synthesizer_error_is_none_when_the_voice_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Adapter(_ToneSynth):
        def __init__(self, block: object) -> None:
            del block

    monkeypatch.setattr(bridge_module, "TTSEngineSynthesizer", _Adapter)
    bridge = ReachyMiniBridge(
        ReachyMiniConfig(backend="fake", tts={"module": {"type": "x"}})
    )
    assert bridge.synthesizer_error is None


def test_explicit_synthesizer_leaves_no_error() -> None:
    config = ReachyMiniConfig(backend="fake", tts={"module": {"type": "x"}})
    bridge = ReachyMiniBridge(config, synthesizer=_ToneSynth())
    assert bridge.synthesizer_error is None


# --- lifecycle order: daemon -> robot -> media --------------------------------------


class _Handle:
    """What the scripted `start_daemon` returns: a `stop()` that records the exit."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stop(self) -> None:
        self._events.append("daemon-exit")


class _Recorder:
    """Records lifecycle events; a scripted `start_daemon` + a build_robot stand-in."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.fake = FakeReachyMini()

    def start_daemon(
        self, config: object, *, host: str, port: int, backend: str
    ) -> _Handle:
        self.events.append(f"daemon-enter {backend} {host}:{port}")
        return _Handle(self.events)

    def build_robot(self, backend: str, **opts: object) -> FakeReachyMini:
        self.events.append(f"build {backend} {sorted(opts)}")
        return self.fake


def _install(monkeypatch: pytest.MonkeyPatch, rec: _Recorder) -> None:
    monkeypatch.setattr(bridge_module._daemon, "start_daemon", rec.start_daemon)
    monkeypatch.setattr(bridge_module, "build_robot", rec.build_robot)


_SPAWNING = ReachyMiniConfig(backend="sim", daemon=DaemonConfig(spawn="auto"))


def test_the_daemon_starts_before_the_robot_and_stops_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)

    async def run() -> None:
        async with ReachyMiniBridge(_SPAWNING) as bridge:
            assert bridge.robot is rec.fake
            rec.events.append("body")

    asyncio.run(run())
    names = [n for n, _ in rec.fake.commands]
    assert rec.events == [
        "daemon-enter sim 127.0.0.1:8000",
        "build sim ['connection_mode', 'host', 'media_backend', 'port']",
        "body",
        "daemon-exit",
    ]
    assert names[0] == "media.start_recording"  # the robot is entered, then media opens
    assert names.index("media.stop_playing") < names.index("__exit__")


def test_a_real_config_spawns_the_real_daemon_before_the_robot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)
    config = ReachyMiniConfig.from_dict(
        {"backend": "real", "daemon": {"spawn": "always"}, "robot": {"port": 9100}}
    )

    async def run() -> None:
        async with ReachyMiniBridge(config):
            rec.events.append("body")

    asyncio.run(run())
    assert rec.events == [
        "daemon-enter real 127.0.0.1:9100",
        "build real ['connection_mode', 'host', 'media_backend', 'port']",
        "body",
        "daemon-exit",
    ]


def test_robot_build_failure_exits_the_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)

    def failing(backend: str, **opts: object) -> FakeReachyMini:
        raise ConnectionError("no daemon")

    monkeypatch.setattr(bridge_module, "build_robot", failing)
    bridge = ReachyMiniBridge(_SPAWNING)

    async def run() -> None:
        async with bridge:
            pass

    with pytest.raises(ConnectionError):
        asyncio.run(run())
    assert rec.events == ["daemon-enter sim 127.0.0.1:8000", "daemon-exit"]
    with pytest.raises(BridgeError):
        _ = bridge.robot


def test_media_open_failure_exits_the_robot_and_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)

    def broken() -> None:
        raise RuntimeError("no audio")

    monkeypatch.setattr(rec.fake.media, "start_recording", broken)
    bridge = ReachyMiniBridge(_SPAWNING)

    async def run() -> None:
        async with bridge:
            pass

    with pytest.raises(RuntimeError, match="no audio"):
        asyncio.run(run())
    # The robot was entered (the fake records only its exit) and unwound; media never
    # got past the failing start, so nothing else was recorded.
    assert [n for n, _ in rec.fake.commands] == ["__exit__"]
    assert rec.events[-1] == "daemon-exit"
    with pytest.raises(BridgeError):
        _ = bridge.robot


def test_exit_tears_down_everything_even_if_media_teardown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)

    def broken() -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(rec.fake.media, "stop_playing", broken)
    bridge = ReachyMiniBridge(_SPAWNING)

    async def run() -> None:
        async with bridge:
            pass

    with pytest.raises(RuntimeError, match="stop failed"):
        asyncio.run(run())
    names = [n for n, _ in rec.fake.commands]
    assert "media.stop_recording" in names and names[-1] == "__exit__"
    assert rec.events[-1] == "daemon-exit"
    with pytest.raises(BridgeError):
        _ = bridge.robot


def test_audio_verbs_require_an_open_api() -> None:
    bridge = ReachyMiniBridge("fake")

    async def run() -> None:
        with pytest.raises(BridgeError):
            await bridge.say("hi", _ToneSynth())
        async with bridge:
            pass
        with pytest.raises(BridgeError):
            await bridge.say("hi", _ToneSynth())

    asyncio.run(run())
    # Raised at call time, not at the first `async for`.
    with pytest.raises(BridgeError):
        bridge.audio_input()


# --- motors ------------------------------------------------------------------------


def test_motor_state_round_trips_through_the_daemon() -> None:
    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            seen = [await bridge.get_motors_state()]
            await bridge.set_motors_state("enabled")
            seen.append(await bridge.get_motors_state())
            await bridge.set_motors_state("gravity_compensation")
            seen.append(await bridge.get_motors_state())
            await bridge.set_motors_state("disabled")
            seen.append(await bridge.get_motors_state())
            return seen

    assert asyncio.run(run()) == [
        "disabled",
        "enabled",
        "gravity_compensation",
        "disabled",
    ]


def test_set_motors_state_dispatches_to_matching_primitive() -> None:
    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            await bridge.set_motors_state("gravity_compensation")
            await bridge.set_motors_state("disabled")
            return _command_names(bridge)

    names = asyncio.run(run())
    assert "enable_motors" in names
    assert "enable_gravity_compensation" in names
    assert "disable_motors" in names


def test_set_motors_state_rejects_unknown_state() -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            with pytest.raises(ValueError, match="unknown motor state"):
                await bridge.set_motors_state("asleep")

    asyncio.run(run())


def test_motors_disabled_pauses_the_loop_and_enabled_resumes_anchored() -> None:
    async def run() -> tuple[int, int, list[float]]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(0.3)
            await bridge.set_motors_state("disabled")
            await asyncio.sleep(0.2)
            count_after_disable = len(_fake(bridge).targets)
            await asyncio.sleep(0.2)
            count_still = len(_fake(bridge).targets)  # nothing sent while paused
            head = np.eye(4)
            head[2, 3] = 0.05
            bridge.robot.set_target(head=head)  # a caller driving the head directly
            marker = len(_fake(bridge).targets)
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(0.1)
            return count_after_disable, count_still, _head_z(bridge)[marker:]

    count_after_disable, count_still, resumed_zs = asyncio.run(run())
    assert count_still == count_after_disable
    assert resumed_zs
    assert resumed_zs[0] == pytest.approx(0.05, abs=0.01)


def test_gravity_compensation_is_refused_off_placo_without_sending() -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            _fake(bridge).client.kinematics_engine = "AnalyticalKinematics"
            with pytest.raises(GravityCompensationUnsupportedError) as excinfo:
                await bridge.set_motors_state("gravity_compensation")
            message = str(excinfo.value)
            assert "AnalyticalKinematics" in message
            assert "--kinematics-engine Placo" in message
            assert "enable_gravity_compensation" not in _command_names(bridge)
            assert await bridge.get_motors_state() == "enabled"  # untouched

    asyncio.run(run())


def test_gravity_compensation_is_refused_when_the_engine_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(robot: object) -> str:
        raise OSError("connection refused")

    monkeypatch.setattr(bridge_module, "_daemon_kinematics_engine", unreachable)

    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            with pytest.raises(GravityCompensationUnsupportedError) as excinfo:
                await bridge.set_motors_state("gravity_compensation")
            assert isinstance(excinfo.value.__cause__, OSError)
            assert "enable_gravity_compensation" not in _command_names(bridge)

    asyncio.run(run())


def test_gravity_compensation_is_sent_unchecked_to_a_simulation() -> None:
    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            client = _fake(bridge).client
            client.kinematics_engine = "AnalyticalKinematics"
            for flag in ("simulation_enabled", "mockup_sim_enabled"):
                client.simulation_enabled = flag == "simulation_enabled"
                client.mockup_sim_enabled = flag == "mockup_sim_enabled"
                await bridge.set_motors_state("gravity_compensation")
            return _command_names(bridge)

    assert asyncio.run(run()).count("enable_gravity_compensation") == 2


def test_daemon_kinematics_engine_reads_the_daemon_http_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[str] = []

    def fetch(robot: object, path: str) -> object:
        fetched.append(path)
        return {"info": {"engine": "Placo", "collision check": False}}

    monkeypatch.setattr(robot_module, "fetch_daemon_json", fetch)
    robot = SimpleNamespace(client=SimpleNamespace(host="192.168.1.5", port=8000))
    assert bridge_module._daemon_kinematics_engine(robot) == "Placo"  # pyright: ignore[reportArgumentType]
    assert fetched == ["/api/kinematics/info"]


# --- motor precondition on movement verbs ------------------------------------------


def test_play_emotion_requires_motors_enabled() -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("disabled")
            with pytest.raises(MotorsNotEnabledError):
                await bridge.play_emotion("happy")
            # nothing was sent downstream
            assert "async_play_move" not in _command_names(bridge)

    asyncio.run(run())


def test_start_head_tracking_needs_no_motors() -> None:
    async def run() -> tuple[bool, bool, list[str]]:
        async with ReachyMiniBridge(
            _custom_config(_Scene([]), tracking=False)
        ) as bridge:
            await bridge.set_motors_state("gravity_compensation")
            before = len(_fake(bridge).commands)
            await bridge.start_head_tracking()
            await asyncio.sleep(0.15)
            return (
                bridge.tracking,
                bridge.faces.value.active,
                _command_names(bridge)[before:],
            )

    tracking, detecting, sent = asyncio.run(run())
    assert tracking is True
    assert detecting is True  # tracking started the detection loop
    assert (
        sent == []
    )  # nothing to the robot: the tracker steers through the motion loop


def test_movement_verbs_run_once_motors_enabled() -> None:
    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            await bridge.play_emotion("happy")
            return _command_names(bridge)

    names = asyncio.run(run())
    assert "media.play_sound" in names  # the move played through the motion loop


def test_focus_holds_the_head_on_the_face_without_the_breath(
    fast_attention: None,
) -> None:
    async def run(focus: bool) -> tuple[bool, float]:
        scene = _Scene([_pixel_face(0.1, 0.0)])
        async with ReachyMiniBridge(
            _custom_config(scene)
        ) as bridge:  # breathing by default
            await bridge.set_motors_state("enabled")
            await bridge.start_head_tracking(focus=focus)
            await asyncio.sleep(BLEND_S + 0.5)
            marker = len(_fake(bridge).targets)
            await asyncio.sleep(2.0)  # the first breath rises over these seconds
            zs = _head_z(bridge)[marker:]
            return bridge.tracking_focus, max(zs) - min(zs)

    focused, z_range = asyncio.run(run(True))
    assert focused is True
    assert z_range < 1e-4  # the head holds on the face
    composed, z_range = asyncio.run(run(False))
    assert composed is False
    assert z_range > 0.002  # it breathes around the face


def test_list_emotions_returns_the_offline_library() -> None:
    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            return await bridge.list_emotions()

    assert asyncio.run(run()) == ["happy", "sad", "curious"]


def test_play_emotion_resolves_name_and_plays_it() -> None:
    async def run() -> list[tuple[str, dict[str, Any]]]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            await bridge.play_emotion("curious")
            return list(_fake(bridge).commands)

    commands = asyncio.run(run())
    assert ("media.play_sound", {"sound_file": "curious.ogg"}) in commands


def test_play_emotion_unknown_name_raises() -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            with pytest.raises(ValueError, match="not found"):
                await bridge.play_emotion("nonexistent")

    asyncio.run(run())


# --- audio ------------------------------------------------------------------------


def test_play_emotion_on_the_fake_takes_the_moves_duration() -> None:
    async def run() -> float:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            t0 = time.monotonic()
            await bridge.play_emotion("curious")
            return time.monotonic() - t0

    # The entry blend (BLEND_S) precedes the move's own trajectory.
    assert asyncio.run(run()) >= BLEND_S + 0.25


async def _cancel_emotion_mid_flight(name: str) -> tuple[float, list[str]]:
    async with ReachyMiniBridge("fake", synthesizer=_ToneSynth()) as bridge:
        await bridge.set_motors_state("enabled")
        task = asyncio.create_task(bridge.play_emotion(name))
        if name == "sad":  # soundless: nothing to wait on but the entry blend
            await asyncio.sleep(BLEND_S + 0.1)
        else:
            while "media.play_sound" not in _command_names(bridge):
                await asyncio.sleep(0)
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - t0
        await bridge.say("still here")  # the session is usable right after
        return elapsed, _command_names(bridge)


def test_cancelled_play_emotion_stops_the_sound_and_keeps_the_session() -> None:
    elapsed, names = asyncio.run(_cancel_emotion_mid_flight("happy"))

    assert elapsed < 0.05
    i = names.index("media.play_sound")
    assert names[i + 1 : i + 3] == ["media.stop_sound", "audio.clear_player"]
    assert "media.push_audio_sample" in names[i + 3 :]


def test_cancelled_emotion_still_in_its_entry_blend_stops_no_sound() -> None:
    """The sound starts with the trajectory, after the blend: a cancel before that has
    no sound to stop (the loop stops only what it started)."""

    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            task = asyncio.create_task(bridge.play_emotion("happy"))
            await asyncio.sleep(0.1)  # inside the BLEND_S entry blend
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return _command_names(bridge)

    names = asyncio.run(run())
    assert "media.play_sound" not in names and "media.stop_sound" not in names


def test_cancelling_a_queued_emotion_leaves_the_playing_ones_sound_alone() -> None:
    """specs/core/bridge.md "Cancellation": a play_emotion cancelled while it waits in
    the queue has no effect to undo — the emotion ahead of it plays on, sound included."""

    async def run() -> tuple[list[str], list[str]]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            first = asyncio.create_task(bridge.play_emotion("happy"))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(bridge.play_emotion("happy"))
            while "media.play_sound" not in _command_names(bridge):
                await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            at_cancel = _command_names(bridge)
            await first
            await bridge.play_emotion("curious")  # the session plays on
            return at_cancel, _command_names(bridge)

    at_cancel, final = asyncio.run(run())
    assert "media.stop_sound" not in at_cancel
    assert "media.stop_sound" not in final
    assert (
        final.count("media.play_sound") == 2
    )  # the first and the third, never the second


def test_cancelled_soundless_emotion_does_not_stop_a_sound() -> None:
    _elapsed, names = asyncio.run(_cancel_emotion_mid_flight("sad"))

    assert "media.stop_sound" not in names
    assert "media.push_audio_sample" in names


def test_play_emotion_failure_stops_the_sound_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomMove(bridge_module._FakeRecordedMove):
        def evaluate(
            self, t: float
        ) -> tuple[
            npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
        ]:
            if t > 0.05:
                raise RuntimeError("boom")
            return super().evaluate(t)

    def get(
        self: bridge_module._FakeRecordedMoves, move_name: str
    ) -> bridge_module._FakeRecordedMove:
        return _BoomMove(move_name, sound_path=Path(f"{move_name}.ogg"))

    monkeypatch.setattr(bridge_module._FakeRecordedMoves, "get", get)

    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            with pytest.raises(RuntimeError, match="boom"):
                await bridge.play_emotion("happy")
            return _command_names(bridge)

    names = asyncio.run(run())
    assert "media.play_sound" in names
    assert names.index("media.stop_sound") > names.index("media.play_sound")


def test_completed_play_emotion_does_not_stop_the_sound() -> None:
    # A completed move's sound plays to its natural end.
    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            await bridge.play_emotion("happy")
            return _command_names(bridge)

    assert "media.stop_sound" not in asyncio.run(run())


def test_play_emotion_under_a_tracked_face_plays_as_recorded(
    fast_attention: None,
) -> None:
    config = _custom_config(_Scene([_pixel_face(0.5, 0.0)]), idle="hold")

    async def run() -> tuple[str | None, list[tuple[float, float]], list[str]]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_motors_state("enabled")
            robot = _fake(bridge)
            await asyncio.sleep(1.0)  # the head has turned toward the face
            engaged = bridge.attention
            before = len(robot.commands)
            marker = len(robot.targets)
            await bridge.play_emotion("sad")
            heads = [h for h, _, _ in robot.targets[marker:] if h is not None]
            return (
                engaged,
                [(_yaw_deg(h), float(h[2, 3])) for h in heads],
                [n for n, _ in robot.commands[before:]],
            )

    engaged, poses, names = asyncio.run(run())
    assert engaged == "engaged"
    assert not any(n.endswith("head_tracking") for n in names)
    # the trajectory itself, after the entry blend: the recorded nod, straight ahead
    move = bridge_module._FakeRecordedMove("sad")
    played = poses[-int(0.2 * 60) :]
    assert all(abs(yaw) < 1e-6 for yaw, _ in played)
    assert max(z for _, z in played) <= 0.01 + 1e-9
    start = next(i for i, (yaw, _) in enumerate(poses) if abs(yaw) < 1e-6)
    head, _a, _y = move.evaluate(0.0)
    assert head is not None
    assert poses[start][1] == pytest.approx(float(head[2, 3]), abs=0.002)


def test_play_emotion_pauses_wobbling_and_restores_it() -> None:
    async def run() -> tuple[list[str], bool]:
        async with ReachyMiniBridge("fake") as bridge:  # wobbling on by default
            await bridge.set_motors_state("enabled")
            await bridge.play_emotion("happy")
            return _command_names(bridge), bridge.wobbling

    names, wobbling = asyncio.run(run())
    sound_i = names.index("media.play_sound")
    assert "disable_wobbling" in names[:sound_i]
    assert "enable_wobbling" in names[sound_i:]
    assert wobbling is True


def test_wobbling_is_paused_once_across_queued_emotions() -> None:
    """One lease across consecutive emotions: disabled when the first begins, restored
    when the last one ends — never re-enabled between the two."""

    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:  # wobbling on by default
            await bridge.set_motors_state("enabled")
            first = asyncio.create_task(bridge.play_emotion("happy"))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(bridge.play_emotion("sad"))
            await asyncio.gather(first, second)
            return _command_names(bridge)

    names = asyncio.run(run())
    calls = [n for n in names if n in ("enable_wobbling", "disable_wobbling")]
    assert calls == ["enable_wobbling", "disable_wobbling", "enable_wobbling"]


def test_a_cancel_caught_inside_the_wobbling_pause_still_restores_it() -> None:
    """The disable call runs in its thread past the cancel; the release waits for it,
    so the restore comes after and wobbling is not left off."""

    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            robot = _fake(bridge)
            original = robot.disable_wobbling

            def slow_disable() -> None:
                time.sleep(0.2)
                original()

            robot.disable_wobbling = slow_disable  # type: ignore[method-assign]
            task = asyncio.create_task(bridge.play_emotion("happy"))
            await asyncio.sleep(0.05)  # inside the disable call
            t0 = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert time.monotonic() - t0 < 0.1  # the cancel returned promptly
            await asyncio.sleep(
                0.3
            )  # the disable thread finishes, the release restores
            return _command_names(bridge)

    names = asyncio.run(run())
    calls = [n for n in names if n in ("enable_wobbling", "disable_wobbling")]
    assert calls == ["enable_wobbling", "disable_wobbling", "enable_wobbling"]
    assert "media.play_sound" not in names  # the move never started


def test_play_emotion_restores_layers_after_a_cancel() -> None:
    async def run() -> list[tuple[str, dict[str, Any]]]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            task = asyncio.create_task(bridge.play_emotion("happy"))
            while "media.play_sound" not in _command_names(bridge):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return list(_fake(bridge).commands)

    commands = asyncio.run(run())
    wobbling_calls = [
        n for n, _ in commands if n in ("enable_wobbling", "disable_wobbling")
    ]
    assert wobbling_calls[-1] == "enable_wobbling"


def test_play_emotion_restores_to_the_current_record() -> None:
    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            task = asyncio.create_task(bridge.play_emotion("sad"))
            await asyncio.sleep(0.1)  # wobbling paused for the move
            await bridge.set_wobbling(False)  # the caller turns it off meanwhile
            await task
            return _command_names(bridge)

    names = asyncio.run(run())
    wobbling_calls = [n for n in names if n in ("enable_wobbling", "disable_wobbling")]
    # no restore of the wobbling the caller turned off
    assert wobbling_calls[-1] == "disable_wobbling"


def test_cancelled_library_load_is_reused_by_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads = 0
    started, release = threading.Event(), threading.Event()

    def slow_load(self: ReachyMiniBridge) -> Any:
        nonlocal loads
        loads += 1
        started.set()
        release.wait()
        return bridge_module._FakeRecordedMoves()

    monkeypatch.setattr(ReachyMiniBridge, "_load_recorded_moves", slow_load)

    async def run() -> list[str]:
        async with ReachyMiniBridge("fake") as bridge:
            task = asyncio.create_task(bridge.list_emotions())
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            return await bridge.list_emotions()

    assert asyncio.run(run()) == ["happy", "sad", "curious"]
    assert loads == 1


def test_cancel_during_bring_up_exits_the_robot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()

    class _SlowEnter(FakeReachyMini):
        def __enter__(self) -> Self:
            started.set()
            release.wait()
            return super().__enter__()

    robot = _SlowEnter()
    monkeypatch.setattr(bridge_module, "build_robot", lambda backend, **kw: robot)
    bridge = ReachyMiniBridge("fake")

    async def run() -> None:
        task = asyncio.create_task(bridge.start())
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert robot.commands[-1][0] == "__exit__"
    assert "media.start_recording" not in [n for n, _ in robot.commands]
    with pytest.raises(BridgeError):
        _ = bridge.robot


def test_say_routes_through_the_media_pipeline() -> None:
    async def run() -> list[str]:
        async with ReachyMiniBridge("fake", synthesizer=_ToneSynth()) as bridge:
            await bridge.say("hello")
            return _command_names(bridge)

    names = asyncio.run(run())
    assert "media.push_audio_sample" in names


def test_say_accepts_a_per_call_synthesizer() -> None:
    async def run() -> int:
        async with ReachyMiniBridge("fake") as bridge:  # no default synth configured
            await bridge.say("hello", _ToneSynth())
            return sum(
                1 for n in _command_names(bridge) if n == "media.push_audio_sample"
            )

    assert asyncio.run(run()) >= 1


def test_say_without_any_synthesizer_raises_bridge_error() -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            with pytest.raises(BridgeError, match="SpeechSynthesizer"):
                await bridge.say("hello")

    asyncio.run(run())


def test_play_sound_plays_a_built_in_sound_to_its_end() -> None:
    async def run() -> tuple[float, dict[str, object]]:
        async with ReachyMiniBridge("fake") as bridge:
            t0 = time.monotonic()
            await bridge.play_sound("wake_up.wav")  # an SDK asset, 0.41 s
            elapsed = time.monotonic() - t0
            args = next(a for n, a in _fake(bridge).commands if n == "media.play_sound")
            return elapsed, args

    elapsed, args = asyncio.run(run())
    assert elapsed >= 0.4
    played = Path(str(args["sound_file"]))
    assert played.name == "wake_up.wav" and played.is_file()


def test_play_sound_outside_a_session_raises(tmp_path: Path) -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniBridge("fake").play_sound(str(_wav(tmp_path, 0.1))))


def test_a_cancelled_play_sound_stops_the_sound_and_keeps_the_session(
    tmp_path: Path,
) -> None:
    path = _wav(tmp_path, 3.0)

    async def run() -> tuple[float, list[str]]:
        async with ReachyMiniBridge("fake", synthesizer=_ToneSynth()) as bridge:
            task = asyncio.create_task(bridge.play_sound(str(path)))
            while "media.play_sound" not in _command_names(bridge):
                await asyncio.sleep(0.01)
            t0 = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            elapsed = time.monotonic() - t0
            await bridge.say("still here")
            return elapsed, _command_names(bridge)

    elapsed, names = asyncio.run(run())
    assert elapsed < 0.05
    i = names.index("media.play_sound")
    assert names[i + 1] == "media.stop_sound"
    assert "media.push_audio_sample" in names[i + 1 :]


def test_a_play_sound_during_an_emotion_survives_the_emotion_cancel(
    tmp_path: Path,
) -> None:
    path = _wav(tmp_path, 1.0)

    async def run() -> list[tuple[str, dict[str, Any]]]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            emotion = asyncio.create_task(bridge.play_emotion("happy"))
            while "media.play_sound" not in _command_names(bridge):
                await asyncio.sleep(0)
            sound = asyncio.create_task(bridge.play_sound(str(path)))
            while _command_names(bridge).count("media.play_sound") < 2:
                await asyncio.sleep(0.01)
            emotion.cancel()
            with pytest.raises(asyncio.CancelledError):
                await emotion
            await sound  # plays to its end: the emotion's stop was not ours
            return list(_fake(bridge).commands)

    commands = asyncio.run(run())
    names = [n for n, _ in commands]
    user_start = max(i for i, n in enumerate(names) if n == "media.play_sound")
    assert commands[user_start][1]["sound_file"] == str(path)
    assert "media.stop_sound" not in names[user_start:]


def test_an_emotion_sound_interrupts_the_play_sound_in_flight(tmp_path: Path) -> None:
    path = _wav(tmp_path, 3.0)

    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            sound = asyncio.create_task(bridge.play_sound(str(path)))
            while "media.play_sound" not in _command_names(bridge):
                await asyncio.sleep(0.01)
            emotion = asyncio.create_task(bridge.play_emotion("happy"))
            with pytest.raises(SoundInterruptedError):
                await sound
            await emotion  # the emotion plays as usual

    asyncio.run(run())


def test_an_emotion_cancelled_during_a_say_leaves_the_utterance_playing() -> None:
    class _LongTone:
        sample_rate = 16000

        async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
            yield np.full(16000, 0.2, dtype=np.float32)  # one second

    async def run() -> tuple[list[str], int]:
        async with ReachyMiniBridge("fake", synthesizer=_LongTone()) as bridge:
            await bridge.set_motors_state("enabled")
            emotion = asyncio.create_task(bridge.play_emotion("happy"))
            while "media.play_sound" not in _command_names(bridge):
                await asyncio.sleep(0)
            say = asyncio.create_task(bridge.say("one second of speech"))
            while "media.push_audio_sample" not in _command_names(bridge):
                await asyncio.sleep(0)
            emotion.cancel()
            with pytest.raises(asyncio.CancelledError):
                await emotion
            at_cancel = _command_names(bridge)
            await say  # returns normally, its audio never flushed
            return at_cancel, _command_names(bridge).count("audio.clear_player")

    at_cancel, flushes = asyncio.run(run())
    assert "media.stop_sound" in at_cancel
    assert flushes == 0


def test_audio_input_streams_mic_bytes_and_exposes_format() -> None:
    async def run() -> tuple[int, int, bytes]:
        async with ReachyMiniBridge("fake") as bridge:
            chunk = b""
            async for c in bridge.audio_input():
                chunk = c
                break
            return bridge.mic_sample_rate, bridge.mic_channels, chunk

    sr, ch, chunk = asyncio.run(run())
    assert sr == 16000
    assert ch == 2
    assert len(chunk) > 0 and len(chunk) % 2 == 0  # whole int16 samples


# --- audio-reactive motion (head wobbling) ----------------------------------------


def test_set_wobbling_dispatches_and_tracks_state() -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            assert bridge.wobbling is True  # on by default
            await bridge.set_wobbling(False)
            assert bridge.wobbling is False
            assert _command_names(bridge)[-1] == "disable_wobbling"
            await bridge.set_wobbling(True)
            assert bridge.wobbling is True
            assert _command_names(bridge)[-1] == "enable_wobbling"

    asyncio.run(run())


def test_set_wobbling_needs_no_motors() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(wobbling=False))

    async def run() -> None:
        async with ReachyMiniBridge(
            config
        ) as bridge:  # the fake boots with motors disabled
            await bridge.set_wobbling(True)
            assert bridge.wobbling is True
            with pytest.raises(MotorsNotEnabledError):
                await bridge.play_emotion("happy")

    asyncio.run(run())


def test_wobbling_is_on_by_default_at_entry_and_off_at_exit() -> None:
    bridge = ReachyMiniBridge("fake")

    async def run() -> FakeReachyMini:
        async with bridge:
            assert bridge.wobbling is True
            return _fake(bridge)

    robot = asyncio.run(run())
    names = [name for name, _ in robot.commands]
    assert (
        names.index("media.start_playing")
        < names.index("enable_wobbling")
        < names.index("disable_wobbling")
        < names.index("media.stop_recording")
        < names.index("__exit__")
    )
    assert bridge.wobbling is False


def test_wobbling_off_in_the_config_is_never_touched() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(wobbling=False))

    async def run() -> FakeReachyMini:
        async with ReachyMiniBridge(config, synthesizer=_ToneSynth()) as bridge:
            await bridge.say("hello")
            return _fake(bridge)

    names = [name for name, _ in asyncio.run(run()).commands]
    assert "enable_wobbling" not in names
    assert "disable_wobbling" not in names


def test_wobbling_enabled_at_runtime_is_disabled_at_exit() -> None:
    bridge = ReachyMiniBridge(
        ReachyMiniConfig(backend="fake", motion=MotionSettings(wobbling=False))
    )

    async def run() -> FakeReachyMini:
        async with bridge:
            await bridge.set_wobbling(True)
            return _fake(bridge)

    names = [name for name, _ in asyncio.run(run()).commands]
    assert names.index("disable_wobbling") < names.index("__exit__")
    assert bridge.wobbling is False


def test_wobbling_turned_off_at_runtime_is_not_disabled_again_at_exit() -> None:
    async def run() -> FakeReachyMini:
        async with ReachyMiniBridge("fake") as bridge:  # on by default
            await bridge.set_wobbling(False)
            return _fake(bridge)

    names = [name for name, _ in asyncio.run(run()).commands]
    assert names.count("disable_wobbling") == 1


def test_failing_wobbling_enable_unwinds_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_enable = FakeReachyMini.enable_wobbling

    def boom(self: FakeReachyMini) -> None:
        original_enable(self)  # a delivered command can fail before acknowledgment
        raise RuntimeError("no wobbler")

    monkeypatch.setattr(FakeReachyMini, "enable_wobbling", boom)
    bridge = ReachyMiniBridge("fake")  # wobbling on by default
    robots: list[FakeReachyMini] = []
    original_enter = FakeReachyMini.__enter__

    def spy_enter(self: FakeReachyMini) -> FakeReachyMini:
        robots.append(self)
        return original_enter(self)

    monkeypatch.setattr(FakeReachyMini, "__enter__", spy_enter)

    async def run() -> None:
        async with bridge:
            pass

    with pytest.raises(RuntimeError, match="no wobbler"):
        asyncio.run(run())
    names = [name for name, _ in robots[0].commands]
    assert names.index("enable_wobbling") < names.index("disable_wobbling")
    assert "media.stop_playing" in names and names[-1] == "__exit__"
    assert bridge.wobbling is False
    with pytest.raises(BridgeError):
        _ = bridge.robot


def test_wobbling_property_is_false_outside_a_session() -> None:
    assert ReachyMiniBridge("fake").wobbling is False


def test_set_wobbling_requires_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniBridge("fake").set_wobbling(True))


# --- attention / gaze: opt-in, needing a detector (specs/core/bridge.md, specs/core/config.md) ----


def test_tracking_property_reads_the_config() -> None:
    assert ReachyMiniBridge("fake").tracking is False  # off by default: no detector
    assert ReachyMiniBridge(_custom_config(_Scene([]))).tracking is True
    assert (
        ReachyMiniBridge(_custom_config(_Scene([]), tracking=False)).tracking is False
    )


def test_the_default_config_runs_no_detector_and_the_switches_refuse() -> None:
    """specs/vision/user_perception.md "Detectors": with `face_detection.detector` null nothing is
    detected and nothing tracks; the switches raise, and the session works otherwise."""
    bridge = ReachyMiniBridge("fake")

    async def run() -> tuple[FaceReport, str | None, bool, bool, list[str]]:
        async with bridge:
            await asyncio.sleep(0.2)
            report = bridge.faces.value
            with pytest.raises(ValueError, match=r"face_detection\.detector is null"):
                await bridge.start_head_tracking()
            with pytest.raises(ValueError, match=r"face_detection\.detector is null"):
                await bridge.set_face_detection(True)
            await bridge.set_face_detection(False)  # off is fine
            await bridge.stop_head_tracking()
            await bridge.set_motors_state("enabled")
            return (
                report,
                bridge.attention,
                bridge.tracking,
                bridge.face_detection,
                _command_names(bridge),
            )

    report, attention, tracking, detection, names = asyncio.run(run())
    assert report == FaceReport.inactive(None)
    assert (attention, tracking, detection) == (None, False, False)
    assert "enable_motors" in names and not any("tracking" in n for n in names)


def test_a_switch_on_without_a_detector_fails_entry_before_anything_starts() -> None:
    """A config assembled in code can say what `from_dict` refuses; entry refuses it."""
    robots: list[FakeReachyMini] = []

    def build(backend: str, **kw: object) -> FakeReachyMini:
        robots.append(FakeReachyMini())
        return robots[-1]

    tracking = ReachyMiniBridge(
        ReachyMiniConfig(backend="fake", motion=MotionSettings(tracking=True))
    )
    detection = ReachyMiniBridge(
        ReachyMiniConfig(
            backend="fake", face_detection=FaceDetectionSettings(enabled=True)
        )
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bridge_module, "build_robot", build)
        for bridge in (tracking, detection):
            with pytest.raises(ValueError, match=r"face_detection\.detector is null"):
                asyncio.run(bridge.start())
    assert robots == []


class _BrokenFactory:
    """Callable, so registration accepts it; every build fails — a model that could not
    be loaded when the loop starts."""

    def __init__(self) -> None:
        self.builds = 0

    def __call__(self) -> _StubDetector:
        self.builds += 1
        raise OSError("no network: the model could not be downloaded")


def test_a_detector_that_cannot_be_built_fails_bring_up_and_unwinds() -> None:
    factory = _BrokenFactory()
    config = ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="custom", enabled=True, face_detector=factory
        ),
    )
    robots: list[Any] = []
    real_build = bridge_module.build_robot

    def build(backend: str, **kw: object) -> Any:
        robot = real_build(backend, **kw)
        robots.append(robot)
        return robot

    bridge = ReachyMiniBridge(config)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bridge_module, "build_robot", build)
        with pytest.raises(BridgeError, match="could not be built.*no network") as info:
            asyncio.run(bridge.start())
    assert isinstance(info.value.__cause__, OSError)
    assert (
        factory.builds == 1
    )  # built once, at the loop's start — never at registration
    (robot,) = robots
    assert robot.commands[-1][0] == "__exit__"  # everything started was unwound
    with pytest.raises(BridgeError):
        _ = bridge.robot
    assert bridge.faces.value == FaceReport.inactive("custom")


def test_a_detector_that_cannot_be_built_leaves_a_switch_as_it_was() -> None:
    scene = _Scene([])
    config = _custom_config(scene, detection=False, tracking=False)

    async def run() -> tuple[bool, bool, bool]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_face_detector(_BrokenFactory())  # callable: accepted
            with pytest.raises(BridgeError, match="could not be built"):
                await bridge.set_face_detection(True)
            detection = bridge.face_detection
            with pytest.raises(BridgeError, match="could not be built"):
                await bridge.start_head_tracking(focus=True)
            await bridge.set_motors_state("enabled")  # the session still works
            return detection, bridge.tracking, bridge.tracking_focus

    assert asyncio.run(run()) == (False, False, False)


# --- attention (specs/core/bridge.md "Attention"), derived from the tracker -----------------


@pytest.fixture
def fast_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(head_tracking_module, "TRACKING_LOST_S", 0.3)
    monkeypatch.setattr(head_tracking_module, "TRACKING_SWITCH_S", 0.15)
    monkeypatch.setattr(face_detection_module, "FACE_POLL_HZ", 20.0)


def test_attention_follows_the_face(fast_attention: None) -> None:
    scene = _Scene([])

    async def run() -> list[str | None]:
        async with ReachyMiniBridge(
            _custom_config(scene)
        ) as bridge:  # tracking, no motors
            await asyncio.sleep(0.15)
            seen = [bridge.attention]
            scene.show()
            await asyncio.sleep(0.5)
            seen.append(bridge.attention)
            scene.hide()
            await asyncio.sleep(0.3 + 0.3)  # TRACKING_LOST_S, and a few frames
            seen.append(bridge.attention)
            await bridge.stop_head_tracking()
            seen.append(bridge.attention)
            return seen

    assert asyncio.run(run()) == ["watching", "engaged", "watching", None]


def test_the_configured_width_and_ceiling_reach_the_shipped_detector(
    monkeypatch: pytest.MonkeyPatch, fast_faces: None
) -> None:
    """`face_detection.width` is handed to the shipped detector's constructor, and the
    loop runs under `target_fps` (specs/vision/user_perception.md "Configuration")."""
    scene = _Scene([_pixel_face(0.0)])
    widths: list[int | None] = []

    def shipped(width: int | None = None) -> _StubDetector:
        widths.append(width)
        return _StubDetector(scene)

    monkeypatch.setattr(face_detection_module, "_yunet_factory", shipped)
    config = ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="yunet", enabled=True, width=640, target_fps=2.0
        ),
    )

    async def run() -> int:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.faces.wait_for(lambda r: bool(r.faces))
            scene.calls = 0
            await asyncio.sleep(1.5)
            return scene.calls

    calls = asyncio.run(run())
    assert widths == [640]
    assert 2 <= calls <= 4, calls  # about two a second, under the fake's frame rate


def test_attention_is_none_outside_a_session() -> None:
    assert ReachyMiniBridge("fake").attention is None


# --- the head tracking report (specs/motion/head_tracking.md "The head tracking report") ----


def _state(report: HeadTrackingReport) -> tuple[bool, bool, str | None, int | None]:
    return (report.active, report.focus, report.attention, report.track_id)


def test_the_head_tracking_report_wakes_on_each_change_of_state(
    fast_attention: None,
) -> None:
    """`bridge.head_tracking.changes()` wakes on tracking starting, engaging a face (its
    track_id, the one `bridge.faces` reports), a focus switch, the loss, and tracking
    stopping — each once, and never while the face merely moves."""
    scene = _Scene([])
    bridge = ReachyMiniBridge(_custom_config(scene, tracking=False))
    assert bridge.head_tracking.value == HeadTrackingReport.inactive()

    async def run() -> tuple[list[HeadTrackingReport], int, float, float]:
        woken: list[HeadTrackingReport] = []

        async def subscribe() -> None:
            async for report in bridge.head_tracking.changes():
                woken.append(report)

        subscriber = asyncio.create_task(subscribe())
        await asyncio.sleep(0)
        async with bridge:
            await bridge.start_head_tracking()
            scene.show(0.0)
            await asyncio.sleep(0.4)
            face_id = bridge.faces.value.faces[0].track_id
            ts_before = bridge.head_tracking.value.ts
            for x in (0.1, 0.2, 0.3):  # the face moves: fresh ts, no wake
                scene.show(x)
                await asyncio.sleep(0.15)
            ts_after = bridge.head_tracking.value.ts
            await bridge.start_head_tracking(focus=True)
            await asyncio.sleep(0.05)
            scene.hide()
            await asyncio.sleep(0.3 + 0.3)  # TRACKING_LOST_S, and a few frames
            await bridge.stop_head_tracking()
            await asyncio.sleep(0.05)
        await asyncio.sleep(0)
        subscriber.cancel()
        return woken, face_id, ts_before, ts_after

    woken, face_id, ts_before, ts_after = asyncio.run(run())
    assert face_id >= 1
    assert [_state(r) for r in woken] == [
        (True, False, "watching", None),  # tracking started
        (True, False, "engaged", face_id),  # the face engaged
        (True, True, "engaged", face_id),  # focus switched
        (True, True, "watching", None),  # the loss
        (False, False, None, None),  # tracking stopped
    ]
    assert ts_after > ts_before
    assert bridge.head_tracking.value == HeadTrackingReport.inactive()


def test_the_head_tracking_report_outlives_sessions(fast_attention: None) -> None:
    """The observable is the bridge's: tracking on from the config publishes the active
    report at every entry and the inactive one at every exit, to the same subscriber."""
    bridge = ReachyMiniBridge(_custom_config(_Scene([])))

    async def run() -> list[tuple[bool, bool, str | None, int | None]]:
        woken: list[HeadTrackingReport] = []

        async def subscribe() -> None:
            async for report in bridge.head_tracking.changes():
                woken.append(report)

        subscriber = asyncio.create_task(subscribe())
        await asyncio.sleep(0)
        for _ in range(2):
            async with bridge:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0)
        subscriber.cancel()
        return [_state(r) for r in woken]

    assert (
        asyncio.run(run())
        == [
            (True, False, "watching", None),
            (False, False, None, None),
        ]
        * 2
    )


def test_a_head_tracking_subscriber_cancelled_mid_wait_ends_cleanly(
    fast_attention: None,
) -> None:
    async def run() -> bool:
        async with ReachyMiniBridge(_custom_config(_Scene([]))) as bridge:

            async def wait() -> None:
                await bridge.head_tracking.wait_for(lambda r: r.track_id == 99)

            waiter = asyncio.create_task(wait())
            await asyncio.sleep(0.05)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await bridge.stop_head_tracking()  # the session still works
            return bridge.head_tracking.value.active

    assert asyncio.run(run()) is False


def test_the_head_turns_toward_a_face_and_back_once_it_is_gone(
    fast_attention: None,
) -> None:
    scene = _Scene([_pixel_face(0.5, 0.0)])  # to the robot's right
    config = _custom_config(scene, idle="hold")

    async def run() -> tuple[float, float]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_motors_state("enabled")
            robot = _fake(bridge)
            await asyncio.sleep(1.5)
            turned = _yaw_deg(robot.last_target[0])
            scene.hide()
            await asyncio.sleep(0.3 + BLEND_S + 0.4)  # the loss, then the fade-out
            return turned, _yaw_deg(robot.last_target[0])

    turned, back = asyncio.run(run())
    assert turned < -5.0  # toward the face's side (negative yaw is to the right)
    assert abs(back) < 1.0


def test_stopping_tracking_hands_the_head_back_and_ignores_faces(
    fast_attention: None,
) -> None:
    config = _custom_config(_Scene([_pixel_face(0.5, 0.0)]), idle="hold")

    async def run() -> tuple[float, float]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_motors_state("enabled")
            robot = _fake(bridge)
            await asyncio.sleep(1.0)
            turned = _yaw_deg(robot.last_target[0])
            await bridge.stop_head_tracking()
            await asyncio.sleep(BLEND_S + 0.3)  # the face still shown
            return turned, _yaw_deg(robot.last_target[0])

    turned, after = asyncio.run(run())
    assert turned < -5.0
    assert abs(after) < 1e-6


def test_tracking_started_without_motors_aims_once_they_are_enabled(
    fast_attention: None,
) -> None:
    config = _custom_config(
        _Scene([_pixel_face(0.5, 0.0)]), idle="hold", tracking=False
    )

    async def run() -> tuple[int, float]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.start_head_tracking()  # motors disabled on the fake
            robot = _fake(bridge)
            await asyncio.sleep(0.3)
            sent = len(robot.targets)  # the loop is paused: nothing moves
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(1.0)
            return sent, _yaw_deg(robot.last_target[0])

    sent, yaw = asyncio.run(run())
    assert sent == 0
    assert yaw < -5.0


# --- faces (specs/vision/user_perception.md) -----------------------------------------------


@pytest.fixture
def fast_faces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(face_detection_module, "FACE_POLL_HZ", 20.0)
    monkeypatch.setattr(face_detection_module, "FACE_ABSENT_S", 0.15)


def test_faces_are_inactive_outside_a_session_and_active_inside(
    fast_faces: None,
) -> None:
    bridge = ReachyMiniBridge(_custom_config(_Scene([]), tracking=False))
    before = bridge.faces.value

    async def run() -> bool:
        async with bridge:
            await asyncio.sleep(0.15)  # the first poll
            return bridge.faces.value.active

    inside = asyncio.run(run())
    assert before == FaceReport.inactive("custom")
    assert inside is True
    assert bridge.faces.value == FaceReport.inactive("custom")


def test_a_subscriber_is_told_when_someone_appears_and_leaves(
    fast_faces: None,
) -> None:
    scene = _Scene([])

    async def run() -> list[FaceReport]:
        async with ReachyMiniBridge(_custom_config(scene, tracking=False)) as bridge:
            await asyncio.sleep(0.15)  # active, nobody there
            woken: list[FaceReport] = []

            async def subscriber() -> None:
                async for report in bridge.faces.changes():
                    woken.append(report)

            task = asyncio.create_task(subscriber())
            await asyncio.sleep(0)
            scene.show(0.3, -0.2)
            await asyncio.sleep(0.3)
            scene.show(0.4, -0.2)  # moving: no wake-up
            await asyncio.sleep(0.3)
            scene.hide()
            await asyncio.sleep(0.5)  # past the absence window
            task.cancel()
            return woken

    woken = asyncio.run(run())
    assert [len(r.faces) for r in woken] == [1, 0]
    assert woken[0].faces[0].x == pytest.approx(0.3, abs=0.02)
    assert all(r.active and r.source == "custom" for r in woken)


def test_cancelling_a_faces_subscriber_returns_promptly_and_the_session_works(
    fast_faces: None,
) -> None:
    scene = _Scene([])

    async def run() -> tuple[float, str]:
        async with ReachyMiniBridge(_custom_config(scene)) as bridge:

            async def subscriber() -> None:
                async for _ in bridge.faces.changes():
                    pass

            task = asyncio.create_task(subscriber())
            await asyncio.sleep(0.2)  # blocked in `async for`
            t0 = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            elapsed = time.monotonic() - t0
            scene.show()  # a later publication reaches nobody, fails nothing
            await asyncio.sleep(0.15)
            await bridge.set_motors_state("enabled")
            await bridge.set_wobbling(False)
            return elapsed, await bridge.get_motors_state()

    elapsed, motors = asyncio.run(run())
    assert elapsed < 0.05
    assert motors == "enabled"


def test_set_face_detection_off_without_tracking_stops_the_loop(
    fast_faces: None,
) -> None:
    scene = _Scene([])

    async def run() -> tuple[bool, bool, int, bool, int]:
        async with ReachyMiniBridge(_custom_config(scene, tracking=False)) as bridge:
            await asyncio.sleep(0.15)
            await bridge.set_face_detection(False)
            off = (bridge.face_detection, bridge.faces.value.active)
            calls = scene.calls
            await asyncio.sleep(0.3)
            idle_calls = scene.calls - calls  # the detector no longer runs
            await bridge.set_face_detection(True)
            await asyncio.sleep(0.2)
            return *off, idle_calls, bridge.faces.value.active, scene.calls - calls

    switch, active, idle_calls, active_again, calls_again = asyncio.run(run())
    assert (switch, active) == (False, False)
    assert idle_calls == 0
    assert active_again is True and calls_again > 0


def test_set_face_detection_off_with_tracking_on_changes_nothing(
    fast_faces: None,
) -> None:
    async def run() -> tuple[bool, bool, int, int]:
        async with ReachyMiniBridge(_custom_config(_Scene([]))) as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(0.15)
            before = len(_fake(bridge).commands)
            await bridge.set_face_detection(False)
            await asyncio.sleep(0.15)
            return (
                bridge.face_detection,
                bridge.faces.value.active,
                before,
                len(_fake(bridge).commands),
            )

    switch, active, before, after = asyncio.run(run())
    assert switch is False
    assert active is True  # the tracker still needs the loop
    assert after == before  # nothing sent to the daemon


def test_stopping_tracking_stops_a_loop_nobody_else_wants(fast_faces: None) -> None:
    config = _custom_config(_Scene([]), detection=False)

    async def run() -> tuple[bool, bool]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(0.15)
            running = bridge.faces.value.active
            await bridge.stop_head_tracking()
            return running, bridge.faces.value.active

    assert asyncio.run(run()) == (True, False)


def test_face_detection_reads_and_resets_to_the_config() -> None:
    config = _custom_config(_Scene([]), detection=False, tracking=False)
    bridge = ReachyMiniBridge(config)
    assert bridge.face_detection is False

    async def run() -> bool:
        async with bridge:
            await bridge.set_face_detection(True)
            return bridge.face_detection

    assert asyncio.run(run()) is True
    assert bridge.face_detection is False


def test_set_face_detection_requires_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniBridge("fake").set_face_detection(True))


def test_a_custom_source_without_a_detector_fails_before_anything_is_entered() -> None:
    """specs/vision/user_perception.md "Custom detectors": `custom` with nothing registered
    (or a bad factory) is refused at the top of bring-up — no daemon, no robot."""
    robots: list[FakeReachyMini] = []

    def build(backend: str, **kw: object) -> FakeReachyMini:
        robots.append(FakeReachyMini())
        return robots[-1]

    bridge = ReachyMiniBridge(
        ReachyMiniConfig(
            backend="fake", face_detection=FaceDetectionSettings(detector="custom")
        )
    )
    not_callable: Any = 42
    bad = ReachyMiniBridge(
        ReachyMiniConfig(
            backend="fake",
            face_detection=FaceDetectionSettings(
                detector="custom", face_detector=not_callable
            ),
        )
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bridge_module, "build_robot", build)
        with pytest.raises(ValueError, match="no face detector is registered"):
            asyncio.run(bridge.start())
        with pytest.raises(ValueError, match="zero-argument callable"):
            asyncio.run(bad.start())
    assert robots == []  # nothing was built, nothing to unwind
    with pytest.raises(BridgeError):
        _ = bridge.robot
    assert bridge.faces.value == FaceReport.inactive("custom")


def test_start_head_tracking_requires_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniBridge("fake").start_head_tracking())


# --- presence & breathing (motion loop) ---------------------------------------------


def test_breathing_rises_from_neutral_and_antennas_lean_outward() -> None:
    async def run() -> tuple[list[float], npt.NDArray[np.float64]]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 1.0)
            _head, antennas, _yaw = _fake(bridge).last_target
            return _head_z(bridge)[-20:], np.asarray(antennas, dtype=np.float64)

    z, antennas = asyncio.run(run())
    # 1 s into the first breath z has risen ~1.7 mm, from ~0.8 mm twenty ticks earlier
    assert max(z) - min(z) > 0.0005
    assert all(-1e-6 <= v <= BREATH_Z_M + 1e-6 for v in z)
    # outward only: neither antenna ever leans inside its neutral lean
    assert np.all(ANTENNA_OUTWARD * antennas >= ANTENNA_MIN_RAD - 1e-6)


def test_idle_hold_holds_neutral() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(idle="hold"))

    async def run() -> list[float]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_z(bridge)[-10:]

    z = asyncio.run(run())
    assert z
    assert all(v == pytest.approx(0.0, abs=1e-6) for v in z)


def test_presence_off_sends_nothing_when_idle() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(presence=False))

    async def run() -> tuple[bool, list[float], int, int]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(0.3)
            idle_targets = _head_z(bridge)
            await bridge.play_emotion("sad")
            after_count = len(_fake(bridge).targets)
            await asyncio.sleep(0.3)
            final_count = len(_fake(bridge).targets)
            return bridge.presence, idle_targets, after_count, final_count

    presence, idle_targets, after_count, final_count = asyncio.run(run())
    assert presence is False
    assert idle_targets == []
    assert after_count > 0
    assert final_count == after_count


def test_set_idle_hold_while_breathing_eases_to_neutral() -> None:
    async def run() -> list[float]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(1.0)
            before = len(_fake(bridge).targets)
            await bridge.set_idle("hold")
            # A fade-out (playing the breathing plan on, its offsets fading to zero) precedes
            # the neutral blend, so this settles a full BLEND_S later than a plain one.
            await asyncio.sleep(2 * BLEND_S + 0.2)
            return _head_z(bridge)[before:]

    z = asyncio.run(run())
    assert z
    assert all(v == pytest.approx(0.0, abs=1e-3) for v in z[-5:])
    assert max(abs(b - a) for a, b in itertools.pairwise(z)) < 0.003


def test_set_presence_on_resumes_from_the_present_pose() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(presence=False))

    async def run() -> list[float]:
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_motors_state("enabled")
            head = np.eye(4)
            head[2, 3] = 0.02
            bridge.robot.set_target(head=head)  # a caller driving the head directly
            await bridge.set_presence(True)
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_z(bridge)

    z = asyncio.run(run())
    assert z
    assert z[0] == pytest.approx(0.02, abs=0.003)
    assert abs(z[-1]) < BREATH_Z_M + 0.002


def test_switches_are_recorded_and_default_from_the_config() -> None:
    config = ReachyMiniConfig(
        backend="fake", motion=MotionSettings(presence=False, idle="hold")
    )
    bridge = ReachyMiniBridge(config)
    assert bridge.presence is False
    assert bridge.idle == "hold"

    async def run() -> None:
        async with bridge:
            assert bridge.presence is False
            await bridge.set_presence(True)
            assert bridge.presence is True

    asyncio.run(run())
    assert bridge.presence is False  # reset to the config's values after exit
    assert bridge.idle == "hold"


def test_switch_verbs_require_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniBridge("fake").set_presence(True))
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniBridge("fake").set_idle("hold"))
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniBridge("fake").set_idle_move(None))


class _Lift(IdleMove):
    """A custom idle move: the head held 8 mm above neutral."""

    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=8.0)


def test_custom_idle_move_from_the_config_plays() -> None:
    config = ReachyMiniConfig(
        backend="fake", motion=MotionSettings(idle="custom", idle_move=_Lift)
    )
    bridge = ReachyMiniBridge(config)
    assert (bridge.idle, bridge.idle_move) == ("custom", _Lift)  # readable before entry

    async def run() -> list[float]:
        async with bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_z(bridge)

    z = asyncio.run(run())
    assert z[-1] == pytest.approx(0.008, abs=1e-6)


def test_set_idle_move_and_set_idle_work_in_either_order() -> None:
    async def run(move_first: bool) -> tuple[list[float], str, object]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            if move_first:
                await bridge.set_idle_move(_Lift)  # stored while breathing plays
                await bridge.set_idle("custom")
            else:
                await bridge.set_idle("custom")  # the hold, until a move is registered
                await bridge.set_idle_move(_Lift)
            await asyncio.sleep(2 * BLEND_S + 0.4)
            return _head_z(bridge), bridge.idle, bridge.idle_move

    for move_first in (True, False):
        z, idle, idle_move = asyncio.run(run(move_first))
        assert z[-1] == pytest.approx(0.008, abs=1e-6)
        assert (idle, idle_move) == ("custom", _Lift)


def test_idle_modes_reset_to_the_config_on_exit() -> None:
    bridge = ReachyMiniBridge("fake")

    async def run() -> None:
        async with bridge:
            await bridge.set_idle_move(_Lift)
            await bridge.set_idle("custom")
            assert (bridge.idle, bridge.idle_move) == ("custom", _Lift)

    asyncio.run(run())
    assert (bridge.idle, bridge.idle_move) == ("breathing", None)


def test_set_idle_rejects_an_unknown_mode() -> None:
    async def run() -> str:
        async with ReachyMiniBridge("fake") as bridge:
            with pytest.raises(ValueError, match="idle mode"):
                await bridge.set_idle("sleeping")
            return bridge.idle

    assert asyncio.run(run()) == "breathing"


def test_set_idle_move_rejects_a_bad_factory_and_keeps_the_registered_one() -> None:
    async def run() -> object:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_idle_move(_Lift)
            with pytest.raises(ValueError, match="idle move"):
                await bridge.set_idle_move(HoldMove)  # type: ignore[arg-type]
            return bridge.idle_move

    assert asyncio.run(run()) is _Lift


def test_a_bad_idle_move_in_the_config_fails_bring_up() -> None:
    config = ReachyMiniConfig(
        backend="fake",
        motion=MotionSettings(idle="custom", idle_move=HoldMove),  # type: ignore[arg-type]
    )

    async def run() -> None:
        async with ReachyMiniBridge(config):
            pass

    with pytest.raises(ValueError, match="idle move"):
        asyncio.run(run())


def test_exit_leaves_the_head_at_neutral() -> None:
    async def run() -> FakeReachyMini:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(1.0)
            return _fake(bridge)

    robot = asyncio.run(run())
    head, antennas, _yaw = robot.last_target
    assert abs(head[2, 3]) < 0.001
    assert antennas == pytest.approx(NEUTRAL_ANTENNAS, abs=1e-3)


# --- perception (camera): the feed (specs/vision/camera.md) ---------------------------------


async def _first_frame(bridge: ReachyMiniBridge, timeout: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    while bridge.camera.latest() is None:
        assert time.monotonic() < deadline, "no frame within the wait"
        await asyncio.sleep(0.005)
    return bridge.camera.latest()


def test_camera_publishes_the_fakes_frames_while_entered() -> None:
    bridge = ReachyMiniBridge("fake")
    camera = (
        bridge.camera
    )  # exists from construction: a consumer wires to it before entry
    assert camera.latest() is None

    async def run() -> tuple[Any, int]:
        async with bridge:
            assert bridge.camera is camera  # the same object inside
            frame = await _first_frame(bridge, timeout=1.5 / FAKE_FRAME_HZ)
            before = camera.published_count
            await asyncio.sleep(1.0)
            return frame, camera.published_count - before

    frame, per_second = asyncio.run(run())
    assert camera.latest() is None  # after exit: no frame from a camera that is gone
    assert frame.frame_id >= 1
    assert frame.image.shape == (48, 64, 3) and frame.image.dtype == np.uint8
    assert frame.image.min() != frame.image.max()  # real structure, not a constant
    assert 7 <= per_second <= 13  # the fake's 10 fps, the feed does not pace itself


def test_camera_frames_carry_the_head_pose_at_their_time() -> None:
    config = ReachyMiniConfig(
        backend="fake", motion=MotionSettings(tracking=False, presence=False)
    )
    turned = np.eye(4)
    turned[:3, :3] = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]  # 90° yaw

    async def run() -> tuple[Any, Any]:
        async with ReachyMiniBridge(config) as bridge:
            first = await _first_frame(bridge)
            robot = _fake(bridge)
            robot.set_target(head=turned)  # the fake's head is now at this pose
            await asyncio.sleep(3.0 / FAKE_FRAME_HZ)
            return first, bridge.camera.latest()

    first, later = asyncio.run(run())
    assert first.head_pose is not None and _yaw_deg(first.head_pose) == 0.0
    assert later.head_pose is not None
    assert _yaw_deg(later.head_pose) == pytest.approx(90.0)
    assert later.ts > first.ts


def test_camera_frame_ids_count_on_across_sessions() -> None:
    bridge = ReachyMiniBridge("fake")

    async def run() -> int:
        async with bridge:
            await _first_frame(bridge)
        async with bridge:
            frame = await _first_frame(bridge)
            return frame.frame_id

    assert asyncio.run(run()) >= 2


# --- faces: the custom detection source (specs/vision/user_perception.md) --------------------


# --- the sim's face markers (specs/daemon/sim_displays.md) ------------------------------------


class _DisplaysDaemon:
    """Stands in for the sim daemon's HTTP port: records every request's method, path
    and JSON body, and answers 200 — after `hold` is released, when a test holds it."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.entered = threading.Event()
        self.hold = threading.Event()
        self.hold.set()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                daemon.requests.append(("PUT", self.path, body))
                daemon.entered.set()
                daemon.hold.wait(5.0)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"markers": 0}')

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def marker_sets(self) -> list[list[dict[str, Any]]]:
        return [body["markers"] for _, _, body in self.requests]

    def close(self) -> None:
        self.hold.set()
        self._server.shutdown()
        self._server.server_close()


def _sim_with_face_markers(
    scene: _Scene, port: int, *, face_markers: bool = True
) -> ReachyMiniConfig:
    """A `sim` session on a daemon somebody else runs at `port`, with the face markers
    display on and a stub detector standing in for the person."""
    return ReachyMiniConfig(
        backend="sim",
        robot={"host": "127.0.0.1", "port": port},
        daemon=DaemonConfig(
            headless=False, sim_displays=SimDisplaySettings(face_markers=face_markers)
        ),
        face_detection=FaceDetectionSettings(
            detector="custom", enabled=True, face_detector=scene.detector
        ),
        motion=MotionSettings(tracking=True, idle="hold"),
    )


@pytest.fixture
def displays_daemon(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A `sim` bridge whose robot is the fake and whose daemon port is a recorder."""
    monkeypatch.setattr(
        bridge_module, "build_robot", lambda backend, **kw: FakeReachyMini()
    )
    daemon = _DisplaysDaemon()
    yield daemon
    daemon.close()


async def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


def test_the_sim_session_sends_its_faces_to_the_viewer(
    displays_daemon: _DisplaysDaemon,
) -> None:
    """With `sim_displays.face_markers` on, a `sim` session sends each face report to
    the daemon's displays route: the face placed ahead on the side it is seen, marked
    as followed once the head follows it; an empty set once nobody is there; and
    nothing after the session stops."""
    scene = _Scene([_pixel_face(0.4, 0.0)])

    async def run() -> int:
        config = _sim_with_face_markers(scene, displays_daemon.port)
        async with ReachyMiniBridge(config) as bridge:
            sets = displays_daemon.marker_sets
            await _wait_until(lambda: any(m and m[0]["followed"] for m in sets()))
            assert bridge.head_tracking.value.track_id is not None
            scene.hide()
            await _wait_until(lambda: sets()[-1] == [])
        stopped = len(displays_daemon.requests)
        await asyncio.sleep(0.2)
        return stopped

    stopped = asyncio.run(run())
    assert len(displays_daemon.requests) == stopped
    assert {(method, path) for method, path, _ in displays_daemon.requests} == {
        ("PUT", "/api/sim/displays/face_markers")
    }
    (marker,) = next(m for m in displays_daemon.marker_sets() if m)
    x, y, _ = marker["pos"]
    # Seen to the image's right of a camera looking along +x: ahead, at negative y.
    assert x > 0.1 and y < -0.02
    assert marker["label"].isdigit()  # the face's track id
    # The rendered eye camera looks at the test scene's portrait: its box height.
    assert marker["size"][1] == pytest.approx(FACE_BOX_HEIGHT_M)


def test_no_face_markers_are_sent_with_the_display_off(
    displays_daemon: _DisplaysDaemon,
) -> None:
    scene = _Scene([_pixel_face(0.4, 0.0)])

    async def run() -> None:
        config = _sim_with_face_markers(scene, displays_daemon.port, face_markers=False)
        async with ReachyMiniBridge(config) as bridge:
            await _wait_until(lambda: len(bridge.faces.value.faces) == 1)
            await asyncio.sleep(0.2)

    asyncio.run(run())
    assert displays_daemon.requests == []


def test_stopping_the_session_mid_request_leaves_it_usable(
    displays_daemon: _DisplaysDaemon,
) -> None:
    """The daemon holding a face-markers request does not hold the session: `stop()`
    returns without waiting for it, and the same bridge starts and sends again."""
    scene = _Scene([_pixel_face(0.4, 0.0)])
    bridge = ReachyMiniBridge(_sim_with_face_markers(scene, displays_daemon.port))

    async def run() -> float:
        displays_daemon.hold.clear()
        await bridge.start()
        await _wait_until(displays_daemon.entered.is_set)
        started = time.monotonic()
        await bridge.stop()
        took = time.monotonic() - started
        displays_daemon.hold.set()
        before = len(displays_daemon.requests)
        async with bridge:
            await _wait_until(lambda: len(displays_daemon.requests) > before)
        return took

    took = asyncio.run(run())
    assert took < 2.0  # the daemon would have held the request for 5 s
    assert not bridge.running


class _Scene:
    """What a stub detector sees: pixel faces on the fake's 64x48 frame, shown and
    hidden by a test as a person would step in front of the robot and leave."""

    def __init__(self, faces: list[PixelFace]) -> None:
        self.faces = faces
        self.calls = 0
        self.raises = False  # a detector failing on every frame: no observation at all
        # The factory: one object, so identity checks on `bridge.face_detector` hold.
        self.detector: Callable[[], _StubDetector] = lambda: _StubDetector(self)

    def show(self, x: float = 0.0, y: float = 0.0) -> None:
        self.faces[:] = [_pixel_face(x, y)]

    def hide(self) -> None:
        self.faces.clear()


class _StubDetector:
    def __init__(self, scene: _Scene) -> None:
        self._scene = scene

    def detect(self, frame_bgr: npt.NDArray[np.uint8], ts: float) -> list[PixelFace]:
        self._scene.calls += 1
        if self._scene.raises:
            raise RuntimeError("model crashed")
        return list(self._scene.faces)


def _pixel_face(x_norm: float, y_norm: float = 0.0) -> PixelFace:
    """A face whose nose sits at the normalised (x, y) of the fake's frame."""
    u = (x_norm + 1.0) / 2.0 * 63
    v = (y_norm + 1.0) / 2.0 * 47
    return PixelFace(bbox=(u - 5, v - 8, 10, 16), nose=(u, v))


def _custom_config(
    scene: _Scene | None, *, detection: bool = True, **motion: Any
) -> ReachyMiniConfig:
    """A fake with a stub detector registered through the `custom` path — the fake's
    stand-in for a person (specs/vision/user_perception.md "`fake` backend support"); detection
    and tracking on unless told otherwise."""
    motion.setdefault("tracking", True)
    return ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="custom",
            enabled=detection,
            face_detector=None if scene is None else scene.detector,
        ),
        motion=MotionSettings(**motion),
    )


async def _wait_for_face_x(
    bridge: ReachyMiniBridge, x: float, timeout: float = 1.0
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        faces = bridge.faces.value.faces
        if faces and abs(faces[0].x - x) < 0.02:
            return
        assert time.monotonic() < deadline, f"no face at x={x}: {bridge.faces.value}"
        await asyncio.sleep(0.01)


def test_a_custom_config_enters_and_reports_the_stubs_faces() -> None:
    scene = _Scene([_pixel_face(0.5, -0.25)])

    async def run() -> tuple[FaceReport, list[str], int]:
        async with ReachyMiniBridge(_custom_config(scene, tracking=False)) as bridge:
            assert bridge.face_detector is scene.detector
            await _wait_for_face_x(bridge, 0.5)
            await asyncio.sleep(0.5)
            return bridge.faces.value, _command_names(bridge), scene.calls

    report, commands, calls = asyncio.run(run())
    face = report.faces[0]
    assert (report.source, report.active) == ("custom", True)
    assert face.y == pytest.approx(-0.25) and face.size == pytest.approx(16 / 48)
    assert report.head_pose is not None  # the frame's, on the fake
    assert not any(c.endswith("head_tracking") for c in commands)
    assert calls >= 4  # once per frame at 10 fps, over at least half a second


def test_set_face_detector_swaps_detectors_mid_session() -> None:
    left, right = _Scene([_pixel_face(-0.5)]), _Scene([_pixel_face(0.5)])

    async def run() -> tuple[int, Any]:
        async with ReachyMiniBridge(_custom_config(left, tracking=False)) as bridge:
            await _wait_for_face_x(bridge, -0.5)
            await bridge.set_face_detector(right.detector)
            await _wait_for_face_x(bridge, 0.5, timeout=2.5 / FAKE_FRAME_HZ)
            bad: Any = 42  # not callable: refused at registration
            with pytest.raises(ValueError, match="zero-argument callable"):
                await bridge.set_face_detector(bad)
            assert bridge.face_detector is right.detector  # the bad one left it alone
            calls_left = left.calls
            await asyncio.sleep(3.0 / FAKE_FRAME_HZ)
            assert left.calls == calls_left  # the old detector is never called again
            with pytest.raises(ValueError, match="cannot be cleared"):
                await bridge.set_face_detector(None)  # the loop runs it
            assert bridge.face_detector is right.detector
            await bridge.set_face_detection(False)  # nobody needs faces now
            await bridge.set_face_detector(None)  # cleared
            await asyncio.sleep(0.05)
            return left.calls, bridge.faces.value

    _calls, value = asyncio.run(run())
    assert value.active is False


def test_face_detector_reads_the_config_outside_a_session_and_resets() -> None:
    scene, other = _Scene([]), _Scene([])
    bridge = ReachyMiniBridge(_custom_config(scene, tracking=False))
    assert bridge.face_detector is scene.detector

    async def run() -> None:
        async with bridge:
            await bridge.set_face_detector(other.detector)
            assert bridge.face_detector is other.detector

    asyncio.run(run())
    assert bridge.face_detector is scene.detector
    assert ReachyMiniBridge("fake").face_detector is None


def test_the_head_turns_toward_a_custom_detectors_face() -> None:
    """The whole pipeline on the fake: feed → stub detector → selection → report with
    the frame's pose → the tracker's aim → the motion loop's gaze layer."""
    scene = _Scene([_pixel_face(0.5, 0.0)])  # to the robot's right

    async def run() -> tuple[float, str | None, bool, float]:
        async with ReachyMiniBridge(_custom_config(scene, idle="hold")) as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(1.5)
            tracker = bridge._tracker
            assert tracker is not None
            return (
                _yaw_deg(_fake(bridge).last_target[0]),
                bridge.attention,
                bridge.faces.value.head_pose is not None,
                tracker.delay_s,
            )

    yaw, attention, with_pose, delay = asyncio.run(run())
    assert yaw < -5.0  # negative yaw is to the right
    assert attention == "engaged"
    # The fake's frames carry their capture pose, so the tracker aims against it exactly
    # and its delay estimate is never exercised (specs/motion/head_tracking.md "The aim").
    assert with_pose and delay == head_tracking_module.DELAY_PRIOR_S


# --- the lifecycle pair (specs/core/bridge.md "Lifecycle") ----------------------------


def _lifecycle_names(robot: FakeReachyMini) -> list[str]:
    return [name for name, _ in robot.commands if name != "set_target"]


def test_start_and_stop_are_the_block() -> None:
    """The pair and `async with` run the same session: the same commands reach the
    robot in the same order, wobbling is off at the end, the modes are the config's."""

    async def block() -> tuple[list[str], bool, bool]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_presence(False)
            robot = _fake(bridge)
        return _lifecycle_names(robot), bridge.wobbling, bridge.presence

    async def pair() -> tuple[list[str], bool, bool]:
        bridge = ReachyMiniBridge("fake")
        await bridge.start()
        await bridge.set_presence(False)
        robot = _fake(bridge)
        await bridge.stop()
        return _lifecycle_names(robot), bridge.wobbling, bridge.presence

    names_block, wobbling_block, presence_block = asyncio.run(block())
    names_pair, wobbling_pair, presence_pair = asyncio.run(pair())
    assert names_pair == names_block
    assert "disable_wobbling" in names_pair and names_pair[-1] == "__exit__"
    assert (
        (wobbling_pair, presence_pair)
        == (wobbling_block, presence_block)
        == (False, True)
    )


def test_running_follows_the_session() -> None:
    bridge = ReachyMiniBridge("fake")
    assert bridge.running is False

    async def run() -> tuple[bool, bool]:
        await bridge.start()
        inside = bridge.running
        await bridge.stop()
        return inside, bridge.running

    assert asyncio.run(run()) == (True, False)


def test_running_is_false_after_a_failed_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(backend: str, **opts: object) -> FakeReachyMini:
        raise ConnectionError("no daemon")

    monkeypatch.setattr(bridge_module, "build_robot", failing)
    bridge = ReachyMiniBridge("fake")
    with pytest.raises(ConnectionError):
        asyncio.run(bridge.start())
    assert bridge.running is False
    with pytest.raises(BridgeError):
        _ = bridge.robot


def test_stop_before_start_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[str] = []

    def build(backend: str, **opts: object) -> FakeReachyMini:
        builds.append(backend)
        return FakeReachyMini()

    monkeypatch.setattr(bridge_module, "build_robot", build)
    bridge = ReachyMiniBridge("fake")
    asyncio.run(bridge.stop())
    assert bridge.running is False and builds == []


def test_stop_then_start_is_a_second_session_on_the_same_object() -> None:
    bridge = ReachyMiniBridge("fake")
    faces, camera = bridge.faces, bridge.camera

    async def run() -> tuple[str, bool]:
        await bridge.start()
        first = _fake(bridge)
        await bridge.stop()
        await bridge.start()
        second = _fake(bridge)
        await bridge.set_motors_state("enabled")
        state = await bridge.get_motors_state()
        await bridge.stop()
        return state, first is second

    state, same_robot = asyncio.run(run())
    assert state == "enabled" and same_robot is False  # a fresh robot per session
    assert bridge.faces is faces and bridge.camera is camera  # the same objects across
    assert bridge.running is False


# --- the detector built once, and only when someone needs faces --------------------------


def test_the_custom_factory_is_called_once_per_start_on_a_worker() -> None:
    scene = _Scene([])
    built: list[int] = []

    def factory() -> _StubDetector:
        built.append(threading.get_ident())
        return _StubDetector(scene)

    async def run() -> tuple[int, int, int, int]:
        config = _custom_config(scene, detection=False, tracking=False)
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_face_detector(factory)
            registered = len(built)
            await asyncio.sleep(0.05)
            idle = len(built)  # nobody needs faces: nothing is built
            await bridge.start_head_tracking()
            await asyncio.sleep(0.05)
            started = len(built)
            await bridge.stop_head_tracking()
            await bridge.set_face_detection(True)
            await asyncio.sleep(0.05)
            return registered, idle, started, len(built)

    assert asyncio.run(run()) == (0, 0, 1, 2)
    assert threading.get_ident() not in built  # on a worker thread, never the loop's


def test_a_factory_result_without_detect_fails_entry_and_the_verbs_alike() -> None:
    class NoDetect:
        pass

    no_detect: Any = (
        NoDetect  # callable, so the type checker and the check both pass it
    )
    config = ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="custom", enabled=True, face_detector=no_detect
        ),
    )
    with pytest.raises(ValueError, match="NoDetect has no callable `detect"):
        asyncio.run(ReachyMiniBridge(config).start())

    async def run() -> tuple[bool, bool, bool]:
        config = _custom_config(_Scene([]), detection=False, tracking=False)
        async with ReachyMiniBridge(config) as bridge:
            await bridge.set_face_detector(no_detect)  # callable: accepted
            with pytest.raises(ValueError, match="no callable `detect"):
                await bridge.start_head_tracking()
            with pytest.raises(ValueError, match="no callable `detect"):
                await bridge.set_face_detection(True)
            await bridge.set_motors_state("enabled")  # the session still works
            return bridge.tracking, bridge.face_detection, bridge.faces.value.active

    assert asyncio.run(run()) == (False, False, False)


def test_clearing_the_custom_detector_while_tracking_is_refused(
    fast_faces: None,
) -> None:
    scene = _Scene([_pixel_face(0.3)])

    async def run() -> tuple[
        str | None, tuple[bool, bool, str | None, bool], Any, bool
    ]:
        async with ReachyMiniBridge(_custom_config(scene, detection=False)) as bridge:
            await _wait_for_face_x(bridge, 0.3)
            await asyncio.sleep(0.1)
            engaged = bridge.attention
            with pytest.raises(
                ValueError, match="stop head tracking and face detection"
            ):
                await bridge.set_face_detector(None)
            kept = (
                bridge.tracking,
                bridge.face_detector is scene.detector,
                bridge.attention,
                bridge.faces.value.active,
            )
            await bridge.stop_head_tracking()
            await bridge.set_face_detector(None)  # nobody needs faces: cleared
            with pytest.raises(ValueError, match="no face detector is registered"):
                await bridge.start_head_tracking()
            return engaged, kept, bridge.face_detector, bridge.tracking

    engaged, kept, cleared, tracking = asyncio.run(run())
    assert engaged == "engaged"
    assert kept == (True, True, "engaged", True)
    assert cleared is None and tracking is False


# --- a silent detector: the loss by time, the inactive report empty ------------------------


def test_a_silent_detector_releases_the_head_reads_inactive_and_re_engages(
    monkeypatch: pytest.MonkeyPatch, fast_attention: None
) -> None:
    """The detector fails on every frame, so no observation reaches the tracker at all:
    the aim is withdrawn after TRACKING_LOST_S anyway (the loop's ticks keep the
    tracker's clock), the report turns inactive and empty after FACE_SOURCE_DOWN_S, and
    the head re-engages once observations return."""
    monkeypatch.setattr(face_detection_module, "FACE_SOURCE_DOWN_S", 0.8)
    scene = _Scene([_pixel_face(0.5)])

    async def run() -> tuple[
        tuple[str | None, float], tuple[str | None, bool], FaceReport, float, str | None
    ]:
        async with ReachyMiniBridge(_custom_config(scene, idle="hold")) as bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(1.0)
            engaged = (bridge.attention, _yaw_deg(_fake(bridge).last_target[0]))
            scene.raises = True
            await asyncio.sleep(
                0.5
            )  # past TRACKING_LOST_S (0.3), before the source-down
            lost = (bridge.attention, bridge.faces.value.active)
            await asyncio.sleep(0.5)  # past FACE_SOURCE_DOWN_S
            down = bridge.faces.value
            await asyncio.sleep(0.8)  # the gaze layer faded out onto the neutral hold
            released = _yaw_deg(_fake(bridge).last_target[0])
            scene.raises = False
            await asyncio.sleep(0.6)
            return engaged, lost, down, released, bridge.attention

    engaged, lost, down, released, again = asyncio.run(run())
    assert engaged[0] == "engaged" and engaged[1] < -5.0
    assert lost == ("watching", True)
    assert down == FaceReport.inactive("custom")  # no stale face while nobody looks
    assert abs(released) < 1.0
    assert again == "engaged"


# --- the modes outside a session (specs/core/bridge.md: no side effect on a failed call)


def test_set_presence_outside_a_session_raises_and_changes_nothing() -> None:
    bridge = ReachyMiniBridge("fake")
    with pytest.raises(BridgeError):
        asyncio.run(bridge.set_presence(False))
    assert bridge.presence is True  # the config's value, untouched by the failed call

    async def run() -> tuple[bool, int]:
        async with bridge:
            await bridge.set_motors_state("enabled")
            await asyncio.sleep(0.3)
            return bridge.presence, len(_fake(bridge).targets)

    presence, targets = asyncio.run(run())
    assert presence is True and targets > 0  # the next session idles: presence on


# --- one say at a time, the newest wins (specs/audio/audio.md "TTS out") -----------------


class _SecondSynth:
    """A SpeechSynthesizer whose utterance is one second of tone, synthesized at once."""

    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        yield np.full(16000, 0.1, dtype=np.float32)


def test_a_new_say_interrupts_the_one_playing_and_the_session_plays_on() -> None:
    async def run() -> tuple[float, float, list[str]]:
        async with ReachyMiniBridge("fake", synthesizer=_SecondSynth()) as bridge:
            first = asyncio.create_task(bridge.say("first"))
            while "media.push_audio_sample" not in _command_names(bridge):
                await asyncio.sleep(0)
            t0 = time.monotonic()
            second = asyncio.create_task(bridge.say("second"))
            with pytest.raises(SpeechInterruptedError):
                await first
            interrupted_after = time.monotonic() - t0
            await second
            second_took = time.monotonic() - t0
            await bridge.say("third")
            return interrupted_after, second_took, _command_names(bridge)

    interrupted_after, second_took, names = asyncio.run(run())
    assert interrupted_after < 0.2  # the first ended at once, not after its second
    assert second_took >= 1.0  # the second played in full
    assert names.count("audio.clear_player") == 1  # the first's audio flushed, once


@pytest.mark.parametrize("state", ["disabled", "gravity_compensation"])
def test_motor_pause_stops_emotion_sound_before_its_failure(state: str) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            robot = _fake(bridge)
            await bridge.set_motors_state("enabled")
            emotion = asyncio.create_task(bridge.play_emotion("happy"))
            await _wait_until(lambda: "media.play_sound" in _command_names(bridge))
            await bridge.set_motors_state(state)
            with pytest.raises(BridgeError, match="motors left"):
                await emotion
            assert "media.stop_sound" in _command_names(bridge)
            count = len(robot.targets)
            await asyncio.sleep(0.05)
            assert len(robot.targets) == count
            await bridge.set_motors_state("enabled")
            await bridge.play_emotion("sad")

    asyncio.run(run())


def test_shutdown_stops_emotion_sound_before_media_and_allows_restart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> None:
        bridge = ReachyMiniBridge("fake")
        await bridge.start()
        robot = _fake(bridge)
        await bridge.set_motors_state("enabled")
        emotion = asyncio.create_task(bridge.play_emotion("happy"))
        await _wait_until(lambda: "media.play_sound" in _command_names(bridge))
        await bridge.stop()
        with pytest.raises(asyncio.CancelledError):
            await emotion
        names = [name for name, _ in robot.commands]
        assert names.index("media.stop_sound") < names.index("media.stop_playing")
        assert not bridge.running
        async with bridge:
            await bridge.say("still works", _ToneSynth())

    asyncio.run(run())
    assert "could not stop the emotion's sound" not in caplog.text
    assert "could not restore wobbling" not in caplog.text


def test_motor_pause_spares_a_sound_replacing_the_emotion(tmp_path: Path) -> None:
    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            emotion = asyncio.create_task(bridge.play_emotion("happy"))
            await _wait_until(lambda: "media.play_sound" in _command_names(bridge))
            sound = asyncio.create_task(bridge.play_sound(str(_wav(tmp_path, 0.15))))
            await _wait_until(
                lambda: _command_names(bridge).count("media.play_sound") == 2
            )
            await bridge.set_motors_state("disabled")
            with pytest.raises(BridgeError):
                await emotion
            await sound
            assert "media.stop_sound" not in _command_names(bridge)
            await bridge.say("speaker works", _ToneSynth())

    asyncio.run(run())


def test_motor_pause_stops_emotion_sound_without_flushing_concurrent_speech() -> None:
    async def run() -> None:
        first_chunk, continue_speech = asyncio.Event(), asyncio.Event()

        class Synth:
            sample_rate = 16000

            async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
                yield np.full(400, 0.2, dtype=np.float32)
                first_chunk.set()
                await continue_speech.wait()
                yield np.full(800, 0.3, dtype=np.float32)

        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            emotion = asyncio.create_task(bridge.play_emotion("happy"))
            await _wait_until(lambda: "media.play_sound" in _command_names(bridge))
            speech = asyncio.create_task(bridge.say("keep speaking", Synth()))
            try:
                await asyncio.wait_for(first_chunk.wait(), 2)
                await bridge.set_motors_state("disabled")
                with pytest.raises(BridgeError, match="motors left"):
                    await emotion
                assert "media.stop_sound" in _command_names(bridge)
                continue_speech.set()
                await asyncio.wait_for(speech, 2)
                frames = [
                    data["frames"]
                    for name, data in _fake(bridge).commands
                    if name == "media.push_audio_sample"
                ]
                assert frames == [400, 800]
                assert "audio.clear_player" not in _command_names(bridge)
            finally:
                continue_speech.set()
                speech.cancel()
                await asyncio.gather(speech, return_exceptions=True)

    asyncio.run(run())


class _OwnedFaceDetector:
    def __init__(self, x: float = 0) -> None:
        self.closed = 0
        self.x = x

    def detect(self, frame_bgr: npt.NDArray[np.uint8], ts: float) -> list[PixelFace]:
        assert not self.closed
        return [_pixel_face(self.x)]

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("phase", ["start", "enable", "replacement"])
def test_cancelled_detector_construction_is_closed_and_session_recovers(
    phase: str,
) -> None:
    async def run() -> None:
        started, release = threading.Event(), threading.Event()
        built: list[_OwnedFaceDetector] = []

        def held_factory() -> _OwnedFaceDetector:
            started.set()
            assert release.wait(5)
            detector = _OwnedFaceDetector()
            built.append(detector)
            return detector

        originals: list[_OwnedFaceDetector] = []

        def original_factory() -> _OwnedFaceDetector:
            detector = _OwnedFaceDetector()
            originals.append(detector)
            return detector

        cfg = ReachyMiniConfig(
            backend="fake",
            face_detection=FaceDetectionSettings(
                detector="custom",
                enabled=phase != "enable",
                face_detector=held_factory if phase == "start" else original_factory,
            ),
            motion=MotionSettings(wobbling=False),
        )
        bridge = ReachyMiniBridge(cfg)
        pending: asyncio.Task[None] | None = None
        try:
            if phase == "start":
                pending = asyncio.create_task(bridge.start())
            else:
                await bridge.start()
                await bridge.set_face_detector(held_factory)
                if phase == "enable":
                    pending = asyncio.create_task(bridge.set_face_detection(True))
            await _wait_until(started.is_set)
            if phase == "replacement":
                pending = asyncio.create_task(bridge.stop())
            else:
                assert pending is not None
                pending.cancel()
            if phase == "enable":
                assert pending is not None
                # Runtime cancellation returns while the model worker is still held.
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 1)
                assert not bridge.face_detection and not bridge.faces.value.active
                await bridge.say("session stays usable", _ToneSynth())
                new = _OwnedFaceDetector(0.3)
                await bridge.set_face_detector(lambda: new)
                await bridge.set_face_detection(True)
                await _wait_until(lambda: bool(bridge.faces.value.faces))
                assert bridge.faces.value.faces[0].x == pytest.approx(0.3, abs=0.03)
            release.set()
            assert pending is not None
            if phase == "start":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 3)
            elif phase == "replacement":
                await asyncio.wait_for(pending, 3)
                assert originals[0].closed == 1
            await _wait_until(lambda: bool(built) and built[0].closed == 1)
            if phase != "enable":
                assert not bridge.running and not bridge.faces.value.active
                async with bridge:
                    await _wait_until(lambda: bool(bridge.faces.value.faces))
            else:
                assert bridge.faces.value.faces[0].x == pytest.approx(0.3, abs=0.03)
        finally:
            release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await bridge.stop()
        assert built[0].closed == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "enabled,close_while_held", [(True, False), (False, False), (True, True)]
)
def test_cancelled_wobbling_command_keeps_completion_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, close_while_held: bool
) -> None:
    async def run() -> None:
        bridge = ReachyMiniBridge(
            ReachyMiniConfig(
                backend="fake", motion=MotionSettings(wobbling=not enabled)
            )
        )
        await bridge.start()
        robot = _fake(bridge)
        entered, release, completed = (threading.Event() for _ in range(3))
        original = robot.enable_wobbling if enabled else robot.disable_wobbling

        def held() -> None:
            entered.set()
            assert release.wait(5)
            original()
            completed.set()

        monkeypatch.setattr(
            robot, "enable_wobbling" if enabled else "disable_wobbling", held
        )
        caller = asyncio.create_task(bridge.set_wobbling(enabled))
        stopping: asyncio.Task[None] | None = None
        try:
            await _wait_until(entered.is_set)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
            assert not completed.is_set()
            if close_while_held:
                stopping = asyncio.create_task(bridge.stop())
                await _wait_until(lambda: not bridge.running)
                assert not stopping.done()
            release.set()
            await _wait_until(completed.is_set)
            if stopping is not None:
                await asyncio.wait_for(stopping, 3)
            else:
                await _wait_until(lambda: bridge.wobbling == enabled)
                await bridge.say("usable", _ToneSynth())
                await bridge.stop()
            names = [name for name, _ in robot.commands if "wobbling" in name]
            assert names == ["enable_wobbling", "disable_wobbling"]
            assert not bridge.wobbling
            async with bridge:
                await bridge.say("next session", _ToneSynth())
        finally:
            release.set()
            await asyncio.gather(
                caller,
                *([] if stopping is None else [stopping]),
                return_exceptions=True,
            )
            await bridge.stop()

    asyncio.run(run())


def test_cancelled_startup_wobbling_enable_is_disabled_before_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    robots: list[FakeReachyMini] = []
    original = FakeReachyMini.enable_wobbling

    def held(robot: FakeReachyMini) -> None:
        robots.append(robot)
        entered.set()
        assert release.wait(5)
        original(robot)

    monkeypatch.setattr(FakeReachyMini, "enable_wobbling", held)

    async def run() -> None:
        bridge = ReachyMiniBridge("fake")
        starting = asyncio.create_task(bridge.start())
        try:
            await _wait_until(entered.is_set)
            starting.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(starting, 3)
            assert [name for name, _ in robots[0].commands if "wobbling" in name] == [
                "enable_wobbling",
                "disable_wobbling",
            ]
            assert not bridge.running and not bridge.wobbling
            async with bridge:
                assert bridge.wobbling
                await bridge.say("restarted", _ToneSynth())
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await bridge.stop()

    asyncio.run(run())


def test_startup_cancelled_twice_still_disables_the_held_wobbling_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second cancel while the bring-up cleanup waits on the held enable does not
    cut the wobbling session's stop short: the enable lands, the disable follows it,
    and only then does the connection exit (specs/core/bridge.md "Lifecycle" — nothing
    is leaked, a second cancel does not shorten the cleanup)."""
    entered, release = threading.Event(), threading.Event()
    robots: list[FakeReachyMini] = []
    original = FakeReachyMini.enable_wobbling

    def held(robot: FakeReachyMini) -> None:
        robots.append(robot)
        entered.set()
        assert release.wait(5)
        original(robot)

    monkeypatch.setattr(FakeReachyMini, "enable_wobbling", held)

    async def run() -> None:
        bridge = ReachyMiniBridge("fake")
        starting = asyncio.create_task(bridge.start())
        try:
            await _wait_until(entered.is_set)
            starting.cancel()
            await asyncio.sleep(0.05)  # the cleanup is now waiting on the held enable
            assert not starting.done()
            starting.cancel()
            await asyncio.sleep(0.05)
            assert not starting.done(), "the second cancel cut the cleanup short"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(starting, 3)
            names = [name for name, _ in robots[0].commands]
            assert [n for n in names if "wobbling" in n] == [
                "enable_wobbling",
                "disable_wobbling",
            ]
            assert names.index("disable_wobbling") < names.index("__exit__")
            assert not bridge.running and not bridge.wobbling
            async with bridge:
                assert bridge.wobbling
                await bridge.say("restarted", _ToneSynth())
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await bridge.stop()

    asyncio.run(run())


def test_cancelled_enable_is_ordered_before_a_later_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with ReachyMiniBridge(
            ReachyMiniConfig(backend="fake", motion=MotionSettings(wobbling=False))
        ) as bridge:
            robot = _fake(bridge)
            entered, release = threading.Event(), threading.Event()
            original = robot.enable_wobbling

            def held() -> None:
                entered.set()
                assert release.wait(5)
                original()

            monkeypatch.setattr(robot, "enable_wobbling", held)
            first = asyncio.create_task(bridge.set_wobbling(True))
            second: asyncio.Task[None] | None = None
            try:
                await _wait_until(entered.is_set)
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(first, 1)
                second = asyncio.create_task(bridge.set_wobbling(False))
                await asyncio.sleep(0)
                release.set()
                await asyncio.wait_for(second, 3)
                assert not bridge.wobbling
                assert [name for name, _ in robot.commands if "wobbling" in name] == [
                    "enable_wobbling",
                    "disable_wobbling",
                ]
            finally:
                release.set()
                await asyncio.gather(
                    first, *([] if second is None else [second]), return_exceptions=True
                )

    asyncio.run(run())


def test_face_track_ids_continue_across_sessions_restarts_and_replacement() -> None:
    cfg = ReachyMiniConfig(
        backend="fake",
        face_detection=FaceDetectionSettings(
            detector="custom", enabled=True, face_detector=_OwnedFaceDetector
        ),
        motion=MotionSettings(wobbling=False),
    )

    async def face_id(bridge: ReachyMiniBridge) -> int:
        await _wait_until(lambda: bool(bridge.faces.value.faces))
        return bridge.faces.value.faces[0].track_id

    async def run() -> None:
        bridge = ReachyMiniBridge(cfg)
        ids: list[int] = []
        async with bridge:
            ids.append(await face_id(bridge))
            frame = bridge.faces.value.frame_id
            await bridge.set_face_detection(False)
            await bridge.set_face_detection(True)
            ids.append(await face_id(bridge))
            await bridge.set_face_detector(_OwnedFaceDetector)
            await _wait_until(
                lambda: (
                    bool(bridge.faces.value.faces)
                    and bridge.faces.value.faces[0].track_id > ids[-1]
                )
            )
            ids.append(await face_id(bridge))
        async with bridge:
            ids.append(await face_id(bridge))
            assert bridge.faces.value.frame_id > frame
        assert ids == sorted(set(ids)) and ids[0] == 1
        async with ReachyMiniBridge(cfg) as independent:
            assert await face_id(independent) == 1

    asyncio.run(run())


def test_overlapping_detection_starts_build_one_detector_and_stop_leaves_none() -> None:
    """set_face_detection(True) and start_head_tracking() scheduled together build one
    detector and run one loop; stop() leaves no loop and closes that one detector
    (specs/vision/user_perception.md "Lifecycle")."""

    async def run() -> None:
        started, release = threading.Event(), threading.Event()
        built: list[_OwnedFaceDetector] = []

        def held_factory() -> _OwnedFaceDetector:
            started.set()
            assert release.wait(5)
            detector = _OwnedFaceDetector()
            built.append(detector)
            return detector

        cfg = ReachyMiniConfig(
            backend="fake",
            face_detection=FaceDetectionSettings(
                detector="custom", face_detector=held_factory
            ),
            motion=MotionSettings(wobbling=False),
        )
        bridge = ReachyMiniBridge(cfg)
        try:
            await bridge.start()
            first = asyncio.create_task(bridge.set_face_detection(True))
            second = asyncio.create_task(bridge.start_head_tracking())
            await _wait_until(started.is_set)
            await asyncio.sleep(0.2)  # the second call has reached the sync by now
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 3)
            assert len(built) == 1
            assert bridge.face_detection and bridge.tracking
            loops = [t for t in asyncio.all_tasks() if t.get_name() == "face-detection"]
            assert len(loops) == 1
            await _wait_until(lambda: bool(bridge.faces.value.faces))
        finally:
            release.set()
            await bridge.stop()
        assert len(built) == 1 and built[0].closed == 1
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "face-detection"]
        assert not bridge.faces.value.active

    asyncio.run(run())


def test_a_stop_during_a_held_tracking_start_leaves_tracking_off() -> None:
    """stop_head_tracking() called while start_head_tracking() builds the detector:
    the last call's value stands as a whole — tracking off, the tracker inactive, no
    attention, the loop stopped — and tracking starts again afterwards
    (specs/core/bridge.md "Cancellation": one mode verb at a time per mode)."""

    async def run() -> None:
        started, release = threading.Event(), threading.Event()

        def held_factory() -> _OwnedFaceDetector:
            started.set()
            assert release.wait(5)
            return _OwnedFaceDetector()

        cfg = ReachyMiniConfig(
            backend="fake",
            face_detection=FaceDetectionSettings(
                detector="custom", face_detector=held_factory
            ),
            motion=MotionSettings(wobbling=False),
        )
        async with ReachyMiniBridge(cfg) as bridge:
            starting = asyncio.create_task(bridge.start_head_tracking())
            await _wait_until(started.is_set)
            stopping = asyncio.create_task(bridge.stop_head_tracking())
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.gather(starting, stopping)
            await _wait_until(lambda: not bridge.faces.value.active)
            assert not bridge.tracking
            assert not bridge.head_tracking.value.active
            assert bridge.attention is None
            assert not bridge.faces.value.active
            await bridge.start_head_tracking()
            assert bridge.tracking and bridge.head_tracking.value.active
            assert bridge.attention == "watching"
            await _wait_until(lambda: bridge.faces.value.active)

    asyncio.run(run())


def _hold_motor_call(
    monkeypatch: pytest.MonkeyPatch, fake: FakeReachyMini, name: str
) -> tuple[threading.Event, threading.Event]:
    """Hold the fake's motor method ``name`` on a release event; returns (entered, release)."""
    entered, release = threading.Event(), threading.Event()
    original = getattr(fake, name)

    def held(*args: object, **kwargs: object) -> None:
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)

    monkeypatch.setattr(fake, name, held)
    return entered, release


@pytest.mark.parametrize("state", ["enabled", "disabled"])
def test_a_cancelled_motor_command_still_moves_the_loop(
    state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set_motors_state cancelled inside the SDK call completes as a whole: the torque
    changes *and* the loop resumes (enabled) or pauses (disabled) — specs/core/bridge.md
    "Cancellation", specs/motion/motion.md "Motors"."""

    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            fake = _fake(bridge)
            if state == "disabled":
                await bridge.set_motors_state("enabled")
                await asyncio.sleep(0.2)
            method = "enable_motors" if state == "enabled" else "disable_motors"
            entered, release = _hold_motor_call(monkeypatch, fake, method)
            pending = asyncio.create_task(bridge.set_motors_state(state))
            await _wait_until(entered.is_set)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 1)
            assert fake.client.motor_control_mode != state  # the call is still held
            release.set()
            await _wait_until(lambda: fake.client.motor_control_mode == state)
            await asyncio.sleep(0.2)  # the transition reaches the loop
            before = len(fake.targets)
            await asyncio.sleep(0.3)
            after = len(fake.targets)
            if state == "enabled":
                assert after > before  # the loop resumed with the accepted command
            else:
                assert after == before  # the loop paused with it
            await bridge.say("session stays usable", _ToneSynth())

    asyncio.run(run())


def test_motor_commands_apply_in_order_past_a_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A "disabled" sent right after a cancelled "enabled" waits for it and lands after
    it: the state ends disabled and the loop paused."""

    async def run() -> None:
        async with ReachyMiniBridge("fake") as bridge:
            fake = _fake(bridge)
            entered, release = _hold_motor_call(monkeypatch, fake, "enable_motors")
            pending = asyncio.create_task(bridge.set_motors_state("enabled"))
            await _wait_until(entered.is_set)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            follow = asyncio.create_task(bridge.set_motors_state("disabled"))
            await asyncio.sleep(0.2)
            assert not follow.done()  # queued behind the held command
            release.set()
            await asyncio.wait_for(follow, 2)
            names = _command_names(bridge)
            assert names.index("disable_motors") > names.index("enable_motors")
            assert await bridge.get_motors_state() == "disabled"
            await asyncio.sleep(0.2)
            count = len(fake.targets)
            await asyncio.sleep(0.3)
            assert len(fake.targets) == count  # paused

    asyncio.run(run())


def test_teardown_drains_a_held_motor_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop() waits for an accepted motor command before the motion session stops."""

    async def run() -> list[str]:
        bridge = ReachyMiniBridge("fake")
        await bridge.start()
        fake = _fake(bridge)
        entered, release = _hold_motor_call(monkeypatch, fake, "enable_motors")
        pending = asyncio.create_task(bridge.set_motors_state("enabled"))
        await _wait_until(entered.is_set)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        stopping = asyncio.create_task(bridge.stop())
        await asyncio.sleep(0.3)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, 5)
        assert not bridge.running
        return [name for name, _args in fake.commands]

    names = asyncio.run(run())
    assert "enable_motors" in names


def _wobbling_calls(names: list[str]) -> list[str]:
    return [n for n in names if n in ("enable_wobbling", "disable_wobbling")]


@pytest.mark.parametrize("wobbling_at_entry", [True, False])
def test_set_wobbling_on_during_an_emotion_takes_effect_at_its_end(
    wobbling_at_entry: bool,
) -> None:
    """A set_wobbling(True) during the emotion's pause is recorded and sent only once
    the emotion ends (specs/motion/motion.md "Emotions through the loop") — whether the
    session entered with wobbling on (paused for the move) or off (never enabled)."""

    async def run() -> tuple[list[str], list[str], bool]:
        cfg = ReachyMiniConfig(
            backend="fake", motion=MotionSettings(wobbling=wobbling_at_entry)
        )
        async with ReachyMiniBridge(cfg) as bridge:
            await bridge.set_motors_state("enabled")
            task = asyncio.create_task(bridge.play_emotion("happy"))
            await _wait_until(lambda: "media.play_sound" in _command_names(bridge))
            await bridge.set_wobbling(True)  # lands while the emotion plays
            during = _command_names(bridge)
            assert not task.done()
            await task
            return during, _command_names(bridge), bridge.wobbling

    during, names, wobbling = asyncio.run(run())
    sound_i = during.index("media.play_sound")
    assert "enable_wobbling" not in during[sound_i:]  # nothing while the move plays
    assert _wobbling_calls(names)[-1] == "enable_wobbling"  # the release sends it
    assert wobbling is True


def test_set_wobbling_off_during_an_emotion_stays_off_after_it() -> None:
    async def run() -> tuple[list[str], bool]:
        async with ReachyMiniBridge("fake") as bridge:  # wobbling on by default
            await bridge.set_motors_state("enabled")
            task = asyncio.create_task(bridge.play_emotion("happy"))
            await _wait_until(lambda: "media.play_sound" in _command_names(bridge))
            await bridge.set_wobbling(False)
            await task
            return _command_names(bridge), bridge.wobbling

    names, wobbling = asyncio.run(run())
    sound_i = names.index("media.play_sound")
    assert "enable_wobbling" not in names[sound_i:]
    assert wobbling is False


# --- teardown and mode transitions owned past a cancel (specs/core/bridge.md "Lifecycle",
# "Cancellation"; specs/vision/user_perception.md "Lifecycle") ---------------------------


def test_a_cancelled_stop_joins_the_motion_thread_before_the_connection_closes() -> (
    None
):
    """`stop()` is owned once begun: a cancel during the exit blend is absorbed until
    every step has run — the blend played to neutral and the thread joined before the
    connection exits, so no target follows it — and propagates then; the bridge starts
    again afterwards."""

    async def run() -> tuple[list[str], int, int, bool]:
        cfg = ReachyMiniConfig(backend="fake", motion=MotionSettings(wobbling=False))
        bridge = ReachyMiniBridge(cfg)
        await bridge.start()
        robot = _fake(bridge)
        await bridge.set_motors_state("enabled")
        await _wait_until(lambda: len(robot.targets) > 5)  # commanding the idle move
        stop = asyncio.create_task(bridge.stop())
        await asyncio.sleep(0.1)  # inside the BLEND_S exit blend
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
        names = [name for name, _ in robot.commands]
        sent_at_return = len(robot.targets)
        last_head = robot.targets[-1][0]
        assert last_head is not None
        assert np.allclose(last_head, NEUTRAL_HEAD, atol=1e-6)  # the blend ran out
        await asyncio.sleep(0.2)
        sent_after = len(robot.targets)
        await bridge.start()
        restarted = bridge.running
        await bridge.stop()
        return names, sent_at_return, sent_after, restarted

    names, sent_at_return, sent_after, restarted = asyncio.run(run())
    assert "__exit__" in names
    assert sent_after == sent_at_return  # nothing sent once stop() has returned
    assert restarted


def test_an_emotion_failing_at_its_entry_raises_and_restores_wobbling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """specs/motion/motion.md "Lifecycle": a move whose start pose cannot be evaluated
    fails the `play_emotion` that queued it — promptly, wobbling restored — and the
    session plays the next emotion."""

    class _BadMove(bridge_module._FakeRecordedMove):
        def evaluate(
            self, t: float
        ) -> tuple[
            npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
        ]:
            raise RuntimeError("bad pose")

    real_get = bridge_module._FakeRecordedMoves.get

    def get(self: Any, name: str) -> Any:
        return _BadMove(name) if name == "sad" else real_get(self, name)

    monkeypatch.setattr(bridge_module._FakeRecordedMoves, "get", get)

    async def run() -> tuple[float, list[str]]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            t0 = time.monotonic()
            with pytest.raises(RuntimeError, match="bad pose"):
                await asyncio.wait_for(bridge.play_emotion("sad"), 1.0)
            elapsed = time.monotonic() - t0
            names = _command_names(bridge)
            await bridge.play_emotion("happy")  # the session plays on
            return elapsed, names

    elapsed, names = asyncio.run(run())
    assert elapsed < 0.5
    paused = names.index("disable_wobbling")
    assert "enable_wobbling" in names[paused + 1 :]  # restored on the failure path


def test_a_failed_enable_does_not_erase_a_later_enable() -> None:
    """specs/core/bridge.md "Cancellation": one detection transition at a time, the last
    call's value standing — the first enable's build fails after the second was called,
    and its rollback restores its own change only: the second builds and runs."""

    async def run() -> tuple[type[BaseException] | None, bool, float | None, int]:
        started, release = threading.Event(), threading.Event()
        builds = 0

        def factory() -> _OwnedFaceDetector:
            nonlocal builds
            builds += 1
            if builds == 1:
                started.set()
                assert release.wait(5)
                raise OSError("no network")
            return _OwnedFaceDetector(0.3)

        cfg = ReachyMiniConfig(
            backend="fake",
            face_detection=FaceDetectionSettings(
                detector="custom", face_detector=factory
            ),
            motion=MotionSettings(wobbling=False),
        )
        async with ReachyMiniBridge(cfg) as bridge:
            first = asyncio.create_task(bridge.set_face_detection(True))
            await _wait_until(started.is_set)
            second = asyncio.create_task(bridge.set_face_detection(True))
            await asyncio.sleep(0.05)  # queued behind the first
            release.set()
            first_error = None
            try:
                await first
            except BridgeError as e:
                first_error = type(e)
            await second
            await _wait_until(lambda: bool(bridge.faces.value.faces))
            x = bridge.faces.value.faces[0].x
            return first_error, bridge.face_detection, x, builds

    first_error, switch, x, builds = asyncio.run(run())
    assert first_error is BridgeError
    assert switch is True
    assert x == pytest.approx(0.3, abs=0.03)
    assert builds == 2


def test_a_cancelled_disable_completes_and_leaves_the_switch_off() -> None:
    """A disable, once begun, is owned: the cancel arriving while the detector's release
    waits on a `detect` in flight is absorbed until the release is done — the switch off,
    the loop stopped, the detector closed — and propagates then; detection then turns on
    again on a fresh detector."""

    class _HeldDetector:
        def __init__(self, gate: threading.Event, x: float) -> None:
            self.gate, self.x, self.closed, self.calls = gate, x, 0, 0

        def detect(
            self, frame_bgr: npt.NDArray[np.uint8], ts: float
        ) -> list[PixelFace]:
            self.calls += 1
            assert self.gate.wait(5)
            return [_pixel_face(self.x)]

        def close(self) -> None:
            self.closed += 1

    async def run() -> tuple[bool, bool, int, bool, float | None]:
        gate = threading.Event()
        gate.set()
        detectors: list[_HeldDetector] = []

        def factory() -> _HeldDetector:
            detector = _HeldDetector(gate, 0.1 * (len(detectors) + 1))
            detectors.append(detector)
            return detector

        cfg = ReachyMiniConfig(
            backend="fake",
            face_detection=FaceDetectionSettings(
                detector="custom", face_detector=factory
            ),
            motion=MotionSettings(wobbling=False),
        )
        async with ReachyMiniBridge(cfg) as bridge:
            await bridge.set_face_detection(True)
            await _wait_until(lambda: bool(bridge.faces.value.faces))
            gate.clear()
            held = detectors[0]
            calls = held.calls
            await _wait_until(lambda: held.calls > calls)  # a detect now blocks
            off = asyncio.create_task(bridge.set_face_detection(False))
            await asyncio.sleep(0.2)  # the release waits for that detect to return
            off.cancel()
            gate.set()
            cancelled = False
            try:
                await off
            except asyncio.CancelledError:
                cancelled = True
            switch, active, closed = (
                bridge.face_detection,
                bridge.faces.value.active,
                held.closed,
            )
            await bridge.set_face_detection(True)
            await _wait_until(lambda: bool(bridge.faces.value.faces))
            x = bridge.faces.value.faces[0].x
            return cancelled, switch, closed, active, x

    cancelled, switch, closed, active, x = asyncio.run(run())
    assert cancelled  # the cancel is neither swallowed nor served before the release
    assert switch is False and active is False
    assert closed == 1
    assert x == pytest.approx(0.2, abs=0.03)  # a fresh detector on the next enable
