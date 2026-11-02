"""The voice tour: one robot performs a text, each part in a different voice.

Each voice is a bridge config under ``voice-configs/`` whose ``tts`` block is what any bridge user
would write, and the tour builds one ``TTSEngineSynthesizer`` from each, the adapter the
bridge builds from a ``tts`` block, handing it to ``say`` per call. One bridge, so the
robot stays alive across the handovers: breathing underneath, the head swaying with each
voice.

Every engine is built and warmed before the show, so the handover is the next voice's time
to first audio and no loading. A voice that cannot be built (no key, a missing extra, a
missing model) is dropped with one line, and the tour goes on without its part.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from tts_engine.config import TTSEngineConfig

from reachy_mini_bridge import (
    BridgeError,
    ReachyMiniBridge,
    ReachyMiniConfig,
    SpeechSynthesizer,
    TTSEngineSynthesizer,
)

log = logging.getLogger("voice_tour")

HERE = Path(__file__).parent
VOICES = HERE / "voice-configs"
DEFAULT_TEXT = HERE / "slam.json"
# Added to what a handover already takes: the end of the audio, then the next first audio.
VOICE_PAUSE_S = 0.5
LINE_PAUSE_S = 0.15


@dataclass(frozen=True)
class Line:
    """One sentence, with the pause added before it when it differs from the tour's: the
    place to shorten the wait into a voice that is slow to start."""

    text: str
    pause_s: float | None = None


@dataclass(frozen=True)
class Part:
    """One voice's turn: each of its ``lines`` is spoken on its own, a short pause apart."""

    voice: str
    label: str
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class Beat:
    """What one part did, times in seconds from the first part's request: the raw
    material of a recording's captions and the handovers' measure."""

    part: int
    voice: str
    label: str
    hosted: bool
    start_s: float  # the first chunk reached the bridge
    end_s: float  # the last `say` returned: the audio has been heard
    gap_before_s: float  # from the previous part's end to this start
    ttfa_ms: float  # from the first `say` to the first chunk
    lines: tuple[str, ...]
    line_starts_s: tuple[float, ...]  # each line's first chunk, for its subtitle


class TimedSynthesizer:
    """A synthesizer that records when its first chunk of each utterance arrives."""

    def __init__(self, inner: SpeechSynthesizer) -> None:
        self._inner = inner
        self.first_chunk_at: float | None = None

    @property
    def sample_rate(self) -> int:
        return self._inner.sample_rate

    async def stream(self, text: str) -> AsyncIterator[npt.NDArray[np.float32]]:
        self.first_chunk_at = None
        async for chunk in self._inner.stream(text):
            if self.first_chunk_at is None:
                self.first_chunk_at = time.perf_counter()
            yield chunk


@dataclass(frozen=True)
class Voice:
    synth: TimedSynthesizer
    hosted: bool


def _load_line(line: str | dict[str, Any]) -> Line:
    """A line is its text, or ``{"text": ..., "pause_s": ...}``. A pause cannot be negative:
    a `say` started during another interrupts it."""
    if isinstance(line, str):
        return Line(line)
    pause_s = line.get("pause_s")
    if pause_s is not None and pause_s < 0:
        raise ValueError(f"negative pause_s before {line['text']!r}")
    return Line(line["text"], pause_s)


def load_parts(path: Path) -> list[Part]:
    parts = json.loads(path.read_text(encoding="utf-8"))["parts"]
    return [
        Part(p["voice"], p["label"], tuple(_load_line(line) for line in p["lines"]))
        for p in parts
    ]


def _is_hosted(voice_file: Path) -> bool:
    """A hosted module names its key; a local one needs none."""
    return (
        "api_key_env"
        in json.loads(voice_file.read_text(encoding="utf-8"))["tts"]["module"]
    )


async def _build_voice(voice_file: Path) -> TimedSynthesizer:
    """Load the voice's engine, then synthesize once: a first synthesis is slower than the
    next ones, and that one is not part of the show."""
    block = ReachyMiniConfig.from_json_file(voice_file).tts
    if block is None:
        raise BridgeError(f"{voice_file.name} has no tts block")
    # A relative model path is the voice file's, wherever the tour is run from.
    config = TTSEngineConfig.from_dict(block, base_dir=voice_file.parent)
    synth = TimedSynthesizer(await asyncio.to_thread(TTSEngineSynthesizer, config))
    async for _ in synth.stream("Warming up."):
        pass
    return synth


async def build_voices(
    parts: list[Part], voices_dir: Path, *, local_only: bool = False
) -> dict[str, Voice]:
    """Every voice the parts name, built and warmed, without the ones that failed."""
    voices: dict[str, Voice] = {}
    for name in dict.fromkeys(part.voice for part in parts):
        voice_file = voices_dir / f"{name}.json"
        started = time.perf_counter()
        try:
            hosted = _is_hosted(voice_file)
            if hosted and local_only:
                log.info("%s: left out, hosted", name)
                continue
            voices[name] = Voice(await _build_voice(voice_file), hosted)
        except Exception as e:  # noqa: BLE001 - a voice that fails must not end the tour
            log.warning("%s: dropped, %s", name, e)
            continue
        log.info("%s: ready in %.1f s", name, time.perf_counter() - started)
    return voices


def _pause_before(line: Line, default_s: float) -> float:
    return default_s if line.pause_s is None else line.pause_s


async def _speak_lines(
    bridge: ReachyMiniBridge, part: Part, synth: TimedSynthesizer, line_pause_s: float
) -> list[float]:
    """Say each line of a part, and return when each one's first chunk arrived."""
    first_chunks: list[float] = []
    for index, line in enumerate(part.lines):
        if index > 0:
            await asyncio.sleep(_pause_before(line, line_pause_s))
        await bridge.say(line.text, synth)
        first_chunks.append(synth.first_chunk_at or time.perf_counter())
    return first_chunks


async def perform(
    bridge: ReachyMiniBridge,
    parts: list[Part],
    voices: dict[str, Voice],
    *,
    voice_pause_s: float = VOICE_PAUSE_S,
    line_pause_s: float = LINE_PAUSE_S,
) -> list[Beat]:
    """Play the parts whose voice is there, one after the other, on a started bridge."""
    beats: list[Beat] = []
    show_start = time.perf_counter()
    previous_end_s = 0.0
    for number, part in enumerate(parts, start=1):
        voice = voices.get(part.voice)
        if voice is None:
            continue
        if beats:
            await asyncio.sleep(_pause_before(part.lines[0], voice_pause_s))
        log.info(
            "part %d: %s (%s)",
            number,
            part.label,
            "hosted" if voice.hosted else "local",
        )
        requested_at = time.perf_counter()
        try:
            first_chunks = await _speak_lines(bridge, part, voice.synth, line_pause_s)
        except BridgeError as e:
            log.warning("part %d: %s failed, %s", number, part.label, e)
            continue
        ended_at = time.perf_counter()
        first_chunk_at = first_chunks[0]
        beat = Beat(
            part=number,
            voice=part.voice,
            label=part.label,
            hosted=voice.hosted,
            start_s=round(first_chunk_at - show_start, 3),
            end_s=round(ended_at - show_start, 3),
            gap_before_s=round(first_chunk_at - show_start - previous_end_s, 3),
            ttfa_ms=round((first_chunk_at - requested_at) * 1000, 1),
            lines=tuple(line.text for line in part.lines),
            line_starts_s=tuple(round(t - show_start, 3) for t in first_chunks),
        )
        beats.append(beat)
        previous_end_s = beat.end_s
        log.info(
            "part %d: first audio %.0f ms, gap %.2f s",
            number,
            beat.ttfa_ms,
            beat.gap_before_s,
        )
    return beats


async def press_enter() -> None:
    """Wait for Enter. The reader is a daemon thread, so a Ctrl-C here still ends the run."""
    loop = asyncio.get_running_loop()
    pressed = asyncio.Event()

    def read() -> None:
        with contextlib.suppress(EOFError):
            input()
        loop.call_soon_threadsafe(pressed.set)

    threading.Thread(target=read, daemon=True).start()
    await pressed.wait()


async def run(
    config: ReachyMiniConfig,
    parts: list[Part],
    voices_dir: Path = VOICES,
    *,
    local_only: bool = False,
    voice_pause_s: float = VOICE_PAUSE_S,
    line_pause_s: float = LINE_PAUSE_S,
    go: Callable[[], Awaitable[None]] = press_enter,
) -> list[Beat]:
    """The whole tour: the engines, the bridge session, the signal to start, the show."""
    voices = await build_voices(parts, voices_dir, local_only=local_only)
    missing = [part.label for part in parts if part.voice not in voices]
    if len(missing) == len(parts):
        log.error("no voice left, nothing to perform")
        return []
    async with ReachyMiniBridge(config) as bridge:
        await bridge.set_motors_state("enabled")  # breathing, and the sway over it
        log.info(
            "%d parts ready, left out: %s", len(parts) - len(missing), missing or "none"
        )
        log.info("press Enter to start")
        await go()
        return await perform(
            bridge,
            parts,
            voices,
            voice_pause_s=voice_pause_s,
            line_pause_s=line_pause_s,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m examples.voice_tour",
        description="One robot, one text, a different voice for each part.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="a ReachyMiniConfig JSON file for the robot, e.g. examples/voice_tour/configs/lite-usb.json",
    )
    parser.add_argument(
        "--text",
        type=Path,
        default=DEFAULT_TEXT,
        help=f"the parts, each naming its voice (default: {DEFAULT_TEXT})",
    )
    parser.add_argument(
        "--local-only", action="store_true", help="leave the hosted voices out"
    )
    parser.add_argument(
        "--voice-pause",
        type=float,
        default=VOICE_PAUSE_S,
        help="seconds added between two voices, unless a line sets its own (default: %(default)s)",
    )
    parser.add_argument(
        "--line-pause",
        type=float,
        default=LINE_PAUSE_S,
        help="seconds added between two lines of a voice, unless a line sets its own (default: %(default)s)",
    )
    parser.add_argument(
        "--timeline",
        type=Path,
        help="write each part's times and lines to this JSON file",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    config = ReachyMiniConfig.from_json_file(args.config)
    parts = load_parts(args.text)
    with contextlib.suppress(KeyboardInterrupt):
        beats = asyncio.run(
            run(
                config,
                parts,
                local_only=args.local_only,
                voice_pause_s=args.voice_pause,
                line_pause_s=args.line_pause,
            )
        )
        if args.timeline is not None:
            args.timeline.write_text(
                json.dumps(
                    [asdict(beat) for beat in beats], indent=2, ensure_ascii=False
                )
                + "\n",
                encoding="utf-8",
            )
            log.info("timeline written to %s", args.timeline)


if __name__ == "__main__":
    main()
