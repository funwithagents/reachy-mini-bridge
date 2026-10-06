# reachy-mini-bridge

A Python library that sits between the [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) robot and whatever drives it — a script, a service, or an LLM agent. It wraps the upstream `reachy_mini` SDK behind one async, intent-level API (`ReachyMiniBridge`) whose verbs speak in human terms — *enable the motors, play "happy", follow my face, say this, give me the mic, give me a camera frame* — and runs the same code unchanged against the real robot, the MuJoCo simulator, or an offline fake.

## Project status

> [!WARNING]
> - **Under development: the API may change.** The bridge is at version 0.1 and is being shaped as it is used. Verbs, config fields and module layout can change between commits, without a deprecation period. If you depend on it, pin a commit (`reachy-mini-bridge @ git+https://github.com/funwithagents/reachy-mini-bridge@<sha>`).
> - **Built and tested on a Reachy Mini Lite, not on the wireless Reachy Mini.** Everything described here was exercised on a Lite plugged in over USB, on the MuJoCo simulator and on the offline fake. The wireless code paths exist (`robot.host` pointing at the robot, `daemon.spawn: "never"` since it runs its own daemon) but have never been run against one: the author has no wireless robot yet, so nothing here is guaranteed to work on it. If you try, an issue saying what happened, working or not, is the most useful thing you can send.

> [!TIP]
> **Feedback is welcome: open an issue.** Bug reports, questions, and what you would want the API or the config to do differently all go to [the issue tracker](https://github.com/funwithagents/reachy-mini-bridge/issues). The project is developed by one person and is not taking pull requests yet; [CONTRIBUTING.md](CONTRIBUTING.md) says why and how to help anyway.

## What the bridge adds to the SDK

The upstream `reachy_mini` SDK gives full, low-level access to the robot. The bridge keeps that access (`bridge.robot` is the native `ReachyMini`) and adds what a conversational app otherwise has to build, and get right, itself:

| Concern | With the SDK alone | With the bridge |
|---|---|---|
| **Calling style** | Mostly blocking calls, 4x4 head poses and radians | One `async` API in human terms (named emotions, `enabled` motors, degrees, seconds). Blocking SDK calls and the emotions-library download run off the event loop, so moving never stalls audio |
| **Speaking** | `media.push_audio_sample` takes float32 audio at the robot's sample rate and channel layout, and returns as soon as the audio is queued | `say(text)` with any text-to-speech engine behind a small `SpeechSynthesizer` protocol. The bridge resamples to the robot's rate and fans mono out to its channels. `say` returns when the robot has *finished* speaking, cancelling it silences the speaker at once, and a new `say` interrupts the one playing (the interrupted call raises `SpeechInterruptedError`) |
| **Listening** | Poll `media.get_audio_sample` for float32 stereo blocks | `async for chunk in bridge.audio_input()` yields int16 mono PCM, ready for any speech recognizer. Recording and playback share one media session, which is what keeps the robot's hardware echo cancellation working while it talks and listens at once |
| **Interrupting** | Cancelling `async_play_move` stops the motion, but the emotion's sound plays to its end and the head keeps swaying to it. `cancel_move()` stops the sound by tearing down the whole audio pipeline, which kills the microphone | Cancelling the task interrupts any verb. `play_emotion` stops both motion and sound, and the microphone, speaker and robot stay usable for the next verb |
| **Staying alive** | Nothing: the head holds whatever pose the last command left it at | Between verbs the robot breathes (or holds a still neutral pose, or plays an idle move you wrote) so it never looks dead; emotions blend in and back out instead of snapping, and one thread is the only writer of the target pose |
| **Motor safety** | A move sent with motors off does nothing, with no error. Gravity compensation sent to a daemon that doesn't support it drops the connection | Moving verbs raise `MotorsNotEnabledError`. Gravity compensation is checked first and raises `GravityCompensationUnsupportedError` without sending anything |
| **The daemon** | Start `reachy-mini-daemon` yourself. Spawning it from a Python process that has already imported `reachy_mini` can crash it | Optionally started for you (sim, or a robot plugged in over USB), or an already running one is reused. A daemon the bridge started is stopped on exit, and the robot goes to sleep |
| **Clean shutdown** | Up to the app | `stop()` — what leaving `async with` calls — eases the head to neutral, turns head wobbling back off (the setting is shared by every app on the daemon), then closes the audio, the connection and the daemon in order, even when a step fails. Cancelling during start-up leaks nothing |
| **Configuration** | Constructor arguments in code | One JSON config for the backend, connection, daemon, voice, mic profile and wobbling. The same file switches between the real robot and the simulator, and runs on the fake with daemon management off |
| **Following a face** | `start_head_tracking()` makes the daemon aim the head at a detected face. Once the face is lost, the head recentres and then stays frozen at neutral, ignoring your targets, until tracking is re-armed | Name a detector and turn tracking on in the config (`"face_detection": {"detector": "yunet"}, "motion": {"tracking": true}` — upstream's own model, run by the bridge; nothing to install) and the bridge detects and aims the head itself: the robot looks at the person **and keeps breathing** while it does, its idle roaming toned down so it stays on them. When nobody has been seen for two seconds the head eases back into the idle motion, and it turns back as soon as a face returns. Emotions play as recorded over it. Who is there is `bridge.faces`: every face the detector sees with its size, read as a value, or `async for report in bridge.faces.changes()` to be told when someone appears or leaves (once per camera frame, 10 a second on a local daemon — not the SDK's once-a-second status) |
| **The simulator** | In the MuJoCo sim, face tracking does not work: the loop never runs the tracking step, and even when it does, the tracker's camera matrix is wrong for the sim camera, so the head settles ~45° away from the face. The sim camera can only show the rendered scene | The bridge detects faces itself in the sim's camera stream and its tracker aims with the sim camera's true geometry: the head turns onto a face and settles on it as on a robot. Every sim the bridge starts runs through its own launcher, which can use your **webcam** as the robot's camera, so the simulated robot sees and follows you (see [the simulator guide](docs/guides/running-daemons.md)) |
| **Testing** | Needs a daemon: the sim or the robot, and a person in front of the camera to test face tracking | An offline `fake` backend that records every command, for fast unit tests. A pytest plugin for live tests that checks what the target can actually do (motion, audio, camera, gravity compensation, faces) and skips a test instead of failing it. Its sim includes a portrait a test can show, move and hide, so face tracking is tested without a person |

## Install

Python 3.12+. The package is not on PyPI yet; add it from its git URL, or from a local checkout:

```
uv add "reachy-mini-bridge @ git+https://github.com/funwithagents/reachy-mini-bridge"
uv add "reachy-mini-bridge[sim,test] @ git+https://github.com/funwithagents/reachy-mini-bridge"
uv add "reachy-mini-bridge[sim,test] @ ../reachy-mini-bridge"   # a checkout next door
```

| Extra | Adds | You need it for |
|---|---|---|
| *(none)* | `reachy_mini`, `numpy`, `samplerate`, `tts-engine` | The `real` backend, the offline `fake`, and `say` with your own synthesizer. `tts-engine` is our small first-party engine with no provider; one of the `tts-*` extras adds one |
| `sim` | `mujoco` 3.3.1+ (3.3.x) | The `sim` backend (MuJoCo). Declared directly rather than through `reachy_mini[mujoco]`, whose exact pin on 3.3.0 predates the viewer overlays the sim launcher needs; don't install that upstream extra alongside ([docs/guides/running-daemons.md](docs/guides/running-daemons.md) "The MuJoCo version") |
| `tts-pocket` | `tts-engine[pocket]` | The default voice for `say` on the local pocket-tts model: no key, no network once the weights are cached, but torch (hundreds of MB; on Linux a dev checkout takes the CPU build from PyTorch's index) |
| `tts-elevenlabs` | `tts-engine[elevenlabs]` | The default voice for `say` on ElevenLabs (`ELEVENLABS_API_KEY`); a few MB |
| `tts-gradium` | `tts-engine[gradium]` | The default voice for `say` on Gradium (`GRADIUM_API_KEY`); a few MB |
| `test` | `pytest` | The shipped `reachy_mini_bridge.testing` harness for your e2e tests |

Importing the package imports `reachy_mini`, which needs its native libraries installed but not a running daemon.

**Platforms.** Developed on macOS; Linux is where CI runs the whole test suite, sim camera included; Windows is untested. On macOS and Windows, GStreamer comes with `reachy_mini`'s wheels. **On Linux it comes from the system**, and running a daemon on the machine — the sim, or a robot plugged in over USB — also needs the Rust GStreamer webrtc plugin, which no distribution packages; the headless sim's camera needs Mesa's EGL. The packages, the plugin's two routes, and what works without a sound card are in [docs/guides/linux.md](docs/guides/linux.md). What each setup — fake, sim, a Lite over USB, a wireless robot — needs installed and gives you, and how far it has been validated, is one table in [docs/reference/backends-and-capabilities.md](docs/reference/backends-and-capabilities.md).

## Quick start

Everything goes through `ReachyMiniBridge`, used as an async context manager. Nothing connects until you enter it; leaving it tears everything down. The `fake` backend needs no daemon, no hardware and no extras:

```python
import asyncio

from reachy_mini_bridge import ReachyMiniBridge


async def main() -> None:
    async with ReachyMiniBridge("fake") as bridge:
        await bridge.set_motors_state("enabled")
        print(await bridge.list_emotions())  # ['happy', 'sad', 'curious'] on the fake
        await bridge.play_emotion("happy")
        frame = bridge.camera.latest()  # the newest CameraFrame (BGR image, time, head pose), or None


asyncio.run(main())
```

Swap `"fake"` for `"sim"` or `"real"` to talk to a daemon you already run. For anything beyond the backend name, build the bridge from a config ([docs/reference/configuration.md](docs/reference/configuration.md); a short profile per setup is in [examples/configs/](examples/configs/)):

```python
async with ReachyMiniBridge.from_json_file("robot.json") as bridge:
    await bridge.say("hello")  # voice from the config's `tts` block
```

`from_dict(...)` and `from_json(...)` take the same config as a dict or a JSON string.

Face detection and tracking are opt-in: name a detector in the config's `face_detection` block (`"detector": "yunet"`) and switch on what you want — `"enabled": true` for detection alone, or `"motion": {"tracking": true}` for the head to follow whoever is in view; the detector name alone starts nothing ([docs/guides/perception-and-tracking.md](docs/guides/perception-and-tracking.md)). The complete first application — a voice, the microphone, the lifecycle, an agent's tools — continues in [docs/getting-started.md](docs/getting-started.md).

### Try it from a browser

The repo ships a Gradio **control panel** — every verb a button, the bridge's state on screen and refreshed twice a second (motor state, attention, the mode flags, a mic level meter, the camera frame, a log), with a Stop button next to `say` and `play_emotion` that cancels the verb mid-flight. It runs from a checkout (it needs the `demo` dependency group, and its voice the `tts` group — both installed by a plain `uv sync`):

```
uv run python -m examples.control_panel --config config.example.json   # the sim viewer, spawned for you
uv run python -m examples.control_panel                                # no config: the offline fake
```

then open `http://127.0.0.1:7860`. The example config opens the MuJoCo viewer window next to the panel and uses your **webcam** as the robot's camera, shown in the corner of that window: enable the motors and the simulated robot turns to follow you ([docs/guides/running-daemons.md](docs/guides/running-daemons.md)). It needs an unlocked GUI session and, on macOS, camera permission for the terminal that runs it. Set the config's `daemon.camera.source` to `"sim"` for the rendered scene instead. Point `--config` at a `real` config to drive the robot. Design and limits: [specs/examples/control_panel.md](specs/examples/control_panel.md).

## Documentation

Start at [docs/index.md](docs/index.md), the documentation by task. The pages a consumer uses most:

| | |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Your first application, on the fake, then on a sim or a robot; the lifecycle; driving it from an agent |
| [docs/reference/api.md](docs/reference/api.md) | Every verb, value and error; lifecycle, cancellation and concurrency, units; the extension contracts (a voice, a detector, an idle move) |
| [docs/reference/configuration.md](docs/reference/configuration.md) | Every config field, its default and its runtime verb |
| [docs/reference/backends-and-capabilities.md](docs/reference/backends-and-capabilities.md) | What each setup needs and gives — the one OS / target matrix, expected apart from validated |
| [docs/guides/](docs/index.md#by-task) | Audio, perception and tracking, a custom face detector, a custom idle move, testing your project, running daemons, Linux |
| [examples/configs/](examples/configs/) | A minimal config profile per setup |

For maintainers: this is a spec-driven project — [specs/](specs/) holds the normative design, one spec per concept with its status ([specs/_overview.md](specs/_overview.md) is the architecture), [plans/](plans/) the implementation plans that built it, and [AGENTS.md](AGENTS.md) is the operating manual (the project map, the status discipline, verification, running the live tests). [docs/internals/](docs/internals/upstream-sdk-notes.md) holds what we learned about the upstream SDK.

## Development

```
uv sync                  # every group: the tooling, the sim, the TTS providers
uv run ruff check .
uv run ruff format src tests tests-e2e examples
uv run pyright
uv run pytest            # fast offline tier only
uv run pytest tests-e2e -rs   # live tier against a sim the harness spawns (or a robot: see AGENTS.md)
```

The live tier's targets, capabilities and skips are described in [AGENTS.md](AGENTS.md) "Running the live e2e tests"; CI runs the same gate plus the live tier on a headless Linux sim on every pull request and push to `main` ([specs/testing/ci.md](specs/testing/ci.md)). How the project is run today, and how to help: [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
