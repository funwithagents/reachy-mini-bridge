"""First-party fake stand-in for the upstream ``reachy_mini.ReachyMini``.

Specified by [specs/robot.md](../../specs/robot.md). ``FakeReachyMini`` implements the
v1 consumed slice the layers above call, imports no ``reachy_mini`` itself, records
every command it receives (so tests assert on them), and returns synthetic
perception/audio. It is the backbone of the deterministic ``tests/`` tier — no daemon,
hardware, or network — and runs the full api/audio stack offline for dev and demos.

The union alias and backend factory that select between this fake and the real robot
live in [robot.py](robot.py).
"""

from __future__ import annotations

from typing import Any, Self

import numpy as np
import numpy.typing as npt

__all__ = ["FakeReachyMini"]

# The XVF3800 voice pipeline: 16 kHz float32 stereo capture in 10 ms blocks, as the
# sim and a real Reachy Mini Lite report (see specs/audio.md). The fake reports these
# via the getters so downstream conversion code reads them rather than hardcoding.
_SAMPLE_RATE = 16000
_CHANNELS = 2
_CHUNK_FRAMES = 160  # 10 ms at 16 kHz

# Synthetic camera frame size (small; just enough for tests to assert real HxWx3 shape).
_FRAME_WIDTH = 64
_FRAME_HEIGHT = 48


class _FakeBackendStatus:
    """Stand-in for the upstream ``RobotBackendStatus`` (only the field we read)."""

    def __init__(self, motor_control_mode: str) -> None:
        self.motor_control_mode = motor_control_mode


class _FakeStatus:
    """Stand-in for the upstream ``DaemonStatus`` (only the fields the api reads)."""

    def __init__(
        self,
        motor_control_mode: str,
        *,
        simulation_enabled: bool,
        mockup_sim_enabled: bool,
    ) -> None:
        self.backend_status = _FakeBackendStatus(motor_control_mode)
        self.simulation_enabled = simulation_enabled
        self.mockup_sim_enabled = mockup_sim_enabled


class _FakeDaemonClient:
    """Stand-in for ``ReachyMini.client`` (the daemon client).

    The public ``ReachyMini`` exposes no motor-mode getter; the SDK reads state via
    ``robot.client.get_status()``, so the api does the same and the fake mirrors it.
    """

    def __init__(self) -> None:
        self.motor_control_mode = "disabled"
        # The daemon's kinematics engine (upstream serves it over HTTP, not the SDK):
        # Placo, so gravity compensation is accepted; tests flip it to exercise refusal.
        self.kinematics_engine = "Placo"
        self.simulation_enabled = False
        self.mockup_sim_enabled = False

    def get_status(self) -> _FakeStatus:
        return _FakeStatus(
            self.motor_control_mode,
            simulation_enabled=self.simulation_enabled,
            mockup_sim_enabled=self.mockup_sim_enabled,
        )


class _FakeAudioControl:
    """Stand-in for ``media.audio`` — the XVF3800 tuning / barge-in surface."""

    def __init__(self, commands: list[tuple[str, dict[str, Any]]]) -> None:
        self._commands = commands

    def apply_audio_config(
        self,
        config: object,
        *,
        verify: bool = True,
        write_settle_seconds: float = 0.1,
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

    # --- camera ---
    def get_frame(self) -> npt.NDArray[np.uint8]:
        """Return one synthetic camera frame: BGR, ``(H, W, 3)`` uint8.

        A deterministic horizontal gradient (not a flat constant) so tests assert real
        structure. Mirrors the upstream ``media.get_frame`` shape; the fake always has a
        frame ready, so unlike the real daemon it never returns ``None``. Not recorded
        as a command — a perception getter, like ``get_audio_sample``.
        """
        frame = np.zeros((_FRAME_HEIGHT, _FRAME_WIDTH, 3), dtype=np.uint8)
        ramp = np.linspace(0, 255, _FRAME_WIDTH, dtype=np.uint8)
        frame[:, :, 0] = ramp  # B channel ramps left→right
        return frame


class FakeReachyMini:
    """First-party stand-in for ``reachy_mini.ReachyMini`` (imports no ``reachy_mini``).

    Implements the v1 consumed slice (see specs/robot.md), records every command on
    ``commands`` for tests to assert on, and returns synthetic perception/audio. Motor
    state is reflected on ``client`` (as the real SDK does), not a bespoke getter.
    """

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.client = _FakeDaemonClient()
        self.media = _FakeMedia(self.commands)

    # --- motion / expression ---
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

    # --- audio-reactive head wobbling (a mode; moves nothing on the fake) ---
    def enable_wobbling(self) -> None:
        self.commands.append(("enable_wobbling", {}))

    def disable_wobbling(self) -> None:
        self.commands.append(("disable_wobbling", {}))

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
