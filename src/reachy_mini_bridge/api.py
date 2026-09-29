"""High-level interaction API (specs/core/api.md).

``ReachyMiniApi`` is the intention-level surface for driving the robot in **human
units** (degrees, seconds, named emotions), orchestrating the lower-level
[robot](robot.py) primitives into single semantic verbs. It is **async-native**
because audio forces it (see [audio](audio.py)): synthesis is async and a live mic
stream runs concurrently with playback and motion on one event loop, so the upstream
SDK's blocking calls run under ``asyncio.to_thread``.

Constructed from a [``ReachyMiniConfig``](config.py) (or a backend-string shorthand for
one); ``async with`` brings up the managed daemon (when configured), the robot, the
media session, the camera feed ([camera](camera.py) — the one reader of the robot's
camera), the detection loop with the head tracker ([head_tracking](head_tracking.py)),
and the motion session ([motion](motion.py) — the one ``set_target`` writer, playing
emotions and the idle behaviour, with the tracker's aim composed in) in that order on an
``AsyncExitStack`` — see "Lifecycle" in the spec.

v1 is the smallest verb set that makes the robot a conversational, face-following
presence — talk, listen, express, follow a face, manage motors, and stay visibly alive
in between. Manual movement/gaze and rich perception are deferred to post-v1 (see
specs/core/api.md).
"""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast

from reachy_mini.motion.move import Move

from . import daemon as _daemon
from . import robot as _robot
from .audio import MediaSession, TTSEngineSynthesizer, cancel_safe_step
from .camera import CameraFeed, frame_reader
from .config import IDLE_MODES, ReachyMiniConfig
from .errors import (
    BridgeError,
    GravityCompensationUnsupportedError,
    MotorsNotEnabledError,
)
from .face_detection import FaceDetection, FaceReport, check_face_detector_factory
from .fake_reachy_mini import FakeReachyMini
from .head_tracking import CameraModel, HeadTracker
from .motion import NEUTRAL_ANTENNAS, NEUTRAL_BODY_YAW, NEUTRAL_HEAD, MotionSession
from .observable import Observable
from .robot import build_robot

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import numpy as np
    import numpy.typing as npt

    from .audio import SpeechSynthesizer
    from .face_detection import FaceDetectorFactory
    from .motion import IdleMode, IdleMoveFactory
    from .robot import AnyReachyMini

__all__ = ["ReachyMiniApi"]

_logger = logging.getLogger(__name__)

# The refusal of a detection or tracking switch without a detector
# (specs/vision/user_perception.md "Configuration").
_NO_DETECTOR_MESSAGE = (
    "face detection and head tracking need a face detector, but faces.detector is "
    'null: name one in the config — "yunet" (the shipped detector) or "custom" '
    "with a registered factory"
)

# Motor torque states, as the caller-facing single verb takes/returns them.
_MOTOR_STATES = ("enabled", "disabled", "gravity_compensation")

# The only kinematics engine on which the robot daemon accepts gravity compensation.
_GRAVITY_COMPENSATION_ENGINE = "Placo"


def _daemon_kinematics_engine(robot: AnyReachyMini) -> str:
    """The kinematics engine the robot's daemon runs (e.g. ``"Placo"``). Blocking.

    Upstream exposes no SDK getter; the daemon serves it at ``/api/kinematics/info``. The
    fake reports it on its daemon client stand-in. Raises on an unreachable daemon or an
    unexpected payload. Shared with the testing harness's capability probe.
    """
    if isinstance(robot, FakeReachyMini):
        return robot.client.kinematics_engine
    info = _robot.fetch_daemon_json(robot, "/api/kinematics/info")
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


def _idle_mode(value: str) -> IdleMode:
    """``value`` narrowed to an idle mode, or ``ValueError``."""
    if value not in IDLE_MODES:
        raise ValueError(f"idle mode must be one of {IDLE_MODES}, got {value!r}")
    return cast("IdleMode", value)


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
        # The motion loop's switches (specs/motion/motion.md), initialized from the config and
        # reset to it on exit; set_presence/set_idle/set_idle_move change them while
        # entered.
        self._presence = self._config.motion.presence
        self._idle: IdleMode = _idle_mode(self._config.motion.idle)
        self._idle_move: IdleMoveFactory | None = self._config.motion.idle_move
        # Head tracking (specs/motion/head_tracking.md): the caller's switch (the config
        # default, reset on exit) — a mode like presence, needing no motors — and the
        # tracker while entered, built once per session and fed the detection loop's
        # reports while the switch is on.
        self._tracking_wanted = self._config.motion.tracking
        self._tracker: HeadTracker | None = None
        # Faces (specs/vision/user_perception.md): the report, readable at any time and
        # outliving sessions; the caller's detection switch (config default, reset on
        # exit); and the detection loop while entered.
        self._faces: Observable[FaceReport] = Observable(
            FaceReport.inactive(self._config.faces.detector)
        )
        self._face_detection_wanted = self._config.faces.detection
        self._detection: FaceDetection | None = None
        # The custom detector's factory (config default, reset on exit); set_face_detector
        # changes it while entered.
        self._face_detector: FaceDetectorFactory | None = (
            self._config.faces.face_detector
        )
        # The camera feed (specs/vision/camera.md): the object exists from construction so a
        # consumer wires to it before entry; bound to the robot and started at entry.
        self._camera = CameraFeed()

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
        if cfg.faces.detector is None and (
            self._face_detection_wanted or self._tracking_wanted
        ):
            # A config assembled in code can say what `from_dict` refuses
            # (specs/core/config.md "Validation rules"); refused here, before anything starts.
            raise ValueError(_NO_DETECTOR_MESSAGE)
        if cfg.faces.detector == "custom":
            # Checked before anything is entered (specs/vision/user_perception.md "Custom
            # detectors"): a bad or missing detector fails bring-up with nothing to undo.
            self._check_face_detector(self._face_detector)
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
            # Constructed here, before the camera feed, the detection loop and the
            # tracker: the feed stamps frames through its head_pose_at, the tracker is
            # wired to its set_gaze / head_pose_history. Construction starts no thread;
            # its thread starts below (specs/core/api.md "Lifecycle") — an aim handed over
            # meanwhile waits in its command queue.
            motion = MotionSession(
                robot,
                presence=self._presence,
                idle=self._idle,
                idle_move=self._idle_move,
            )
            # The camera feed (specs/vision/camera.md "Lifecycle"): the one reader of the
            # camera, started right after the media session and stopped right before it
            # is torn down; `latest()` reads None again from then on.
            camera = self._camera
            camera.bind(frame_reader(robot), motion.head_pose_at)
            camera.start()
            stack.push_async_callback(asyncio.to_thread, camera.stop)
            # Registered before the enable, so a failing enable still unwinds cleanly
            # (the mode is still off, so the callback is a no-op). It holds the robot
            # itself: __aexit__ clears `self._robot` before the stack closes.
            stack.push_async_callback(self._disable_wobbling_if_on, robot)
            if cfg.motion.wobbling:
                await self.set_wobbling(True)
            motors_enabled = await self.get_motors_state() == "enabled"
            self._tracker = HeadTracker(
                self._camera_model(robot),
                history=motion.head_pose_history,
                set_gaze=motion.set_gaze,
            )
            # The detection loop (specs/vision/user_perception.md "Lifecycle"), feeding the
            # tracker while tracking is on: the configured detector (the shipped
            # `yunet`, or the registered custom one) over the camera feed.
            detection = FaceDetection(
                detector=cfg.faces.detector,
                faces=self._faces,
                on_observation=self._on_face_observation,
                feed=camera,
                detector_factory=self._face_detector,
            )
            self._detection = detection
            # Exits after the motion session, before wobbling's cleanup.
            stack.push_async_callback(self._stop_detection)
            if self._face_detection_wanted or self._tracking_wanted:
                await self._start_detection(detection)
            # Entered after wobbling, exits first (specs/motion/motion.md "Lifecycle"): the
            # stack unwinds in reverse, so the loop eases to neutral before wobbling
            # (and everything else) tears down.
            await stack.enter_async_context(motion)
            self._motion = motion
            if motors_enabled:
                motion.resume()
        except BaseException:
            self._robot = None
            self._media = None
            self._motion = None
            self._tracker = None
            # The stack's callbacks read the mode records (wobbling left on is disabled),
            # so they are cleared only once it has unwound.
            try:
                await stack.aclose()
            finally:
                self._wobbling = False
                self._detection = None
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
        self._tracker = None
        self._recorded_moves_future = None
        try:
            if stack is not None:
                await stack.aclose()
        finally:
            self._wobbling = False
            self._tracking_wanted = self._config.motion.tracking
            self._presence = self._config.motion.presence
            self._idle = _idle_mode(self._config.motion.idle)
            self._idle_move = self._config.motion.idle_move
            self._face_detection_wanted = self._config.faces.detection
            self._face_detector = self._config.faces.face_detector
            self._detection = None
            if self._faces.value.active:  # the loop never stopped cleanly
                self._faces.set(FaceReport.inactive(self._config.faces.detector))

    async def _disable_wobbling_if_on(self, robot: AnyReachyMini) -> None:
        # The daemon-side switch is shared across clients: never leave it armed.
        if self._wobbling:
            await asyncio.to_thread(robot.disable_wobbling)
            self._wobbling = False

    async def _stop_detection(self) -> None:
        """Stop the detection loop and publish the inactive report; an exit-stack step."""
        detection = self._detection
        if detection is not None:
            await detection.stop()

    async def _start_detection(self, detection: FaceDetection) -> None:
        """Start the loop: a detector it cannot run is the caller's ``ValueError``; a
        detector that cannot be built (a model that fails to load) is a ``BridgeError``
        chaining the cause (specs/vision/user_perception.md "Building the detector")."""
        try:
            await detection.start()
        except ValueError:
            raise
        except Exception as e:
            raise BridgeError(
                f"the face detector {self._config.faces.detector!r} could not be built: "
                f"{type(e).__name__}: {e}"
            ) from e

    def _camera_model(self, robot: AnyReachyMini) -> CameraModel:
        """The tracker's camera (specs/motion/head_tracking.md "The aim"): the bridge's pinhole
        of the sim's camera source for a `sim` backend, the SDK client's calibration
        otherwise."""
        if self._config.backend == "sim":
            return CameraModel.for_sim(self._config.daemon.camera)
        return CameraModel.for_robot(robot)

    def _on_face_observation(self, report: FaceReport) -> None:
        """The detection loop's every poll, undebounced: the tracker's feed while
        tracking is on."""
        tracker = self._tracker
        if tracker is not None and self._tracking_wanted:
            tracker.observe(report)

    async def _sync_detection(self) -> None:
        """Run the detection loop while anyone needs faces (the caller's switch, or
        tracking), stop it when nobody does."""
        detection = self._detection
        if detection is None:
            return
        wanted = self._face_detection_wanted or self._tracking_wanted
        if wanted and not detection.running:
            await self._start_detection(detection)
        elif not wanted and detection.running:
            await detection.stop()

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

        Also drives the motion loop (specs/motion/motion.md "Motors"): ``enabled`` resumes it
        — re-anchored on the present pose, so the head eases into the idle move rather
        than snapping, and the tracker's aim, if any, composed into it — and the two
        resting states pause it.
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

        The move plays through the bridge's motion loop (specs/motion/motion.md "Emotions
        through the loop"), never through upstream's ``async_play_move``: the loop is
        the one writer of the robot's target. Primaries are exclusive and FIFO, so a
        second ``play_emotion`` while one plays waits its turn. The loop blends into
        the move's start pose, starts its sidecar sound as the trajectory starts, and
        plays it for its duration; **completes when the trajectory has played** — the
        return to neutral that follows is the idle behaviour's, not the verb's.

        Around the move, wobbling is paused and restored on every exit path —
        completion, cancel, or failure — because the emotion's own sound would otherwise
        sway the head on top of the choreography. The motion loop leaves the gaze layer
        out of the move and fades it back in afterwards, so an emotion plays as recorded
        whether or not a face is tracked.

        Cancelling the task stops the emotion — motion and sound — and leaves the head
        where the cancel caught it, with the session still open; the same stop runs
        when the move fails. See specs/core/api.md "Cancellation".

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
        # Pause wobbling for the move; restored below on every exit path, to its
        # *current* record (specs/motion/motion.md "Emotions through the loop").
        if self._wobbling:
            await asyncio.to_thread(robot.disable_wobbling)
        future = motion.submit(move, None if sound_path is None else Path(sound_path))
        try:
            await asyncio.wrap_future(future)
        except BaseException:
            future.cancel()  # idempotent; wrap_future already propagated a cancel
            if sound_path is not None:
                media.stop_sound()
            raise
        finally:
            await self._restore_layers_after_move()

    async def _restore_layers_after_move(self) -> None:
        if self._wobbling:
            try:
                await asyncio.to_thread(self.robot.enable_wobbling)
            except Exception as e:  # noqa: BLE001 - never mask the verb's own outcome
                _logger.warning("could not restore wobbling after the emotion: %s", e)

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

    # --- attention / gaze (autonomous: the bridge's own head tracker) ---

    def _require_tracker(self) -> HeadTracker:
        if self._tracker is None:
            raise BridgeError(
                "head tracking is only available inside `async with ReachyMiniApi(...)`"
            )
        return self._tracker

    async def start_head_tracking(self, *, focus: bool = False) -> None:
        """Have the robot autonomously keep the tracked face in view
        (specs/motion/head_tracking.md): the bridge's tracker aims the face the detection loop
        reports, and the motion loop composes that aim into the idle move — the head
        looks at the face and keeps breathing around it. With ``focus`` the head holds
        exactly on the face instead, the idle move's head motion left out (its antennas
        kept); calling it again switches between the two.

        A mode, not a move: it holds until changed and needs no motors (the motion loop
        is paused without them, so the aim shows once they are enabled). Starts the
        detection loop if it is not already running — so it needs a configured detector
        (``faces.detector``; ``ValueError`` with none), and a detector that cannot be
        built raises ``BridgeError`` and leaves tracking off.
        """
        tracker = self._require_tracker()
        self._require_detector()
        was_focus, was_wanted = tracker.focus, self._tracking_wanted
        tracker.focus = focus
        self._tracking_wanted = True
        try:
            await self._sync_detection()
        except BaseException:
            tracker.focus, self._tracking_wanted = was_focus, was_wanted
            raise

    async def stop_head_tracking(self) -> None:
        """Stop the head tracker: the aim is withdrawn and the head eases back onto the
        idle move. The detection loop keeps running while :attr:`face_detection` is on,
        and stops otherwise."""
        tracker = self._require_tracker()
        self._tracking_wanted = False
        tracker.stop()
        await self._sync_detection()

    @property
    def tracking(self) -> bool:
        """Whether the bridge's tracker is on — its own record, initially the config's
        ``motion.tracking`` flag (off by default: it needs a detector); outside a
        session reads the config's value."""
        return self._tracking_wanted

    @property
    def tracking_focus(self) -> bool:
        """Whether tracking holds the head exactly on the face (``focus``) rather than
        composing it with the idle move — ``False`` while tracking is off and outside a
        session."""
        tracker = self._tracker
        return tracker is not None and self._tracking_wanted and tracker.focus

    @property
    def attention(self) -> str | None:
        """Derived from the tracker (specs/core/api.md "Attention"): ``"engaged"`` while it
        holds an aim (a face seen within ``TRACKING_LOST_S``), ``"watching"`` while
        tracking is on and nobody has been seen for longer, ``None`` when tracking is
        off or outside a session."""
        tracker = self._tracker
        if tracker is None or not self._tracking_wanted:
            return None
        return "engaged" if tracker.engaged else "watching"

    # --- faces (perception) ---

    @property
    def faces(self) -> Observable[FaceReport]:
        """The faces in front of the robot as the detection loop last saw them
        (specs/vision/user_perception.md): ``faces.value`` is the current :class:`FaceReport`;
        ``async for report in faces.changes()`` wakes when the number of faces changes
        (debounced) or detection starts or stops — never on a face merely moving.

        Readable at any time: outside a session, and while no detector is looking, the
        value is the inactive, empty report (``active=False`` means *unknown*, not
        *nobody*).
        """
        return self._faces

    async def set_face_detection(self, enabled: bool) -> None:
        """Whether the detection loop runs for the caller's sake.

        A mode needing no motors (nothing moves) but a configured detector
        (``faces.detector``; enabling with none is a ``ValueError``). The loop also runs
        whenever head tracking is on, whatever this says; ``faces.value.active`` reports
        what is actually running. Needs an entered session (:class:`BridgeError`
        otherwise); a detector that cannot be built raises ``BridgeError`` and leaves
        the switch as it was.
        """
        if self._detection is None:
            raise BridgeError(
                "face detection is only available inside `async with ReachyMiniApi(...)`"
            )
        if enabled:
            self._require_detector()
        was_wanted = self._face_detection_wanted
        self._face_detection_wanted = enabled
        try:
            await self._sync_detection()
        except BaseException:
            self._face_detection_wanted = was_wanted
            raise

    @property
    def face_detection(self) -> bool:
        """The caller's detection switch — the config's ``faces.detection`` outside a
        session."""
        return self._face_detection_wanted

    def _require_detector(self) -> None:
        if self._config.faces.detector is None:
            raise ValueError(_NO_DETECTOR_MESSAGE)

    @staticmethod
    def _check_face_detector(factory: object) -> None:
        if factory is None:
            raise ValueError(
                "faces.detector is 'custom' but no face detector is registered: set "
                "FaceSettings.face_detector or call set_face_detector(...)"
            )
        check_face_detector_factory(factory)

    async def set_face_detector(self, factory: FaceDetectorFactory | None) -> None:
        """Register the custom detector for ``faces.detector: "custom"``
        (specs/vision/user_perception.md "Custom detectors"): a zero-argument callable
        returning an object with ``detect(frame_bgr, ts) -> Sequence[PixelFace]`` — a
        class is one — or ``None`` to clear it.

        Checked before it is stored: ``ValueError`` for a factory that is not callable,
        raises, or builds something without a callable ``detect`` — the registered one
        then stays. Stored whatever the detector is; with the detector ``custom`` and
        the loop running, the loop swaps to the new one between two polls (clearing it
        stops the loop, which the next start will refuse until one is registered).
        """
        if factory is not None:
            check_face_detector_factory(factory)
        self._face_detector = factory
        detection = self._detection
        if detection is None or self._config.faces.detector != "custom":
            return
        detection.restart(factory)
        if factory is None:
            if detection.running:
                await detection.stop()
        else:
            await self._sync_detection()

    @property
    def face_detector(self) -> FaceDetectorFactory | None:
        """The registered custom detector factory — the config's outside a session."""
        return self._face_detector

    # --- perception (camera) ---

    @property
    def camera(self) -> CameraFeed:
        """The camera feed (specs/vision/camera.md): the one reader of the robot's camera.
        ``camera.latest()`` is the newest :class:`~reachy_mini_bridge.camera.CameraFrame`
        — ``frame_id``, ``ts``, ``image`` (BGR ``HxWx3`` ``uint8``, shared and
        read-only: copy before drawing), ``head_pose`` — or ``None`` while no frame is
        available: before the session, after it, and wherever the daemon has no frame
        (the headless sim on macOS). A property, not a verb: an instant, thread-safe
        sample any number of consumers take without stealing frames from one another.
        The feed exists from construction, so a consumer wires to it before entry.
        """
        return self._camera

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
                "(the tts-engine adapter, TTSEngineSynthesizer)"
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

    # --- presence & the idle move (background motion) ---

    async def set_presence(self, enabled: bool) -> None:
        """Whether the robot stays alive between verbs (specs/motion/motion.md).

        On, the motion loop fills every idle moment with the idle move (breathing, a
        still neutral hold, or the caller's own); off, the bridge commands the head only while a verb
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

    async def set_idle(self, mode: str) -> None:
        """Which idle move presence plays (specs/motion/motion.md): ``"breathing"`` (the
        built-in animation), ``"hold"`` (a still neutral) or ``"custom"`` (the move
        registered with :meth:`set_idle_move`; the hold while none is registered).

        A mode, not a move: it holds until changed and needs no motors. Idle, it
        transitions at once (a playing breathing or custom move fades out to neutral
        first); during an emotion it is recorded and applied when the emotion ends.
        Raises ``ValueError`` for any other value.
        """
        checked = _idle_mode(mode)
        motion = self._require_motion()
        self._idle = checked
        motion.set_idle(checked)

    @property
    def idle(self) -> str:
        """The idle mode — the config's value outside a session."""
        return self._idle

    async def set_idle_move(self, factory: IdleMoveFactory | None) -> None:
        """Register the custom idle move (specs/motion/motion.md "Custom idle moves"): a
        zero-argument callable returning a fresh ``IdleMove`` — a subclass itself, or a
        function — or ``None`` to clear it.

        Stored whatever the idle mode is, and played whenever the mode is ``"custom"``;
        in that mode, idle, it takes effect at once. Raises ``ValueError`` for a factory
        that is not callable, raises, builds no ``IdleMove``, or whose ``offsets(0.0)``
        is not an ``IdleOffsets`` of finite numbers — the registered move then stays.
        """
        self._require_motion().set_idle_move(factory)  # checks before it stores
        self._idle_move = factory

    @property
    def idle_move(self) -> IdleMoveFactory | None:
        """The registered custom idle move factory — the config's outside a session."""
        return self._idle_move

    # --- audio in (microphone) ---

    def audio_input(self, *, mono: bool = True) -> AsyncIterator[bytes]:
        """Async iterator of echo-cancelled mic PCM (int16 LE) for the caller's own ASR.

        ``mono=True`` (default) is the ASR drop-in; ``mono=False`` yields the raw
        interleaved capture at :attr:`mic_channels` channels. See specs/audio/audio.md.
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


def _default_synthesizer(
    tts_block: dict[str, Any] | None,
) -> tuple[SpeechSynthesizer | None, Exception | None]:
    """The config's `tts` block as a ``TTSEngineSynthesizer``, and any build error.

    ``(None, None)`` without a block. Any exception from building the adapter — a
    tts-engine ``ConfigError`` for a provider whose extra is not installed (its message
    names the ``tts-engine[<provider>]`` to install) or for a bad block, an unset
    ``api_key_env``, a model that fails to load — is caught and returned as the cause,
    so a `tts` block that cannot be built degrades to no voice rather than failing
    construction.
    """
    if tts_block is None:
        return None, None
    try:
        return TTSEngineSynthesizer(tts_block), None
    except Exception as e:  # noqa: BLE001 - recorded, not swallowed; see synthesizer_error
        return None, e
