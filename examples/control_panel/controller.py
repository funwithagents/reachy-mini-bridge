"""The control panel's gradio-free core: one `ReachyMiniApi` session on a background
event loop, exposed to synchronous callers (specs/control_panel.md "The controller").

Gradio runs its handlers in worker threads, so the api — async-native, one session
that must outlive every request — lives on a thread of its own with its own asyncio
loop. Instant verbs submit a coroutine there and wait; the two spanning verbs (`say`,
`play_emotion`) block for their whole effect and can be stopped from any other thread
through `stop_saying()` / `stop_emotion()`, which cancel the task on the loop and wait
until the verb has returned — the api stops the effect before re-raising its
`CancelledError`, so "returned" means "silent" / "no longer commanded".
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import logging
import threading
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal, Self

import numpy as np
import numpy.typing as npt

from reachy_mini_bridge import (
    BridgeError,
    ReachyMiniApi,
    ReachyMiniConfig,
    SpeechSynthesizer,
)

_logger = logging.getLogger(__name__)

Slot = Literal["say", "emotion"]
_SLOTS: tuple[Slot, ...] = ("say", "emotion")

# The mic level decays by this factor per chunk when quieter than the last, so a short
# burst survives until the panel's next 0.5 s refresh (chunks are ~10 ms).
MIC_LEVEL_DECAY = 0.97
# How long a stop waits for the cancelled verbs to return before giving up on them.
STOP_TIMEOUT_S = 5.0


@dataclass
class PanelState:
    """Everything the panel displays, read in one go by :meth:`snapshot`."""

    backend: str
    motors: str
    presence: bool
    breathing: bool
    wobbling: bool
    tracking: bool
    attention: str | None
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
    """Owns one ``ReachyMiniApi`` session on a background loop; a sync facade over it.

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
        self._api = ReachyMiniApi(config, synthesizer=synthesizer)
        self._has_synthesizer = (
            synthesizer is not None or self._api.config.tts is not None
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

    # --- lifecycle -----------------------------------------------------------------

    @property
    def api(self) -> ReachyMiniApi:
        """The api this controller drives (its config is readable at any time)."""
        return self._api

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
        api unwinds what it had started, and ``TimeoutError`` is raised.
        """
        if self._thread is not None:
            raise BridgeError("the controller is already started")
        self._ready = cf.Future()
        self._loop_ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="control-panel-api", daemon=True
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
            # Still in bring-up: cancel it (specs/api.md "Bring-up is cancellable").
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
            async with self._api as api:
                try:
                    self._emotions = await api.list_emotions()
                except Exception as exc:  # noqa: BLE001 - the panel works without the list
                    _logger.warning("could not list the emotions library: %s", exc)
                    self._emotions = []
                meter = asyncio.create_task(self._mic_meter(api))
                ready.set_result(None)
                try:
                    await self._stop_event.wait()
                finally:
                    meter.cancel()
                    with suppress(BaseException):
                        await meter
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
                return
            _logger.exception("the robot session ended with an error")
        finally:
            self._mic_level = 0.0

    async def _mic_meter(self, api: ReachyMiniApi) -> None:
        """Keep :attr:`mic_level` from the echo-cancelled mic (specs/control_panel.md)."""
        try:
            rate = api.mic_sample_rate
            async for chunk in api.audio_input():
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
                # The verb has already stopped its effect (specs/api.md "Cancellation");
                # report the stop instead of propagating it to the caller thread.
                return False

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
        # the api has stopped the effect — so waiting on it is the silence guarantee.
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
        """The spanning-verb slots currently occupied (``"say"`` / ``"emotion"``)."""
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
        api = self._api
        if api.synthesizer_error is not None:
            return f"unavailable: {api.synthesizer_error}"
        return "ready" if self._has_synthesizer else "none"

    def snapshot(self) -> PanelState:
        """Read everything the panel shows (one ``get_motors_state`` round trip)."""
        api = self._api
        try:
            motors = self.get_motors_state()
        except Exception as exc:  # noqa: BLE001 - shown as text, never a crash
            motors = f"unknown ({exc})"
        try:
            rate: int | None = api.mic_sample_rate
        except BridgeError:
            rate = None
        return PanelState(
            backend=api.config.backend,
            motors=motors,
            presence=api.presence,
            breathing=api.breathing,
            wobbling=api.wobbling,
            tracking=api.tracking,
            attention=api.attention,
            voice=self._voice(),
            mic_level=self._mic_level,
            mic_sample_rate=rate,
            busy=self.busy,
            emotions=self.emotions,
        )

    def camera_frame_rgb(self) -> npt.NDArray[np.uint8] | None:
        """The latest camera frame as RGB (the api's BGR flipped), or ``None``."""
        frame = self._call(self._api.get_camera_frame())
        return None if frame is None else np.ascontiguousarray(frame[:, :, ::-1])

    # --- instant verbs ------------------------------------------------------------

    def get_motors_state(self) -> str:
        return self._call(self._api.get_motors_state())

    def set_motors_state(self, state: str) -> None:
        self._call(self._api.set_motors_state(state))

    def play_sound(self, sound_file: str) -> None:
        self._call(self._api.play_sound(sound_file))

    def start_head_tracking(self, weight: float = 1.0) -> None:
        self._call(self._api.start_head_tracking(weight))

    def stop_head_tracking(self) -> None:
        self._call(self._api.stop_head_tracking())

    def set_wobbling(self, enabled: bool) -> None:
        self._call(self._api.set_wobbling(enabled))

    def set_presence(self, enabled: bool) -> None:
        self._call(self._api.set_presence(enabled))

    def set_breathing(self, enabled: bool) -> None:
        self._call(self._api.set_breathing(enabled))

    # --- spanning verbs ---------------------------------------------------------------

    def say(self, text: str) -> bool:
        """Speak ``text`` and block until heard; ``False`` if stopped.

        A `say` already playing is stopped first (barge-in): two on one media session
        would interleave.
        """
        self.stop_saying()
        return self._run_spanning("say", self._api.say(text))

    def stop_saying(self) -> int:
        """Stop the `say` in flight, waiting until the speaker is flushed; how many stopped."""
        return self._stop_slot("say")

    def play_emotion(self, name: str) -> bool:
        """Play an emotion and block until its trajectory has played; ``False`` if stopped.

        Not pre-empting: the api queues emotions FIFO, so a second call waits its turn.
        """
        return self._run_spanning("emotion", self._api.play_emotion(name))

    def stop_emotion(self) -> int:
        """Stop the playing emotion and every queued one; how many stopped."""
        return self._stop_slot("emotion")
