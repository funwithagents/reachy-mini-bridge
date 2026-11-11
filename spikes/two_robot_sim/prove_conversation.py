"""Two cables, both directions at once: each robot's mouth in the other robot's ear.

Needs the host running with each robot's output on a cable the other robot listens to:

    uv run python -m spikes.two_robot_sim.host --headless \\
        --audio-output-device-a a_mouth --audio-input-device-b b_ear \\
        --audio-output-device-b b_mouth --audio-input-device-a a_ear
    uv run python -m spikes.two_robot_sim.prove_conversation [--say "text"]

(``b_ear`` hears ``a_mouth``, ``a_ear`` hears ``b_mouth``: on Linux two PulseAudio null
sinks, each with a source remapped over its monitor.) One ``ReachyMiniBridge`` per robot,
a process each, both following their microphone for the whole run. Three windows on one
wall clock: robot a plays alone, robot b plays alone, both play at once. For each window
the script reads, from each bridge's own microphone feed, what that robot heard:

* the other robot's sound is there (RMS well above the silence before);
* its own sound is not (no acoustic path: a robot's mouth is not wired to its own ear);
* each verb lasts about its sound's duration.

Exits non-zero when a check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from reachy_mini_bridge.daemon import scrubbed_env
from spikes.two_robot_sim.prove_audio import TONE_S, write_tone

LOG_PATH = os.environ.get("PROVE_AUDIO_LOG", os.devnull)  # the children's stderr
LEAD_S = float(
    os.environ.get("PROVE_CONVERSATION_LEAD_S", "40")
)  # for both sessions to open
WINDOW_S = 7.0
HEARD_RMS = 0.01
SILENT_RMS = 0.002


def rms(timeline: list[list[float]], start: float, end: float) -> float:
    values = [v for t, v in timeline if start <= t <= end]
    return float(np.sqrt(np.mean(np.square(values)))) if values else float("nan")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", type=int, nargs=2, default=[8011, 8012])
    parser.add_argument(
        "--say", help="speak this text (phonon) instead of playing the tone"
    )
    ns = parser.parse_args()
    t0 = time.time() + LEAD_S
    # a alone, b alone, both
    schedule = {"a": [t0, t0 + 2 * WINDOW_S], "b": [t0 + WINDOW_S, t0 + 2 * WINDOW_S]}
    until = t0 + 3 * WINDOW_S
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        tone = tmp / "tone.wav"
        write_tone(tone)
        sound = ["--say", ns.say] if ns.say else ["--wav", str(tone)]
        sessions = {}
        with open(LOG_PATH, "ab") as log:
            for robot, port in zip("ab", ns.ports, strict=True):
                out = tmp / f"{robot}.json"
                args = ["--port", str(port), "--out", str(out), "--listen", *sound]
                args += [
                    "--until-wall",
                    str(until),
                    "--play-at-wall",
                    *map(str, schedule[robot]),
                ]
                command = [
                    sys.executable,
                    "-m",
                    "spikes.two_robot_sim.sound_session",
                    *args,
                ]
                sessions[robot] = (
                    subprocess.Popen(command, stderr=log, env=scrubbed_env()),
                    out,
                )
        try:
            for robot, (process, _) in sessions.items():
                if process.wait(timeout=LEAD_S + 3 * WINDOW_S + 120) != 0:
                    raise SystemExit(f"bridge {robot}'s session failed")
        finally:
            for process, _ in sessions.values():
                if process.poll() is None:
                    process.kill()
        results = {
            robot: json.loads(out.read_text()) for robot, (_, out) in sessions.items()
        }
    what = f"say({ns.say!r})" if ns.say else f"play_sound(a {TONE_S:.0f} s 440 Hz tone)"
    print(f"=== both directions at once: {what}")
    for robot in "ab":
        print(f"  bridge {robot}: {results[robot]['audio']}")
    failures = 0
    plays = {robot: results[robot]["plays"] for robot in "ab"}
    windows = (
        ("robot a plays alone", {"a": plays["a"][0]}),
        ("robot b plays alone", {"b": plays["b"][0]}),
        ("both play at once", {"a": plays["a"][1], "b": plays["b"][1]}),
    )
    for label, playing in windows:
        start = max(p["start"] for p in playing.values())
        end = min(p["end"] for p in playing.values())
        print(f"--- {label}")
        for robot, play in playing.items():
            ok = ns.say or TONE_S - 0.2 <= play["elapsed"] <= TONE_S + 1.5
            failures += not ok
            print(
                f"  bridge {robot}: verb took {play['elapsed']:.2f} s{'' if ok else ' FAIL'}"
            )
        for listener, speaker in (("a", "b"), ("b", "a")):
            mic = results[listener]["mic"]
            before = rms(mic, start - 2.5, start - 0.5)
            during = rms(mic, start + 0.5, end - 0.7)
            if speaker in playing:
                ok = during > HEARD_RMS
                expected = f"robot {speaker}'s sound"
            else:
                ok = during < SILENT_RMS
                expected = "silence (only its own robot plays)"
            failures += not ok
            print(
                f"  robot {listener}'s ear (bridge {listener}'s mic feed): RMS before "
                f"{before:.5f}, during {during:.5f} -> {'OK' if ok else 'FAIL'}, expected {expected}"
            )
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
