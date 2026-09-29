"""Functional tests for ReachyMiniApi on the fake backend (specs/core/api.md).

Drives the public api the way a caller would and asserts through the escape hatch
(`api.robot`, the FakeReachyMini) and its recorded commands. No network, no daemon,
no hardware. Async runs via `asyncio.run` (fast-tier convention).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import api as api_module
from reachy_mini_bridge import face_detection as face_detection_module
from reachy_mini_bridge import head_tracking as head_tracking_module
from reachy_mini_bridge import robot as robot_module
from reachy_mini_bridge.api import ReachyMiniApi
from reachy_mini_bridge.config import (
    DaemonConfig,
    FaceSettings,
    MotionSettings,
    ReachyMiniConfig,
)
from reachy_mini_bridge.errors import (
    BridgeError,
    ConfigError,
    GravityCompensationUnsupportedError,
    MotorsNotEnabledError,
)
from reachy_mini_bridge.face_detection import FaceReport, PixelFace
from reachy_mini_bridge.fake_reachy_mini import FAKE_FRAME_HZ, FakeReachyMini
from reachy_mini_bridge.motion import (
    ANTENNA_MIN_RAD,
    ANTENNA_OUTWARD,
    BLEND_S,
    BREATH_Z_M,
    NEUTRAL_ANTENNAS,
    HoldMove,
    IdleMove,
    IdleOffsets,
)


class _ToneSynth:
    sample_rate = 16000

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        yield np.full(400, 0.2, dtype=np.float32)


def _fake(api: ReachyMiniApi) -> FakeReachyMini:
    robot = api.robot
    assert isinstance(robot, FakeReachyMini)
    return robot


def _command_names(api: ReachyMiniApi) -> list[str]:
    return [name for name, _ in _fake(api).commands]


def _yaw_deg(head: npt.NDArray[np.float64]) -> float:
    return float(np.degrees(np.arctan2(head[1, 0], head[0, 0])))


def _head_z(api: ReachyMiniApi) -> list[float]:
    return [float(h[2, 3]) for h, _, _ in _fake(api).targets if h is not None]


# --- construction / escape hatch ---------------------------------------------------


def test_robot_escape_hatch_is_the_fake() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            assert isinstance(api.robot, FakeReachyMini)
            assert api.raw is api.robot

    asyncio.run(run())


def test_string_shorthand_builds_a_config() -> None:
    api = ReachyMiniApi("fake")
    assert api.config == ReachyMiniConfig(backend="fake")
    assert ReachyMiniApi().config.backend == "real"


def test_from_dict_from_json_from_json_file(tmp_path: Path) -> None:
    data = {"backend": "fake"}
    text = json.dumps(data)
    path = tmp_path / "robot.json"
    path.write_text(text)
    synth = _ToneSynth()
    for api in (
        ReachyMiniApi.from_dict(data, synthesizer=synth),
        ReachyMiniApi.from_json(text, synthesizer=synth),
        ReachyMiniApi.from_json_file(path, synthesizer=synth),
    ):
        assert api.config.backend == "fake"
        assert api._synthesizer is synth
    with pytest.raises(ConfigError):
        ReachyMiniApi.from_json("{not json")


def test_robot_requires_entry() -> None:
    api = ReachyMiniApi("fake")
    with pytest.raises(BridgeError):
        _ = api.robot
    with pytest.raises(BridgeError):
        _ = api.raw
    with pytest.raises(BridgeError):
        _ = api.mic_sample_rate

    async def run() -> None:
        async with api:
            assert isinstance(api.robot, FakeReachyMini)

    asyncio.run(run())
    with pytest.raises(BridgeError):
        _ = api.robot


def test_double_enter_raises() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            with pytest.raises(BridgeError):
                await api.__aenter__()

    asyncio.run(run())


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
        "IdleMove",
        "IdleOffsets",
        "MotorsNotEnabledError",
        "Observable",
        "PixelFace",
        "ReachyMiniApi",
        "ReachyMiniConfig",
        "SpeechSynthesizer",
        "TTSEngineSynthesizer",
    }
    assert all(hasattr(rmb, name) for name in rmb.__all__)
    assert isinstance(_ToneSynth(), rmb.SpeechSynthesizer)

    # The front-door names are the ones a caller catches.
    async def run() -> None:
        async with rmb.ReachyMiniApi("fake") as api:
            with pytest.raises(rmb.BridgeError):
                await api.say("hi")  # no synthesizer
            with pytest.raises(rmb.MotorsNotEnabledError):
                await api.play_emotion("happy")  # the fake boots disabled
            _fake(api).client.kinematics_engine = "AnalyticalKinematics"
            with pytest.raises(rmb.GravityCompensationUnsupportedError):
                await api.set_motors_state("gravity_compensation")

    asyncio.run(run())
    with pytest.raises(rmb.ConfigError):
        rmb.ReachyMiniConfig.from_json("{not json")


# --- default synthesizer from the config's `tts` block ------------------------------


def test_explicit_synthesizer_wins_over_tts_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_block: object) -> object:
        raise AssertionError("tts block must not be consumed")

    monkeypatch.setattr(api_module, "TTSEngineSynthesizer", boom)
    config = ReachyMiniConfig(backend="fake", tts={"module": {"type": "x"}})

    async def run() -> int:
        async with ReachyMiniApi(config, synthesizer=_ToneSynth()) as api:
            await api.say("hi")
            return sum(1 for n in _command_names(api) if n == "media.push_audio_sample")

    assert asyncio.run(run()) >= 1


def test_tts_block_builds_the_default_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[object] = []

    class _Adapter(_ToneSynth):
        def __init__(self, block: object) -> None:
            built.append(block)

    monkeypatch.setattr(api_module, "TTSEngineSynthesizer", _Adapter)
    block = {"module": {"type": "x"}}

    async def run() -> int:
        async with ReachyMiniApi(ReachyMiniConfig(backend="fake", tts=block)) as api:
            await api.say("hi")
            return sum(1 for n in _command_names(api) if n == "media.push_audio_sample")

    assert asyncio.run(run()) >= 1
    assert built == [block]


def test_tts_block_for_an_uninstalled_provider_degrades_to_no_voice() -> None:
    """A real block for a provider whose extra is missing: tts-engine's ConfigError
    is recorded, the api comes up, and `say` names the extra to install."""
    from tts_engine.config import ConfigError as TTSEngineConfigError

    api = ReachyMiniApi(
        ReachyMiniConfig(backend="fake", tts={"module": {"type": "no-such-provider"}})
    )
    assert isinstance(api.synthesizer_error, TTSEngineConfigError)

    async def run() -> None:
        async with api:
            with pytest.raises(BridgeError, match="no-such-provider"):
                await api.say("hi")

    asyncio.run(run())


def test_tts_block_build_failure_degrades_to_no_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cause = ValueError("environment variable 'X' is unset")

    def failing(_block: object) -> object:
        raise cause

    monkeypatch.setattr(api_module, "TTSEngineSynthesizer", failing)
    block = {"module": {"type": "x"}}
    api = ReachyMiniApi(ReachyMiniConfig(backend="fake", tts=block))
    assert api.synthesizer_error is cause

    async def run() -> list[str]:
        async with api:
            with pytest.raises(BridgeError, match="X") as exc_info:
                await api.say("hi")
            assert exc_info.value.__cause__ is cause
            return await api.list_emotions()  # the robot is still up

    assert asyncio.run(run()) == ["happy", "sad", "curious"]


def test_synthesizer_error_is_none_when_the_voice_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Adapter(_ToneSynth):
        def __init__(self, block: object) -> None:
            del block

    monkeypatch.setattr(api_module, "TTSEngineSynthesizer", _Adapter)
    api = ReachyMiniApi(ReachyMiniConfig(backend="fake", tts={"module": {"type": "x"}}))
    assert api.synthesizer_error is None


def test_explicit_synthesizer_leaves_no_error() -> None:
    config = ReachyMiniConfig(backend="fake", tts={"module": {"type": "x"}})
    api = ReachyMiniApi(config, synthesizer=_ToneSynth())
    assert api.synthesizer_error is None


# --- lifecycle order: daemon -> robot -> media --------------------------------------


class _Recorder:
    """Records lifecycle events; a scripted daemon context + a build_robot stand-in."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.fake = FakeReachyMini()

    @contextmanager
    def managed_daemon(
        self, config: object, *, host: str, port: int, backend: str
    ) -> Iterator[None]:
        self.events.append(f"daemon-enter {backend} {host}:{port}")
        try:
            yield None
        finally:
            self.events.append("daemon-exit")

    def build_robot(self, backend: str, **opts: object) -> FakeReachyMini:
        self.events.append(f"build {backend} {sorted(opts)}")
        return self.fake


def _install(monkeypatch: pytest.MonkeyPatch, rec: _Recorder) -> None:
    monkeypatch.setattr(api_module._daemon, "managed_daemon", rec.managed_daemon)
    monkeypatch.setattr(api_module, "build_robot", rec.build_robot)


_SPAWNING = ReachyMiniConfig(backend="sim", daemon=DaemonConfig(spawn="auto"))


def test_managed_daemon_enters_before_the_robot_and_exits_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)

    async def run() -> None:
        async with ReachyMiniApi(_SPAWNING) as api:
            assert api.robot is rec.fake
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
        async with ReachyMiniApi(config):
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

    monkeypatch.setattr(api_module, "build_robot", failing)
    api = ReachyMiniApi(_SPAWNING)

    async def run() -> None:
        async with api:
            pass

    with pytest.raises(ConnectionError):
        asyncio.run(run())
    assert rec.events == ["daemon-enter sim 127.0.0.1:8000", "daemon-exit"]
    with pytest.raises(BridgeError):
        _ = api.robot


def test_media_open_failure_exits_the_robot_and_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)

    def broken() -> None:
        raise RuntimeError("no audio")

    monkeypatch.setattr(rec.fake.media, "start_recording", broken)
    api = ReachyMiniApi(_SPAWNING)

    async def run() -> None:
        async with api:
            pass

    with pytest.raises(RuntimeError, match="no audio"):
        asyncio.run(run())
    # The robot was entered (the fake records only its exit) and unwound; media never
    # got past the failing start, so nothing else was recorded.
    assert [n for n, _ in rec.fake.commands] == ["__exit__"]
    assert rec.events[-1] == "daemon-exit"
    with pytest.raises(BridgeError):
        _ = api.robot


def test_exit_tears_down_everything_even_if_media_teardown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)

    def broken() -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(rec.fake.media, "stop_playing", broken)
    api = ReachyMiniApi(_SPAWNING)

    async def run() -> None:
        async with api:
            pass

    with pytest.raises(RuntimeError, match="stop failed"):
        asyncio.run(run())
    names = [n for n, _ in rec.fake.commands]
    assert "media.stop_recording" in names and names[-1] == "__exit__"
    assert rec.events[-1] == "daemon-exit"
    with pytest.raises(BridgeError):
        _ = api.robot


def test_audio_verbs_require_an_open_api() -> None:
    api = ReachyMiniApi("fake")

    async def run() -> None:
        with pytest.raises(BridgeError):
            await api.say("hi", _ToneSynth())
        async with api:
            pass
        with pytest.raises(BridgeError):
            await api.say("hi", _ToneSynth())

    asyncio.run(run())
    # Raised at call time, not at the first `async for`.
    with pytest.raises(BridgeError):
        api.audio_input()


# --- motors ------------------------------------------------------------------------


def test_motor_state_round_trips_through_the_daemon() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            seen = [await api.get_motors_state()]
            await api.set_motors_state("enabled")
            seen.append(await api.get_motors_state())
            await api.set_motors_state("gravity_compensation")
            seen.append(await api.get_motors_state())
            await api.set_motors_state("disabled")
            seen.append(await api.get_motors_state())
            return seen

    assert asyncio.run(run()) == [
        "disabled",
        "enabled",
        "gravity_compensation",
        "disabled",
    ]


def test_set_motors_state_dispatches_to_matching_primitive() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.set_motors_state("gravity_compensation")
            await api.set_motors_state("disabled")
            return _command_names(api)

    names = asyncio.run(run())
    assert "enable_motors" in names
    assert "enable_gravity_compensation" in names
    assert "disable_motors" in names


def test_set_motors_state_rejects_unknown_state() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            with pytest.raises(ValueError, match="unknown motor state"):
                await api.set_motors_state("asleep")

    asyncio.run(run())


def test_motors_disabled_pauses_the_loop_and_enabled_resumes_anchored() -> None:
    async def run() -> tuple[int, int, list[float]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(0.3)
            await api.set_motors_state("disabled")
            await asyncio.sleep(0.2)
            count_after_disable = len(_fake(api).targets)
            await asyncio.sleep(0.2)
            count_still = len(_fake(api).targets)  # nothing sent while paused
            head = np.eye(4)
            head[2, 3] = 0.05
            api.robot.set_target(head=head)  # a caller driving the head directly
            marker = len(_fake(api).targets)
            await api.set_motors_state("enabled")
            await asyncio.sleep(0.1)
            return count_after_disable, count_still, _head_z(api)[marker:]

    count_after_disable, count_still, resumed_zs = asyncio.run(run())
    assert count_still == count_after_disable
    assert resumed_zs
    assert resumed_zs[0] == pytest.approx(0.05, abs=0.01)


def test_gravity_compensation_is_refused_off_placo_without_sending() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            _fake(api).client.kinematics_engine = "AnalyticalKinematics"
            with pytest.raises(GravityCompensationUnsupportedError) as excinfo:
                await api.set_motors_state("gravity_compensation")
            message = str(excinfo.value)
            assert "AnalyticalKinematics" in message
            assert "--kinematics-engine Placo" in message
            assert "enable_gravity_compensation" not in _command_names(api)
            assert await api.get_motors_state() == "enabled"  # untouched

    asyncio.run(run())


def test_gravity_compensation_is_refused_when_the_engine_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(robot: object) -> str:
        raise OSError("connection refused")

    monkeypatch.setattr(api_module, "_daemon_kinematics_engine", unreachable)

    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            with pytest.raises(GravityCompensationUnsupportedError) as excinfo:
                await api.set_motors_state("gravity_compensation")
            assert isinstance(excinfo.value.__cause__, OSError)
            assert "enable_gravity_compensation" not in _command_names(api)

    asyncio.run(run())


def test_gravity_compensation_is_sent_unchecked_to_a_simulation() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            client = _fake(api).client
            client.kinematics_engine = "AnalyticalKinematics"
            for flag in ("simulation_enabled", "mockup_sim_enabled"):
                client.simulation_enabled = flag == "simulation_enabled"
                client.mockup_sim_enabled = flag == "mockup_sim_enabled"
                await api.set_motors_state("gravity_compensation")
            return _command_names(api)

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
    assert api_module._daemon_kinematics_engine(robot) == "Placo"  # pyright: ignore[reportArgumentType]
    assert fetched == ["/api/kinematics/info"]


# --- motor precondition on movement verbs ------------------------------------------


def test_play_emotion_requires_motors_enabled() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("disabled")
            with pytest.raises(MotorsNotEnabledError):
                await api.play_emotion("happy")
            # nothing was sent downstream
            assert "async_play_move" not in _command_names(api)

    asyncio.run(run())


def test_start_head_tracking_needs_no_motors() -> None:
    async def run() -> tuple[bool, bool, list[str]]:
        async with ReachyMiniApi(_custom_config(_Scene([]), tracking=False)) as api:
            await api.set_motors_state("gravity_compensation")
            before = len(_fake(api).commands)
            await api.start_head_tracking()
            await asyncio.sleep(0.15)
            return api.tracking, api.faces.value.active, _command_names(api)[before:]

    tracking, detecting, sent = asyncio.run(run())
    assert tracking is True
    assert detecting is True  # tracking started the detection loop
    assert (
        sent == []
    )  # nothing to the robot: the tracker steers through the motion loop


def test_movement_verbs_run_once_motors_enabled() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.play_emotion("happy")
            return _command_names(api)

    names = asyncio.run(run())
    assert "media.play_sound" in names  # the move played through the motion loop


def test_focus_holds_the_head_on_the_face_without_the_breath(
    fast_attention: None,
) -> None:
    async def run(focus: bool) -> tuple[bool, float]:
        scene = _Scene([_pixel_face(0.1, 0.0)])
        async with ReachyMiniApi(_custom_config(scene)) as api:  # breathing by default
            await api.set_motors_state("enabled")
            await api.start_head_tracking(focus=focus)
            await asyncio.sleep(BLEND_S + 0.5)
            marker = len(_fake(api).targets)
            await asyncio.sleep(2.0)  # the first breath rises over these seconds
            zs = _head_z(api)[marker:]
            return api.tracking_focus, max(zs) - min(zs)

    focused, z_range = asyncio.run(run(True))
    assert focused is True
    assert z_range < 1e-4  # the head holds on the face
    composed, z_range = asyncio.run(run(False))
    assert composed is False
    assert z_range > 0.002  # it breathes around the face


def test_list_emotions_returns_the_offline_library() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            return await api.list_emotions()

    assert asyncio.run(run()) == ["happy", "sad", "curious"]


def test_play_emotion_resolves_name_and_plays_it() -> None:
    async def run() -> list[tuple[str, dict[str, Any]]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.play_emotion("curious")
            return list(_fake(api).commands)

    commands = asyncio.run(run())
    assert ("media.play_sound", {"sound_file": "curious.ogg"}) in commands


def test_play_emotion_unknown_name_raises() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            with pytest.raises(ValueError, match="not found"):
                await api.play_emotion("nonexistent")

    asyncio.run(run())


# --- audio ------------------------------------------------------------------------


def test_play_emotion_on_the_fake_takes_the_moves_duration() -> None:
    async def run() -> float:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            t0 = time.monotonic()
            await api.play_emotion("curious")
            return time.monotonic() - t0

    # The entry blend (BLEND_S) precedes the move's own trajectory.
    assert asyncio.run(run()) >= BLEND_S + 0.25


async def _cancel_emotion_mid_flight(name: str) -> tuple[float, list[str]]:
    async with ReachyMiniApi("fake", synthesizer=_ToneSynth()) as api:
        await api.set_motors_state("enabled")
        task = asyncio.create_task(api.play_emotion(name))
        if name == "sad":  # soundless: nothing to wait on but the entry blend
            await asyncio.sleep(BLEND_S + 0.1)
        else:
            while "media.play_sound" not in _command_names(api):
                await asyncio.sleep(0)
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - t0
        await api.say("still here")  # the session is usable right after
        return elapsed, _command_names(api)


def test_cancelled_play_emotion_stops_the_sound_and_keeps_the_session() -> None:
    elapsed, names = asyncio.run(_cancel_emotion_mid_flight("happy"))

    assert elapsed < 0.05
    i = names.index("media.play_sound")
    assert names[i + 1 : i + 3] == ["media.stop_sound", "audio.clear_player"]
    assert "media.push_audio_sample" in names[i + 3 :]


def test_cancelled_soundless_emotion_does_not_stop_a_sound() -> None:
    _elapsed, names = asyncio.run(_cancel_emotion_mid_flight("sad"))

    assert "media.stop_sound" not in names
    assert "media.push_audio_sample" in names


def test_play_emotion_failure_stops_the_sound_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomMove(api_module._FakeRecordedMove):
        def evaluate(
            self, t: float
        ) -> tuple[
            npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
        ]:
            if t > 0.05:
                raise RuntimeError("boom")
            return super().evaluate(t)

    def get(
        self: api_module._FakeRecordedMoves, move_name: str
    ) -> api_module._FakeRecordedMove:
        return _BoomMove(move_name, sound_path=Path(f"{move_name}.ogg"))

    monkeypatch.setattr(api_module._FakeRecordedMoves, "get", get)

    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            with pytest.raises(RuntimeError, match="boom"):
                await api.play_emotion("happy")
            return _command_names(api)

    names = asyncio.run(run())
    assert "media.play_sound" in names
    assert names.index("media.stop_sound") > names.index("media.play_sound")


def test_completed_play_emotion_does_not_stop_the_sound() -> None:
    # A completed move's sound plays to its natural end.
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.play_emotion("happy")
            return _command_names(api)

    assert "media.stop_sound" not in asyncio.run(run())


def test_play_emotion_under_a_tracked_face_plays_as_recorded(
    fast_attention: None,
) -> None:
    config = _custom_config(_Scene([_pixel_face(0.5, 0.0)]), idle="hold")

    async def run() -> tuple[str | None, list[tuple[float, float]], list[str]]:
        async with ReachyMiniApi(config) as api:
            await api.set_motors_state("enabled")
            robot = _fake(api)
            await asyncio.sleep(1.0)  # the head has turned toward the face
            engaged = api.attention
            before = len(robot.commands)
            marker = len(robot.targets)
            await api.play_emotion("sad")
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
    move = api_module._FakeRecordedMove("sad")
    played = poses[-int(0.2 * 60) :]
    assert all(abs(yaw) < 1e-6 for yaw, _ in played)
    assert max(z for _, z in played) <= 0.01 + 1e-9
    start = next(i for i, (yaw, _) in enumerate(poses) if abs(yaw) < 1e-6)
    head, _a, _y = move.evaluate(0.0)
    assert head is not None
    assert poses[start][1] == pytest.approx(float(head[2, 3]), abs=0.002)


def test_play_emotion_pauses_wobbling_and_restores_it() -> None:
    async def run() -> tuple[list[str], bool]:
        async with ReachyMiniApi("fake") as api:  # wobbling on by default
            await api.set_motors_state("enabled")
            await api.play_emotion("happy")
            return _command_names(api), api.wobbling

    names, wobbling = asyncio.run(run())
    sound_i = names.index("media.play_sound")
    assert "disable_wobbling" in names[:sound_i]
    assert "enable_wobbling" in names[sound_i:]
    assert wobbling is True


def test_play_emotion_restores_layers_after_a_cancel() -> None:
    async def run() -> list[tuple[str, dict[str, Any]]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            task = asyncio.create_task(api.play_emotion("happy"))
            while "media.play_sound" not in _command_names(api):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return list(_fake(api).commands)

    commands = asyncio.run(run())
    wobbling_calls = [
        n for n, _ in commands if n in ("enable_wobbling", "disable_wobbling")
    ]
    assert wobbling_calls[-1] == "enable_wobbling"


def test_play_emotion_restores_to_the_current_record() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            task = asyncio.create_task(api.play_emotion("sad"))
            await asyncio.sleep(0.1)  # wobbling paused for the move
            await api.set_wobbling(False)  # the caller turns it off meanwhile
            await task
            return _command_names(api)

    names = asyncio.run(run())
    wobbling_calls = [n for n in names if n in ("enable_wobbling", "disable_wobbling")]
    # no restore of the wobbling the caller turned off
    assert wobbling_calls[-1] == "disable_wobbling"


def test_cancelled_library_load_is_reused_by_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads = 0
    started, release = threading.Event(), threading.Event()

    def slow_load(self: ReachyMiniApi) -> Any:
        nonlocal loads
        loads += 1
        started.set()
        release.wait()
        return api_module._FakeRecordedMoves()

    monkeypatch.setattr(ReachyMiniApi, "_load_recorded_moves", slow_load)

    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            task = asyncio.create_task(api.list_emotions())
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            return await api.list_emotions()

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
    monkeypatch.setattr(api_module, "build_robot", lambda backend, **kw: robot)
    api = ReachyMiniApi("fake")

    async def run() -> None:
        task = asyncio.create_task(api.__aenter__())
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
        _ = api.robot


def test_say_routes_through_the_media_pipeline() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake", synthesizer=_ToneSynth()) as api:
            await api.say("hello")
            return _command_names(api)

    names = asyncio.run(run())
    assert "media.push_audio_sample" in names


def test_say_accepts_a_per_call_synthesizer() -> None:
    async def run() -> int:
        async with ReachyMiniApi("fake") as api:  # no default synth configured
            await api.say("hello", _ToneSynth())
            return sum(1 for n in _command_names(api) if n == "media.push_audio_sample")

    assert asyncio.run(run()) >= 1


def test_say_without_any_synthesizer_raises_bridge_error() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            with pytest.raises(BridgeError, match="SpeechSynthesizer"):
                await api.say("hello")

    asyncio.run(run())


def test_play_sound_reaches_the_media_layer() -> None:
    async def run() -> dict[str, object]:
        async with ReachyMiniApi("fake") as api:
            await api.play_sound("wake_up.wav")
            return next(a for n, a in _fake(api).commands if n == "media.play_sound")

    assert asyncio.run(run())["sound_file"] == "wake_up.wav"


def test_audio_input_streams_mic_bytes_and_exposes_format() -> None:
    async def run() -> tuple[int, int, bytes]:
        async with ReachyMiniApi("fake") as api:
            chunk = b""
            async for c in api.audio_input():
                chunk = c
                break
            return api.mic_sample_rate, api.mic_channels, chunk

    sr, ch, chunk = asyncio.run(run())
    assert sr == 16000
    assert ch == 2
    assert len(chunk) > 0 and len(chunk) % 2 == 0  # whole int16 samples


# --- audio-reactive motion (head wobbling) ----------------------------------------


def test_set_wobbling_dispatches_and_tracks_state() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            assert api.wobbling is True  # on by default
            await api.set_wobbling(False)
            assert api.wobbling is False
            assert _command_names(api)[-1] == "disable_wobbling"
            await api.set_wobbling(True)
            assert api.wobbling is True
            assert _command_names(api)[-1] == "enable_wobbling"

    asyncio.run(run())


def test_set_wobbling_needs_no_motors() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(wobbling=False))

    async def run() -> None:
        async with ReachyMiniApi(config) as api:  # the fake boots with motors disabled
            await api.set_wobbling(True)
            assert api.wobbling is True
            with pytest.raises(MotorsNotEnabledError):
                await api.play_emotion("happy")

    asyncio.run(run())


def test_wobbling_is_on_by_default_at_entry_and_off_at_exit() -> None:
    api = ReachyMiniApi("fake")

    async def run() -> FakeReachyMini:
        async with api:
            assert api.wobbling is True
            return _fake(api)

    robot = asyncio.run(run())
    names = [name for name, _ in robot.commands]
    assert (
        names.index("media.start_playing")
        < names.index("enable_wobbling")
        < names.index("disable_wobbling")
        < names.index("media.stop_recording")
        < names.index("__exit__")
    )
    assert api.wobbling is False


def test_wobbling_off_in_the_config_is_never_touched() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(wobbling=False))

    async def run() -> FakeReachyMini:
        async with ReachyMiniApi(config, synthesizer=_ToneSynth()) as api:
            await api.say("hello")
            return _fake(api)

    names = [name for name, _ in asyncio.run(run()).commands]
    assert "enable_wobbling" not in names
    assert "disable_wobbling" not in names


def test_wobbling_enabled_at_runtime_is_disabled_at_exit() -> None:
    api = ReachyMiniApi(
        ReachyMiniConfig(backend="fake", motion=MotionSettings(wobbling=False))
    )

    async def run() -> FakeReachyMini:
        async with api:
            await api.set_wobbling(True)
            return _fake(api)

    names = [name for name, _ in asyncio.run(run()).commands]
    assert names.index("disable_wobbling") < names.index("__exit__")
    assert api.wobbling is False


def test_wobbling_turned_off_at_runtime_is_not_disabled_again_at_exit() -> None:
    async def run() -> FakeReachyMini:
        async with ReachyMiniApi("fake") as api:  # on by default
            await api.set_wobbling(False)
            return _fake(api)

    names = [name for name, _ in asyncio.run(run()).commands]
    assert names.count("disable_wobbling") == 1


def test_failing_wobbling_enable_unwinds_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(self: FakeReachyMini) -> None:
        raise RuntimeError("no wobbler")

    monkeypatch.setattr(FakeReachyMini, "enable_wobbling", boom)
    api = ReachyMiniApi("fake")  # wobbling on by default
    robots: list[FakeReachyMini] = []
    original_enter = FakeReachyMini.__enter__

    def spy_enter(self: FakeReachyMini) -> FakeReachyMini:
        robots.append(self)
        return original_enter(self)

    monkeypatch.setattr(FakeReachyMini, "__enter__", spy_enter)

    async def run() -> None:
        async with api:
            pass

    with pytest.raises(RuntimeError, match="no wobbler"):
        asyncio.run(run())
    names = [name for name, _ in robots[0].commands]
    assert "disable_wobbling" not in names  # the mode never came on
    assert "media.stop_playing" in names and names[-1] == "__exit__"
    assert api.wobbling is False
    with pytest.raises(BridgeError):
        _ = api.robot


def test_wobbling_property_is_false_outside_a_session() -> None:
    assert ReachyMiniApi("fake").wobbling is False


def test_set_wobbling_requires_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniApi("fake").set_wobbling(True))


# --- attention / gaze: opt-in, needing a detector (specs/core/api.md, specs/core/config.md) ----


def test_tracking_property_reads_the_config() -> None:
    assert ReachyMiniApi("fake").tracking is False  # off by default: no detector
    assert ReachyMiniApi(_custom_config(_Scene([]))).tracking is True
    assert ReachyMiniApi(_custom_config(_Scene([]), tracking=False)).tracking is False


def test_the_default_config_runs_no_detector_and_the_switches_refuse() -> None:
    """specs/vision/user_perception.md "Detectors": with `faces.detector` null nothing is
    detected and nothing tracks; the switches raise, and the session works otherwise."""
    api = ReachyMiniApi("fake")

    async def run() -> tuple[FaceReport, str | None, bool, bool, list[str]]:
        async with api:
            await asyncio.sleep(0.2)
            report = api.faces.value
            with pytest.raises(ValueError, match=r"faces\.detector is null"):
                await api.start_head_tracking()
            with pytest.raises(ValueError, match=r"faces\.detector is null"):
                await api.set_face_detection(True)
            await api.set_face_detection(False)  # off is fine
            await api.stop_head_tracking()
            await api.set_motors_state("enabled")
            return (
                report,
                api.attention,
                api.tracking,
                api.face_detection,
                _command_names(api),
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

    tracking = ReachyMiniApi(
        ReachyMiniConfig(backend="fake", motion=MotionSettings(tracking=True))
    )
    detection = ReachyMiniApi(
        ReachyMiniConfig(backend="fake", faces=FaceSettings(detection=True))
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(api_module, "build_robot", build)
        for api in (tracking, detection):
            with pytest.raises(ValueError, match=r"faces\.detector is null"):
                asyncio.run(api.__aenter__())
    assert robots == []


class _FlakyFactory:
    """Passes the registration check (its first build succeeds) and fails every build
    after it — a model that could not be loaded when the loop starts."""

    def __init__(self, scene: _Scene) -> None:
        self._scene = scene
        self.builds = 0

    def __call__(self) -> _StubDetector:
        self.builds += 1
        if self.builds > 1:
            raise OSError("no network: the model could not be downloaded")
        return _StubDetector(self._scene)


def test_a_detector_that_cannot_be_built_fails_bring_up_and_unwinds() -> None:
    factory = _FlakyFactory(_Scene([]))
    config = ReachyMiniConfig(
        backend="fake",
        faces=FaceSettings(detector="custom", detection=True, face_detector=factory),
    )
    robots: list[Any] = []
    real_build = api_module.build_robot

    def build(backend: str, **kw: object) -> Any:
        robot = real_build(backend, **kw)
        robots.append(robot)
        return robot

    api = ReachyMiniApi(config)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(api_module, "build_robot", build)
        with pytest.raises(BridgeError, match="could not be built.*no network") as info:
            asyncio.run(api.__aenter__())
    assert isinstance(info.value.__cause__, OSError)
    (robot,) = robots
    assert robot.commands[-1][0] == "__exit__"  # everything started was unwound
    with pytest.raises(BridgeError):
        _ = api.robot
    assert api.faces.value == FaceReport.inactive("custom")


def test_a_detector_that_cannot_be_built_leaves_a_switch_as_it_was() -> None:
    scene = _Scene([])
    config = _custom_config(scene, detection=False, tracking=False)

    async def run() -> tuple[bool, bool, bool]:
        async with ReachyMiniApi(config) as api:
            await api.set_face_detector(_FlakyFactory(scene))  # the check builds once
            with pytest.raises(BridgeError, match="could not be built"):
                await api.set_face_detection(True)
            detection = api.face_detection
            with pytest.raises(BridgeError, match="could not be built"):
                await api.start_head_tracking(focus=True)
            await api.set_motors_state("enabled")  # the session still works
            return detection, api.tracking, api.tracking_focus

    assert asyncio.run(run()) == (False, False, False)


# --- attention (specs/core/api.md "Attention"), derived from the tracker -----------------


@pytest.fixture
def fast_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(head_tracking_module, "TRACKING_LOST_S", 0.3)
    monkeypatch.setattr(face_detection_module, "FACE_POLL_HZ", 20.0)


def test_attention_follows_the_face(fast_attention: None) -> None:
    scene = _Scene([])

    async def run() -> list[str | None]:
        async with ReachyMiniApi(_custom_config(scene)) as api:  # tracking, no motors
            await asyncio.sleep(0.15)
            seen = [api.attention]
            scene.show()
            await asyncio.sleep(0.5)
            seen.append(api.attention)
            scene.hide()
            await asyncio.sleep(0.3 + 0.3)  # TRACKING_LOST_S, and a few frames
            seen.append(api.attention)
            await api.stop_head_tracking()
            seen.append(api.attention)
            return seen

    assert asyncio.run(run()) == ["watching", "engaged", "watching", None]


def test_attention_is_none_outside_a_session() -> None:
    assert ReachyMiniApi("fake").attention is None


def test_the_head_turns_toward_a_face_and_back_once_it_is_gone(
    fast_attention: None,
) -> None:
    scene = _Scene([_pixel_face(0.5, 0.0)])  # to the robot's right
    config = _custom_config(scene, idle="hold")

    async def run() -> tuple[float, float]:
        async with ReachyMiniApi(config) as api:
            await api.set_motors_state("enabled")
            robot = _fake(api)
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
        async with ReachyMiniApi(config) as api:
            await api.set_motors_state("enabled")
            robot = _fake(api)
            await asyncio.sleep(1.0)
            turned = _yaw_deg(robot.last_target[0])
            await api.stop_head_tracking()
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
        async with ReachyMiniApi(config) as api:
            await api.start_head_tracking()  # motors disabled on the fake
            robot = _fake(api)
            await asyncio.sleep(0.3)
            sent = len(robot.targets)  # the loop is paused: nothing moves
            await api.set_motors_state("enabled")
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
    api = ReachyMiniApi(_custom_config(_Scene([]), tracking=False))
    before = api.faces.value

    async def run() -> bool:
        async with api:
            await asyncio.sleep(0.15)  # the first poll
            return api.faces.value.active

    inside = asyncio.run(run())
    assert before == FaceReport.inactive("custom")
    assert inside is True
    assert api.faces.value == FaceReport.inactive("custom")


def test_a_subscriber_is_told_when_someone_appears_and_leaves(
    fast_faces: None,
) -> None:
    scene = _Scene([])

    async def run() -> list[FaceReport]:
        async with ReachyMiniApi(_custom_config(scene, tracking=False)) as api:
            await asyncio.sleep(0.15)  # active, nobody there
            woken: list[FaceReport] = []

            async def subscriber() -> None:
                async for report in api.faces.changes():
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
        async with ReachyMiniApi(_custom_config(scene)) as api:

            async def subscriber() -> None:
                async for _ in api.faces.changes():
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
            await api.set_motors_state("enabled")
            await api.set_wobbling(False)
            return elapsed, await api.get_motors_state()

    elapsed, motors = asyncio.run(run())
    assert elapsed < 0.05
    assert motors == "enabled"


def test_set_face_detection_off_without_tracking_stops_the_loop(
    fast_faces: None,
) -> None:
    scene = _Scene([])

    async def run() -> tuple[bool, bool, int, bool, int]:
        async with ReachyMiniApi(_custom_config(scene, tracking=False)) as api:
            await asyncio.sleep(0.15)
            await api.set_face_detection(False)
            off = (api.face_detection, api.faces.value.active)
            calls = scene.calls
            await asyncio.sleep(0.3)
            idle_calls = scene.calls - calls  # the detector no longer runs
            await api.set_face_detection(True)
            await asyncio.sleep(0.2)
            return *off, idle_calls, api.faces.value.active, scene.calls - calls

    switch, active, idle_calls, active_again, calls_again = asyncio.run(run())
    assert (switch, active) == (False, False)
    assert idle_calls == 0
    assert active_again is True and calls_again > 0


def test_set_face_detection_off_with_tracking_on_changes_nothing(
    fast_faces: None,
) -> None:
    async def run() -> tuple[bool, bool, int, int]:
        async with ReachyMiniApi(_custom_config(_Scene([]))) as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(0.15)
            before = len(_fake(api).commands)
            await api.set_face_detection(False)
            await asyncio.sleep(0.15)
            return (
                api.face_detection,
                api.faces.value.active,
                before,
                len(_fake(api).commands),
            )

    switch, active, before, after = asyncio.run(run())
    assert switch is False
    assert active is True  # the tracker still needs the loop
    assert after == before  # nothing sent to the daemon


def test_stopping_tracking_stops_a_loop_nobody_else_wants(fast_faces: None) -> None:
    config = _custom_config(_Scene([]), detection=False)

    async def run() -> tuple[bool, bool]:
        async with ReachyMiniApi(config) as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(0.15)
            running = api.faces.value.active
            await api.stop_head_tracking()
            return running, api.faces.value.active

    assert asyncio.run(run()) == (True, False)


def test_face_detection_reads_and_resets_to_the_config() -> None:
    config = _custom_config(_Scene([]), detection=False, tracking=False)
    api = ReachyMiniApi(config)
    assert api.face_detection is False

    async def run() -> bool:
        async with api:
            await api.set_face_detection(True)
            return api.face_detection

    assert asyncio.run(run()) is True
    assert api.face_detection is False


def test_set_face_detection_requires_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniApi("fake").set_face_detection(True))


def test_a_custom_source_without_a_detector_fails_before_anything_is_entered() -> None:
    """specs/vision/user_perception.md "Custom detectors": `custom` with nothing registered
    (or a bad factory) is refused at the top of bring-up — no daemon, no robot."""
    robots: list[FakeReachyMini] = []

    def build(backend: str, **kw: object) -> FakeReachyMini:
        robots.append(FakeReachyMini())
        return robots[-1]

    api = ReachyMiniApi(
        ReachyMiniConfig(backend="fake", faces=FaceSettings(detector="custom"))
    )
    not_callable: Any = 42
    bad = ReachyMiniApi(
        ReachyMiniConfig(
            backend="fake",
            faces=FaceSettings(detector="custom", face_detector=not_callable),
        )
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(api_module, "build_robot", build)
        with pytest.raises(ValueError, match="no face detector is registered"):
            asyncio.run(api.__aenter__())
        with pytest.raises(ValueError, match="zero-argument callable"):
            asyncio.run(bad.__aenter__())
    assert robots == []  # nothing was built, nothing to unwind
    with pytest.raises(BridgeError):
        _ = api.robot
    assert api.faces.value == FaceReport.inactive("custom")


def test_start_head_tracking_requires_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniApi("fake").start_head_tracking())


# --- presence & breathing (motion loop) ---------------------------------------------


def test_breathing_rises_from_neutral_and_antennas_lean_outward() -> None:
    async def run() -> tuple[list[float], npt.NDArray[np.float64]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 1.0)
            _head, antennas, _yaw = _fake(api).last_target
            return _head_z(api)[-20:], np.asarray(antennas, dtype=np.float64)

    z, antennas = asyncio.run(run())
    # 1 s into the first breath z has risen ~1.7 mm, from ~0.8 mm twenty ticks earlier
    assert max(z) - min(z) > 0.0005
    assert all(-1e-6 <= v <= BREATH_Z_M + 1e-6 for v in z)
    # outward only: neither antenna ever leans inside its neutral lean
    assert np.all(ANTENNA_OUTWARD * antennas >= ANTENNA_MIN_RAD - 1e-6)


def test_idle_hold_holds_neutral() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(idle="hold"))

    async def run() -> list[float]:
        async with ReachyMiniApi(config) as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_z(api)[-10:]

    z = asyncio.run(run())
    assert z
    assert all(v == pytest.approx(0.0, abs=1e-6) for v in z)


def test_presence_off_sends_nothing_when_idle() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(presence=False))

    async def run() -> tuple[bool, list[float], int, int]:
        async with ReachyMiniApi(config) as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(0.3)
            idle_targets = _head_z(api)
            await api.play_emotion("sad")
            after_count = len(_fake(api).targets)
            await asyncio.sleep(0.3)
            final_count = len(_fake(api).targets)
            return api.presence, idle_targets, after_count, final_count

    presence, idle_targets, after_count, final_count = asyncio.run(run())
    assert presence is False
    assert idle_targets == []
    assert after_count > 0
    assert final_count == after_count


def test_set_idle_hold_while_breathing_eases_to_neutral() -> None:
    async def run() -> list[float]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(1.0)
            before = len(_fake(api).targets)
            await api.set_idle("hold")
            # A fade-out (playing the breathing plan on, its offsets fading to zero) precedes
            # the neutral blend, so this settles a full BLEND_S later than a plain one.
            await asyncio.sleep(2 * BLEND_S + 0.2)
            return _head_z(api)[before:]

    z = asyncio.run(run())
    assert z
    assert all(v == pytest.approx(0.0, abs=1e-3) for v in z[-5:])
    assert max(abs(b - a) for a, b in itertools.pairwise(z)) < 0.003


def test_set_presence_on_resumes_from_the_present_pose() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(presence=False))

    async def run() -> list[float]:
        async with ReachyMiniApi(config) as api:
            await api.set_motors_state("enabled")
            head = np.eye(4)
            head[2, 3] = 0.02
            api.robot.set_target(head=head)  # a caller driving the head directly
            await api.set_presence(True)
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_z(api)

    z = asyncio.run(run())
    assert z
    assert z[0] == pytest.approx(0.02, abs=0.003)
    assert abs(z[-1]) < BREATH_Z_M + 0.002


def test_switches_are_recorded_and_default_from_the_config() -> None:
    config = ReachyMiniConfig(
        backend="fake", motion=MotionSettings(presence=False, idle="hold")
    )
    api = ReachyMiniApi(config)
    assert api.presence is False
    assert api.idle == "hold"

    async def run() -> None:
        async with api:
            assert api.presence is False
            await api.set_presence(True)
            assert api.presence is True

    asyncio.run(run())
    assert api.presence is False  # reset to the config's values after exit
    assert api.idle == "hold"


def test_switch_verbs_require_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniApi("fake").set_presence(True))
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniApi("fake").set_idle("hold"))
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniApi("fake").set_idle_move(None))


class _Lift(IdleMove):
    """A custom idle move: the head held 8 mm above neutral."""

    def offsets(self, t: float) -> IdleOffsets:
        return IdleOffsets(z_mm=8.0)


def test_custom_idle_move_from_the_config_plays() -> None:
    config = ReachyMiniConfig(
        backend="fake", motion=MotionSettings(idle="custom", idle_move=_Lift)
    )
    api = ReachyMiniApi(config)
    assert (api.idle, api.idle_move) == ("custom", _Lift)  # readable before entry

    async def run() -> list[float]:
        async with api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 0.3)
            return _head_z(api)

    z = asyncio.run(run())
    assert z[-1] == pytest.approx(0.008, abs=1e-6)


def test_set_idle_move_and_set_idle_work_in_either_order() -> None:
    async def run(move_first: bool) -> tuple[list[float], str, object]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            if move_first:
                await api.set_idle_move(_Lift)  # stored while breathing plays
                await api.set_idle("custom")
            else:
                await api.set_idle("custom")  # the hold, until a move is registered
                await api.set_idle_move(_Lift)
            await asyncio.sleep(2 * BLEND_S + 0.4)
            return _head_z(api), api.idle, api.idle_move

    for move_first in (True, False):
        z, idle, idle_move = asyncio.run(run(move_first))
        assert z[-1] == pytest.approx(0.008, abs=1e-6)
        assert (idle, idle_move) == ("custom", _Lift)


def test_idle_modes_reset_to_the_config_on_exit() -> None:
    api = ReachyMiniApi("fake")

    async def run() -> None:
        async with api:
            await api.set_idle_move(_Lift)
            await api.set_idle("custom")
            assert (api.idle, api.idle_move) == ("custom", _Lift)

    asyncio.run(run())
    assert (api.idle, api.idle_move) == ("breathing", None)


def test_set_idle_rejects_an_unknown_mode() -> None:
    async def run() -> str:
        async with ReachyMiniApi("fake") as api:
            with pytest.raises(ValueError, match="idle mode"):
                await api.set_idle("sleeping")
            return api.idle

    assert asyncio.run(run()) == "breathing"


def test_set_idle_move_rejects_a_bad_factory_and_keeps_the_registered_one() -> None:
    async def run() -> object:
        async with ReachyMiniApi("fake") as api:
            await api.set_idle_move(_Lift)
            with pytest.raises(ValueError, match="idle move"):
                await api.set_idle_move(HoldMove)  # type: ignore[arg-type]
            return api.idle_move

    assert asyncio.run(run()) is _Lift


def test_a_bad_idle_move_in_the_config_fails_bring_up() -> None:
    config = ReachyMiniConfig(
        backend="fake",
        motion=MotionSettings(idle="custom", idle_move=HoldMove),  # type: ignore[arg-type]
    )

    async def run() -> None:
        async with ReachyMiniApi(config):
            pass

    with pytest.raises(ValueError, match="idle move"):
        asyncio.run(run())


def test_exit_leaves_the_head_at_neutral() -> None:
    async def run() -> FakeReachyMini:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(1.0)
            return _fake(api)

    robot = asyncio.run(run())
    head, antennas, _yaw = robot.last_target
    assert abs(head[2, 3]) < 0.001
    assert antennas == pytest.approx(NEUTRAL_ANTENNAS, abs=1e-3)


# --- perception (camera): the feed (specs/vision/camera.md) ---------------------------------


async def _first_frame(api: ReachyMiniApi, timeout: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    while api.camera.latest() is None:
        assert time.monotonic() < deadline, "no frame within the wait"
        await asyncio.sleep(0.005)
    return api.camera.latest()


def test_camera_publishes_the_fakes_frames_while_entered() -> None:
    api = ReachyMiniApi("fake")
    camera = api.camera  # exists from construction: a consumer wires to it before entry
    assert camera.latest() is None

    async def run() -> tuple[Any, int]:
        async with api:
            assert api.camera is camera  # the same object inside
            frame = await _first_frame(api, timeout=1.5 / FAKE_FRAME_HZ)
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
        async with ReachyMiniApi(config) as api:
            first = await _first_frame(api)
            robot = _fake(api)
            robot.set_target(head=turned)  # the fake's head is now at this pose
            await asyncio.sleep(3.0 / FAKE_FRAME_HZ)
            return first, api.camera.latest()

    first, later = asyncio.run(run())
    assert first.head_pose is not None and _yaw_deg(first.head_pose) == 0.0
    assert later.head_pose is not None
    assert _yaw_deg(later.head_pose) == pytest.approx(90.0)
    assert later.ts > first.ts


def test_camera_frame_ids_count_on_across_sessions() -> None:
    api = ReachyMiniApi("fake")

    async def run() -> int:
        async with api:
            await _first_frame(api)
        async with api:
            frame = await _first_frame(api)
            return frame.frame_id

    assert asyncio.run(run()) >= 2


# --- faces: the custom detection source (specs/vision/user_perception.md) --------------------


class _Scene:
    """What a stub detector sees: pixel faces on the fake's 64x48 frame, shown and
    hidden by a test as a person would step in front of the robot and leave."""

    def __init__(self, faces: list[PixelFace]) -> None:
        self.faces = faces
        self.calls = 0
        # The factory: one object, so identity checks on `api.face_detector` hold.
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
        faces=FaceSettings(
            detector="custom",
            detection=detection,
            face_detector=None if scene is None else scene.detector,
        ),
        motion=MotionSettings(**motion),
    )


async def _wait_for_face_x(api: ReachyMiniApi, x: float, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        faces = api.faces.value.faces
        if faces and abs(faces[0].x - x) < 0.02:
            return
        assert time.monotonic() < deadline, f"no face at x={x}: {api.faces.value}"
        await asyncio.sleep(0.01)


def test_a_custom_config_enters_and_reports_the_stubs_faces() -> None:
    scene = _Scene([_pixel_face(0.5, -0.25)])

    async def run() -> tuple[FaceReport, list[str], int]:
        async with ReachyMiniApi(_custom_config(scene, tracking=False)) as api:
            assert api.face_detector is scene.detector
            await _wait_for_face_x(api, 0.5)
            await asyncio.sleep(0.5)
            return api.faces.value, _command_names(api), scene.calls

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
        async with ReachyMiniApi(_custom_config(left, tracking=False)) as api:
            await _wait_for_face_x(api, -0.5)
            await api.set_face_detector(right.detector)
            await _wait_for_face_x(api, 0.5, timeout=2.5 / FAKE_FRAME_HZ)
            bad: Any = object
            with pytest.raises(ValueError, match="no callable"):
                await api.set_face_detector(bad)
            assert api.face_detector is right.detector  # the bad one left it alone
            calls_left = left.calls
            await asyncio.sleep(3.0 / FAKE_FRAME_HZ)
            assert left.calls == calls_left  # the old detector is never called again
            await api.set_face_detector(None)  # cleared: nothing looks any more
            await asyncio.sleep(0.05)
            return left.calls, api.faces.value

    _calls, value = asyncio.run(run())
    assert value.active is False


def test_face_detector_reads_the_config_outside_a_session_and_resets() -> None:
    scene, other = _Scene([]), _Scene([])
    api = ReachyMiniApi(_custom_config(scene, tracking=False))
    assert api.face_detector is scene.detector

    async def run() -> None:
        async with api:
            await api.set_face_detector(other.detector)
            assert api.face_detector is other.detector

    asyncio.run(run())
    assert api.face_detector is scene.detector
    assert ReachyMiniApi("fake").face_detector is None


def test_the_head_turns_toward_a_custom_detectors_face() -> None:
    """The whole pipeline on the fake: feed → stub detector → selection → report with
    the frame's pose → the tracker's aim → the motion loop's gaze layer."""
    scene = _Scene([_pixel_face(0.5, 0.0)])  # to the robot's right

    async def run() -> tuple[float, str | None, bool, float]:
        async with ReachyMiniApi(_custom_config(scene, idle="hold")) as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(1.5)
            tracker = api._tracker
            assert tracker is not None
            return (
                _yaw_deg(_fake(api).last_target[0]),
                api.attention,
                api.faces.value.head_pose is not None,
                tracker.delay_s,
            )

    yaw, attention, with_pose, delay = asyncio.run(run())
    assert yaw < -5.0  # negative yaw is to the right
    assert attention == "engaged"
    # The fake's frames carry their capture pose, so the tracker aims against it exactly
    # and its delay estimate is never exercised (specs/motion/head_tracking.md "The aim").
    assert with_pose and delay == head_tracking_module.DELAY_PRIOR_S
