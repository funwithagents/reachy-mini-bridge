"""Connection seam to the upstream ``reachy_mini`` SDK.

Specified by [specs/client.md](../../specs/client.md). ``real``/``sim`` drive the
upstream ``reachy_mini.ReachyMini`` directly; ``fake`` drives the first-party
``FakeReachyMini`` here, which imports no ``reachy_mini`` and records the commands
it receives. ``RobotClient`` is a union type alias over the two so pyright keeps the
fake in lockstep with the surface the layers above call. ``build_robot`` selects a
backend, importing ``reachy_mini`` lazily so importing this module never pulls in the
heavy upstream package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    # Type-only: loaded at type-check time, never imported when running the fake path.
    from reachy_mini import ReachyMini

__all__ = ["FakeReachyMini", "RobotClient", "build_robot"]

# The XVF3800 voice pipeline: 16 kHz, and float32 stereo capture in the current
# GStreamer backend (see specs/audio.md). The fake reports these via the getters so
# downstream conversion code reads them rather than hardcoding; adjust here if a
# pinned SDK / hardware reports otherwise.
_SAMPLE_RATE = 16000
_CHANNELS = 2
_CHUNK_FRAMES = 160  # 10 ms at 16 kHz


class _FakeBackendStatus:
    """Stand-in for the upstream ``RobotBackendStatus`` (only the field we read)."""

    def __init__(self, motor_control_mode: str) -> None:
        self.motor_control_mode = motor_control_mode


class _FakeStatus:
    """Stand-in for the upstream ``DaemonStatus`` (only the path the api reads)."""

    def __init__(self, motor_control_mode: str) -> None:
        self.backend_status = _FakeBackendStatus(motor_control_mode)


class _FakeDaemonClient:
    """Stand-in for ``ReachyMini.client`` (the daemon client).

    The public ``ReachyMini`` exposes no motor-mode getter; the SDK reads state via
    ``robot.client.get_status()``, so the api does the same and the fake mirrors it.
    """

    def __init__(self) -> None:
        self.motor_control_mode = "disabled"

    def get_status(self) -> _FakeStatus:
        return _FakeStatus(self.motor_control_mode)


class _FakeAudioControl:
    """Stand-in for ``media.audio`` — the XVF3800 tuning / barge-in surface."""

    def __init__(self, commands: list[tuple[str, dict[str, Any]]]) -> None:
        self._commands = commands

    def apply_audio_config(
        self,
        config: object,
        *,
        verify: bool = True,
        write_settle_seconds: float = 0.5,
    ) -> bool:
        self._commands.append(
            (
                "audio.apply_audio_config",
                {
                    "config": config,
                    "verify": verify,
                    "write_settle_seconds": write_settle_seconds,
                },
            )
        )
        return True

    def clear_player(self) -> None:
        self._commands.append(("audio.clear_player", {}))


class _FakeMedia:
    """Stand-in for the upstream ``MediaManager`` — the v1 media slice only."""

    def __init__(self, commands: list[tuple[str, dict[str, Any]]]) -> None:
        self._commands = commands
        self.audio = _FakeAudioControl(commands)
        self._recording = False
        self._playing = False

    # --- input (mic) ---
    def start_recording(self) -> None:
        self._recording = True
        self._commands.append(("media.start_recording", {}))

    def stop_recording(self) -> None:
        self._recording = False
        self._commands.append(("media.stop_recording", {}))

    def get_audio_sample(self) -> npt.NDArray[np.float32]:
        """Return one synthetic capture chunk: float32, ``(frames, channels)``."""
        return np.zeros((_CHUNK_FRAMES, _CHANNELS), dtype=np.float32)

    def get_input_audio_samplerate(self) -> int:
        return _SAMPLE_RATE

    def get_input_channels(self) -> int:
        return _CHANNELS

    # --- output (speaker) ---
    def start_playing(self) -> None:
        self._playing = True
        self._commands.append(("media.start_playing", {}))

    def stop_playing(self) -> None:
        self._playing = False
        self._commands.append(("media.stop_playing", {}))

    def push_audio_sample(self, data: npt.NDArray[np.float32]) -> None:
        self._commands.append(
            ("media.push_audio_sample", {"frames": int(np.asarray(data).shape[0])})
        )

    def get_output_audio_samplerate(self) -> int:
        return _SAMPLE_RATE

    def get_output_channels(self) -> int:
        return _CHANNELS

    def play_sound(self, sound_file: str) -> None:
        self._commands.append(("media.play_sound", {"sound_file": sound_file}))


class FakeReachyMini:
    """First-party stand-in for ``reachy_mini.ReachyMini`` (imports no ``reachy_mini``).

    Implements the v1 consumed slice (see specs/client.md), records every command on
    ``commands`` for tests to assert on, and returns synthetic perception/audio. Motor
    state is reflected on ``client`` (as the real SDK does), not a bespoke getter.
    """

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.client = _FakeDaemonClient()
        self.media = _FakeMedia(self.commands)

    # --- motion / expression ---
    def goto_target(
        self,
        head: npt.NDArray[np.float64] | None = None,
        antennas: npt.NDArray[np.float64] | list[float] | None = None,
        duration: float = 0.5,
        method: Any = None,
        body_yaw: float | None = 0.0,
    ) -> None:
        self.commands.append(
            (
                "goto_target",
                {
                    "head": head,
                    "antennas": antennas,
                    "duration": duration,
                    "body_yaw": body_yaw,
                },
            )
        )

    async def async_play_move(
        self,
        move: object,
        play_frequency: float = 100.0,
        initial_goto_duration: float = 0.0,
        sound: bool = True,
    ) -> None:
        self.commands.append(
            (
                "async_play_move",
                {
                    "move": move,
                    "initial_goto_duration": initial_goto_duration,
                    "sound": sound,
                },
            )
        )

    def start_head_tracking(self, weight: float = 1.0) -> None:
        self.commands.append(("start_head_tracking", {"weight": weight}))

    def stop_head_tracking(self) -> None:
        self.commands.append(("stop_head_tracking", {}))

    # --- motors (setters update the mode reported by client.get_status()) ---
    def enable_motors(self, ids: list[str] | None = None) -> None:
        self.client.motor_control_mode = "enabled"
        self.commands.append(("enable_motors", {"ids": ids}))

    def disable_motors(self, ids: list[str] | None = None) -> None:
        self.client.motor_control_mode = "disabled"
        self.commands.append(("disable_motors", {"ids": ids}))

    def enable_gravity_compensation(self) -> None:
        self.client.motor_control_mode = "gravity_compensation"
        self.commands.append(("enable_gravity_compensation", {}))

    # --- lifecycle ---
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.commands.append(("__exit__", {}))


# A readable name for "either backend"; the union keeps the fake honest under pyright.
type RobotClient = ReachyMini | FakeReachyMini

_BACKENDS = ("real", "sim", "fake")


def build_robot(backend: str = "real", **opts: Any) -> RobotClient:
    """Build (and connect) the robot for ``backend``.

    ``fake`` returns a :class:`FakeReachyMini`; ``real``/``sim`` lazily import and
    construct the upstream ``reachy_mini.ReachyMini`` (``sim`` sets ``use_sim=True``).
    ``opts`` forwards upstream connection options (``host``, ``port``, ``timeout``, …).
    """
    if backend not in _BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; expected one of {_BACKENDS}")
    if backend == "fake":
        return FakeReachyMini()
    from reachy_mini import ReachyMini  # lazy: only the real/sim path imports upstream

    return ReachyMini(use_sim=(backend == "sim"), **opts)
