"""Functional tests for ReachyMiniConfig (specs/config.md).

Builds configs the way a caller would — from dicts, JSON strings, and files — and pins
the validation rules and the derived views the api consumes. Imports `reachy_mini` only
through the `robot` key check (no daemon).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reachy_mini_bridge.config import (
    AudioSettings,
    DaemonConfig,
    MotionSettings,
    ReachyMiniConfig,
)
from reachy_mini_bridge.errors import ConfigError

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_defaults() -> None:
    cfg = ReachyMiniConfig()
    assert cfg.backend == "real"
    assert cfg.robot == {}
    assert cfg.daemon == DaemonConfig()
    assert cfg.daemon.spawn == "never"
    assert cfg.tts is None
    assert cfg.audio.xvf3800 is None
    assert cfg.wobbling is True
    assert cfg.motion == MotionSettings()
    assert ReachyMiniConfig.from_dict({}) == cfg


def test_from_json_file_round_trips_the_repo_example() -> None:
    cfg = ReachyMiniConfig.from_json_file(_REPO_ROOT / "config.example.json")
    assert cfg.backend == "sim"
    assert cfg.robot == {
        "host": "127.0.0.1",
        "port": 8000,
        "connection_mode": "network",
        "media_backend": "local",
        "timeout": 5.0,
    }
    assert cfg.daemon == DaemonConfig(
        spawn="auto",
        headless=True,
        scene=None,
        preload_datasets=True,
        startup_timeout=45.0,
    )
    assert cfg.tts is not None
    assert cfg.tts["module"]["type"] == "elevenlabs"
    assert cfg.tts["module"]["api_key_env"] == "ELEVENLABS_API_KEY"
    assert cfg.audio == AudioSettings(xvf3800=None)
    assert cfg.wobbling is True
    assert cfg.motion == MotionSettings()


def test_from_json_and_from_json_file_delegate_to_from_dict(tmp_path: Path) -> None:
    data = {"backend": "fake", "daemon": {"headless": False}, "tts": None}
    text = json.dumps(data)
    path = tmp_path / "robot.json"
    path.write_text(text)
    assert (
        ReachyMiniConfig.from_dict(data)
        == ReachyMiniConfig.from_json(text)
        == ReachyMiniConfig.from_json_file(path)
        == ReachyMiniConfig.from_json_file(str(path))
    )
    assert ReachyMiniConfig.from_json(text).daemon.headless is False

    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ConfigError, match="bad.json"):
        ReachyMiniConfig.from_json_file(bad)
    with pytest.raises(ConfigError, match="Invalid JSON"):
        ReachyMiniConfig.from_json("{nope")


def test_wobbling_defaults_on_and_round_trips() -> None:
    assert ReachyMiniConfig.from_dict({}).wobbling is True
    assert ReachyMiniConfig.from_dict({"wobbling": False}).wobbling is False
    assert ReachyMiniConfig.from_json('{"wobbling": false}').wobbling is False


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_wobbling_must_be_a_boolean(value: object) -> None:
    with pytest.raises(ConfigError, match="wobbling"):
        ReachyMiniConfig.from_dict({"wobbling": value})


def test_config_error_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        ReachyMiniConfig.from_dict({"backend": "bogus"})


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({"backend": "bogus"}, "backend"),
        ({"backned": "fake"}, "backned"),
        ({"daemon": {"spwan": "auto"}}, "spwan"),
        ({"audio": {"profile": []}}, "profile"),
        ([], "object"),
        ({"robot": "x"}, "robot"),
        ({"daemon": []}, "daemon"),
        ({"audio": 3}, "audio"),
    ],
)
def test_shape_and_key_errors(data: object, fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment):
        ReachyMiniConfig.from_dict(data)  # type: ignore[arg-type]


def test_robot_keys_checked_against_upstream_signature() -> None:
    ok = {
        "host": "h",
        "port": 1,
        "connection_mode": "network",
        "media_backend": "local",
        "timeout": 2.0,
        "robot_name": "r",
    }
    assert ReachyMiniConfig.from_dict({"robot": ok}).robot == ok
    with pytest.raises(ConfigError, match="hots"):
        ReachyMiniConfig.from_dict({"robot": {"hots": "x"}})


def test_reserved_robot_keys_rejected() -> None:
    with pytest.raises(ConfigError, match="'backend'"):
        ReachyMiniConfig.from_dict({"backend": "sim", "robot": {"use_sim": True}})
    with pytest.raises(ConfigError, match="'daemon.spawn'"):
        ReachyMiniConfig.from_dict({"backend": "sim", "robot": {"spawn_daemon": True}})


def test_spawn_requires_a_backend_with_a_daemon() -> None:
    with pytest.raises(ConfigError, match="'fake' has no daemon"):
        ReachyMiniConfig.from_dict({"backend": "fake", "daemon": {"spawn": "auto"}})
    for backend in ("sim", "real"):
        cfg = ReachyMiniConfig.from_dict(
            {"backend": backend, "daemon": {"spawn": "always"}}
        )
        assert cfg.daemon.spawn == "always"
        assert cfg.manages_daemon
    with pytest.raises(ConfigError, match="daemon.spawn"):
        ReachyMiniConfig.from_dict({"backend": "sim", "daemon": {"spawn": "maybe"}})


def test_a_sim_config_switches_to_a_spawned_real_daemon_by_backend_alone() -> None:
    sim = {
        "backend": "sim",
        "daemon": {
            "spawn": "auto",
            "headless": False,
            "scene": "minimal",
            "preload_datasets": True,
            "startup_timeout": 60,
        },
    }
    real = ReachyMiniConfig.from_dict({**sim, "backend": "real"})
    assert real.backend == "real"
    assert real.daemon == ReachyMiniConfig.from_dict(sim).daemon


def test_spawn_requires_loopback_host() -> None:
    # A wireless robot runs its own daemon: the bridge never spawns one elsewhere.
    with pytest.raises(ConfigError, match="loopback"):
        ReachyMiniConfig.from_dict(
            {
                "backend": "real",
                "daemon": {"spawn": "auto"},
                "robot": {"host": "192.168.1.42"},
            }
        )
    base = {"backend": "sim", "daemon": {"spawn": "auto"}}
    with pytest.raises(ConfigError, match="loopback"):
        ReachyMiniConfig.from_dict({**base, "robot": {"host": "10.0.0.5"}})
    for host in ("127.0.0.1", "localhost", "::1"):
        assert ReachyMiniConfig.from_dict({**base, "robot": {"host": host}}).robot == {
            "host": host
        }
    # Without daemon management any host is fine.
    ReachyMiniConfig.from_dict({"backend": "sim", "robot": {"host": "10.0.0.5"}})


def test_effective_robot_options_fill_in_for_a_managed_daemon() -> None:
    managed = ReachyMiniConfig.from_dict(
        {"backend": "sim", "daemon": {"spawn": "auto"}}
    )
    assert managed.manages_daemon
    assert managed.effective_robot_options() == {
        "host": "127.0.0.1",
        "port": 8000,
        "connection_mode": "network",
        "media_backend": "local",
    }
    kept = ReachyMiniConfig.from_dict(
        {
            "backend": "sim",
            "daemon": {"spawn": "auto"},
            "robot": {"port": 9100, "media_backend": "no_media", "timeout": 1.0},
        }
    )
    assert kept.effective_robot_options() == {
        "host": "127.0.0.1",
        "port": 9100,
        "connection_mode": "network",
        "media_backend": "no_media",
        "timeout": 1.0,
    }
    real = ReachyMiniConfig.from_dict({"backend": "real", "daemon": {"spawn": "auto"}})
    assert real.effective_robot_options() == managed.effective_robot_options()
    plain = ReachyMiniConfig.from_dict({"backend": "sim", "robot": {"timeout": 1.0}})
    assert not plain.manages_daemon
    assert plain.effective_robot_options() == {"timeout": 1.0}


def test_tts_block_shape() -> None:
    block = {
        "module": {"type": "elevenlabs", "voice_id": "v"},
        "player": {"device": None},
    }
    cfg = ReachyMiniConfig.from_dict({"tts": block})
    assert cfg.tts == block
    assert cfg.tts is not block  # a copy, but verbatim
    assert ReachyMiniConfig.from_dict({"tts": None}).tts is None
    for bad in (
        {"tts": {}},
        {"tts": {"module": "elevenlabs"}},
        {"tts": {"module": {"type": ""}}},
        {"tts": {"module": {"type": 3}}},
        {"tts": "elevenlabs"},
    ):
        with pytest.raises(ConfigError, match="tts"):
            ReachyMiniConfig.from_dict(bad)


def test_audio_xvf3800_shape() -> None:
    profile = [["AEC_ENABLED", [1]], ["AGC_MAX_GAIN", [0.5, 1]]]
    cfg = ReachyMiniConfig.from_dict({"audio": {"xvf3800": profile}})
    assert cfg.audio.xvf3800 == profile
    for bad in (
        {"audio": {"xvf3800": "high"}},
        {"audio": {"xvf3800": [["AEC_ENABLED"]]}},
        {"audio": {"xvf3800": [[1, [1]]]}},
        {"audio": {"xvf3800": [["AEC_ENABLED", 1]]}},
    ):
        with pytest.raises(ConfigError, match="xvf3800"):
            ReachyMiniConfig.from_dict(bad)


@pytest.mark.parametrize(
    "daemon",
    [
        {"headless": "yes"},
        {"preload_datasets": 1},
        {"startup_timeout": 0},
        {"startup_timeout": True},
        {"startup_timeout": "45"},
        {"scene": ""},
        {"scene": 3},
    ],
)
def test_daemon_field_types(daemon: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="daemon"):
        ReachyMiniConfig.from_dict({"backend": "sim", "daemon": daemon})


def test_motion_defaults_are_on() -> None:
    assert ReachyMiniConfig.from_dict({}).motion == MotionSettings()


def test_motion_block_sets_the_switches() -> None:
    cfg = ReachyMiniConfig.from_dict(
        {"motion": {"presence": False, "breathing": False}}
    )
    assert cfg.motion == MotionSettings(presence=False, breathing=False)
    cfg = ReachyMiniConfig.from_dict({"motion": {"breathing": False}})
    assert cfg.motion == MotionSettings(presence=True, breathing=False)


@pytest.mark.parametrize(
    "motion",
    [
        {"presence": "yes"},
        {"breathing": 1},
    ],
)
def test_motion_rejects_non_booleans(motion: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="motion"):
        ReachyMiniConfig.from_dict({"motion": motion})


def test_motion_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="idle"):
        ReachyMiniConfig.from_dict({"motion": {"idle": True}})


def test_motion_must_be_an_object() -> None:
    with pytest.raises(ConfigError, match="motion"):
        ReachyMiniConfig.from_dict({"motion": True})


def test_nested_blocks_have_their_own_constructor_trio(tmp_path: Path) -> None:
    text = '{"spawn": "auto", "scene": "minimal"}'
    path = tmp_path / "daemon.json"
    path.write_text(text)
    expected = DaemonConfig(spawn="auto", scene="minimal")
    assert DaemonConfig.from_json(text) == expected
    assert DaemonConfig.from_json_file(path) == expected
    assert AudioSettings.from_json('{"xvf3800": [["X", [1]]]}') == AudioSettings(
        xvf3800=[["X", [1]]]
    )
