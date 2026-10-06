"""The control panel's gradio-free core: one `ReachyMiniBridge` session on a background
event loop, exposed to synchronous callers (specs/examples/control_panel.md "The controller").

Gradio runs its handlers in worker threads, so the bridge — async-native, one session
that must outlive every request — lives on a thread of its own with its own asyncio
loop. Instant verbs submit a coroutine there and wait; the two spanning verbs (`say`,
`play_emotion`) block for their whole effect and can be stopped from any other thread
through `stop_saying()` / `stop_emotion()`, which cancel the task on the loop and wait
until the verb has returned — the bridge stops the effect before re-raising its
`CancelledError`, so "returned" means "silent" / "no longer commanded".
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Coroutine, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal, Self

import numpy as np
import numpy.typing as npt

from reachy_mini_bridge import (
    BridgeError,
    ReachyMiniBridge,
    ReachyMiniConfig,
    SoundInterruptedError,
    SpeechInterruptedError,
    SpeechSynthesizer,
)
from reachy_mini_bridge.face_detection import FaceReport

_logger = logging.getLogger(__name__)

Slot = Literal["say", "sound", "emotion"]
_SLOTS: tuple[Slot, ...] = ("say", "sound", "emotion")

# The mic level decays by this factor per chunk when quieter than the last, so a short
# burst survives until the panel's next 0.5 s refresh (chunks are ~10 ms).
MIC_LEVEL_DECAY = 0.97
# How long a stop waits for the cancelled verbs to return before giving up on them.
STOP_TIMEOUT_S = 5.0
# The face meter: how often it samples `bridge.faces.value`, and the window its rate of new
# observations is averaged over.
FACE_METER_HZ = 50.0
FACE_RATE_WINDOW_S = 2.0
# `draw_faces`: the marker's side as a fraction of the frame height, and its colour (RGB).
FACE_MARKER_SIZE = 0.15
FACE_MARKER_RGB = (0, 255, 0)


@dataclass
class PanelState:
    """Everything the panel displays, read in one go by :meth:`snapshot`."""

    backend: str
    motors: str
    presence: bool
    idle: str
    wobbling: bool
    tracking: bool
    attention: str | None
    face_detection: bool
    # the number of faces the detection loop reports; -1 while no detector is looking
    faces: int
    # each face's normalised (x, y), in the report's order (by track id); empty while no
    # detector is looking
    face_positions: list[tuple[float, float]]
    # the index in face_positions of the face the head follows (the tracker's
    # `head_tracking.track_id`); None when it follows none
    face_target: int | None
    # new face observations per second; None while no detector is looking
    face_rate: float | None
    voice: str
    mic_level: float
    mic_sample_rate: int | None
    busy: list[str] = field(default_factory=list)
    emotions: list[str] = field(default_factory=list)


@dataclass
class _InFlight:
    """A spanning verb in progress: the caller-side future (set once submitted) and,
    once the coroutine has started on the loop, the task to cancel."""

    future: cf.Future[bool] | None = None
    task: asyncio.Task[bool] | None = None


class ControlPanelController:
    """Owns one ``ReachyMiniBridge`` session on a background loop; a sync facade over it.

    ``start()`` brings the session up (blocking until it is entered, or re-raising
    what bring-up raised); ``stop()`` stops every in-flight verb, exits the session in
    order and joins the thread. ``with controller:`` does both.
    """

    def __init__(
        self,
        config: ReachyMiniConfig | str = "fake",
        *,
        synthesizer: SpeechSynthesizer | None = None,
    ) -> None:
        self._bridge = ReachyMiniBridge(config, synthesizer=synthesizer)
        self._has_synthesizer = (
            synthesizer is not None or self._bridge.config.tts is not None
        )
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._loop_ready = threading.Event()
        self._ready: cf.Future[None] | None = None
        self._in_flight: dict[Slot, list[_InFlight]] = {slot: [] for slot in _SLOTS}
        self._in_flight_lock = threading.Lock()
        self._mic_level = 0.0
        self._emotions: list[str] = []
        self._face_rate: float | None = None

    # --- lifecycle -----------------------------------------------------------------

    @property
    def bridge(self) -> ReachyMiniBridge:
        """The bridge this controller drives (its config is readable at any time)."""
        return self._bridge

    @property
    def running(self) -> bool:
        """Whether the session is entered and accepting verbs."""
        ready = self._ready
        return (
            ready is not None
            and ready.done()
            and ready.exception() is None
            and self._thread is not None
            and self._thread.is_alive()
        )

    def start(self, *, timeout: float | None = None) -> None:
        """Bring the session up; returns once entered, re-raises what bring-up raised.

        ``timeout`` (seconds) bounds the wait: on expiry the bring-up is cancelled, the
        bridge unwinds what it had started, and ``TimeoutError`` is raised.
        """
        if self._thread is not None:
            raise BridgeError("the controller is already started")
        self._ready = cf.Future()
        self._loop_ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="control-panel-bridge", daemon=True
        )
        self._thread.start()
        try:
            self._ready.result(timeout)
        except cf.TimeoutError:
            self.stop()
            raise TimeoutError(
                f"the robot session did not come up within {timeout} s"
            ) from None
        except BaseException:
            self._thread.join()
            self._reset()
            raise

    def stop(self) -> None:
        """Stop in-flight verbs, exit the session in order, and join the loop thread."""
        thread = self._thread
        if thread is None:
            return
        self._loop_ready.wait()
        loop, task, stop_event = self._loop, self._main_task, self._stop_event
        assert loop is not None and task is not None and stop_event is not None
        for slot in _SLOTS:
            self._stop_slot(slot)
        ready = self._ready
        if ready is not None and not ready.done():
            # Still in bring-up: cancel it (specs/core/bridge.md "Bring-up is cancellable").
            loop.call_soon_threadsafe(task.cancel)
        else:
            loop.call_soon_threadsafe(stop_event.set)
        thread.join()
        self._reset()

    def _reset(self) -> None:
        self._thread = None
        self._loop = None
        self._main_task = None
        self._stop_event = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        ready = self._ready
        assert ready is not None
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        self._stop_event = asyncio.Event()
        self._loop_ready.set()
        try:
            bridge = self._bridge
            await bridge.start()
            try:
                try:
                    self._emotions = await bridge.list_emotions()
                except Exception as exc:  # noqa: BLE001 - the panel works without the list
                    _logger.warning("could not list the emotions library: %s", exc)
                    self._emotions = []
                meters = [
                    asyncio.create_task(self._mic_meter(bridge)),
                    asyncio.create_task(self._face_meter(bridge)),
                ]
                ready.set_result(None)
                try:
                    await self._stop_event.wait()
                finally:
                    for meter in meters:
                        meter.cancel()
                    for meter in meters:
                        with suppress(BaseException):
                            await meter
            finally:
                await bridge.stop()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
                return
            _logger.exception("the robot session ended with an error")
        finally:
            self._mic_level = 0.0
            self._face_rate = None

    async def _mic_meter(self, bridge: ReachyMiniBridge) -> None:
        """Keep :attr:`mic_level` from the echo-cancelled mic (specs/examples/control_panel.md)."""
        try:
            rate = bridge.mic_sample_rate
            async for chunk in bridge.audio_input():
                pcm = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(np.square(pcm)))) if pcm.size else 0.0
                self._mic_level = max(rms, self._mic_level * MIC_LEVEL_DECAY)
                # Half the chunk's duration: drains faster than real time on a live
                # backend, and stops the fake (always a chunk ready) from spinning.
                await asyncio.sleep(0.5 * pcm.size / rate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead meter must not end the panel
            _logger.warning("the mic meter stopped: %s", exc)
            self._mic_level = 0.0

    async def _face_meter(self, bridge: ReachyMiniBridge) -> None:
        """Keep :attr:`face_rate` — new face observations per second, from the report's
        timestamps (specs/examples/control_panel.md "The face meter")."""
        arrivals: deque[float] = deque()
        last_ts: float | None = None
        while True:
            report = bridge.faces.value
            now = time.monotonic()
            if not report.active:
                arrivals.clear()
                last_ts = None
                self._face_rate = None
            else:
                if report.ts != last_ts:
                    if last_ts is not None:  # the first sample is not an arrival
                        arrivals.append(now)
                    last_ts = report.ts
                while arrivals and now - arrivals[0] > FACE_RATE_WINDOW_S:
                    arrivals.popleft()
                self._face_rate = len(arrivals) / FACE_RATE_WINDOW_S
            await asyncio.sleep(1.0 / FACE_METER_HZ)

    # --- submitting to the loop ------------------------------------------------------

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if not self.running or self._loop is None:
            raise BridgeError("the controller is not running (call start() first)")
        return self._loop

    def _call[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run an instant verb on the loop and return its result (or raise its error)."""
        try:
            loop = self._require_loop()
        except BridgeError:
            coro.close()
            raise
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def _run_spanning(self, slot: Slot, coro: Coroutine[Any, Any, None]) -> bool:
        """Run a spanning verb to its end; ``False`` if a stop cancelled it."""
        try:
            loop = self._require_loop()
        except BridgeError:
            coro.close()
            raise
        entry = _InFlight()

        async def guarded() -> bool:
            entry.task = asyncio.current_task()
            try:
                await coro
                return True
            except asyncio.CancelledError:
                # The verb has already stopped its effect (specs/core/bridge.md "Cancellation");
                # report the stop instead of propagating it to the caller thread.
                return False
            except (SpeechInterruptedError, SoundInterruptedError):
                return False  # a later say / sound file took over: this one stopped

        with self._in_flight_lock:
            self._in_flight[slot].append(entry)
        try:
            entry.future = asyncio.run_coroutine_threadsafe(guarded(), loop)
            try:
                return entry.future.result()
            except cf.CancelledError:
                return False  # cancelled before the coroutine ever started
        finally:
            with self._in_flight_lock:
                self._in_flight[slot].remove(entry)

    def _stop_slot(self, slot: Slot) -> int:
        """Cancel every verb in ``slot`` and wait for them to return; how many stopped."""
        with self._in_flight_lock:
            entries = list(self._in_flight[slot])
        loop = self._loop
        if not entries or loop is None:
            return 0
        for entry in entries:
            if entry.task is not None:
                loop.call_soon_threadsafe(entry.task.cancel)
            elif entry.future is not None:
                entry.future.cancel()
        # A verb's future completes once its guarded coroutine has returned — i.e. once
        # the bridge has stopped the effect — so waiting on it is the silence guarantee.
        futures = [entry.future for entry in entries if entry.future is not None]
        _, pending = cf.wait(futures, timeout=STOP_TIMEOUT_S)
        if pending:
            _logger.warning(
                "%d %s verb(s) did not stop within %s s",
                len(pending),
                slot,
                STOP_TIMEOUT_S,
            )
        return len(entries)

    @property
    def busy(self) -> list[str]:
        """The spanning-verb slots currently occupied (``"say"`` / ``"sound"`` /
        ``"emotion"``)."""
        with self._in_flight_lock:
            return [slot for slot in _SLOTS if self._in_flight[slot]]

    # --- state ------------------------------------------------------------------

    @property
    def emotions(self) -> list[str]:
        """The emotions library's names, cached at start (empty if it could not load)."""
        return list(self._emotions)

    @property
    def mic_level(self) -> float:
        """The mic meter's current level, 0–1."""
        return self._mic_level

    def _voice(self) -> str:
        bridge = self._bridge
        if bridge.synthesizer_error is not None:
            return f"unavailable: {bridge.synthesizer_error}"
        return "ready" if self._has_synthesizer else "none"

    def snapshot(self) -> PanelState:
        """Read everything the panel shows (one ``get_motors_state`` round trip)."""
        bridge = self._bridge
        try:
            motors = self.get_motors_state()
        except Exception as exc:  # noqa: BLE001 - shown as text, never a crash
            motors = f"unknown ({exc})"
        try:
            rate: int | None = bridge.mic_sample_rate
        except BridgeError:
            rate = None
        report = bridge.faces.value
        positions = [(f.x, f.y) for f in report.faces] if report.active else []
        return PanelState(
            backend=bridge.config.backend,
            motors=motors,
            presence=bridge.presence,
            idle=bridge.idle,
            wobbling=bridge.wobbling,
            tracking=bridge.tracking,
            attention=bridge.attention,
            face_detection=bridge.face_detection,
            faces=len(report.faces) if report.active else -1,
            face_positions=positions,
            face_target=self._face_target(report),
            face_rate=self._face_rate if report.active else None,
            voice=self._voice(),
            mic_level=self._mic_level,
            mic_sample_rate=rate,
            busy=self.busy,
            emotions=self.emotions,
        )

    @property
    def face_rate(self) -> float | None:
        """New face observations per second, ``None`` while no detector is looking."""
        return self._face_rate if self._bridge.faces.value.active else None

    def face_positions(self) -> list[tuple[float, float]]:
        """The reported faces' normalised ``(x, y)`` right now (no robot round trip)."""
        report = self._bridge.faces.value
        return [(f.x, f.y) for f in report.faces] if report.active else []

    def face_rolls(self) -> list[float | None]:
        """The reported faces' roll in radians (``None`` when unknown), in the order of
        :meth:`face_positions`."""
        report = self._bridge.faces.value
        return [f.roll for f in report.faces] if report.active else []

    def face_target(self) -> int | None:
        """The index in :meth:`face_positions` of the face the head follows — the
        tracker's ``head_tracking.track_id`` (specs/motion/head_tracking.md) looked up in
        the report; ``None`` while it follows none (or that face is not reported)."""
        return self._face_target(self._bridge.faces.value)

    def _face_target(self, report: FaceReport) -> int | None:
        followed = self._bridge.head_tracking.value.track_id
        if followed is None or not report.active:
            return None
        return next(
            (i for i, face in enumerate(report.faces) if face.track_id == followed),
            None,
        )

    def camera_frame_rgb(self) -> npt.NDArray[np.uint8] | None:
        """The camera feed's newest frame as RGB, or ``None`` while there is none.

        ``bridge.camera.latest()`` is a thread-safe sample, so no round trip to the loop;
        the feed's image is shared and read-only, so the flip to RGB is into a copy.
        """
        frame = self._bridge.camera.latest()
        return None if frame is None else np.ascontiguousarray(frame.image[:, :, ::-1])

    # --- instant verbs ------------------------------------------------------------

    def get_motors_state(self) -> str:
        return self._call(self._bridge.get_motors_state())

    def set_motors_state(self, state: str) -> None:
        self._call(self._bridge.set_motors_state(state))

    def start_head_tracking(self, focus: bool = False) -> None:
        self._call(self._bridge.start_head_tracking(focus=focus))

    def set_head_tracking(self, enabled: bool, focus: bool = False) -> None:
        """The Gaze checkboxes: tracking on (with or without focus) or off."""
        if enabled:
            self.start_head_tracking(focus)
        else:
            self.stop_head_tracking()

    def stop_head_tracking(self) -> None:
        self._call(self._bridge.stop_head_tracking())

    def set_wobbling(self, enabled: bool) -> None:
        self._call(self._bridge.set_wobbling(enabled))

    def set_presence(self, enabled: bool) -> None:
        self._call(self._bridge.set_presence(enabled))

    def set_idle(self, mode: str) -> None:
        self._call(self._bridge.set_idle(mode))

    def set_face_detection(self, enabled: bool) -> None:
        self._call(self._bridge.set_face_detection(enabled))

    # --- spanning verbs ---------------------------------------------------------------

    def say(self, text: str) -> bool:
        """Speak ``text`` and block until heard; ``False`` if stopped — or interrupted
        by a later ``say`` (the bridge's rule: the newest ``say`` wins)."""
        return self._run_spanning("say", self._bridge.say(text))

    def stop_saying(self) -> int:
        """Stop the `say` in flight, waiting until the speaker is flushed; how many stopped."""
        return self._stop_slot("say")

    def play_sound(self, sound_file: str) -> bool:
        """Play a sound file and block until heard; ``False`` if stopped — or replaced by a
        later sound file (the bridge's rule: the newest sound file wins)."""
        return self._run_spanning("sound", self._bridge.play_sound(sound_file))

    def stop_sound(self) -> int:
        """Stop the sound file in flight, waiting until it is stopped; how many stopped."""
        return self._stop_slot("sound")

    def play_emotion(self, name: str) -> bool:
        """Play an emotion and block until its trajectory has played; ``False`` if stopped.

        Not pre-empting: the bridge queues emotions FIFO, so a second call waits its turn.
        """
        return self._run_spanning("emotion", self._bridge.play_emotion(name))

    def stop_emotion(self) -> int:
        """Stop the playing emotion and every queued one; how many stopped."""
        return self._stop_slot("emotion")


def draw_faces(
    frame: npt.NDArray[np.uint8],
    positions: Sequence[tuple[float, float]],
    rolls: Sequence[float | None] = (),
    target: int | None = None,
) -> npt.NDArray[np.uint8]:
    """A copy of the RGB ``frame`` with a square outline on each face.

    ``positions`` are normalised image coordinates (``[-1, 1]``, x right, y down), as
    ``bridge.faces`` reports them; the face at index ``target`` — the one the head
    follows — is drawn thicker, none when ``None``. ``rolls`` (radians, per face,
    ``None`` when unknown) tilt each square with the face's eye line, in the frame's
    own coordinates.
    """
    out = frame.copy()
    height, width = out.shape[:2]
    half = max(2, round(FACE_MARKER_SIZE * height / 2))
    for i, (x, y) in enumerate(positions):
        roll = rolls[i] if i < len(rolls) else None
        cx = round((x + 1.0) * 0.5 * (width - 1))
        cy = round((y + 1.0) * 0.5 * (height - 1))
        thick = max(1, height // (120 if i == target else 240))
        reach = math.ceil(half * math.sqrt(2)) + 1  # the tilted square's bounding box
        x0, x1 = max(cx - reach, 0), min(cx + reach, width - 1)
        y0, y1 = max(cy - reach, 0), min(cy + reach, height - 1)
        if x0 > x1 or y0 > y1:
            continue  # off the frame
        ys, xs = np.mgrid[y0 : y1 + 1, x0 : x1 + 1]
        dx, dy = xs - cx, ys - cy
        c, s = math.cos(roll or 0.0), math.sin(roll or 0.0)
        # each pixel in the square's own axes: u along the eye line, v across it
        u, v = dx * c + dy * s, -dx * s + dy * c
        edge = np.maximum(np.abs(u), np.abs(v))
        mask = (edge <= half + 0.5) & (edge > half + 0.5 - thick)
        out[y0 : y1 + 1, x0 : x1 + 1][mask] = FACE_MARKER_RGB
    return out
