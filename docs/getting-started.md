# Getting started

Your first application on the bridge, then the connection choices that take the same code to a simulator or a robot. The API's contracts in full are in [reference/api.md](reference/api.md); every config field in [reference/configuration.md](reference/configuration.md); what each setup needs and gives in [reference/backends-and-capabilities.md](reference/backends-and-capabilities.md).

## Install

Python 3.12+, in a Python project of your own (`uv init my-robot-app && cd my-robot-app` creates one). The package is not on PyPI; add it from its git URL — **one** of these two lines, the second when you also want the MuJoCo simulator:

```
uv add "reachy-mini-bridge @ git+https://github.com/funwithagents/reachy-mini-bridge"          # the robot and the offline fake
uv add "reachy-mini-bridge[sim] @ git+https://github.com/funwithagents/reachy-mini-bridge"     # the same, plus the simulator
```

The API changes between commits without a deprecation period while the bridge is at 0.1: pin a commit (`@<sha>`). The extras — `sim`, the `tts-*` voices, `test` — are listed in the [README](../README.md#install); importing the package imports `reachy_mini`, which needs its native libraries installed (GStreamer: with the wheels on macOS and Windows, from the system on Linux — [guides/linux.md](guides/linux.md)) but no running daemon.

## Your first application

Everything goes through `ReachyMiniBridge`, used as an async context manager: nothing connects until you enter it, leaving it tears everything down. This program runs on the offline `fake`, which needs no daemon, no hardware and no extra — and it brings its own voice, a beep, which is also the smallest complete `SpeechSynthesizer`:

```python
import asyncio

import numpy as np

from reachy_mini_bridge import ReachyMiniBridge


class Beep:
    """A stand-in voice: 0.3 s of a 440 Hz tone per utterance. Any object with these two
    members — `sample_rate`, and `stream(text)` yielding float32 mono chunks in [-1, 1] —
    is a SpeechSynthesizer; a real one streams a text-to-speech engine's audio instead."""

    sample_rate = 16_000

    async def stream(self, text: str):
        t = np.arange(int(0.3 * self.sample_rate)) / self.sample_rate
        yield (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


async def main() -> None:
    async with ReachyMiniBridge.from_dict({"backend": "fake"}) as bridge:
        await bridge.set_motors_state("enabled")  # moving verbs need torque; the modes do not
        print(await bridge.list_emotions())  # ['happy', 'sad', 'curious'] on the fake
        await bridge.play_emotion("happy")  # returns when the move ends; breathing resumes after it
        await bridge.say("Hello, I am Reachy.", Beep())  # returns when the robot has finished speaking
        async for chunk in bridge.audio_input():  # the echo-cancelled mic: int16 mono PCM bytes
            print(f"{len(chunk)} bytes of microphone audio at {bridge.mic_sample_rate} Hz")
            break  # stop iterating to stop the tap
        frame = bridge.camera.latest()  # the newest camera frame (BGR image, time, head pose), or None
        print("a camera frame" if frame is not None else "no frame yet")


asyncio.run(main())
```

Save it as `first_app.py` and run it:

```
uv run python first_app.py
```

It prints:

```
['happy', 'sad', 'curious']
320 bytes of microphone audio at 16000 Hz
a camera frame
```

**Nothing is heard and nothing moves: this is the fake.** It records every command on `bridge.robot` instead of sending it anywhere, answers the microphone and the camera with synthetic audio and frames, and keeps the real timing of the verbs that span time — `say` takes the beep's 0.3 s, the emotion its recorded length — so the program runs as it would against a robot, about a second and a half of it waiting. The same file drives a simulator or a robot once the dict becomes a config (next section); there the beep plays on the speaker and the head moves.

What happened, in order: the session started (on the fake, instantly; on a robot, the daemon, the connection, the media session, the camera feed, detection and the motion loop come up in order); the motors were enabled, which starts the idle behaviour — the robot breathes between verbs; the emotion played as the one primary move, blended in and out; `say` streamed the beep to the speaker and returned once its estimated end had passed (on a robot, once it had been heard); the mic tap yielded one chunk; the camera feed's newest frame was read. Leaving the block eased the head to neutral and closed everything, a daemon the bridge had started included.

**To interrupt what spans time — the emotion, the speech, the mic stream — cancel the task that awaits it.** The effect stops — speech flushed, the move no longer commanded — and the session stays usable for the next verb. The instant verbs (`set_motors_state` here) complete once accepted, their effect a mode the counterpart verb switches off ([reference/api.md](reference/api.md) "Cancellation and concurrency").

## Running it on a simulator or a robot

Swap the dict for a config file and the same program drives a daemon. `ReachyMiniBridge.from_json_file("robot.json")` takes it; a config names the backend, how to reach the daemon or whether the bridge should start one, the voice, the detector and the idle behaviour ([reference/configuration.md](reference/configuration.md)). Short profiles, one per setup, are in [examples/configs/](../examples/configs/):

| You have | Start from | The bridge |
|---|---|---|
| nothing | [fake.json](../examples/configs/fake.json) | runs offline |
| a Reachy Mini Lite on USB | [lite-usb.json](../examples/configs/lite-usb.json) | starts the hardware daemon, wakes the robot, puts it to sleep on exit |
| the `sim` extra, on Linux or with a screen | [sim-rendered-camera.json](../examples/configs/sim-rendered-camera.json) | starts the MuJoCo sim; the rendered eye camera works headless on Linux, under the viewer anywhere |
| the `sim` extra and a webcam | [sim-webcam.json](../examples/configs/sim-webcam.json) | starts the sim in its viewer with your webcam as the robot's camera, shown in the window's corner: it sees and follows you |
| a wireless Reachy Mini | [wireless.json](../examples/configs/wireless.json) | connects to the robot's own daemon over the network — **untested** by the author, who has no wireless robot |

What each setup gives (`camera` on a headless macOS sim: nothing) and how far it has been validated is one table in [reference/backends-and-capabilities.md](reference/backends-and-capabilities.md). The daemon the bridge starts can also be started by hand, to be borrowed ([guides/running-daemons.md](guides/running-daemons.md)).

A real voice is the config's `tts` block, built on the first-party tts-engine with the provider of your choice behind a `tts-*` extra; `say(text)` then needs no synthesizer argument ([guides/audio.md](guides/audio.md)). Face tracking is the `face_detection` and `motion.tracking` blocks: name the shipped `yunet` detector and turn tracking on, and the robot looks at whoever is in front of it while it keeps breathing ([guides/perception-and-tracking.md](guides/perception-and-tracking.md)).

## The lifecycle, in a host of your own

`async with` is the recommended form: the block stops the session on every way out, a cancel included. A host with lifecycle hooks of its own — a web framework's lifespan, a GUI, an agent runtime — uses the pair it is sugar over:

```python
bridge = ReachyMiniBridge.from_json_file("robot.json")
await bridge.start()    # on the event loop that will await the verbs
...
await bridge.stop()     # idempotent; eases the head to neutral, closes everything in order
```

One event loop owns a session: start it, await its verbs and stop it on the same loop. A synchronous host runs the bridge on a background loop and submits each call to it, as the [control panel](../examples/control_panel/) does. Tasks you start around the bridge — an ASR consumer over `audio_input()`, a subscriber on `faces.changes()` — are yours to cancel and await before the session ends.

## Waiting for what you need

The perception values are readable at any time and tell you whether they are live: `bridge.faces.value.active` is `False` while no detector is looking, `bridge.camera.latest()` is `None` before the first frame. An application that depends on them waits rather than assumes — with a detector configured, this returns once somebody is in view:

```python
report = await bridge.faces.wait_for(lambda r: r.active and len(r.faces) > 0)
```

A frame on a fresh session can take a couple of seconds on a live daemon; poll `camera.latest()` with a short sleep, or read it when a face report hands it to you (`report.frame`). A voice that failed to build is on `bridge.synthesizer_error` right after construction, so a host that wants hard failure raises there rather than at the first `say`.

## Driving it from an agent

An LLM agent drives the robot through the same `ReachyMiniBridge`: its tools are plain functions you write, each calling one verb. The verbs take and return JSON-friendly values in human terms, so most tools are a docstring and one line; perception is read from the bridge and encoded for your model inside the tool (a camera frame as base64 JPEG with the image library of your choice). Register them with the agent runtime you use — the registration below is schematic, the functions are complete:

```python
from reachy_mini_bridge import BridgeError, ReachyMiniBridge


def make_tools(bridge: ReachyMiniBridge):
    async def play_emotion(name: str) -> str:
        """Play a recorded emotion on the robot, e.g. "happy". Call list_emotions for the names."""
        try:
            await bridge.play_emotion(name)
        except BridgeError as e:  # e.g. motors not enabled
            return f"could not play {name}: {e}"
        return f"played {name}"

    async def list_emotions() -> list[str]:
        """List the emotions the robot can play."""
        return await bridge.list_emotions()

    async def say(text: str) -> str:
        """Speak the text out loud through the robot's speaker."""
        await bridge.say(text)
        return "done"

    async def who_is_there() -> int | None:
        """Count the faces the robot sees; None while no detector is looking."""
        report = bridge.faces.value
        return len(report.faces) if report.active else None

    return [play_emotion, list_emotions, say, who_is_there]
```

Cancelling the task that runs a tool stops the action on the robot (speech flushed, the move stopped) and leaves the session ready for the next call. A runtime that calls tools synchronously can run the bridge on a background event loop and submit each call to it.

## Where next

- [guides/audio.md](guides/audio.md) — a voice, speech, sound files, the microphone.
- [guides/perception-and-tracking.md](guides/perception-and-tracking.md) — the robot looks at people; what `faces` and `head_tracking` report and how to consume them.
- [guides/testing.md](guides/testing.md) — unit tests on the fake, live tests with the shipped pytest harness.
- [reference/api.md](reference/api.md) — every verb, value, error and contract.
- [guides/troubleshooting.md](guides/troubleshooting.md) — when it does not connect, see, move or speak.
