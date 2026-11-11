"""Sound out per robot, and robot a's mouth into robot b's ear.

Needs the host running with robot a playing into the loopback cable, robot b's output
discarded and robot b's microphone on the cable:

    uv run python -m spikes.two_robot_sim.host --headless \\
        --audio-output-device-a "BlackHole 2ch" --discard-audio-output-b \\
        --audio-input-device-a "Micro MacBook Pro" --audio-input-device-b "BlackHole 2ch"
    uv run python -m spikes.two_robot_sim.prove_audio [--say "text"]

Every role is a process of its own: one ``ReachyMiniBridge`` per robot
(``sound_session``) and the cable's recorder (``record_cable``). Three scenarios:

1. bridge a plays: the cable carries it, the verb lasts about the sound's duration;
2. bridge b plays the same: the cable stays silent, the verb lasts as long;
3. both play at once while bridge b listens: b's microphone feed carries a's sound.

Exits non-zero when a check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from reachy_mini_bridge.daemon import scrubbed_env

CABLE = "BlackHole 2ch"
TONE_S = 3.0
PLAY_AT_S = 3.0
SESSION_S = 10.0
SILENT_RMS = 1e-4  # the cable with nothing playing reads exactly 0
LOUD_RMS = 0.01
LOG_PATH = os.environ.get("PROVE_AUDIO_LOG", os.devnull)  # the children's stderr


def write_tone(path: Path) -> None:
    rate = 48000
    t = np.arange(int(rate * TONE_S)) / rate
    samples = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())


def module(name: str, *args: str) -> subprocess.Popen[bytes]:
    # A Python child of a Python process inherits the GStreamer paths the wheels' .pth
    # set, sets them again and crashes scanning plugins: the bridge's own spawn
    # environment (``scrubbed_env``) drops them.
    with open(LOG_PATH, "ab") as log:
        return subprocess.Popen(
            [sys.executable, "-m", f"spikes.two_robot_sim.{name}", *args],
            stdout=subprocess.PIPE,
            stderr=log,
            env=scrubbed_env(),
        )


def wait_line(process: subprocess.Popen[bytes], word: str) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        if word in line.decode():
            return
    raise SystemExit(f"a child process ended before saying {word!r}")


def rms(timeline: list[list[float]], start: float, end: float) -> float:
    values = [v for t, v in timeline if start <= t <= end]
    return float(np.sqrt(np.mean(np.square(values)))) if values else float("nan")


def scenario(
    tmp: Path,
    label: str,
    players: dict[str, int],
    listener: str | None,
    sound: list[str],
) -> dict[str, dict]:
    """Run one bridge session per robot in ``players`` (each plays ``sound``), the one
    named ``listener`` also following its microphone, with the cable recorded throughout."""
    cable_file = tmp / f"{label}_cable.json"
    recorder = module(
        "record_cable",
        "--device",
        CABLE,
        "--seconds",
        str(SESSION_S + 25),
        "--out",
        str(cable_file),
    )
    wait_line(recorder, "recording")
    sessions = {}
    try:
        for robot, port in players.items():
            out = tmp / f"{label}_{robot}.json"
            args = ["--port", str(port), "--out", str(out), "--seconds", str(SESSION_S)]
            args += ["--play-at", str(PLAY_AT_S), *sound]
            if robot == listener:
                args.append("--listen")
            sessions[robot] = (module("sound_session", *args), out)
        for process, _ in sessions.values():
            if process.wait(timeout=180) != 0:
                raise SystemExit(f"{label}: a bridge session failed")
    finally:
        recorder.terminate()
        recorder.wait()
        for process, _ in sessions.values():
            if process.poll() is None:
                process.kill()
    results = {
        robot: json.loads(out.read_text()) for robot, (_, out) in sessions.items()
    }
    results["cable"] = {
        "timeline": json.loads(cable_file.read_text())
    }  # written on SIGTERM
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", type=int, nargs=2, default=[8011, 8012])
    parser.add_argument(
        "--say", help="speak this text (phonon) instead of playing the tone"
    )
    ns = parser.parse_args()
    port_a, port_b = ns.ports
    failures = 0
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        tone = tmp / "tone.wav"
        write_tone(tone)
        sound = ["--say", ns.say] if ns.say else ["--wav", str(tone)]
        what = (
            f"say({ns.say!r})"
            if ns.say
            else f"play_sound(a {TONE_S:.0f} s 440 Hz tone)"
        )
        for label, players, listener in (
            ("1 bridge a plays", {"a": port_a}, None),
            ("2 bridge b plays", {"b": port_b}, None),
            ("3 both play, b listens", {"a": port_a, "b": port_b}, "b"),
        ):
            results = scenario(tmp, label.split()[0], players, listener, sound)
            cable = results["cable"]["timeline"]
            print(f"--- {label}: {what}")
            window = None
            for robot in players:
                play = results[robot]["play"]
                window = (
                    window if window and robot != "a" else (play["start"], play["end"])
                )
                print(
                    f"  bridge {robot} ({results[robot]['audio']}): verb took {play['elapsed']:.2f} s"
                )
                if not ns.say and not TONE_S - 0.2 <= play["elapsed"] <= TONE_S + 1.5:
                    failures += 1
                    print("  FAIL: not about the sound's duration")
            assert window is not None
            start, end = window
            before = rms(cable, start - 2.5, start - 0.3)
            during = rms(cable, start + 0.4, end - 0.6)
            expect_loud = "a" in players
            ok = (
                (during > LOUD_RMS and before < SILENT_RMS)
                if expect_loud
                else during < SILENT_RMS
            )
            failures += not ok
            heard = [
                t for t, v in cable if v > SILENT_RMS and start - 1 <= t <= end + 2
            ]
            span = (max(heard) - min(heard) + 0.1) if heard else 0.0
            print(f"  cable carried signal for {span:.1f} s")
            print(
                f"  cable ({CABLE}, recorded by a third process): RMS before {before:.5f}, "
                f"during {during:.5f} -> {'OK' if ok else 'FAIL'} "
                f"(expected {'signal' if expect_loud else 'silence'})"
            )
            if listener:
                mic = results[listener]["mic"]
                mic_before = rms(mic, start - 2.5, start - 0.3)
                mic_during = rms(mic, start + 0.4, end - 0.6)
                ok = mic_during > LOUD_RMS and mic_before < mic_during / 10
                failures += not ok
                print(
                    f"  bridge {listener}'s microphone feed: RMS before {mic_before:.5f}, "
                    f"while a plays {mic_during:.5f} -> {'OK' if ok else 'FAIL'}"
                )
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
