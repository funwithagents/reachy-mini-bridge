# reachy-mini-bridge

A Python library that sits between the [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) robot and whatever drives it — a script, a service, or an LLM agent. It wraps the upstream `reachy_mini` SDK behind one async, intent-level API (`ReachyMiniBridge`) whose methods speak in human terms — *enable the motors, play "happy", follow my face, say this, give me the mic, give me a camera frame* — and runs the same code unchanged against the real robot, the MuJoCo simulator, or an offline fake.

**Start here:** [docs/getting-started.md](docs/getting-started.md) — your first application, on the offline fake, then on a simulator or a robot; [docs/index.md](docs/index.md) — the documentation by task. The [quick start](#quick-start) below is the shortest program; [what the bridge gives you](#what-the-bridge-gives-you) is the feature list.

## Project status

> [!WARNING]
> - **Under development: the API may change.** The bridge is at version 0.1 and is being shaped as it is used. Methods, config fields and module layout can change between commits, without a deprecation period. If you depend on it, pin a commit (`reachy-mini-bridge @ git+https://github.com/funwithagents/reachy-mini-bridge@<sha>`).
> - **Built and tested on a Reachy Mini Lite, not on the wireless Reachy Mini.** Everything described here was exercised on a Lite plugged in over USB, on the MuJoCo simulator and on the offline fake. The wireless code paths exist (`robot.host` pointing at the robot, `daemon.spawn: "never"` since it runs its own daemon) but have never been run against one: the author has no wireless robot yet, so nothing here is guaranteed to work on it. If you try, an issue saying what happened, working or not, is the most useful thing you can send.

> [!TIP]
> **Feedback is welcome: open an issue.** Bug reports, questions, and what you would want the API or the config to do differently all go to [the issue tracker](https://github.com/funwithagents/reachy-mini-bridge/issues). The project is developed by one person and is not taking pull requests yet; [CONTRIBUTING.md](CONTRIBUTING.md) says why and how to help anyway.

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
| `placo` | `reachy-mini[placo_kinematics]` | The Placo kinematics engine, selected with `"daemon": {"kinematics_engine": "placo"}` — the one engine with gravity compensation ([docs/reference/configuration.md](docs/reference/configuration.md#kinematics-engines)). Installing it changes nothing until a config asks for it; no Windows wheel |

Importing the package imports `reachy_mini`, which needs its native libraries installed but not a running daemon. On macOS, add the two `dependency-metadata` entries of [docs/getting-started.md](docs/getting-started.md#on-macos-declare-pygobjects-metadata-before-you-add-the-bridge) to your project first, or the lock fails building PyGObject.

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

The repo ships a Gradio **control panel** — every action a button, the bridge's state on screen and refreshed twice a second (motor state, attention, the mode flags, a mic level meter, the camera frame, a log), with a Stop button next to `say` and `play_emotion` that interrupts it mid-flight. It runs from a checkout (it needs the `demo` dependency group, and its voice the `tts` group — both installed by a plain `uv sync`):

```
uv run python -m examples.control_panel --config config.example.json   # the sim viewer, spawned for you
uv run python -m examples.control_panel                                # no config: the offline fake
```

then open `http://127.0.0.1:7860`. The example config opens the MuJoCo viewer window next to the panel and uses your **webcam** as the robot's camera, shown in the corner of that window: enable the motors and the simulated robot turns to follow you ([docs/guides/running-daemons.md](docs/guides/running-daemons.md)). It needs an unlocked GUI session and, on macOS, camera permission for the terminal that runs it. Set the config's `daemon.camera.source` to `"sim"` for the rendered scene instead. Point `--config` at a `real` config to drive the robot. Design and limits: [specs/examples/control_panel.md](specs/examples/control_panel.md).

## What the bridge gives you

The upstream `reachy_mini` SDK gives full, low-level access to the robot, and the bridge keeps it: `bridge.robot` is the native `ReachyMini`. On top of it, the bridge adds what a conversational application otherwise has to build itself — one feature per row.

| Feature | What you get |
|---|---|
| **One async API in human units** | A small set of `async` methods that speak the way a person or an agent does — *enable the motors, play "happy", say this, follow my face* — in named emotions, degrees and seconds rather than pose matrices and radians; the same code runs on the robot, the MuJoCo simulator and an offline fake. |
| **One JSON configuration** | Backend, connection, daemon, voice, detector and idle behaviour in one file; changing `backend` moves it between the robot, the simulator and the fake. |
| **Everything is cancellable** | Cancel the task awaiting `say`, `play_sound`, `play_emotion` or the microphone stream and the effect stops at once — speech flushed, the file stopped, the move no longer commanded — with the robot ready for the next call. |
| **Audio input** | `async for chunk in bridge.audio_input()` yields the echo-cancelled microphone as int16 mono PCM at 16 kHz, ready for any speech recognizer, while the robot talks — to as many listeners as you start, each receiving the whole stream. |
| **Camera input** | `bridge.camera` is one feed of the robot's camera that any number of consumers plug onto at once — the bridge's face detector, a display, an agent tool, a vision pipeline of your own — each reading `latest()` at its own rate: the newest frame with its id, its time and the head pose at that time. |
| **Motors, emotions, sounds, wobbling** | The robot's own capabilities, as bridge calls: `set_motors_state` / `get_motors_state` for torque (`enabled`, `disabled`, `gravity_compensation`, checked before a move is sent), `list_emotions` / `play_emotion` for the recorded emotions library, `play_sound` for the robot's sound player, `set_wobbling` for the audio-reactive head sway — and `bridge.robot`, the native `ReachyMini`, for everything else the SDK offers. |
| **Speech through TTS modules** | `say(text)` streams any text-to-speech engine to the robot's speaker behind a small `SpeechSynthesizer` protocol — the first-party tts-engine's providers from the config's `tts` block, or your own — and returns when the utterance has been heard. Playback and capture share one media session, which keeps the robot's echo cancellation working. |
| **Face detection** | Name a detector — the shipped `yunet`, upstream's own model run by the bridge, or a `FaceDetector` of your own — and `bridge.faces` reports every face in view with its size, its box and a `track_id` that follows the same person from frame to frame; read it, or subscribe to learn who appears and leaves. |
| **Head tracking** | The bridge's own tracker follows one face by its `track_id`, waits for a face that vanishes before turning to another, and hands the head back to the idle motion when nobody has been seen for two seconds; `bridge.head_tracking` says whom it follows. |
| **Motion: presence, idle and blending** | Between actions the robot breathes — or holds a still neutral, or plays an `IdleMove` you wrote — so it never looks dead; emotions, tracking and the idle move are composed by one motion loop, every transition a blend, so the robot looks at someone *and* breathes, and an emotion plays over tracking and hands back to it. Head wobbling sways the head with the audio and is paused around emotions. |
| **Daemon management** | The bridge starts the daemon when asked — the sim, or a robot plugged in over USB — or reuses one already running, and stops what it started on exit, the robot going to sleep; leaving `async with` tears everything down in order, even when a step fails. |
| **Simulator** | Everything above runs unchanged on the MuJoCo sim, face detection and head tracking included: the bridge detects in the sim's camera stream and aims with the sim camera's true geometry. Its launcher can put portraits in the scene for tests, or use your **webcam** as the robot's camera, so the simulated robot follows you ([the simulator guide](docs/guides/running-daemons.md)). |
| **Testing, unit and e2e** | The `fake` backend records every command and keeps the real timing, for unit tests with no daemon. A shipped pytest plugin runs live tests against the sim or a robot, probes what the target can do (motion, audio, camera, faces, gravity compensation) and skips instead of failing; its sim scene has portraits a test shows and moves, so tracking is tested without a person. |

## Documentation

Start at [docs/index.md](docs/index.md), the documentation by task. The pages a consumer uses most:

| | |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Your first application, on the fake, then on a sim or a robot; the lifecycle; driving it from an agent |
| [docs/reference/api.md](docs/reference/api.md) | Every method, value and error; lifecycle, cancellation and concurrency, units; the extension contracts (a voice, a detector, an idle move) |
| [docs/reference/configuration.md](docs/reference/configuration.md) | Every config field, its default and the method that changes it at run time |
| [docs/reference/backends-and-capabilities.md](docs/reference/backends-and-capabilities.md) | What each setup needs and gives — the one OS / target matrix, expected apart from validated |
| [docs/guides/](docs/index.md#by-task) | Audio, perception and tracking, a custom face detector, a custom idle move, testing your project, running daemons, Linux, troubleshooting |
| [examples/configs/](examples/configs/) | A minimal config profile per setup |

## How it is developed

The bridge is a spec-driven project: every feature is designed before it is coded, pinned by tests at two levels, and checked by CI on every change.

- **Specs first.** Each concept has one spec under [specs/](specs/) — the normative design, with its rationale and its open questions — indexed by [specs/_index.md](specs/_index.md); [specs/_overview.md](specs/_overview.md) is the architecture. Each spec names the code and test files it governs, and a test keeps that map and the statuses honest — every file a spec names exists, every module has a spec, every status matches its index. That the code does what a spec says is what the two test tiers establish.
- **Unit tests on the fake.** `tests/` covers every feature of the bridge against the offline `fake` backend: no daemon, no hardware, no network, deterministic; it runs in parallel across the cores, since the fake keeps the real timing of what it tests.
- **End-to-end tests on a live daemon.** `tests-e2e/` checks the high-level behaviours — head tracking, presence, speech and its interruption — on the simulator, headless or with its viewer, or on a real robot, through the shipped harness ([specs/testing/testing.md](specs/testing/testing.md) is the strategy: the two tiers, the targets, the probed capabilities).
- **CI on every change.** Three jobs run side by side on GitHub's Linux runners for every pull request and push to `main`: the static gate (`ruff`, `pyright`), the unit tier, and the e2e tier against a headless simulator the harness spawns ([specs/testing/ci.md](specs/testing/ci.md)).
- **[AGENTS.md](AGENTS.md) is the operating manual** for anyone, or any coding agent, working in the repo: the project map (which module does what, with its spec), the verification gate, how to run the e2e tier on each target and read its skips, and how the specs are kept in step with the code.

The same gate, locally:

```
uv sync                  # every group: the tooling, the sim, the TTS providers
uv run ruff check .
uv run ruff format src tests tests-e2e examples
uv run pyright
uv run pytest            # the unit tier, offline
uv run pytest tests-e2e -rs   # the e2e tier against a sim the harness spawns (or a robot: see AGENTS.md)
```

## License

MIT — see [LICENSE](LICENSE).
