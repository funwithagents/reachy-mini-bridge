"""One ``ReachyMiniBridge`` session on one robot of the two-robot host: play, and listen.

    uv run python -m spikes.two_robot_sim.sound_session --port 8011 --out a.json \\
        --seconds 12 [--play-at 4 --wav tone.wav | --play-at 4 --say "Hello"] [--listen]

Runs for ``--seconds`` once the session is open. At ``--play-at`` it calls ``play_sound(wav)`` or ``say(text)`` and
times the verb. With ``--listen`` it follows ``bridge.audio_input()`` for the whole run and
keeps the loudness of what the robot's microphone hears. Writes
``{"robot", "audio", "play": {"start", "end", "elapsed"} | null, "mic": [[wall_s, rms], …]}``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from reachy_mini_bridge import ReachyMiniBridge, ReachyMiniConfig
from spikes.two_robot_sim.client import bind_camera_to


async def listen(bridge: ReachyMiniBridge, timeline: list[tuple[float, float]]) -> None:
    pending = np.zeros(0, dtype=np.int16)
    block = bridge.mic_sample_rate // 10
    async for pcm in bridge.audio_input(mono=True):
        pending = np.concatenate([pending, np.frombuffer(pcm, dtype=np.int16)])
        while len(pending) >= block:
            chunk, pending = (
                pending[:block].astype(np.float32) / 32768.0,
                pending[block:],
            )
            timeline.append((time.time(), float(np.sqrt(np.mean(np.square(chunk))))))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--play-at", type=float)
    parser.add_argument("--wav")
    parser.add_argument("--say")
    parser.add_argument("--listen", action="store_true")
    ns = parser.parse_args()
    report = bind_camera_to(ns.port)
    config: dict[str, Any] = {
        "backend": "sim",
        "robot": {
            "connection_mode": "network",
            "host": "127.0.0.1",
            "port": ns.port,
            "media_backend": "local",
        },
    }
    if ns.say:
        config["tts"] = {"module": {"type": "phonon", "voice": "Micah"}}
    mic: list[tuple[float, float]] = []
    play: dict[str, float] | None = None
    async with ReachyMiniBridge(ReachyMiniConfig.from_dict(config)) as bridge:
        print(f"session open on robot {report['prefix']}", flush=True)
        start = time.monotonic()  # --play-at and --seconds count from the open session
        listener = asyncio.create_task(listen(bridge, mic)) if ns.listen else None
        if ns.play_at is not None:
            await asyncio.sleep(max(0.0, ns.play_at - (time.monotonic() - start)))
            began, t0 = time.time(), time.monotonic()
            if ns.say:
                await bridge.say(ns.say)
            else:
                await bridge.play_sound(ns.wav)
            play = {
                "start": began,
                "end": time.time(),
                "elapsed": time.monotonic() - t0,
            }
        await asyncio.sleep(max(0.0, ns.seconds - (time.monotonic() - start)))
        if listener is not None:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        audio = bridge.daemon.audio if bridge.daemon is not None else None
    result = {"robot": report["prefix"], "audio": str(audio), "play": play, "mic": mic}
    await asyncio.to_thread(Path(ns.out).write_text, json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
