"""Functional tests for ReachyMiniApi on the fake backend (specs/api.md).

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
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import numpy as np
import numpy.typing as npt
import pytest

from reachy_mini_bridge import api as api_module
from reachy_mini_bridge.api import ReachyMiniApi
from reachy_mini_bridge.config import DaemonConfig, MotionSettings, ReachyMiniConfig
from reachy_mini_bridge.errors import (
    BridgeError,
    ConfigError,
    GravityCompensationUnsupportedError,
    MotorsNotEnabledError,
)
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini
from reachy_mini_bridge.motion import BLEND_S, BREATH_Z_M, NEUTRAL_ANTENNAS


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
        "ConfigError",
        "GravityCompensationUnsupportedError",
        "MotorsNotEnabledError",
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

    def fetch(url: str) -> object:
        fetched.append(url)
        return {"info": {"engine": "Placo", "collision check": False}}

    monkeypatch.setattr(api_module, "_fetch_json", fetch)
    robot = SimpleNamespace(client=SimpleNamespace(host="192.168.1.5", port=8000))
    assert api_module._daemon_kinematics_engine(robot) == "Placo"  # pyright: ignore[reportArgumentType]
    assert fetched == ["http://192.168.1.5:8000/api/kinematics/info"]


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
    assert "media.play_sound" in names  # the move played through the motion loop
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


def test_play_emotion_pauses_tracking_and_restores_it() -> None:
    async def run() -> list[float]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.start_head_tracking(0.7)
            await api.play_emotion("sad")
            return [
                args["weight"]
                for name, args in _fake(api).commands
                if name == "start_head_tracking"
            ]

    assert asyncio.run(run()) == [0.7, 0.0, 0.7]


def test_play_emotion_leaves_tracking_alone_when_off() -> None:
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.play_emotion("sad")
            return _command_names(api)

    assert "start_head_tracking" not in asyncio.run(run())


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
            await api.start_head_tracking(0.7)
            task = asyncio.create_task(api.play_emotion("happy"))
            while "media.play_sound" not in _command_names(api):
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return list(_fake(api).commands)

    commands = asyncio.run(run())
    tracking_weights = [a["weight"] for n, a in commands if n == "start_head_tracking"]
    assert tracking_weights[-1] == 0.7
    wobbling_calls = [
        n for n, _ in commands if n in ("enable_wobbling", "disable_wobbling")
    ]
    assert wobbling_calls[-1] == "enable_wobbling"


def test_play_emotion_restores_to_the_current_record() -> None:
    async def run() -> list[tuple[str, dict[str, Any]]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await api.start_head_tracking(0.7)
            task = asyncio.create_task(api.play_emotion("sad"))
            await asyncio.sleep(0)  # let play_emotion dip tracking before we change it
            await api.stop_head_tracking()
            await task
            return list(_fake(api).commands)

    commands = asyncio.run(run())
    tracking_calls = [
        (n, a)
        for n, a in commands
        if n in ("start_head_tracking", "stop_head_tracking")
    ]
    assert tracking_calls[-1][0] == "stop_head_tracking"


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
    config = ReachyMiniConfig(backend="fake", wobbling=False)

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
    config = ReachyMiniConfig(backend="fake", wobbling=False)

    async def run() -> FakeReachyMini:
        async with ReachyMiniApi(config, synthesizer=_ToneSynth()) as api:
            await api.say("hello")
            return _fake(api)

    names = [name for name, _ in asyncio.run(run()).commands]
    assert "enable_wobbling" not in names
    assert "disable_wobbling" not in names


def test_wobbling_enabled_at_runtime_is_disabled_at_exit() -> None:
    api = ReachyMiniApi(ReachyMiniConfig(backend="fake", wobbling=False))

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


# --- presence & breathing (motion loop) ---------------------------------------------


def test_breathing_targets_oscillate_in_z() -> None:
    async def run() -> tuple[list[float], tuple[float, float]]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(BLEND_S + 0.8)
            z = _head_z(api)[-20:]
            _head, antennas, _yaw = _fake(api).last_target
            return z, (float(antennas[0]), float(antennas[1]))

    z, (a0, a1) = asyncio.run(run())
    assert max(z) - min(z) > 0.0005
    assert all(abs(v) <= BREATH_Z_M + 1e-6 for v in z)
    # counter-phase sway: the two antennas' offsets from neutral have opposite signs
    assert (a0 - NEUTRAL_ANTENNAS[0]) * (a1 - NEUTRAL_ANTENNAS[1]) <= 0


def test_breathing_off_holds_neutral() -> None:
    config = ReachyMiniConfig(backend="fake", motion=MotionSettings(breathing=False))

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


def test_set_breathing_while_idle_eases_to_neutral() -> None:
    async def run() -> list[float]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await asyncio.sleep(1.0)
            before = len(_fake(api).targets)
            await api.set_breathing(False)
            # A fade-out (continuing breathing's own phase to zero velocity) precedes
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
        backend="fake", motion=MotionSettings(presence=False, breathing=False)
    )
    api = ReachyMiniApi(config)
    assert api.presence is False
    assert api.breathing is False

    async def run() -> None:
        async with api:
            assert api.presence is False
            await api.set_presence(True)
            assert api.presence is True

    asyncio.run(run())
    assert api.presence is False  # reset to the config's values after exit
    assert api.breathing is False


def test_switch_verbs_require_entry() -> None:
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniApi("fake").set_presence(True))
    with pytest.raises(BridgeError):
        asyncio.run(ReachyMiniApi("fake").set_breathing(True))


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
