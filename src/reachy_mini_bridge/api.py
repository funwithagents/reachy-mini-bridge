"""High-level interaction API (specs/api.md).

``ReachyMiniApi`` is the intention-level surface for driving the robot in **human
units** (degrees, seconds, named emotions), orchestrating the lower-level
[robot](robot.py) primitives into single semantic verbs. It is **async-native**
because audio forces it (see [audio](audio.py)): synthesis is async and a live mic
stream runs concurrently with playback and motion on one event loop, so the upstream
SDK's blocking calls run under ``asyncio.to_thread``.

Constructed from a [``ReachyMiniConfig``](config.py) (or a backend-string shorthand for
one); ``async with`` brings up the managed daemon (when configured), the robot, the
media session, and the motion session ([motion](motion.py) — the one ``set_target``
writer, playing emotions and the idle behaviour) in that order on an
``AsyncExitStack`` — see "Lifecycle" in the spec.

v1 is the smallest verb set that makes the robot a conversational, face-following
presence — talk, listen, express, follow a face, manage motors, and stay visibly alive
in between. Manual movement/gaze and rich perception are deferred to post-v1 (see
specs/api.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from reachy_mini.motion.move import Move

from . import daemon as _daemon
from .audio import MediaSession, TTSEngineSynthesizer, cancel_safe_step
from .config import ReachyMiniConfig
from .errors import (
    BridgeError,
    ConfigError,
    GravityCompensationUnsupportedError,
    MotorsNotEnabledError,
)
from .fake_reachy_mini import FakeReachyMini
from .motion import NEUTRAL_ANTENNAS, NEUTRAL_BODY_YAW, NEUTRAL_HEAD, MotionSession
from .robot import build_robot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np
    import numpy.typing as npt

    from .audio import SpeechSynthesizer
    from .robot import AnyReachyMini

__all__ = ["ReachyMiniApi"]

_logger = logging.getLogger(__name__)

# Motor torque states, as the caller-facing single verb takes/returns them.
_MOTOR_STATES = ("enabled", "disabled", "gravity_compensation")

# The only kinematics engine on which the robot daemon accepts gravity compensation.
_GRAVITY_COMPENSATION_ENGINE = "Placo"
_DAEMON_HTTP_TIMEOUT_S = 2.0


def _fetch_json(url: str) -> Any:
    """GET ``url`` from the daemon's HTTP API and decode its JSON body (patched by tests)."""
    with urllib.request.urlopen(url, timeout=_DAEMON_HTTP_TIMEOUT_S) as response:
        return json.load(response)


def _daemon_kinematics_engine(robot: AnyReachyMini) -> str:
    """The kinematics engine the robot's daemon runs (e.g. ``"Placo"``). Blocking.

    Upstream exposes no SDK getter; the daemon serves it at ``/api/kinematics/info``. The
    fake reports it on its daemon client stand-in. Raises on an unreachable daemon or an
    unexpected payload. Shared with the testing harness's capability probe.
    """
    if isinstance(robot, FakeReachyMini):
        return robot.client.kinematics_engine
    client = robot.client
    info = _fetch_json(f"http://{client.host}:{client.port}/api/kinematics/info")
    return str(info["info"]["engine"])


# Stub emotion names for the fake/offline path (no HuggingFace access) — enough to
# exercise list_emotions / play_emotion in tests without the real dataset.
_FAKE_EMOTIONS = ("happy", "sad", "curious")
_FAKE_MOVE_DURATION_S = 0.3


class _FakeRecordedMove(Move):
    """Offline stand-in for an upstream ``RecordedMove``: a real short trajectory (a
    small rise-and-fall in z) so the loop path runs in ``tests/``. ``sad`` has no sound
    so both `play_emotion` paths are testable."""

    def __init__(self, name: str, sound_path: Path | None = None) -> None:
        self.name = name
        self._sound_path = sound_path

    @property
    def duration(self) -> float:
        return _FAKE_MOVE_DURATION_S

    @property
    def sound_path(self) -> Path | None:
        return self._sound_path

    def evaluate(
        self, t: float
    ) -> tuple[
        npt.NDArray[np.float64] | None, npt.NDArray[np.float64] | None, float | None
    ]:
        head = NEUTRAL_HEAD.copy()
        head[2, 3] = 0.01 * math.sin(math.pi * t / self.duration)
        return head, NEUTRAL_ANTENNAS.copy(), NEUTRAL_BODY_YAW


class _FakeRecordedMoves:
    """Offline stand-in for the upstream ``RecordedMoves`` on the ``fake`` backend.

    ``get`` raises ``ValueError`` for an unknown name — mirroring the real
    ``RecordedMoves.get`` contract so callers see the same failure either way.
    """

    def list_moves(self) -> list[str]:
        return list(_FAKE_EMOTIONS)

    def get(self, move_name: str) -> _FakeRecordedMove:
        if move_name not in _FAKE_EMOTIONS:
            raise ValueError(f"Move {move_name} not found in emotions library")
        sound = None if move_name == "sad" else Path(f"{move_name}.ogg")
        return _FakeRecordedMove(move_name, sound_path=sound)


class ReachyMiniApi:
    """Async-native, intention-level API over a robot backend.

    Construct from a :class:`ReachyMiniConfig` — or a bare backend string (``"real"`` |
    ``"sim"`` | ``"fake"``), shorthand for ``ReachyMiniConfig(backend=...)``. Nothing
    connects at construction; use it as an async context manager to bring up the daemon
    (when the config manages one), the robot, and the shared media session, and to tear
    them down::

        async with ReachyMiniApi("fake") as api:
            await api.say("hello", synth)

        async with ReachyMiniApi.from_json_file("robot.json") as api:
            await api.say("hello")  # default synthesizer from the config's `tts` block

    While entered, the underlying robot stays reachable as :attr:`robot` (a.k.a.
    :attr:`raw`) — the escape hatch to the full native API, and how tests assert on
    the fake.
    """

    def __init__(
        self,
        config: ReachyMiniConfig | str = "real",
        *,
        synthesizer: SpeechSynthesizer | None = None,
    ) -> None:
        self._config = (
            ReachyMiniConfig(backend=config) if isinstance(config, str) else config
        )
        # An explicit synthesizer wins over the config's `tts` block (then not consumed).
        self._synthesizer: SpeechSynthesizer | None
        self._synthesizer_error: Exception | None
        if synthesizer is not None:
            self._synthesizer, self._synthesizer_error = synthesizer, None
        else:
            self._synthesizer, self._synthesizer_error = _default_synthesizer(
                self._config.tts
            )
            if self._synthesizer_error is not None:
                _logger.warning(
                    "the configured `tts` block could not be built: %s",
                    self._synthesizer_error,
                )
        self._robot: AnyReachyMini | None = None
        self._media: MediaSession | None = None
        self._motion: MotionSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        # The emotions library: loaded lazily, once per connection (see _get_recorded_moves).
        self._recorded_moves_future: asyncio.Future[Any] | None = None
        # The bridge's record of the wobbling mode (upstream has no getter).
        self._wobbling = False
        # The motion loop's switches (specs/motion.md), initialized from the config and
        # reset to it on exit; set_presence/set_breathing change them while entered.
        self._presence = self._config.motion.presence
        self._breathing = self._config.motion.breathing
        # The api's own record of the tracking weight last requested (as for wobbling),
        # so play_emotion can dip it to 0 for a move and restore it afterwards. None
        # means tracking is not currently active (paused for lack of motors, or off).
        self._tracking_weight: float | None = None
        # Whether tracking should be on whenever motors allow it (the config default,
        # or the caller's last start_head_tracking/stop_head_tracking) — mirrors
        # _presence/_breathing; distinct from _tracking_weight because tracking, unlike
        # presence/breathing, needs motors enabled to actually engage.
        self._tracking_wanted = self._config.motion.tracking

    # --- config-based constructors (mirroring ReachyMiniConfig's trio) ---

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, synthesizer: SpeechSynthesizer | None = None
    ) -> ReachyMiniApi:
        """Build the api from a parsed config dict (see :meth:`ReachyMiniConfig.from_dict`)."""
        return cls(ReachyMiniConfig.from_dict(data), synthesizer=synthesizer)

    @classmethod
    def from_json(
        cls, text: str, *, synthesizer: SpeechSynthesizer | None = None
    ) -> ReachyMiniApi:
        """Build the api from a JSON config string."""
        return cls(ReachyMiniConfig.from_json(text), synthesizer=synthesizer)

    @classmethod
    def from_json_file(
        cls, path: str | Path, *, synthesizer: SpeechSynthesizer | None = None
    ) -> ReachyMiniApi:
        """Build the api from a JSON config file."""
        return cls(ReachyMiniConfig.from_json_file(path), synthesizer=synthesizer)

    @property
    def config(self) -> ReachyMiniConfig:
        """The config this api was built from."""
        return self._config

    @property
    def synthesizer_error(self) -> Exception | None:
        """The cause when the config's `tts` block failed to build a synthesizer.

        ``None`` when the voice built successfully, when an explicit ``synthesizer=``
        was passed (the block is then not consumed), or when there is no `tts` block.
        The api still comes up with no voice; `say` raises :class:`BridgeError`
        chained to this cause. A host that wants hard failure checks this after
        construction and raises.
        """
        return self._synthesizer_error

    # --- escape hatch ---

    @property
    def robot(self) -> AnyReachyMini:
        """The underlying robot object — full native ``ReachyMini`` on real/sim.

        Available only while entered (the robot is built and connected on
        ``__aenter__``); raises :class:`BridgeError` otherwise.
        """
        if self._robot is None:
            raise BridgeError(
                "the robot is only available inside `async with ReachyMiniApi(...)`"
            )
        return self._robot

    @property
    def raw(self) -> AnyReachyMini:
        """Alias of :attr:`robot`."""
        return self.robot

    def _require_media(self) -> MediaSession:
        if self._media is None:
            raise BridgeError(
                "the media session is only available inside `async with ReachyMiniApi(...)`"
            )
        return self._media

    def _require_motion(self) -> MotionSession:
        if self._motion is None:
            raise BridgeError(
                "the motion loop is only available inside `async with ReachyMiniApi(...)`"
            )
        return self._motion

    # --- lifecycle ---

    async def __aenter__(self) -> Self:
        if self._exit_stack is not None:
            raise BridgeError("ReachyMiniApi is already entered")
        cfg = self._config
        stack = AsyncExitStack()
        try:
            if cfg.manages_daemon:
                opts = cfg.effective_robot_options()
                daemon_cm = _daemon.managed_daemon(
                    cfg.daemon,
                    host=opts["host"],
                    port=opts["port"],
                    backend=cfg.backend,
                )
                await cancel_safe_step(
                    daemon_cm.__enter__,
                    lambda _: daemon_cm.__exit__(None, None, None),
                )
                stack.push_async_callback(
                    asyncio.to_thread, daemon_cm.__exit__, None, None, None
                )

            # Build and enter are one step: upstream's `ReachyMini` connects in its
            # constructor, so a built-but-dropped robot is already a leaked connection.
            def connect() -> AnyReachyMini:
                built = build_robot(cfg.backend, **cfg.effective_robot_options())
                built.__enter__()
                return built

            robot = await cancel_safe_step(
                connect, lambda r: r.__exit__(None, None, None)
            )
            stack.push_async_callback(
                asyncio.to_thread, lambda: robot.__exit__(None, None, None)
            )
            self._robot = robot
            media = MediaSession(robot, audio_config=cfg.audio.xvf3800)
            await stack.enter_async_context(media)
            self._media = media
            # Registered before the enable, so a failing enable still unwinds cleanly
            # (the mode is still off, so the callback is a no-op). It holds the robot
            # itself: __aexit__ clears `self._robot` before the stack closes.
            stack.push_async_callback(self._disable_wobbling_if_on, robot)
            stack.push_async_callback(self._stop_tracking_if_on, robot)
            if cfg.motion.wobbling:
                await self.set_wobbling(True)
            # Entered after wobbling, exits first (specs/motion.md "Lifecycle"): the
            # stack unwinds in reverse, so the loop eases to neutral before wobbling
            # (and everything else) tears down.
            motion = MotionSession(
                robot, presence=self._presence, breathing=self._breathing
            )
            await stack.enter_async_context(motion)
            self._motion = motion
            if await self.get_motors_state() == "enabled":
                motion.resume()
                if self._tracking_wanted:
                    await self._start_tracking_now()
        except BaseException:
            self._robot = None
            self._media = None
            self._motion = None
            self._wobbling = False
            await stack.aclose()
            raise
        self._exit_stack = stack.pop_all()
        return self

    async def __aexit__(self, *exc: object) -> None:
        stack = self._exit_stack
        # Read as closed even if a teardown step raises.
        self._exit_stack = None
        self._robot = None
        self._media = None
        self._motion = None
        self._recorded_moves_future = None
        try:
            if stack is not None:
                await stack.aclose()
        finally:
            self._wobbling = False
            self._tracking_weight = None
            self._tracking_wanted = self._config.motion.tracking
            self._presence = self._config.motion.presence
            self._breathing = self._config.motion.breathing

    async def _disable_wobbling_if_on(self, robot: AnyReachyMini) -> None:
        # The daemon-side switch is shared across clients: never leave it armed.
        if self._wobbling:
            await asyncio.to_thread(robot.disable_wobbling)
            self._wobbling = False

    async def _stop_tracking_if_on(self, robot: AnyReachyMini) -> None:
        # The daemon-side switch is shared across clients: never leave it armed.
        if self._tracking_weight is not None:
            await asyncio.to_thread(robot.stop_head_tracking)
            self._tracking_weight = None

    async def _start_tracking_now(self, weight: float = 1.0) -> None:
        """Start tracking without re-checking motor state: the caller (__aenter__ or
        set_motors_state) has just confirmed motors are enabled, and a second read
        risks the daemon's ~0.2s status lag reporting the pre-change state."""
        await asyncio.to_thread(self.robot.start_head_tracking, weight)
        self._tracking_weight = weight
        self._tracking_wanted = True

    # --- motors / torque ---

    async def get_motors_state(self) -> str:
        """Read the current torque state: one of ``enabled`` / ``disabled`` /
        ``gravity_compensation``.

        Reads the daemon's ``motor_control_mode`` the way the SDK itself does (via the
        daemon client), so it reflects the *actual* state, not an assumption.
        """
        status = await asyncio.to_thread(self.robot.client.get_status)
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
        ``ValueError`` for an unknown state. ``gravity_compensation`` needs a robot daemon
        on the Placo kinematics engine; on any other engine it raises
        :class:`GravityCompensationUnsupportedError` without sending anything, because
        such a daemon would reject the mode by dropping the connection. A simulation
        ignores motor modes, so there the mode is sent unchecked.

        Also drives the motion loop (specs/motion.md "Motors"): ``enabled`` resumes it
        — re-anchored on the present pose, so the head eases into the idle move rather
        than snapping — and arms face tracking too if it's wanted but not yet active
        (the config default, or a ``start_head_tracking`` call made before motors were
        enabled); the two resting states pause the loop only.
        """
        robot = self.robot
        if state == "enabled":
            await asyncio.to_thread(robot.enable_motors)
        elif state == "disabled":
            await asyncio.to_thread(robot.disable_motors)
        elif state == "gravity_compensation":
            await self._require_gravity_compensation_support()
            await asyncio.to_thread(robot.enable_gravity_compensation)
        else:
            raise ValueError(
                f"unknown motor state {state!r}; expected one of {_MOTOR_STATES}"
            )
        if state == "enabled":
            self._require_motion().resume()
            if self._tracking_wanted and self._tracking_weight is None:
                await self._start_tracking_now()
        else:
            self._require_motion().pause()

    async def _require_gravity_compensation_support(self) -> None:
        robot = self.robot
        status = await asyncio.to_thread(robot.client.get_status)
        if status.simulation_enabled or status.mockup_sim_enabled:
            return  # the sim daemon ignores motor modes; nothing to protect
        try:
            engine = await asyncio.to_thread(_daemon_kinematics_engine, robot)
        except Exception as e:
            raise GravityCompensationUnsupportedError(
                "could not read the daemon's kinematics engine, so gravity compensation "
                "was not sent (a daemon not on Placo would drop this connection)"
            ) from e
        if engine != _GRAVITY_COMPENSATION_ENGINE:
            raise GravityCompensationUnsupportedError(
                f"gravity compensation needs the daemon's {_GRAVITY_COMPENSATION_ENGINE} "
                f"kinematics engine, but it runs {engine!r}; install "
                "reachy-mini[placo_kinematics] and start the daemon with "
                "--kinematics-engine Placo. Nothing was sent: the daemon would reject the "
                "mode by dropping this connection"
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

        The move plays through the bridge's motion loop (specs/motion.md "Emotions
        through the loop"), never through upstream's ``async_play_move``: the loop is
        the one writer of the robot's target. Primaries are exclusive and FIFO, so a
        second ``play_emotion`` while one plays waits its turn. The loop blends into
        the move's start pose, starts its sidecar sound as the trajectory starts, and
        plays it for its duration; **completes when the trajectory has played** — the
        return to neutral that follows is the idle behaviour's, not the verb's.

        Around the move, face tracking (weight 0) and wobbling are paused and restored
        on every exit path — completion, cancel, or failure — because at full tracking
        weight the daemon discards the head target, and because the emotion's own
        sound would otherwise sway the head on top of the choreography.

        Cancelling the task stops the emotion — motion and sound — and leaves the head
        where the cancel caught it, with the session still open; the same stop runs
        when the move fails. See specs/api.md "Cancellation".

        Moves the robot, so it requires motors ``enabled`` (raises
        :class:`MotorsNotEnabledError` otherwise). Raises ``ValueError`` for an unknown
        emotion name.
        """
        await self._require_motors_enabled("play_emotion")
        moves = await self._get_recorded_moves()
        move = moves.get(name)  # ValueError on unknown name
        media = self._require_media()
        motion = self._require_motion()
        robot = self.robot
        sound_path = getattr(move, "sound_path", None)
        # Pause the two daemon-side layers for the move; restored below on every exit
        # path, to their *current* record (specs/motion.md "Emotions through the loop").
        if self._tracking_weight is not None:
            await asyncio.to_thread(robot.start_head_tracking, 0.0)
        if self._wobbling:
            await asyncio.to_thread(robot.disable_wobbling)
        future = motion.submit(move, None if sound_path is None else Path(sound_path))
        try:
            await asyncio.wrap_future(future)
        except BaseException:
            future.cancel()  # idempotent; wrap_future already propagated a task cancel
            if sound_path is not None:
                media.stop_sound()
            raise
        finally:
            await self._restore_layers_after_move()

    async def _restore_layers_after_move(self) -> None:
        robot = self.robot
        if self._wobbling:
            try:
                await asyncio.to_thread(robot.enable_wobbling)
            except Exception as e:  # noqa: BLE001 - never mask the verb's own outcome
                _logger.warning("could not restore wobbling after the emotion: %s", e)
        if self._tracking_weight is not None:
            try:
                await asyncio.to_thread(
                    robot.start_head_tracking, self._tracking_weight
                )
            except Exception as e:  # noqa: BLE001 - never mask the verb's own outcome
                _logger.warning(
                    "could not restore head tracking after the emotion: %s", e
                )

    async def _get_recorded_moves(self) -> Any:
        # One load per connection, shielded: a cancelled first caller does not
        # discard the load, and the next caller awaits the same in-flight future.
        future = self._recorded_moves_future
        if future is None:
            future = asyncio.ensure_future(asyncio.to_thread(self._load_recorded_moves))
            self._recorded_moves_future = future
        try:
            return await asyncio.shield(future)
        except Exception:
            # A failed load is not cached: the next call retries.
            if self._recorded_moves_future is future:
                self._recorded_moves_future = None
            raise

    def _load_recorded_moves(self) -> Any:
        # Blocking disk/network IO; built lazily, once, off the event loop. The fake
        # path stays offline (no HuggingFace) with a stubbed library.
        if isinstance(self.robot, FakeReachyMini):
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
        :class:`MotorsNotEnabledError` otherwise). The bridge keeps the weight last
        requested (its own record, as for wobbling) so :meth:`play_emotion` can dip it
        to ``0`` for a move and restore it afterwards.
        """
        await self._require_motors_enabled("start_head_tracking")
        await self._start_tracking_now(weight)

    async def stop_head_tracking(self) -> None:
        """Stop the autonomous face tracker."""
        await asyncio.to_thread(self.robot.stop_head_tracking)
        self._tracking_weight = None
        self._tracking_wanted = False

    @property
    def tracking(self) -> bool:
        """Whether the bridge wants tracking on — its own record (upstream has no
        getter), mirroring :attr:`wobbling`. On by default (the config's
        ``motion.tracking`` flag); outside a session reads the config's value.
        """
        return self._tracking_wanted

    # --- audio out ---

    async def say(self, text: str, synth: SpeechSynthesizer | None = None) -> None:
        """Synthesize ``text`` and play it through the robot speaker.

        Uses ``synth`` if given, else the synthesizer configured at construction (an
        explicit ``synthesizer=`` or the config's ``tts`` block). Raises
        :class:`BridgeError` if neither is available. Needs no motors.
        """
        chosen = synth or self._synthesizer
        if chosen is None:
            if self._synthesizer_error is not None:
                raise BridgeError(
                    "say requires a SpeechSynthesizer: the configured `tts` block "
                    f"could not be built: {self._synthesizer_error}"
                ) from self._synthesizer_error
            raise BridgeError(
                "say requires a SpeechSynthesizer: pass one, configure a default "
                "synthesizer at construction, or set the config's `tts` block "
                "(the tts extra's TTSEngineSynthesizer)"
            )
        await self._require_media().say(text, chosen)

    async def play_sound(self, sound_file: str) -> None:
        """Play a sound file / built-in sound through the robot speaker."""
        await asyncio.to_thread(self.robot.media.play_sound, sound_file)

    # --- audio-reactive motion (head wobbling) ---

    async def set_wobbling(self, enabled: bool) -> None:
        """Turn upstream's audio-reactive head wobbling on or off.

        While on, every sound the robot plays (``say``, ``play_sound``, an emotion's
        sound) sways the head in time with its loudness, on top of whatever else the
        head is doing. A mode, not a move: it holds until changed and needs no motors.
        Wobbling left on is switched off again when the session exits.
        """
        robot = self.robot
        await asyncio.to_thread(
            robot.enable_wobbling if enabled else robot.disable_wobbling
        )
        self._wobbling = enabled

    @property
    def wobbling(self) -> bool:
        """Whether the bridge has wobbling on — its own record (upstream has no getter).

        On by default once entered (the config's ``wobbling`` flag); ``False`` outside a
        session.
        """
        return self._wobbling

    # --- presence & breathing (background motion) ---

    async def set_presence(self, enabled: bool) -> None:
        """Whether the robot stays alive between verbs (specs/motion.md).

        On, the motion loop fills every idle moment with the idle move (breathing, or
        a still neutral hold); off, the bridge commands the head only while a verb
        runs and leaves it where the last move ended — for a caller driving the head
        through the raw robot. Emotions play either way. A mode, not a move: it holds
        until changed and needs no motors. Idle, it transitions at once (through the
        usual blend when turning on, at once with no easing when turning off); during
        an emotion it is recorded and applied when the emotion ends.
        """
        self._presence = enabled
        self._require_motion().set_presence(enabled)

    @property
    def presence(self) -> bool:
        """Whether presence is on — the config's value outside a session."""
        return self._presence

    async def set_breathing(self, enabled: bool) -> None:
        """Which idle move presence plays (specs/motion.md): breathing (a slow z-axis
        sine with counter-phase antenna sway) or the still hold at neutral.

        A mode, not a move: it holds until changed and needs no motors. Idle, it
        transitions at once through the usual blend; during an emotion it is recorded
        and applied when the emotion ends.
        """
        self._breathing = enabled
        self._require_motion().set_breathing(enabled)

    @property
    def breathing(self) -> bool:
        """Whether breathing is on — the config's value outside a session."""
        return self._breathing

    # --- audio in (microphone) ---

    def audio_input(self, *, mono: bool = True) -> AsyncIterator[bytes]:
        """Async iterator of echo-cancelled mic PCM (int16 LE) for the caller's own ASR.

        ``mono=True`` (default) is the ASR drop-in; ``mono=False`` yields the raw
        interleaved capture at :attr:`mic_channels` channels. See specs/audio.md.
        """
        return self._require_media().audio_input(mono=mono)

    @property
    def mic_sample_rate(self) -> int:
        """Sample rate (Hz) of :meth:`audio_input` — configure your ASR to it."""
        return self._require_media().mic_sample_rate

    @property
    def mic_channels(self) -> int:
        """Raw capture channel count (the ``mono=False`` layout)."""
        return self._require_media().mic_channels

    # --- perception (camera) ---

    async def get_camera_frame(self) -> npt.NDArray[np.uint8] | None:
        """Grab the latest camera frame as a numpy BGR array (``HxWx3``, uint8).

        A perception verb: it returns the raw frame object, not a JSON-friendly value
        (the base64/JPEG encoding for a model is the tools layer's job). Mirrors the
        upstream ``media.get_frame`` exactly — returns ``None`` when no frame is
        available yet (e.g. the headless sim has no GL context). Needs no motors.
        """
        return await asyncio.to_thread(self.robot.media.get_frame)


def _default_synthesizer(
    tts_block: dict[str, Any] | None,
) -> tuple[SpeechSynthesizer | None, Exception | None]:
    """The config's `tts` block as a ``TTSEngineSynthesizer``, and any build error.

    ``(None, None)`` without a block. The missing `tts` extra (``ImportError``) still
    raises ``ConfigError`` — a setup error nothing can fix at runtime. Any other
    exception from building the adapter is caught and returned as the cause instead,
    so a bad `tts` block degrades to no voice rather than failing construction.
    """
    if tts_block is None:
        return None, None
    try:
        return TTSEngineSynthesizer(tts_block), None
    except ImportError as e:
        raise ConfigError(
            "the config's `tts` block needs the tts extra: install "
            "reachy-mini-bridge[tts] (or pass your own synthesizer=)"
        ) from e
    except Exception as e:  # noqa: BLE001 - recorded, not swallowed; see synthesizer_error
        return None, e
