"""Functional tests for ReachyMiniApi on the fake backend (specs/api.md).

Drives the public api the way a caller would and asserts through the escape hatch
(`api.robot`, the FakeReachyMini) and its recorded commands. No network, no daemon,
no hardware. Async runs via `asyncio.run` (fast-tier convention).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import api as api_module
from reachy_mini_bridge.api import ReachyMiniApi
from reachy_mini_bridge.config import DaemonConfig, ReachyMiniConfig
from reachy_mini_bridge.errors import BridgeError, ConfigError, MotorsNotEnabledError
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


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


def test_front_door_exports() -> None:
    import reachy_mini_bridge as rmb

    assert {"ReachyMiniApi", "ReachyMiniConfig", "ConfigError"} <= set(rmb.__all__)
    for name in rmb.__all__:
        assert getattr(rmb, name) is not None
    assert not hasattr(rmb, "hello")


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


def test_tts_block_without_the_extra_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_block: object) -> object:
        raise ImportError("No module named 'tts_engine'")

    monkeypatch.setattr(api_module, "TTSEngineSynthesizer", missing)
    with pytest.raises(ConfigError, match=r"reachy-mini-bridge\[tts\]"):
        ReachyMiniApi(ReachyMiniConfig(backend="fake", tts={"module": {"type": "x"}}))


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
    def managed_daemon(self, config: object, *, host: str, port: int) -> Iterator[None]:
        self.events.append(f"daemon-enter {host}:{port}")
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
        "daemon-enter 127.0.0.1:8000",
        "build sim ['connection_mode', 'host', 'media_backend', 'port']",
        "body",
        "daemon-exit",
    ]
    assert names[0] == "media.start_recording"  # the robot is entered, then media opens
    assert names.index("media.stop_playing") < names.index("__exit__")


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
    assert rec.events == ["daemon-enter 127.0.0.1:8000", "daemon-exit"]
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


def test_start_head_tracking_requires_motors_enabled() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("gravity_compensation")
            with pytest.raises(MotorsNotEnabledError):
                await api.start_head_tracking()
            assert "start_head_tracking" not in _command_names(api)

    asyncio.run(run())


def test_movement_verbs_run_once_motors_enabled() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.play_emotion("happy")
            await api.start_head_tracking(weight=0.5)
            await api.stop_head_tracking()
            return _command_names(api)

    names = asyncio.run(run())
    assert "async_play_move" in names
    assert "start_head_tracking" in names
    assert "stop_head_tracking" in names


def test_start_head_tracking_forwards_weight() -> None:
    async def run() -> float:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.start_head_tracking(weight=0.25)
            args = next(a for n, a in _fake(api).commands if n == "start_head_tracking")
            return args["weight"]

    assert asyncio.run(run()) == 0.25


# --- expression --------------------------------------------------------------------


def test_list_emotions_returns_the_offline_library() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            return await api.list_emotions()

    assert asyncio.run(run()) == ["happy", "sad", "curious"]


def test_play_emotion_resolves_name_and_plays_it() -> None:
    async def run() -> object:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.play_emotion("curious")
            args = next(a for n, a in _fake(api).commands if n == "async_play_move")
            return args["move"]

    assert asyncio.run(run()) == "curious"


def test_play_emotion_unknown_name_raises() -> None:
    async def run() -> None:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            with pytest.raises(ValueError, match="not found"):
                await api.play_emotion("nonexistent")

    asyncio.run(run())


# --- audio ------------------------------------------------------------------------


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


# --- perception (camera) -----------------------------------------------------------


def test_get_camera_frame_returns_a_bgr_frame() -> None:
    async def run() -> npt.NDArray[np.uint8] | None:
        async with ReachyMiniApi("fake") as api:
            return await api.get_camera_frame()

    frame = asyncio.run(run())
    assert frame is not None  # the fake always has a frame ready
    assert frame.ndim == 3 and frame.shape[2] == 3  # HxWx3 BGR
    assert frame.dtype == np.uint8
    assert frame.size > 0
    # The synthetic frame carries real structure (a gradient), not a flat constant.
    assert frame.min() != frame.max()
