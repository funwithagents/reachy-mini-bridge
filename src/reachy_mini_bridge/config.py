"""Configuration: ``ReachyMiniConfig`` (specs/core/config.md).

One declarative object describing everything needed to bring up a ``ReachyMiniBridge``:
the backend, the upstream ``ReachyMini`` connection kwargs (forwarded verbatim), how the
bridge manages the daemon, the tts-engine ``engine`` block for the default synthesizer,
the XVF3800 audio profile, face detection, and the behaviour at rest. Buildable from a
dict, a JSON string, or a JSON file through the same ``from_dict`` / ``from_json`` /
``from_json_file`` trio as tts-engine's ``TTSEngineConfig``, all validating through
``from_dict``.

This module imports neither ``tts_engine`` nor ``reachy_mini`` at load time: the raw
``tts`` and ``robot`` blocks are consumed by the layers that need them. The one upstream
lookup (checking ``robot`` keys against ``ReachyMini``'s signature) imports lazily.
"""

from __future__ import annotations

import inspect
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from .face_detection import FaceDetector
    from .motion import IdleMove

__all__ = [
    "AudioSettings",
    "DaemonConfig",
    "FaceDetectionSettings",
    "MotionSettings",
    "ReachyMiniConfig",
    "SimCameraSettings",
    "SimDisplaySettings",
]

BACKENDS = ("real", "sim", "fake")
# The backends with a daemon the bridge can spawn: MuJoCo, or a USB-attached robot.
DAEMON_BACKENDS = ("sim", "real")
SPAWN_MODES = ("never", "auto", "always")
# What the sim daemon's camera stream carries (specs/daemon/sim_daemon.md "Camera sources").
CAMERA_SOURCES = ("sim", "webcam")
DEFAULT_WEBCAM_HFOV_DEG = 70.0
# The sim displays (specs/core/config.md `daemon.sim_displays`; specs/daemon/sim_displays.md):
# each name is a field of SimDisplaySettings and a `--sim-display` value of the sim
# daemon launcher. Add a display here and as a field, nowhere else.
SIM_DISPLAYS = ("camera_overlay", "robot_gaze", "face_markers")
# The idle modes (specs/motion/motion.md "Presence and the idle mode"); motion.IdleMode is the
# same three values as a type.
IDLE_MODES = ("breathing", "hold", "custom")
# The face detection sources (specs/vision/user_perception.md "Detection sources").
FACE_DETECTORS = ("yunet", "custom")

# Upstream kwargs the bridge owns; each maps to the config field that replaces it.
_RESERVED_ROBOT_KEYS = {
    "use_sim": "'backend' (the bridge derives use_sim from it)",
    "spawn_daemon": "'daemon.spawn' (the bridge manages the daemon itself)",
}
# IPv4 only: upstream's SDK client forms its URLs from the raw host (`ws://{host}:{port}`),
# so IPv6's `::1` could never be reached — refused rather than half-supported.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

# What a bridge-managed (spawned or borrowed local) daemon needs the client to use,
# unless the caller set it: an externally started daemon serves neither the IPC
# transport nor the WebRTC media path (docs/running-the-sim-daemon.md).
_MANAGED_DAEMON_ROBOT_DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8000,
    "connection_mode": "network",
    "media_backend": "local",
}


# --- JSON plumbing -----------------------------------------------------------------


def _loads(text: str, source: str | None = None) -> Any:
    """Parse JSON, re-raising a decode failure as ``ConfigError`` (naming ``source``)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        where = f" in {source}" if source else ""
        raise ConfigError(f"Invalid JSON{where}: {e}") from e


def _load_file(path: str | Path) -> Any:
    return _loads(Path(path).read_text(encoding="utf-8"), source=str(path))


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' must be an object")
    return value


def _reject_unknown_keys(block: dict[str, Any], name: str, allowed: set[str]) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in '{name}': {', '.join(unknown)}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- `daemon` block -----------------------------------------------------------------


@dataclass
class SimCameraSettings:
    """What the sim daemon's camera shows (specs/core/config.md ``daemon.camera``;
    specs/daemon/sim_daemon.md "Camera sources"): ``source`` ``"sim"`` renders the robot's eye
    camera, ``"webcam"`` relays a host camera. ``device`` and ``hfov_deg`` apply to a
    webcam only."""

    source: str = "sim"
    # None: the default camera; an int: a macOS device index; a str: a Linux device path
    device: str | int | None = None
    # the webcam's horizontal field of view, from which the tracker's intrinsics derive
    hfov_deg: float = DEFAULT_WEBCAM_HFOV_DEG

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SimCameraSettings:
        block = _require_object(data, "daemon.camera")
        _reject_unknown_keys(block, "daemon.camera", {"source", "device", "hfov_deg"})
        source = block.get("source", "sim")
        if source not in CAMERA_SOURCES:
            raise ConfigError(
                f"'daemon.camera.source' must be one of {CAMERA_SOURCES}, got {source!r}"
            )
        device = block.get("device")
        valid_device = (
            device is None
            or (isinstance(device, str) and device != "")
            or (
                isinstance(device, int) and not isinstance(device, bool) and device >= 0
            )
        )
        if not valid_device:
            raise ConfigError(
                "'daemon.camera.device' must be null, a non-empty string or a "
                f"non-negative integer, got {device!r}"
            )
        hfov = block.get("hfov_deg", DEFAULT_WEBCAM_HFOV_DEG)
        if not _is_number(hfov) or not 1.0 < hfov < 179.0:
            raise ConfigError(
                "'daemon.camera.hfov_deg' must be a number strictly between 1 and 179, "
                f"got {hfov!r}"
            )
        return cls(source=source, device=device, hfov_deg=float(hfov))

    @classmethod
    def from_json(cls, text: str) -> SimCameraSettings:
        return cls.from_dict(_loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> SimCameraSettings:
        return cls.from_dict(_load_file(path))


@dataclass
class SimDisplaySettings:
    """What the MuJoCo viewer window shows besides the scene (specs/core/config.md
    ``daemon.sim_displays``): one boolean per display, every one off by default.
    ``camera_overlay`` draws the sim daemon's camera stream in the top-right corner of
    the viewer, ``robot_gaze`` the eye camera's optical axis as a line in the 3D scene,
    ``face_markers`` an ellipsoid per face the bridge detects, where it places it — which
    the bridge sends, so that one is read by both sides (specs/daemon/sim_displays.md). A
    display needs the viewer, so ``DaemonConfig.from_dict`` rejects one set with
    ``headless``."""

    camera_overlay: bool = False
    robot_gaze: bool = False
    face_markers: bool = False

    def enabled(self) -> list[str]:
        """The names of the displays that are on, in ``SIM_DISPLAYS`` order."""
        return [name for name in SIM_DISPLAYS if getattr(self, name)]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SimDisplaySettings:
        block = _require_object(data, "daemon.sim_displays")
        _reject_unknown_keys(block, "daemon.sim_displays", set(SIM_DISPLAYS))
        values: dict[str, bool] = {}
        for name in SIM_DISPLAYS:
            value = block.get(name, False)
            if not isinstance(value, bool):
                raise ConfigError(f"'daemon.sim_displays.{name}' must be a boolean")
            values[name] = value
        return cls(**values)

    @classmethod
    def from_json(cls, text: str) -> SimDisplaySettings:
        return cls.from_dict(_loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> SimDisplaySettings:
        return cls.from_dict(_load_file(path))


@dataclass
class DaemonConfig:
    """How the bridge brings up the daemon the robot client talks to (specs/daemon/daemon.md).

    ``headless``, ``scene``, ``camera`` and ``sim_displays`` are MuJoCo knobs: they play
    no part for a ``real`` daemon.
    """

    spawn: str = "never"
    headless: bool = True
    scene: str | None = None
    camera: SimCameraSettings = field(default_factory=SimCameraSettings)
    sim_displays: SimDisplaySettings = field(default_factory=SimDisplaySettings)
    preload_datasets: bool = True
    startup_timeout: float = 45.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DaemonConfig:
        block = _require_object(data, "daemon")
        _reject_unknown_keys(
            block,
            "daemon",
            {
                "spawn",
                "headless",
                "scene",
                "camera",
                "sim_displays",
                "preload_datasets",
                "startup_timeout",
            },
        )
        spawn = block.get("spawn", "never")
        if spawn not in SPAWN_MODES:
            raise ConfigError(
                f"'daemon.spawn' must be one of {SPAWN_MODES}, got {spawn!r}"
            )
        headless = block.get("headless", True)
        if not isinstance(headless, bool):
            raise ConfigError("'daemon.headless' must be a boolean")
        scene = block.get("scene")
        if scene is not None and (not isinstance(scene, str) or not scene):
            raise ConfigError("'daemon.scene' must be a non-empty string or null")
        camera = SimCameraSettings.from_dict(block.get("camera", {}))
        displays = SimDisplaySettings.from_dict(block.get("sim_displays", {}))
        if headless and displays.enabled():
            raise ConfigError(
                f"'daemon.sim_displays.{displays.enabled()[0]}' needs the viewer: set "
                "'daemon.headless' to false"
            )
        preload = block.get("preload_datasets", True)
        if not isinstance(preload, bool):
            raise ConfigError("'daemon.preload_datasets' must be a boolean")
        timeout = block.get("startup_timeout", 45.0)
        # Finite as well as positive: Python's JSON parser accepts `Infinity` / `NaN`,
        # and either would make the readiness deadline unreachable (specs/core/config.md).
        if not _is_number(timeout) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError(
                "'daemon.startup_timeout' must be a positive finite number"
            )
        return cls(
            spawn=spawn,
            headless=headless,
            scene=scene,
            camera=camera,
            sim_displays=displays,
            preload_datasets=preload,
            startup_timeout=float(timeout),
        )

    @classmethod
    def from_json(cls, text: str) -> DaemonConfig:
        return cls.from_dict(_loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> DaemonConfig:
        return cls.from_dict(_load_file(path))


# --- `audio` block ------------------------------------------------------------------


@dataclass
class AudioSettings:
    """The audio profile applied on media-session start (specs/audio/audio.md)."""

    # A list of ``[name, [values...]]`` pairs — upstream's ``AudioConfig`` shape — or
    # ``None`` for the firmware defaults. Carried verbatim.
    xvf3800: list[Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AudioSettings:
        block = _require_object(data, "audio")
        _reject_unknown_keys(block, "audio", {"xvf3800"})
        profile = block.get("xvf3800")
        if profile is None:
            return cls()
        if not isinstance(profile, list):
            raise ConfigError(
                "'audio.xvf3800' must be a list of [name, [values...]] pairs or null"
            )
        for item in profile:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], list)
            ):
                raise ConfigError(
                    "'audio.xvf3800' items must be [name, [values...]] pairs "
                    f"(a string name and a list of values), got {item!r}"
                )
        return cls(xvf3800=list(profile))

    @classmethod
    def from_json(cls, text: str) -> AudioSettings:
        return cls.from_dict(_loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> AudioSettings:
        return cls.from_dict(_load_file(path))


# --- `face_detection` block ---------------------------------------------------------


@dataclass
class FaceDetectionSettings:
    """Face detection (specs/vision/user_perception.md, specs/core/config.md): which detector
    finds the faces, whether the detection loop runs from session entry, and what the
    detector may cost. Opt-in: with no detector named, nothing is detected and nothing
    tracks."""

    # The detector the bridge runs on the camera feed's frames: None (no detection),
    # "yunet" (the shipped detector, upstream's model) or "custom" (the caller's).
    detector: str | None = None
    # Run the detection loop from session entry, so `bridge.faces` reports who is there.
    enabled: bool = False
    # The width the shipped detector works at (face_detection.DETECT_WIDTH, upstream's own);
    # None = the full frame.
    width: int | None = 320
    # A ceiling on detections per second; None = once per new camera frame.
    target_fps: float | None = None
    # Python only: the custom detector's factory, used when detector is "custom".
    face_detector: Callable[[], FaceDetector] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FaceDetectionSettings:
        block = _require_object(data, "face_detection")
        if "face_detector" in block:
            raise ConfigError(
                "'face_detection.face_detector' is set from code, not from a dict / JSON "
                "config: build FaceDetectionSettings(face_detector=...) or call "
                "set_face_detector(...)"
            )
        _reject_unknown_keys(
            block, "face_detection", {"detector", "enabled", "width", "target_fps"}
        )
        detector = block.get("detector")
        enabled = block.get("enabled", False)
        width = block.get("width", 320)
        target_fps = block.get("target_fps")
        if detector is not None and detector not in FACE_DETECTORS:
            raise ConfigError(
                f"'face_detection.detector' must be null or one of {FACE_DETECTORS}, "
                f"got {detector!r}"
            )
        if not isinstance(enabled, bool):
            raise ConfigError("'face_detection.enabled' must be a boolean")
        if width is not None and (
            isinstance(width, bool) or not isinstance(width, int) or width <= 0
        ):
            raise ConfigError(
                "'face_detection.width' must be null or a positive integer, "
                f"got {width!r}"
            )
        if target_fps is not None and (
            isinstance(target_fps, bool)
            or not isinstance(target_fps, int | float)
            or not math.isfinite(target_fps)
            or target_fps <= 0
        ):
            raise ConfigError(
                "'face_detection.target_fps' must be null or a positive number, "
                f"got {target_fps!r}"
            )
        return cls(
            detector=detector,
            enabled=enabled,
            width=width,
            target_fps=None if target_fps is None else float(target_fps),
        )

    @classmethod
    def from_json(cls, text: str) -> FaceDetectionSettings:
        return cls.from_dict(_loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> FaceDetectionSettings:
        return cls.from_dict(_load_file(path))


# --- `motion` block -----------------------------------------------------------------


@dataclass
class MotionSettings:
    """Everything that shapes the robot's behaviour at rest, applied when the session
    starts (specs/motion/motion.md, specs/core/bridge.md): the loop's own idle modes (``presence``,
    ``idle``, ``idle_move``), the daemon-side mode the bridge arms around them (``wobbling``)
    and the bridge's own head tracking (``tracking``)."""

    # The background behaviour: idle moments are filled with the idle move.
    presence: bool = True
    # Which idle move presence plays: "breathing" (built in), "hold" (a still neutral)
    # or "custom" (the caller's own; the hold while none is registered).
    idle: str = "breathing"
    # Python only: the custom idle move's factory, played whenever idle is "custom".
    # A JSON config names the mode; code supplies the move.
    idle_move: Callable[[], IdleMove] | None = None
    # Audio-reactive head sway, enabled on entry.
    wobbling: bool = True
    # The bridge's head tracker, on from session entry (a mode: no motors needed, but a
    # `face_detection.detector`).
    tracking: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MotionSettings:
        block = _require_object(data, "motion")
        if "idle_move" in block:
            raise ConfigError(
                "'motion.idle_move' is set from code, not from a dict / JSON config: "
                "build MotionSettings(idle_move=...) or call set_idle_move(...)"
            )
        _reject_unknown_keys(
            block, "motion", {"presence", "idle", "wobbling", "tracking"}
        )
        presence = block.get("presence", True)
        idle = block.get("idle", "breathing")
        wobbling = block.get("wobbling", True)
        tracking = block.get("tracking", False)
        if not isinstance(presence, bool):
            raise ConfigError("'motion.presence' must be a boolean")
        if idle not in IDLE_MODES:
            raise ConfigError(
                f"'motion.idle' must be one of {IDLE_MODES}, got {idle!r}"
            )
        if not isinstance(wobbling, bool):
            raise ConfigError("'motion.wobbling' must be a boolean")
        if not isinstance(tracking, bool):
            raise ConfigError("'motion.tracking' must be a boolean")
        return cls(presence=presence, idle=idle, wobbling=wobbling, tracking=tracking)

    @classmethod
    def from_json(cls, text: str) -> MotionSettings:
        return cls.from_dict(_loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> MotionSettings:
        return cls.from_dict(_load_file(path))


# --- the top-level config -----------------------------------------------------------


@dataclass
class ReachyMiniConfig:
    """Everything needed to bring up a ``ReachyMiniBridge`` (specs/core/config.md)."""

    # "real" | "sim" | "fake"
    backend: str = "real"
    # upstream ``ReachyMini(...)`` kwargs, verbatim
    robot: dict[str, Any] = field(default_factory=dict)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    # a tts-engine ``engine`` block, verbatim
    tts: dict[str, Any] | None = None
    audio: AudioSettings = field(default_factory=AudioSettings)
    # face detection: the detector (None / yunet / custom), whether it runs from entry,
    # its cost knobs
    face_detection: FaceDetectionSettings = field(default_factory=FaceDetectionSettings)
    # everything that shapes the robot's behaviour at rest (specs/core/config.md "motion block")
    motion: MotionSettings = field(default_factory=MotionSettings)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReachyMiniConfig:
        """Build (and validate) a config from a parsed dict — the one validation path."""
        top = _require_object(data, "config")
        _reject_unknown_keys(
            top,
            "config",
            {"backend", "robot", "daemon", "tts", "audio", "face_detection", "motion"},
        )

        backend = top.get("backend", "real")
        if backend not in BACKENDS:
            raise ConfigError(f"'backend' must be one of {BACKENDS}, got {backend!r}")

        robot = dict(_require_object(top.get("robot", {}), "robot"))
        _validate_robot_keys(robot)

        daemon = DaemonConfig.from_dict(top.get("daemon", {}))
        if daemon.spawn != "never":
            if backend not in DAEMON_BACKENDS:
                raise ConfigError(
                    f"'daemon.spawn' = {daemon.spawn!r} requires backend 'sim' or 'real' "
                    f"(got {backend!r}): 'fake' has no daemon"
                )
            host = robot.get("host")
            if host is not None and host not in LOOPBACK_HOSTS:
                raise ConfigError(
                    "'robot.host' must be a loopback address when 'daemon.spawn' is "
                    f"{daemon.spawn!r} (the bridge only manages local daemons): "
                    f"'127.0.0.1' or 'localhost', got {host!r}"
                )

        tts_raw = top.get("tts")
        tts: dict[str, Any] | None = None
        if tts_raw is not None:
            tts_block = _require_object(tts_raw, "tts")
            module = tts_block.get("module")
            if not isinstance(module, dict):
                raise ConfigError("'tts.module' must be an object")
            module_type = module.get("type")
            if not module_type or not isinstance(module_type, str):
                raise ConfigError("'tts.module.type' must be a non-empty string")
            tts = dict(tts_block)

        audio = AudioSettings.from_dict(top.get("audio", {}))

        face_detection = FaceDetectionSettings.from_dict(top.get("face_detection", {}))

        motion = MotionSettings.from_dict(top.get("motion", {}))
        if face_detection.detector is None and (
            face_detection.enabled or motion.tracking
        ):
            switches = [
                name
                for name, on in (
                    ("face_detection.enabled", face_detection.enabled),
                    ("motion.tracking", motion.tracking),
                )
                if on
            ]
            raise ConfigError(
                f"{' and '.join(repr(s) for s in switches)} need a face detector, but "
                "'face_detection.detector' is null: name one (e.g. \"yunet\", the shipped "
                "detector) or turn the switch off"
            )

        return cls(
            backend=backend,
            robot=robot,
            daemon=daemon,
            tts=tts,
            audio=audio,
            face_detection=face_detection,
            motion=motion,
        )

    @classmethod
    def from_json(cls, text: str) -> ReachyMiniConfig:
        """Parse a JSON string, then delegate to :meth:`from_dict`."""
        return cls.from_dict(_loads(text))

    @classmethod
    def from_json_file(cls, path: str | Path) -> ReachyMiniConfig:
        """Read a JSON file, then delegate to :meth:`from_dict` (errors name the path)."""
        return cls.from_dict(_load_file(path))

    # --- derived views the bridge uses ---

    @property
    def manages_daemon(self) -> bool:
        """Whether the bridge brings up (or borrows) the daemon itself (``spawn != never``)."""
        return self.daemon.spawn != "never"

    def effective_robot_options(self) -> dict[str, Any]:
        """The kwargs to forward to ``build_robot``: ``robot`` plus, when the bridge
        manages the daemon, the local-daemon defaults for anything the caller left unset."""
        if not self.manages_daemon:
            return dict(self.robot)
        return {**_MANAGED_DAEMON_ROBOT_DEFAULTS, **self.robot}


def _validate_robot_keys(robot: dict[str, Any]) -> None:
    for key, replacement in _RESERVED_ROBOT_KEYS.items():
        if key in robot:
            raise ConfigError(f"'robot.{key}' is reserved; use {replacement}")
    unknown = sorted(set(robot) - _upstream_robot_kwargs())
    if unknown:
        raise ConfigError(
            f"unknown key(s) in 'robot' (not a reachy_mini.ReachyMini parameter): "
            f"{', '.join(unknown)}"
        )


def _upstream_robot_kwargs() -> set[str]:
    # Lazy: the upstream import is the only reason this module would need reachy_mini.
    from reachy_mini import ReachyMini

    return {name for name in inspect.signature(ReachyMini).parameters if name != "self"}
