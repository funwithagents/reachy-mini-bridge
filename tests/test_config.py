"""Functional tests for ReachyMiniConfig (specs/core/config.md).

Builds configs the way a caller would — from dicts, JSON strings, and files — and pins
the validation rules and the derived views the bridge consumes. Imports `reachy_mini` only
through the `robot` key check (no daemon).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reachy_mini_bridge.config import (
    SIM_DISPLAYS,
    AudioSettings,
    DaemonConfig,
    FaceDetectionSettings,
    MotionSettings,
    ReachyMiniConfig,
    SimCameraSettings,
    SimDisplaySettings,
)
from reachy_mini_bridge.errors import ConfigError
from reachy_mini_bridge.face_detection import DETECT_WIDTH

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_defaults() -> None:
    cfg = ReachyMiniConfig()
    assert cfg.backend == "real"
    assert cfg.robot == {}
    assert cfg.daemon == DaemonConfig()
    assert cfg.daemon.spawn == "never"
    assert cfg.tts is None
    assert cfg.audio.xvf3800 is None
    # Detection is opt-in (specs/vision/user_perception.md): no detector, no detection, and
    # tracking off since it needs a detector.
    assert cfg.face_detection == FaceDetectionSettings(detector=None, enabled=False)
    # upstream's own detection width, spelled out; null would be the full frame
    assert cfg.face_detection.width == DETECT_WIDTH == 320
    assert cfg.face_detection.target_fps is None  # once per new camera frame
    assert cfg.motion == MotionSettings()
    assert cfg.motion.tracking is False
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
    # The example is the sim viewer seeing through the host webcam (specs/core/config.md):
    # headless is off and the camera source is `webcam` on purpose, with every sim
    # display on: the camera overlay, the robot's gaze and the face markers.
    assert cfg.daemon == DaemonConfig(
        spawn="auto",
        headless=False,
        scene=None,
        camera=SimCameraSettings(source="webcam", device=None, hfov_deg=70.0),
        sim_displays=SimDisplaySettings(
            camera_overlay=True, robot_gaze=True, face_markers=True
        ),
        preload_datasets=True,
        startup_timeout=45.0,
    )
    assert cfg.tts is not None
    assert cfg.tts["module"]["type"] == "pocket"
    assert cfg.tts["module"]["voice"] == "george"
    assert cfg.audio == AudioSettings(xvf3800=None)
    # The example shows the robot that follows the person in front of the webcam: the
    # shipped detector, detection and tracking on — detecting at 640 px, twice the
    # default width, for a finer roll.
    assert cfg.face_detection == FaceDetectionSettings(
        detector="yunet", enabled=True, width=640
    )
    assert cfg.motion == MotionSettings(tracking=True)


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
    for host in ("127.0.0.1", "localhost"):
        assert ReachyMiniConfig.from_dict({**base, "robot": {"host": host}}).robot == {
            "host": host
        }
    # IPv6 loopback is refused: upstream's client cannot form a URL from it.
    with pytest.raises(ConfigError, match="127.0.0.1"):
        ReachyMiniConfig.from_dict({**base, "robot": {"host": "::1"}})
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
        {"startup_timeout": float("inf")},
        {"startup_timeout": float("-inf")},
        {"startup_timeout": float("nan")},
        {"scene": ""},
        {"scene": 3},
    ],
)
def test_daemon_field_types(daemon: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="daemon"):
        ReachyMiniConfig.from_dict({"backend": "sim", "daemon": daemon})


@pytest.mark.parametrize("spelling", ["Infinity", "-Infinity", "NaN"])
def test_a_non_finite_startup_timeout_in_json_is_rejected(spelling: str) -> None:
    """Python's JSON parser accepts these non-standard numbers; a deadline built from
    one is never reached, so the config refuses them (specs/core/config.md)."""
    text = f'{{"backend": "sim", "daemon": {{"startup_timeout": {spelling}}}}}'
    with pytest.raises(ConfigError, match="startup_timeout.*finite"):
        ReachyMiniConfig.from_json(text)
    assert ReachyMiniConfig.from_json(
        '{"backend": "sim", "daemon": {"startup_timeout": 12.5}}'
    ).daemon.startup_timeout == pytest.approx(12.5)


def test_daemon_camera_defaults_to_the_rendered_eye_camera() -> None:
    assert ReachyMiniConfig.from_dict({}).daemon.camera == SimCameraSettings()
    assert SimCameraSettings() == SimCameraSettings(
        source="sim", device=None, hfov_deg=70.0
    )


def test_daemon_camera_selects_a_webcam() -> None:
    cfg = ReachyMiniConfig.from_dict(
        {
            "backend": "sim",
            "daemon": {
                "spawn": "auto",
                "camera": {"source": "webcam", "device": 1, "hfov_deg": 62},
            },
        }
    )
    assert cfg.daemon.camera == SimCameraSettings(
        source="webcam", device=1, hfov_deg=62.0
    )
    assert isinstance(cfg.daemon.camera.hfov_deg, float)
    linux = SimCameraSettings.from_json('{"source": "webcam", "device": "/dev/video2"}')
    assert linux == SimCameraSettings(source="webcam", device="/dev/video2")


@pytest.mark.parametrize(
    ("camera", "key"),
    [
        ({"source": "usb"}, "source"),
        ({"device": ""}, "device"),
        ({"device": -1}, "device"),
        ({"device": True}, "device"),
        ({"device": 1.5}, "device"),
        ({"hfov_deg": 1}, "hfov_deg"),
        ({"hfov_deg": 179}, "hfov_deg"),
        ({"hfov_deg": "70"}, "hfov_deg"),
        ({"hfov_deg": True}, "hfov_deg"),
        ({"fov": 70}, "fov"),
    ],
)
def test_daemon_camera_rejects_bad_values(camera: dict[str, object], key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        ReachyMiniConfig.from_dict({"backend": "sim", "daemon": {"camera": camera}})


def test_daemon_camera_must_be_an_object() -> None:
    with pytest.raises(ConfigError, match=r"daemon\.camera"):
        ReachyMiniConfig.from_dict({"daemon": {"camera": "webcam"}})


def test_daemon_sim_displays_default_off() -> None:
    assert ReachyMiniConfig.from_dict({}).daemon.sim_displays == SimDisplaySettings()
    assert SimDisplaySettings().enabled() == []
    assert SIM_DISPLAYS == ("camera_overlay", "robot_gaze", "face_markers")


def test_daemon_sim_displays_turn_on_a_viewer_display() -> None:
    cfg = ReachyMiniConfig.from_dict(
        {
            "backend": "sim",
            "daemon": {"headless": False, "sim_displays": {"camera_overlay": True}},
        }
    )
    assert cfg.daemon.sim_displays == SimDisplaySettings(camera_overlay=True)
    assert cfg.daemon.sim_displays.enabled() == ["camera_overlay"]
    # Every display parses, and the ones on are listed in SIM_DISPLAYS order whatever
    # the order they were written in.
    every = SimDisplaySettings.from_json(
        '{"face_markers": true, "robot_gaze": true, "camera_overlay": true}'
    )
    assert every == SimDisplaySettings(
        camera_overlay=True, robot_gaze=True, face_markers=True
    )
    assert every.enabled() == ["camera_overlay", "robot_gaze", "face_markers"]
    assert SimDisplaySettings.from_json('{"camera_overlay": true}').enabled() == [
        "camera_overlay"
    ]


def test_daemon_sim_displays_need_the_viewer() -> None:
    """A display is drawn in the viewer window; with the headless daemon (the default)
    there is none, so the config says so instead of silently showing nothing."""
    for name in ("robot_gaze", "face_markers"):
        with pytest.raises(ConfigError, match=rf"sim_displays\.{name}.*headless"):
            ReachyMiniConfig.from_dict(
                {"backend": "sim", "daemon": {"sim_displays": {name: True}}}
            )
    with pytest.raises(ConfigError, match=r"sim_displays\.camera_overlay.*headless"):
        ReachyMiniConfig.from_dict(
            {"backend": "sim", "daemon": {"sim_displays": {"camera_overlay": True}}}
        )
    off = ReachyMiniConfig.from_dict(
        {"backend": "sim", "daemon": {"sim_displays": {"camera_overlay": False}}}
    )
    assert off.daemon.headless and off.daemon.sim_displays.enabled() == []


@pytest.mark.parametrize(
    "displays",
    [{"camera_overlay": "yes"}, {"camera_overlay": 1}, {"hud": True}, "camera_overlay"],
)
def test_daemon_sim_displays_reject_bad_values(displays: object) -> None:
    with pytest.raises(ConfigError, match=r"daemon\.sim_displays"):
        ReachyMiniConfig.from_dict(
            {"backend": "sim", "daemon": {"headless": False, "sim_displays": displays}}
        )


def test_face_detection_block_sets_the_detector_the_switch_and_the_knobs(
    tmp_path: Path,
) -> None:
    text = '{"face_detection": {"detector": "custom", "enabled": false}}'
    expected = FaceDetectionSettings(detector="custom", enabled=False)
    assert ReachyMiniConfig.from_json(text).face_detection == expected
    block = '{"detector": "custom", "enabled": false}'
    path = tmp_path / "face_detection.json"
    path.write_text(block)
    assert FaceDetectionSettings.from_json(block) == expected
    assert FaceDetectionSettings.from_json_file(path) == expected
    yunet = ReachyMiniConfig.from_json(
        '{"face_detection": {"detector": "yunet", "enabled": true, "width": 640,'
        ' "target_fps": 2.5}}'
    )
    assert yunet.face_detection == FaceDetectionSettings(
        detector="yunet", enabled=True, width=640, target_fps=2.5
    )
    whole = ReachyMiniConfig.from_dict({"face_detection": {"target_fps": 5}})
    assert whole.face_detection.target_fps == 5.0
    nulls = ReachyMiniConfig.from_dict(
        {"face_detection": {"width": None, "target_fps": None}}
    )
    assert nulls.face_detection.width is None
    assert nulls.face_detection.target_fps is None


def test_face_detection_detector_null_and_absent_both_mean_none() -> None:
    null = ReachyMiniConfig.from_dict({"face_detection": {"detector": None}})
    assert null.face_detection.detector is None
    assert (
        ReachyMiniConfig.from_dict({"face_detection": {}}).face_detection.detector
        is None
    )
    assert ReachyMiniConfig.from_dict(
        {"face_detection": {"enabled": False}}
    ).face_detection == FaceDetectionSettings(detector=None, enabled=False)


def test_the_faces_block_is_gone() -> None:
    """The block is `face_detection` (specs/core/config.md); `faces` is an unknown key."""
    with pytest.raises(ConfigError, match="faces"):
        ReachyMiniConfig.from_dict({"faces": {"detector": "yunet", "detection": True}})


@pytest.mark.parametrize(
    "data",
    [
        {"face_detection": {"enabled": True}},
        {"motion": {"tracking": True}},
        {"face_detection": {"enabled": True}, "motion": {"tracking": True}},
        {"face_detection": {"detector": None, "enabled": True}},
    ],
)
def test_a_switch_on_without_a_detector_is_a_config_error(data: dict[str, Any]) -> None:
    """The cross-block rule (specs/core/config.md): detection and tracking need a detector."""
    with pytest.raises(ConfigError, match=r"face_detection\.detector") as info:
        ReachyMiniConfig.from_dict(data)
    message = str(info.value)
    if data.get("face_detection", {}).get("enabled"):
        assert "face_detection.enabled" in message
    if data.get("motion", {}).get("tracking"):
        assert "motion.tracking" in message
    assert "yunet" in message  # the message points at the shipped detector
    # The same switches with a detector named are valid.
    block = {**data.get("face_detection", {}), "detector": "yunet"}
    cfg = ReachyMiniConfig.from_dict({**data, "face_detection": block})
    assert cfg.face_detection.detector == "yunet"


@pytest.mark.parametrize(
    ("block", "field"),
    [
        ({"detector": "local"}, r"face_detection\.detector"),
        ({"detector": "daemon"}, r"face_detection\.detector"),
        ({"detector": 1}, r"face_detection\.detector"),
        ({"detector": True}, r"face_detection\.detector"),
        ({"enabled": "yes"}, r"face_detection\.enabled"),
        ({"enabled": 1}, r"face_detection\.enabled"),
        ({"detection": True}, "detection"),
        ({"detecter": "yunet"}, "detecter"),
        ({"face_detector": "my.module:Yunet"}, r"face_detection\.face_detector"),
        ({"width": 0}, r"face_detection\.width"),
        ({"width": -1}, r"face_detection\.width"),
        ({"width": 320.0}, r"face_detection\.width"),
        ({"width": True}, r"face_detection\.width"),
        ({"width": "320"}, r"face_detection\.width"),
        ({"target_fps": 0}, r"face_detection\.target_fps"),
        ({"target_fps": -2}, r"face_detection\.target_fps"),
        ({"target_fps": float("nan")}, r"face_detection\.target_fps"),
        ({"target_fps": float("inf")}, r"face_detection\.target_fps"),
        ({"target_fps": True}, r"face_detection\.target_fps"),
        ({"target_fps": "5"}, r"face_detection\.target_fps"),
    ],
)
def test_face_detection_block_rejects_bad_values(
    block: dict[str, object], field: str
) -> None:
    with pytest.raises(ConfigError, match=field):
        ReachyMiniConfig.from_dict({"face_detection": block})


def test_face_detection_must_be_an_object() -> None:
    with pytest.raises(ConfigError, match="face_detection"):
        ReachyMiniConfig.from_dict({"face_detection": []})


def test_motion_defaults() -> None:
    assert ReachyMiniConfig.from_dict({}).motion == MotionSettings()
    assert MotionSettings().presence is True
    assert MotionSettings().wobbling is True
    assert MotionSettings().tracking is False  # needs a detector, none by default


def test_motion_block_sets_the_switches() -> None:
    cfg = ReachyMiniConfig.from_dict(
        {
            "motion": {
                "presence": False,
                "idle": "hold",
                "wobbling": False,
                "tracking": False,
            }
        }
    )
    assert cfg.motion == MotionSettings(
        presence=False, idle="hold", wobbling=False, tracking=False
    )
    cfg = ReachyMiniConfig.from_dict({"motion": {"idle": "custom"}})
    assert cfg.motion == MotionSettings(presence=True, idle="custom", idle_move=None)
    cfg = ReachyMiniConfig.from_json('{"motion": {"wobbling": false}}')
    assert cfg.motion == MotionSettings(wobbling=False)
    cfg = ReachyMiniConfig.from_dict(
        {"face_detection": {"detector": "custom"}, "motion": {"tracking": True}}
    )
    assert cfg.motion == MotionSettings(tracking=True)


@pytest.mark.parametrize(
    "motion",
    [
        {"presence": "yes"},
        {"wobbling": "yes"},
        {"tracking": 1},
    ],
)
def test_motion_rejects_non_booleans(motion: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="motion"):
        ReachyMiniConfig.from_dict({"motion": motion})


@pytest.mark.parametrize("idle", [True, "breathe", "", None])
def test_motion_rejects_an_unknown_idle_mode(idle: object) -> None:
    with pytest.raises(ConfigError, match=r"motion\.idle"):
        ReachyMiniConfig.from_dict({"motion": {"idle": idle}})


def test_motion_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="breathing"):
        ReachyMiniConfig.from_dict({"motion": {"breathing": True}})


def test_motion_idle_move_is_python_only() -> None:
    with pytest.raises(ConfigError, match="set from code"):
        ReachyMiniConfig.from_dict({"motion": {"idle_move": "my.module:Nod"}})

    def factory() -> object:
        raise AssertionError("the config layer never calls the factory")

    settings = MotionSettings(idle="custom", idle_move=factory)  # type: ignore[arg-type]
    assert ReachyMiniConfig(motion=settings).motion.idle_move is factory


def test_idle_modes_match_the_motion_loops_type() -> None:
    from typing import get_args

    from reachy_mini_bridge.config import IDLE_MODES
    from reachy_mini_bridge.motion import IdleMode

    assert get_args(IdleMode.__value__) == IDLE_MODES


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
