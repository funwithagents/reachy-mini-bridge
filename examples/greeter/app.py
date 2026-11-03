"""The greeter's logic: one loop over the head tracker's state.

- nobody in view: the idle move (breathing) runs, the tracker is on and waits;
- a face appears: the head starts following it and, ``HELLO_DELAY_S`` later so the gaze
  has settled on the face, the robot plays a welcoming emotion and says "Hello there";
- the person comes too close (enters the personal space, ``in_personal_space``): the
  robot is startled and says so, as its head steps back; they step back out: relieved;
- the face is gone (the tracker hands the head back after its loss timeout): the robot
  says "Good bye" with a short farewell move, and the head eases back into breathing.

The config must name a detector, turn tracking on (``motion.tracking.enabled``), and carry a ``tts`` block for the voice:
the two under ``configs/`` do (the sim seeing through the webcam, the default; a Lite over
USB), both on the local phonon voice (the ``tts-phonon`` extra, in a plain ``uv sync``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from pathlib import Path

from reachy_mini_bridge import BridgeError, HeadTrackingReport, ReachyMiniBridge

log = logging.getLogger("greeter")

CONFIGS = Path(__file__).parent / "configs"
DEFAULT_CONFIG = CONFIGS / "sim-webcam.json"

# The first name of each list the library has (the fake lists `happy` and `sad`).
HELLO_EMOTIONS = ("welcoming1", "welcoming2", "cheerful1", "happy")
GOODBYE_EMOTIONS = ("downcast1", "sad1", "sad")
TOO_CLOSE_EMOTIONS = ("surprised1", "fear1")
RELIEVED_EMOTIONS = ("relief1", "relief2")
HELLO_DELAY_S = 1.0  # the head turns onto the face before the greeting starts


def pick(candidates: tuple[str, ...], available: list[str]) -> str | None:
    return next((name for name in candidates if name in available), None)


async def say_safely(bridge: ReachyMiniBridge, text: str) -> None:
    """Speak, or log why the robot could not (no voice configured, provider not installed)."""
    try:
        await bridge.say(text)
    except BridgeError as e:
        log.warning("could not say %r: %s", text, e)


async def greet(bridge: ReachyMiniBridge, emotion: str | None) -> None:
    """Hello: the voice and the move at the same time — ``say`` and ``play_emotion`` run
    together, wobbling paused for the move so speech does not sway the head over it."""
    log.info("someone is here: hello in %.1f s", HELLO_DELAY_S)
    await asyncio.sleep(HELLO_DELAY_S)  # the gaze settles on the face first
    calls = [say_safely(bridge, "Hello there")]
    if emotion is not None:
        calls.append(bridge.play_emotion(emotion))
    await asyncio.gather(*calls)


async def react(bridge: ReachyMiniBridge, text: str, emotion: str | None) -> None:
    """A line and a move at the same time, as the greeting does."""
    calls = [say_safely(bridge, text)]
    if emotion is not None:
        calls.append(bridge.play_emotion(emotion))
    await asyncio.gather(*calls)


async def farewell(bridge: ReachyMiniBridge, emotion: str | None) -> None:
    log.info("nobody left: good bye")
    calls = [say_safely(bridge, "Good bye")]
    if emotion is not None:
        calls.append(bridge.play_emotion(emotion))
    await asyncio.gather(*calls)


async def run(bridge: ReachyMiniBridge) -> None:
    """The greeter over a bridge: runs until cancelled."""
    async with bridge:
        if bridge.synthesizer_error is not None:
            log.warning("no voice: %s", bridge.synthesizer_error)
        await bridge.set_motors_state(
            "enabled"
        )  # breathing starts, the head can follow
        emotions = await bridge.list_emotions()
        hello = pick(HELLO_EMOTIONS, emotions)
        goodbye = pick(GOODBYE_EMOTIONS, emotions)
        too_close = pick(TOO_CLOSE_EMOTIONS, emotions)
        relieved = pick(RELIEVED_EMOTIONS, emotions)
        log.info("waiting for a face (hello: %s, goodbye: %s)", hello, goodbye)

        engaged = bridge.head_tracking.value.attention == "engaged"
        close = False
        # `head_tracking.changes()` wakes when the head engages someone, hands it back,
        # switches person, or the person it follows enters or leaves the personal space —
        # never when the face moves. Two edges matter here: engaged <-> not engaged (a
        # switch from one person to another is the same visitor), and in <-> out of the
        # personal space while someone is engaged.
        async for state in bridge.head_tracking.changes():
            now_engaged = state.attention == "engaged"
            if now_engaged != engaged:
                engaged = now_engaged
                if engaged:
                    await greet(bridge, hello)
                else:
                    close = False  # the visitor left: no relief to express
                    await farewell(bridge, goodbye)
                _log_state(state)
            # the latest state, not the one that woke the loop: a greeting takes seconds
            now_close = engaged and bridge.head_tracking.value.in_personal_space
            if now_close != close:
                close = now_close
                if close:
                    log.info("too close")
                    await react(bridge, "Whoa, you're a bit close!", too_close)
                else:
                    log.info("back at a comfortable distance")
                    await react(bridge, "Ah, that's better.", relieved)


def _log_state(state: HeadTrackingReport) -> None:
    log.info(
        "attention %s (track %s, in personal space: %s)",
        state.attention,
        state.track_id,
        state.in_personal_space,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m examples.greeter",
        description="The robot breathes until someone shows up, then greets and follows them, and reacts when they come too close.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"a ReachyMiniConfig JSON file with a detector, tracking and a voice (default: {DEFAULT_CONFIG})",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    bridge = ReachyMiniBridge.from_json_file(args.config)
    if not bridge.config.motion.tracking.enabled:
        parser.error(f"{args.config}: the greeter needs motion.tracking.enabled on")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(bridge))


if __name__ == "__main__":
    main()
