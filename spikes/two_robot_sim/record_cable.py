"""Record an input device from a process of its own and write its loudness over time.

    uv run python -m spikes.two_robot_sim.record_cable --device "BlackHole 2ch" \\
        --seconds 12 --out timeline.json

The timeline is ``[[wall_clock_s, rms], …]``, one entry per 100 ms block, the RMS of the
float samples in [-1, 1]. A process of its own because on macOS a process that plays into
a loopback cable cannot rely on its own capture of it.
"""

from __future__ import annotations

import argparse
import json
import signal
import time

import numpy as np
import sounddevice as sd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--out", required=True)
    ns = parser.parse_args()
    rate = 48000
    timeline: list[tuple[float, float]] = []
    with sd.InputStream(
        device=ns.device, samplerate=rate, channels=2, dtype="float32"
    ) as stream:
        print("recording", flush=True)
        end = time.time() + ns.seconds
        stop: list[int] = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))  # write what was heard
        while time.time() < end and not stop:
            block, _overflowed = stream.read(rate // 10)
            timeline.append((time.time(), float(np.sqrt(np.mean(np.square(block))))))
    with open(ns.out, "w") as out:
        json.dump(timeline, out)


if __name__ == "__main__":
    main()
