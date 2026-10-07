"""High-level interaction API (specs/core/bridge.md).

``ReachyMiniBridge`` is the intention-level surface for driving the robot in **human
units** (degrees, seconds, named emotions), orchestrating the lower-level
[robot](robot.py) primitives into single semantic verbs. It is **async-native**
because audio forces it (see [audio](audio.py)): synthesis is async and a live mic
stream runs concurrently with playback and motion on one event loop, so the upstream
SDK's blocking calls run under ``asyncio.to_thread``.

Constructed from a [``ReachyMiniConfig``](config.py) (or a backend-string shorthand for
one); ``start()`` — or ``async with``, sugar over ``start()`` / ``stop()`` — brings up
the managed daemon (when configured), the robot, the media session, the camera feed ([camera](camera.py) — the one reader of the robot's
camera), the detection loop with the head tracker ([head_tracking](head_tracking.py)),
and the motion session ([motion](motion.py) — the one ``set_target`` writer, playing
emotions and the idle behaviour, with the tracker's aim composed in) in that order on an
``AsyncExitStack`` — see "Lifecycle" in the spec.

v1 is the smallest verb set that makes the robot a conversational, face-following
presence — talk, listen, express, follow a face, manage motors, and stay visibly alive
in between. Manual movement/gaze and rich perception are deferred to post-v1 (see
specs/core/bridge.md).
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
from .audio import MediaSession, SoundToken, TTSEngineSynthesizer, cancel_safe_step
from .camera import CameraFeed, frame_reader
from .concurrency import owned
from .config import IDLE_MODES, UPSTREAM_KINEMATICS_ENGINES, ReachyMiniConfig
from .errors import (
    BridgeError,
    GravityCompensationUnsupportedError,
    MotorsNotEnabledError,
)
from .face_detection import FaceDetection, FaceReport, check_face_detector_factory
from .fake_reachy_mini import FakeReachyMini
from .head_tracking import CameraModel, HeadTracker, HeadTrackingReport
from .motion import NEUTRAL_ANTENNAS, NEUTRAL_BODY_YAW, NEUTRAL_HEAD, MotionSession
from .observable import Observable
from .robot import build_robot
from .sim_displays import FaceMarkerPublisher, face_markers_url

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import numpy as np
    import numpy.typing as npt

    from .audio import SpeechSynthesizer
    from .face_detection import FaceDetectorFactory
    from .motion import IdleMode, IdleMoveFactory
    from .robot import AnyReachyMini

__all__ = ["ReachyMiniBridge"]

_logger = logging.getLogger(__name__)

# How long play_emotion waits for the motion loop to acknowledge a cancelled primary
# (one tick in practice; a bound so a wedged loop never holds the cancel).
_DROP_ACK_S = 0.5

# The refusal of a detection or tracking switch without a detector
# (specs/vision/user_perception.md "Configuration").
_NO_DETECTOR_MESSAGE = (
    "face detection and head tracking need a face detector, but face_detection.detector is "
    'null: name one in the config — "yunet" (the shipped detector) or "custom" '
    "with a registered factory"
)

# Motor torque states, as the caller-facing single verb takes/returns them.
_MOTOR_STATES = ("enabled", "disabled", "gravity_compensation")

# The only kinematics engine on which the robot daemon accepts gravity compensation, by
# upstream's name (specs/core/config.md "Kinematics engines").
_GRAVITY_COMPENSATION_ENGINE = UPSTREAM_KINEMATICS_ENGINES["placo"]


class _WobblingSession:
    """Own accepted SDK commands past caller cancellation and through teardown."""

    def __init__(self, robot: AnyReachyMini) -> None:
        self.robot = robot
        self.enabled = (
            False  # requested mode; temporary emotion pauses do not change it
        )
        self.closing = False
        self.leases = 0
        self._may_be_enabled = False
        self._tail: asyncio.Task[None] | None = None

    def submit(
        self, enabled: bool | None, *, record: bool = True
    ) -> asyncio.Task[None]:
        if self.closing:
            raise BridgeError("the wobbling session is closing")
        task = asyncio.create_task(self._apply(self._tail, enabled, record))
        self._tail = task
        task.add_done_callback(self._observe)
        return task

    @staticmethod
    def _observe(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            _logger.warning("wobbling command failed: %s", error)

    async def _apply(
        self, previous: asyncio.Task[None] | None, enabled: bool | None, record: bool
    ) -> None:
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        # The request, read at execution time after earlier mode changes; what the
        # robot gets is the request unless an emotion holds the pause (specs/motion/
        # motion.md "Emotions through the loop") — the release restores the record.
        requested = self.enabled if enabled is None else enabled
        target = requested and self.leases == 0
        if target:
            self._may_be_enabled = True  # an SDK failure can follow a delivered enable
        await asyncio.to_thread(
            self.robot.enable_wobbling if target else self.robot.disable_wobbling
        )
        self._may_be_enabled = target
        if record:
            self.enabled = requested

    async def stop(self) -> None:
        self.closing = True
        if self._tail is not None:
            await asyncio.shield(asyncio.gather(self._tail, return_exceptions=True))
        if self._may_be_enabled:
            await asyncio.to_thread(self.robot.disable_wobbling)
            self._may_be_enabled = False
        self.enabled = False


class _MotorCommands:
    """Own accepted motor commands past caller cancellation and through teardown
    (specs/core/bridge.md "Cancellation" — instant verbs; specs/motion/motion.md "Motors"):
    each command is the SDK call *and* the motion loop's transition that belongs to it,
    run in order with the commands around it."""

    def __init__(self) -> None:
        self._tail: asyncio.Task[None] | None = None

    def submit(
        self, command: Callable[[], object], transition: Callable[[], None]
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(self._apply(self._tail, command, transition))
        self._tail = task
        task.add_done_callback(self._observe)
        return task

    @staticmethod
    def _observe(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            _logger.warning("motor command failed: %s", error)

    async def _apply(
        self,
        previous: asyncio.Task[None] | None,
        command: Callable[[], object],
        transition: Callable[[], None],
    ) -> None:
        if previous is not None:
            await asyncio.gather(previous, return_exceptions=True)
        await asyncio.to_thread(command)
        transition()

    async def drain(self) -> None:
        """Wait out the commands accepted so far (their failures already logged)."""
        if self._tail is not None:
            await asyncio.shield(asyncio.gather(self._tail, return_exceptions=True))


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


class ReachyMiniBridge:
    """Async-native, intention-level API over a robot backend.

    Construct from a :class:`ReachyMiniConfig` — or a bare backend string (``"real"`` |
    ``"sim"`` | ``"fake"``), shorthand for ``ReachyMiniConfig(backend=...)``. Nothing
    connects at construction: ``await bridge.start()`` brings up the daemon (when the
    config manages one), the robot, the media session and every worker, and ``await
    bridge.stop()`` tears them down — the pair for a host with lifecycle hooks of its
    own. ``async with`` is sugar over the pair, and the recommended form wherever the
    session fits in one block (it stops on every way out, a cancel included)::

        async with ReachyMiniBridge("fake") as bridge:
            await bridge.say("hello", synth)

        async with ReachyMiniBridge.from_json_file("robot.json") as bridge:
            await bridge.say("hello")  # default synthesizer from the config's `tts` block

    While running, the underlying robot stays reachable as :attr:`robot` (a.k.a.
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
        self._wobbling_session: _WobblingSession | None = None
        self._motor_commands: _MotorCommands | None = None
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
        # The tracker's state (specs/motion/head_tracking.md "The head tracking report"):
        # readable at any time and outliving sessions, published by the tracker.
        self._head_tracking: Observable[HeadTrackingReport] = Observable(
            HeadTrackingReport.inactive()
        )
        # Faces (specs/vision/user_perception.md): the report, readable at any time and
        # outliving sessions; the caller's detection switch (config default, reset on
        # exit); and the detection loop while entered.
        self._faces: Observable[FaceReport] = Observable(
            FaceReport.inactive(self._config.face_detection.detector)
        )
        self._face_detection_wanted = self._config.face_detection.enabled
        self._detection: FaceDetection | None = None
        # One mode verb at a time syncs the loop to the two switches (made per session:
        # a lock binds to the loop it first waits on).
        self._detection_sync = asyncio.Lock()
        # One tracking transition at a time — the flag, the loop sync and the tracker's
        # start or stop as a whole — so a stop cannot slip between a start's sync and
        # its tracker.start() (specs/core/bridge.md "Cancellation": one at a time per mode).
        self._tracking_sync = asyncio.Lock()
        # Its sibling for the detection switch: the flip, the loop sync and the rollback
        # on failure as one transition, so a failed enable's rollback never erases a
        # later call's value.
        self._face_detection_sync = asyncio.Lock()
        self._next_face_track_id = 1  # identities outlive every session of this bridge
        # The custom detector's factory (config default, reset on exit); set_face_detector
        # changes it while entered.
        self._face_detector: FaceDetectorFactory | None = (
            self._config.face_detection.face_detector
        )
        # The camera feed (specs/vision/camera.md): the object exists from construction so a
        # consumer wires to it before entry; bound to the robot and started at entry.
        self._camera = CameraFeed()

    # --- config-based constructors (mirroring ReachyMiniConfig's trio) ---

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, synthesizer: SpeechSynthesizer | None = None
    ) -> ReachyMiniBridge:
        """Build the bridge from a parsed config dict (see :meth:`ReachyMiniConfig.from_dict`)."""
        return cls(ReachyMiniConfig.from_dict(data), synthesizer=synthesizer)

    @classmethod
    def from_json(
        cls, text: str, *, synthesizer: SpeechSynthesizer | None = None
    ) -> ReachyMiniBridge:
        """Build the bridge from a JSON config string."""
        return cls(ReachyMiniConfig.from_json(text), synthesizer=synthesizer)

    @classmethod
    def from_json_file(
        cls, path: str | Path, *, synthesizer: SpeechSynthesizer | None = None
    ) -> ReachyMiniBridge:
        """Build the bridge from a JSON config file."""
        return cls(ReachyMiniConfig.from_json_file(path), synthesizer=synthesizer)

    @property
    def config(self) -> ReachyMiniConfig:
        """The config this bridge was built from."""
        return self._config

    @property
    def synthesizer_error(self) -> Exception | None:
        """The cause when the config's `tts` block failed to build a synthesizer.

        ``None`` when the voice built successfully, when an explicit ``synthesizer=``
        was passed (the block is then not consumed), or when there is no `tts` block.
        The bridge still comes up with no voice; `say` raises :class:`BridgeError`
        chained to this cause. A host that wants hard failure checks this after
        construction and raises.
        """
        return self._synthesizer_error

    # --- escape hatch ---

    @property
    def robot(self) -> AnyReachyMini:
        """The underlying robot object — full native ``ReachyMini`` on real/sim.

        Available only while entered (the robot is built and connected on
        ``start()``); raises :class:`BridgeError` otherwise.
        """
        if self._robot is None:
            raise BridgeError(
                "the robot is only available while the bridge runs (`await bridge.start()`, or `async with ReachyMiniBridge(...)`)"
            )
        return self._robot

    @property
    def raw(self) -> AnyReachyMini:
        """Alias of :attr:`robot`."""
        return self.robot

    def _require_media(self) -> MediaSession:
        if self._media is None:
            raise BridgeError(
                "the media session is only available while the bridge runs (`await bridge.start()`, or `async with ReachyMiniBridge(...)`)"
            )
        return self._media

    def _require_motion(self) -> MotionSession:
        if self._motion is None:
            raise BridgeError(
                "the motion loop is only available while the bridge runs (`await bridge.start()`, or `async with ReachyMiniBridge(...)`)"
            )
        return self._motion

    # --- lifecycle ---

    async def start(self) -> None:
        """Bring the session up, in order: the managed daemon (when configured), the
        robot, the media session, the camera feed, wobbling, the detection loop with
        the head tracker, the motion session (specs/core/bridge.md "Lifecycle"). A
        failure — or a cancel — at any step unwinds what already started, and
        ``BridgeError`` is raised on a bridge already running."""
        if self._exit_stack is not None:
            raise BridgeError("ReachyMiniBridge is already running")
        cfg = self._config
        if cfg.face_detection.detector is None and (
            self._face_detection_wanted or self._tracking_wanted
        ):
            # A config assembled in code can say what `from_dict` refuses
            # (specs/core/config.md "Validation rules"); refused here, before anything starts.
            raise ValueError(_NO_DETECTOR_MESSAGE)
        if cfg.face_detection.detector == "custom":
            # Checked before anything is entered (specs/vision/user_perception.md "Custom
            # detectors"): a bad or missing detector fails bring-up with nothing to undo.
            self._check_face_detector(self._face_detector)
        stack = AsyncExitStack()
        try:
            if cfg.manages_daemon:
                opts = cfg.effective_robot_options()
                handle = await cancel_safe_step(
                    lambda: _daemon.start_daemon(
                        cfg.daemon,
                        host=opts["host"],
                        port=opts["port"],
                        backend=cfg.backend,
                    ),
                    lambda h: h.stop(),
                )
                stack.push_async_callback(asyncio.to_thread, handle.stop)

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
            await media.start()
            stack.push_async_callback(media.stop)
            self._media = media

            def stop_emotion_sound(token: object) -> None:
                if isinstance(token, SoundToken):
                    media.stop_sound(token)

            # Constructed here, before the camera feed, the detection loop and the
            # tracker: the feed stamps frames through its head_pose_at, the tracker is
            # wired to its set_gaze / head_pose_history. Construction starts no thread;
            # its thread starts below (specs/core/bridge.md "Lifecycle") — an aim handed over
            # meanwhile waits in its command queue.
            motion = MotionSession(
                robot,
                presence=self._presence,
                idle=self._idle,
                idle_move=self._idle_move,
                # The loop starts and stops the emotion's sound through the media
                # session's one file player (specs/motion/motion.md "Emotions through
                # the loop", specs/audio/audio.md "Sound files").
                start_sound=media.start_sound,
                stop_sound=stop_emotion_sound,
            )
            # The camera feed (specs/vision/camera.md "Lifecycle"): the one reader of the
            # camera, started right after the media session and stopped right before it
            # is torn down; `latest()` reads None again from then on.
            camera = self._camera
            camera.bind(frame_reader(robot), motion.head_pose_at)
            await camera.start()
            stack.push_async_callback(camera.stop)
            # Registered before enable: even a cancelled or partially delivered command
            # is drained and disabled. Holds the robot after public fields are cleared.
            wobbling = self._wobbling_session = _WobblingSession(robot)
            stack.push_async_callback(wobbling.stop)
            if cfg.motion.wobbling:
                await self.set_wobbling(True)
            motors_enabled = await self.get_motors_state() == "enabled"
            self._tracker = HeadTracker(
                self._camera_model(robot),
                history=motion.head_pose_history,
                set_gaze=motion.set_gaze,
                report=self._head_tracking,
            )
            if self._tracking_wanted:
                self._tracker.start()
            # The detection loop (specs/vision/user_perception.md "Lifecycle"), feeding the
            # tracker while tracking is on: the configured detector (the shipped
            # `yunet`, or the registered custom one) over the camera feed.
            detection = FaceDetection(
                detector=cfg.face_detection.detector,
                faces=self._faces,
                on_observation=self._on_face_observation,
                feed=camera,
                detector_factory=self._face_detector,
                width=cfg.face_detection.width,
                target_fps=cfg.face_detection.target_fps,
                new_track_id=self._new_face_track_id,
            )
            self._detection = detection
            self._detection_sync = asyncio.Lock()
            self._tracking_sync = asyncio.Lock()
            self._face_detection_sync = asyncio.Lock()
            # Exits after the motion session, before wobbling's cleanup.
            stack.push_async_callback(self._stop_detection)
            if self._face_detection_wanted or self._tracking_wanted:
                await detection.start()
            # The sim's face markers (specs/daemon/sim_displays.md): the faces sent to the
            # daemon's viewer. Exits after the motion session, before the detection loop.
            if cfg.backend == "sim" and cfg.daemon.sim_displays.face_markers:
                tracker = self._tracker
                publisher = FaceMarkerPublisher(
                    self._faces,
                    self._head_tracking,
                    camera=tracker.camera,
                    history=motion.head_pose_history,
                    delay=lambda: tracker.delay_s,
                    url=face_markers_url(*self._daemon_address()),
                )
                await publisher.start()
                stack.push_async_callback(publisher.stop)
            # Entered after wobbling, exits first (specs/motion/motion.md "Lifecycle"): the
            # stack unwinds in reverse, so the loop eases to neutral before wobbling
            # (and everything else) tears down.
            await motion.start()
            stack.push_async_callback(motion.stop)
            self._motion = motion
            # Accepted motor commands finish — the SDK call and the loop transition —
            # before the motion session stops (registered after it: unwound before).
            commands = self._motor_commands = _MotorCommands()
            stack.push_async_callback(commands.drain)
            if motors_enabled:
                motion.resume()
        except BaseException as exc:
            if self._wobbling_session is not None:
                self._wobbling_session.closing = True
            self._robot = None
            self._media = None
            self._motion = None
            self._motor_commands = None
            self._tracker = None
            # The stack's callbacks read the mode records (wobbling left on is disabled),
            # so they are cleared only once it has unwound. The unwind is owned past any
            # further cancel (specs/core/bridge.md "Lifecycle": nothing is leaked) — a
            # second cancel would otherwise cut a callback short, leaving the wobbling
            # that a held enable delivers after the connection is gone.
            try:
                interrupted = await owned(stack.aclose())
            finally:
                self._wobbling_session = None
                self._detection = None
                self._reset_head_tracking()
            if interrupted and not isinstance(exc, asyncio.CancelledError):
                raise asyncio.CancelledError() from exc
            raise
        self._exit_stack = stack.pop_all()

    async def stop(self) -> None:
        """Tear the session down in reverse — the motion session first (easing to
        neutral), the detection loop, wobbling, the camera feed, the media session, the
        robot, an owned daemon — each even when another fails, and reset the modes to
        the config's values so ``start()`` may follow. A no-op on a bridge that is not
        running. Owned once begun (specs/core/bridge.md "Lifecycle"): a cancel arriving
        meanwhile is absorbed until every step has run — the motion thread joined before
        the connection closes — and propagates then."""
        stack = self._exit_stack
        if stack is None:
            return
        # Read as not running even if a teardown step raises.
        self._exit_stack = None
        if self._wobbling_session is not None:
            self._wobbling_session.closing = True
        self._robot = None
        self._media = None
        self._motion = None
        self._motor_commands = None
        self._tracker = None
        self._recorded_moves_future = None
        try:
            interrupted = await owned(stack.aclose())
        finally:
            self._wobbling_session = None
            self._tracking_wanted = self._config.motion.tracking
            self._presence = self._config.motion.presence
            self._idle = _idle_mode(self._config.motion.idle)
            self._idle_move = self._config.motion.idle_move
            self._face_detection_wanted = self._config.face_detection.enabled
            self._face_detector = self._config.face_detection.face_detector
            self._detection = None
            if self._faces.value.active:  # the loop never stopped cleanly
                self._faces.set(
                    FaceReport.inactive(self._config.face_detection.detector)
                )
            self._reset_head_tracking()
        if interrupted:
            raise asyncio.CancelledError()

    def _reset_head_tracking(self) -> None:
        """Publish the inactive head tracking report, once, when a session ends."""
        if self._head_tracking.value.active:
            self._head_tracking.set(HeadTrackingReport.inactive())

    @property
    def running(self) -> bool:
        """Whether the session is up: between a ``start()`` that returned and ``stop()``."""
        return self._exit_stack is not None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _stop_detection(self) -> None:
        """Stop the detection loop and publish the inactive report; an exit-stack step."""
        detection = self._detection
        if detection is not None:
            await detection.stop()

    def _daemon_address(self) -> tuple[str, int]:
        """The daemon's HTTP address: the robot options' host and port, else the local
        daemon's defaults (``start_daemon``'s)."""
        opts = self._config.effective_robot_options()
        return (str(opts.get("host", "127.0.0.1")), int(opts.get("port", 8000)))

    def _camera_model(self, robot: AnyReachyMini) -> CameraModel:
        """The tracker's camera (specs/motion/head_tracking.md "The aim"): the bridge's pinhole
        of the sim's camera source for a `sim` backend, the SDK client's calibration
        otherwise."""
        if self._config.backend == "sim":
            return CameraModel.for_sim(self._config.daemon.camera)
        return CameraModel.for_robot(robot)

    def _on_face_observation(self, report: FaceReport | None) -> None:
        """The detection loop's every poll, undebounced: the tracker's feed while
        tracking is on — an observation, or ``None`` for a poll without one, which
        keeps the tracker's loss clock running while the camera is silent."""
        tracker = self._tracker
        if tracker is None or not self._tracking_wanted:
            return
        if report is None:
            tracker.tick()
        else:
            tracker.observe(report)

    async def _sync_detection(self) -> None:
        """Run the detection loop while anyone needs faces (the caller's switch, or
        tracking), stop it when nobody does."""
        detection = self._detection
        if detection is None:
            return
        async with self._detection_sync:
            wanted = self._face_detection_wanted or self._tracking_wanted
            if wanted and not detection.running:
                await detection.start()
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
        on the Placo kinematics engine (``daemon.kinematics_engine: "placo"`` for one the
        bridge spawns, specs/core/config.md "Kinematics engines"); on any other engine it raises
        :class:`GravityCompensationUnsupportedError` without sending anything, because
        such a daemon would reject the mode by dropping the connection. A simulation
        ignores motor modes, so there the mode is sent unchecked.

        Also drives the motion loop (specs/motion/motion.md "Motors"): ``enabled`` resumes it
        — re-anchored on the present pose, so the head eases into the idle move rather
        than snapping, and the tracker's aim, if any, composed into it — and the two
        resting states pause it. The SDK call and that transition are one accepted
        command the bridge owns (specs/core/bridge.md "Cancellation"): a cancel returns
        at once while the command completes as a whole, in order with the motor
        commands around it, and teardown waits for it.
        """
        robot = self.robot
        motion = self._require_motion()
        if state == "enabled":
            command, transition = robot.enable_motors, motion.resume
        elif state == "disabled":
            command, transition = robot.disable_motors, motion.pause
        elif state == "gravity_compensation":
            await self._require_gravity_compensation_support()
            command, transition = robot.enable_gravity_compensation, motion.pause
        else:
            raise ValueError(
                f"unknown motor state {state!r}; expected one of {_MOTOR_STATES}"
            )
        commands = self._motor_commands
        if commands is None:
            raise BridgeError("the motor commands session is not available")
        await asyncio.shield(commands.submit(command, transition))

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
                f"kinematics engine, but it runs {engine!r}; for a daemon the bridge "
                "spawns set 'daemon.kinematics_engine' to 'placo' (the placo extra, "
                "reachy-mini-bridge[placo]), for one started by hand pass "
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
        sway the head on top of the choreography: one pause across consecutive
        emotions (a lease per call in flight), from the first's start to the last's
        end. The motion loop leaves the gaze layer out of the move and fades it back in
        afterwards, so an emotion plays as recorded whether or not a face is tracked.

        Cancelling the task stops the emotion — motion and sound, the loop stopping the
        sound it started for this move and nothing else (a call cancelled while it
        waits in the queue stops nothing) — and leaves the head where the cancel caught
        it, with the session still open; the same stop runs when the move fails. See
        specs/core/bridge.md "Cancellation".

        Moves the robot, so it requires motors ``enabled`` (raises
        :class:`MotorsNotEnabledError` otherwise). Raises ``ValueError`` for an unknown
        emotion name.
        """
        await self._require_motors_enabled("play_emotion")
        moves = await self._get_recorded_moves()
        move = moves.get(name)  # ValueError on unknown name
        self._require_media()
        motion = self._require_motion()
        wobbling = self._require_wobbling()
        sound_path = getattr(move, "sound_path", None)
        # The wobbling lease (specs/motion/motion.md "Emotions through the loop"): taken
        # before the disable call is awaited, so a cancel caught inside that call still
        # releases it below; the pause is one across consecutive emotions.
        wobbling.leases += 1
        pause: asyncio.Future[None] | None = None
        primary = None
        try:
            if wobbling.leases == 1 and wobbling.enabled:
                # Shielded: a cancel returns at once while the call completes in its
                # thread, and the release waits for it so the restore comes after.
                pause = wobbling.submit(False, record=False)
                await asyncio.shield(pause)
            primary = motion.submit(
                move, None if sound_path is None else Path(sound_path)
            )
            await asyncio.wrap_future(primary.done)
        except BaseException:
            if primary is not None:
                # The loop drops the primary at its next tick — its sound stopped if
                # the loop started it — and acknowledges; the effect has stopped when
                # the exception reaches the caller (specs/core/bridge.md "Cancellation").
                primary.done.cancel()  # idempotent; wrap_future already propagated a cancel
                await asyncio.to_thread(primary.dropped.wait, _DROP_ACK_S)
            raise
        finally:
            wobbling.leases -= 1
            if wobbling.leases == 0:
                if pause is not None and not pause.done():
                    # A cancel caught inside the disable call: it completes in its
                    # thread while the cancel propagates at once, and the restore
                    # follows its completion (unless another emotion took the lease
                    # meanwhile — its own release restores then).
                    pause.add_done_callback(
                        lambda done: self._restore_once_released(wobbling, done)
                    )
                else:
                    await self._restore_layers_after_move(wobbling)

    def _restore_once_released(
        self, wobbling: _WobblingSession, _pause: asyncio.Future[None]
    ) -> None:
        if wobbling.leases == 0 and not wobbling.closing:
            asyncio.ensure_future(self._restore_layers_after_move(wobbling))

    async def _restore_layers_after_move(self, wobbling: _WobblingSession) -> None:
        """Release the wobbling pause: restored to the bridge's *current* record."""
        if not wobbling.closing and wobbling.enabled:
            try:
                await asyncio.shield(wobbling.submit(None, record=False))
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
                "head tracking is only available while the bridge runs (`await bridge.start()`, or `async with ReachyMiniBridge(...)`)"
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
        (``face_detection.detector``; ``ValueError`` with none), and a detector that cannot be
        built raises ``BridgeError`` and leaves tracking off.
        """
        tracker = self._require_tracker()
        self._require_detector()
        async with self._tracking_sync:
            was_wanted = self._tracking_wanted
            self._tracking_wanted = True
            try:
                await self._sync_detection()
            except BaseException:
                self._tracking_wanted = was_wanted
                raise
            tracker.start(focus=focus)

    async def stop_head_tracking(self) -> None:
        """Stop the head tracker: the aim is withdrawn and the head eases back onto the
        idle move. The detection loop keeps running while :attr:`face_detection` is on,
        and stops otherwise."""
        tracker = self._require_tracker()
        async with self._tracking_sync:
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
        return self._head_tracking.value.focus

    @property
    def attention(self) -> str | None:
        """Derived from the tracker (specs/core/bridge.md "Attention"): ``"engaged"`` while it
        follows a face, ``"watching"`` while tracking is on and it follows nobody, ``None``
        when tracking is off or outside a session."""
        return self._head_tracking.value.attention

    @property
    def head_tracking(self) -> Observable[HeadTrackingReport]:
        """The head tracker's state (specs/motion/head_tracking.md "The head tracking
        report"): ``head_tracking.value`` is the current :class:`HeadTrackingReport` —
        ``active``, ``focus``, ``attention`` and the ``track_id`` of the face the head
        follows, the link to :attr:`faces`. ``changes()`` wakes when tracking starts or
        stops, focus switches, attention changes, or the head passes to another person —
        never on a face merely moving. Readable at any time: outside a session it is the
        inactive report."""
        return self._head_tracking

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
        (``face_detection.detector``; enabling with none is a ``ValueError``). The loop also runs
        whenever head tracking is on, whatever this says; ``faces.value.active`` reports
        what is actually running. Needs an entered session (:class:`BridgeError`
        otherwise); a detector that cannot be built raises ``BridgeError`` and leaves
        the switch as it was.

        One transition at a time (specs/core/bridge.md "Cancellation"): the switch, the
        loop and the rollback on failure change under one lock, so the last call's
        value stands. A cancel during an enable leaves the switch off and the build
        discarded; a disable, once begun, completes — the loop stopped and its detector
        released — before the cancel propagates.
        """
        if self._detection is None:
            raise BridgeError(
                "face detection is only available while the bridge runs (`await bridge.start()`, or `async with ReachyMiniBridge(...)`)"
            )
        if enabled:
            self._require_detector()
        async with self._face_detection_sync:
            was_wanted = self._face_detection_wanted
            self._face_detection_wanted = enabled
            try:
                await self._sync_detection()
            except asyncio.CancelledError:
                if enabled:  # the loop never started: nothing to stand behind
                    self._face_detection_wanted = was_wanted
                raise
            except BaseException:
                self._face_detection_wanted = was_wanted
                raise

    @property
    def face_detection(self) -> bool:
        """The caller's detection switch — the config's ``face_detection.enabled`` outside a
        session."""
        return self._face_detection_wanted

    def _require_detector(self) -> None:
        if self._config.face_detection.detector is None:
            raise ValueError(_NO_DETECTOR_MESSAGE)

    @staticmethod
    def _check_face_detector(factory: object) -> None:
        if factory is None:
            raise ValueError(
                "face_detection.detector is 'custom' but no face detector is registered: set "
                "FaceDetectionSettings.face_detector or call set_face_detector(...)"
            )
        check_face_detector_factory(factory)

    async def set_face_detector(self, factory: FaceDetectorFactory | None) -> None:
        """Register the custom detector for ``face_detection.detector: "custom"``
        (specs/vision/user_perception.md "Custom detectors"): a zero-argument callable
        returning an object with ``detect(frame_bgr, ts) -> Sequence[PixelFace]`` — a
        class is one — or ``None`` to clear it.

        Checked before it is stored: ``ValueError`` for a factory that is not
        callable — the registered one then stays; the detector itself is built, and
        validated, when the loop starts. Stored whatever the detector is; with the
        detector ``custom`` and the loop running, the loop swaps to the new one between
        two polls. Clearing it while the loop runs it (detection or tracking on) is
        refused with ``ValueError`` and changes nothing: stop both first.
        """
        if factory is not None:
            check_face_detector_factory(factory)
        detection = self._detection
        runs_custom = (
            detection is not None
            and detection.running
            and self._config.face_detection.detector == "custom"
        )
        if factory is None and runs_custom:
            raise ValueError(
                "the custom face detector cannot be cleared while the detection loop "
                "runs it: stop head tracking and face detection first"
            )
        self._face_detector = factory
        if detection is None or self._config.face_detection.detector != "custom":
            return
        detection.restart(factory)
        if factory is not None:
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
        """Play a sound file through the robot speaker and wait until it has been heard.

        ``sound_file`` is a path on this machine or the name of one of the SDK's
        built-in sounds (``"wake_up.wav"``). Cancelling the task stops the sound; run it
        as a task to play it in the background. One sound file plays at a time and the
        newest wins: when a later one — another ``play_sound``, or an emotion's sound —
        replaces this one, the call raises :class:`SoundInterruptedError`. It plays
        alongside ``say``. ``FileNotFoundError`` / ``ValueError`` for a file that cannot
        be found or read, before anything plays. Needs no motors.
        """
        await self._require_media().play_sound(sound_file)

    # --- audio-reactive motion (head wobbling) ---

    async def set_wobbling(self, enabled: bool) -> None:
        """Turn upstream's audio-reactive head wobbling on or off.

        While on, every sound the robot plays (``say``, ``play_sound``, an emotion's
        sound) sways the head in time with its loudness, on top of whatever else the
        head is doing. A mode, not a move: it holds until changed and needs no motors.
        Wobbling left on is switched off again when the session exits.
        """
        await asyncio.shield(self._require_wobbling().submit(enabled))

    def _require_wobbling(self) -> _WobblingSession:
        _ = self.robot  # the same outside-session error as every SDK mode verb
        session = self._wobbling_session
        if session is None or session.closing:
            raise BridgeError("the wobbling session is not available")
        return session

    def _new_face_track_id(self) -> int:
        track_id = self._next_face_track_id
        self._next_face_track_id += 1
        return track_id

    @property
    def wobbling(self) -> bool:
        """Whether the bridge has wobbling on — its own record (upstream has no getter).

        On by default once entered (the config's ``wobbling`` flag); ``False`` outside a
        session.
        """
        session = self._wobbling_session
        return session is not None and not session.closing and session.enabled

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
        motion = self._require_motion()  # before anything is recorded
        motion.set_presence(enabled)
        self._presence = enabled

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
