"""High-level interaction API (specs/api.md).

``ReachyMiniApi`` is the intention-level surface for driving the robot in **human
units** (degrees, seconds, named emotions), orchestrating the lower-level
[robot](robot.py) primitives into single semantic verbs. It is **async-native**
because audio forces it (see [audio](audio.py)): synthesis is async and a live mic
stream runs concurrently with playback and motion on one event loop, so the upstream
SDK's blocking calls run under ``asyncio.to_thread``.

v1 is the smallest verb set that makes the robot a conversational, face-following
presence — talk, listen, express, follow a face, manage motors. Manual movement/gaze
and rich perception are deferred to post-v1 (see specs/api.md).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Self

from .audio import MediaSession
from .errors import BridgeError, MotorsNotEnabledError
from .fake_reachy_mini import FakeReachyMini
from .robot import build_robot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np
    import numpy.typing as npt

    from .audio import SpeechSynthesizer
    from .robot import AnyReachyMini

__all__ = ["ReachyMiniApi"]

# Motor torque states, as the caller-facing single verb takes/returns them.
_MOTOR_STATES = ("enabled", "disabled", "gravity_compensation")

# Stub emotion names for the fake/offline path (no HuggingFace access) — enough to
# exercise list_emotions / play_emotion in tests without the real dataset.
_FAKE_EMOTIONS = ("happy", "sad", "curious")


class _FakeRecordedMoves:
    """Offline stand-in for the upstream ``RecordedMoves`` on the ``fake`` backend.

    ``get`` returns the name itself as the "move" (the fake robot just records whatever
    it is handed), and raises ``ValueError`` for an unknown name — mirroring the real
    ``RecordedMoves.get`` contract so callers see the same failure either way.
    """

    def list_moves(self) -> list[str]:
        return list(_FAKE_EMOTIONS)

    def get(self, move_name: str) -> str:
        if move_name not in _FAKE_EMOTIONS:
            raise ValueError(f"Move {move_name} not found in emotions library")
        return move_name


class ReachyMiniApi:
    """Async-native, intention-level API over a robot backend.

    Construct from a backend string (``"real"`` | ``"sim"`` | ``"fake"``); the robot is
    built internally, so the api is fully unit-testable on ``fake``. Use as an async
    context manager to open (and tear down) the connection and the shared media session::

        async with ReachyMiniApi("fake") as api:
            await api.say("hello", synth)

    The underlying robot stays reachable as :attr:`robot` (a.k.a. :attr:`raw`) — the
    escape hatch to the full native API, and how tests assert on the fake.
    """

    def __init__(
        self,
        backend: str = "real",
        *,
        synthesizer: SpeechSynthesizer | None = None,
        audio_config: object | None = None,
        **opts: Any,
    ) -> None:
        self._robot: AnyReachyMini = build_robot(backend, **opts)
        self._synthesizer = synthesizer
        self._media = MediaSession(self._robot, audio_config=audio_config)
        self._recorded_moves: Any = None  # lazy, cached once per connection

    @classmethod
    def connect(
        cls,
        backend: str = "real",
        *,
        synthesizer: SpeechSynthesizer | None = None,
        audio_config: object | None = None,
        **opts: Any,
    ) -> ReachyMiniApi:
        """Convenience mirror of the constructor (see class docstring)."""
        return cls(backend, synthesizer=synthesizer, audio_config=audio_config, **opts)

    # --- escape hatch ---

    @property
    def robot(self) -> AnyReachyMini:
        """The underlying robot object — full native ``ReachyMini`` on real/sim."""
        return self._robot

    @property
    def raw(self) -> AnyReachyMini:
        """Alias of :attr:`robot`."""
        return self._robot

    # --- lifecycle ---

    async def __aenter__(self) -> Self:
        self._robot.__enter__()
        await self._media.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._media.__aexit__(*exc)
        self._robot.__exit__(*exc)

    # --- motors / torque ---

    async def get_motors_state(self) -> str:
        """Read the current torque state: one of ``enabled`` / ``disabled`` /
        ``gravity_compensation``.

        Reads the daemon's ``motor_control_mode`` the way the SDK itself does (via the
        daemon client), so it reflects the *actual* state, not an assumption.
        """
        status = await asyncio.to_thread(self._robot.client.get_status)
        backend = status.backend_status
        if backend is None:
            raise BridgeError(
                "daemon reported no backend status (motor state unknown); "
                "is the robot connected and awake?"
            )
        mode = backend.motor_control_mode
        # Real reports a str-Enum (read .value); the fake reports a plain str.
        return str(getattr(mode, "value", mode))

    async def set_motors_state(self, state: str) -> None:
        """Set torque: ``enabled`` (ready to move), ``disabled`` (limp), or
        ``gravity_compensation`` (a gentle rest — the head holds position).

        Decoupled from the connection: the state, once set, holds until changed. Raises
        ``ValueError`` for an unknown state.
        """
        if state == "enabled":
            await asyncio.to_thread(self._robot.enable_motors)
        elif state == "disabled":
            await asyncio.to_thread(self._robot.disable_motors)
        elif state == "gravity_compensation":
            await asyncio.to_thread(self._robot.enable_gravity_compensation)
        else:
            raise ValueError(
                f"unknown motor state {state!r}; expected one of {_MOTOR_STATES}"
            )

    async def _require_motors_enabled(self, verb: str) -> None:
        state = await self.get_motors_state()
        if state != "enabled":
            raise MotorsNotEnabledError(
                f"{verb} requires motors 'enabled', but they are {state!r}; "
                "call set_motors_state('enabled') first"
            )

    # --- expression ---

    async def list_emotions(self) -> list[str]:
        """Names of the recorded moves in the emotions library."""
        moves = await self._get_recorded_moves()
        return list(moves.list_moves())

    async def play_emotion(self, name: str) -> None:
        """Play a named recorded move from the emotions library.

        Moves the robot, so it requires motors ``enabled`` (raises
        :class:`MotorsNotEnabledError` otherwise). Raises ``ValueError`` for an unknown
        emotion name.
        """
        await self._require_motors_enabled("play_emotion")
        moves = await self._get_recorded_moves()
        move = moves.get(name)  # ValueError on unknown name
        await self._robot.async_play_move(move)

    async def _get_recorded_moves(self) -> Any:
        if self._recorded_moves is None:
            self._recorded_moves = await asyncio.to_thread(self._load_recorded_moves)
        return self._recorded_moves

    def _load_recorded_moves(self) -> Any:
        # Blocking disk/network IO; built lazily, once, off the event loop. The fake
        # path stays offline (no HuggingFace) with a stubbed library.
        if isinstance(self._robot, FakeReachyMini):
            return _FakeRecordedMoves()
        from reachy_mini.motion.recorded_move import (
            DEFAULT_EMOTIONS_DATASET,
            RecordedMoves,
        )

        return RecordedMoves(DEFAULT_EMOTIONS_DATASET)

    # --- attention / gaze (autonomous, daemon-side) ---

    async def start_head_tracking(self, weight: float = 1.0) -> None:
        """Have the robot autonomously keep a detected face centered.

        Moves the robot, so it requires motors ``enabled`` (raises
        :class:`MotorsNotEnabledError` otherwise).
        """
        await self._require_motors_enabled("start_head_tracking")
        await asyncio.to_thread(self._robot.start_head_tracking, weight)

    async def stop_head_tracking(self) -> None:
        """Stop the autonomous face tracker."""
        await asyncio.to_thread(self._robot.stop_head_tracking)

    # --- audio out ---

    async def say(self, text: str, synth: SpeechSynthesizer | None = None) -> None:
        """Synthesize ``text`` and play it through the robot speaker.

        Uses ``synth`` if given, else the synthesizer configured at construction. Raises
        :class:`BridgeError` if neither is available. Needs no motors.
        """
        chosen = synth or self._synthesizer
        if chosen is None:
            raise BridgeError(
                "say requires a SpeechSynthesizer: pass one, or configure a default "
                "synthesizer at construction (e.g. the tts extra's TTSEngineSynthesizer)"
            )
        await self._media.say(text, chosen)

    async def play_sound(self, sound_file: str) -> None:
        """Play a sound file / built-in sound through the robot speaker."""
        await asyncio.to_thread(self._robot.media.play_sound, sound_file)

    # --- audio in (microphone) ---

    def audio_input(self, *, mono: bool = True) -> AsyncIterator[bytes]:
        """Async iterator of echo-cancelled mic PCM (int16 LE) for the caller's own ASR.

        ``mono=True`` (default) is the ASR drop-in; ``mono=False`` yields the raw
        interleaved capture at :attr:`mic_channels` channels. See specs/audio.md.
        """
        return self._media.audio_input(mono=mono)

    @property
    def mic_sample_rate(self) -> int:
        """Sample rate (Hz) of :meth:`audio_input` — configure your ASR to it."""
        return self._media.mic_sample_rate

    @property
    def mic_channels(self) -> int:
        """Raw capture channel count (the ``mono=False`` layout)."""
        return self._media.mic_channels

    # --- perception (camera) ---

    async def get_camera_frame(self) -> npt.NDArray[np.uint8] | None:
        """Grab the latest camera frame as a numpy BGR array (``HxWx3``, uint8).

        A perception verb: it returns the raw frame object, not a JSON-friendly value
        (the base64/JPEG encoding for a model is the tools layer's job). Mirrors the
        upstream ``media.get_frame`` exactly — returns ``None`` when no frame is
        available yet (e.g. the headless sim has no GL context). Needs no motors.
        """
        return await asyncio.to_thread(self._robot.media.get_frame)
