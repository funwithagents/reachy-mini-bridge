# reachy-mini-bridge

A Python library that sits between the [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) robot and whatever drives it — a script, a service, or an LLM agent. It wraps the upstream `reachy_mini` SDK behind one async, intent-level API (`ReachyMiniApi`) whose verbs speak in human terms — *enable the motors, play "happy", follow my face, say this, give me the mic, give me a camera frame* — and runs the same code unchanged against the real robot, the MuJoCo simulator, or an offline fake.

> **Status: layers 0–1 built.** The connection seam, `ReachyMiniApi`, the audio/media session, the declarative config, the bridge-owned daemon lifecycle (sim, or a USB-attached robot), and the motion loop (presence, breathing, emotions through one target writer) are implemented and tested on the `real` / `sim` / `fake` backends — the motion loop's on-robot checklist is the one thing still outstanding. The agent-tools layer (`ReachyMiniTools`, exposing the same verbs as functions an LLM runtime can call) is designed but not built yet. See [specs/_index.md](specs/_index.md) for per-spec status.

## What the bridge adds to the SDK

The upstream `reachy_mini` SDK gives full, low-level access to the robot. The bridge keeps that access (`api.robot` is the native `ReachyMini`) and adds what a conversational app otherwise has to build, and get right, itself:

| Concern | With the SDK alone | With the bridge |
|---|---|---|
| **Calling style** | Mostly blocking calls, 4x4 head poses and radians | One `async` API in human terms (named emotions, `enabled` motors, degrees, seconds). Blocking SDK calls and the emotions-library download run off the event loop, so moving never stalls audio |
| **Speaking** | `media.push_audio_sample` takes float32 audio at the robot's sample rate and channel layout, and returns as soon as the audio is queued | `say(text)` with any text-to-speech engine behind a small `SpeechSynthesizer` protocol. The bridge resamples to the robot's rate and fans mono out to its channels. `say` returns when the robot has *finished* speaking, and cancelling it silences the speaker at once |
| **Listening** | Poll `media.get_audio_sample` for float32 stereo blocks | `async for chunk in api.audio_input()` yields int16 mono PCM, ready for any speech recognizer. Recording and playback share one media session, which is what keeps the robot's hardware echo cancellation working while it talks and listens at once |
| **Interrupting** | Cancelling `async_play_move` stops the motion, but the emotion's sound plays to its end and the head keeps swaying to it. `cancel_move()` stops the sound by tearing down the whole audio pipeline, which kills the microphone | Cancelling the task interrupts any verb. `play_emotion` stops both motion and sound, and the microphone, speaker and robot stay usable for the next verb |
| **Staying alive** | Nothing: the head holds whatever pose the last command left it at | Between verbs the robot breathes (or holds a still neutral pose) so it never looks dead; emotions blend in and back out instead of snapping, and one thread is the only writer of the target pose |
| **Motor safety** | A move sent with motors off does nothing, with no error. Gravity compensation sent to a daemon that doesn't support it drops the connection | Moving verbs raise `MotorsNotEnabledError`. Gravity compensation is checked first and raises `GravityCompensationUnsupportedError` without sending anything |
| **The daemon** | Start `reachy-mini-daemon` yourself. Spawning it from a Python process that has already imported `reachy_mini` can crash it | Optionally started for you (sim, or a robot plugged in over USB), or an already running one is reused. A daemon the bridge started is stopped on exit, and the robot goes to sleep |
| **Clean shutdown** | Up to the app | Leaving `async with` turns head wobbling back off (the setting is shared by every app on the daemon), then closes the audio, the connection and the daemon in order, even when a step fails. Cancelling during start-up leaks nothing |
| **Configuration** | Constructor arguments in code | One JSON config for the backend, connection, daemon, voice, mic profile and wobbling. The same file switches between the real robot, the simulator and the fake |
| **Following a face** | `start_head_tracking()` makes the daemon aim the head at a detected face. Once the face is lost, the head recentres and then stays frozen at neutral, ignoring your targets, until tracking is re-armed | Tracking is on by default. When nobody has been seen for a few seconds the bridge hands the head back to the idle motion (the robot breathes again) and re-engages as soon as a face returns |
| **The simulator** | In the MuJoCo sim, face tracking does not work: the loop never runs the tracking step, and even when it does, the tracker's camera matrix is wrong for the sim camera, so the head settles ~45° away from the face. The sim camera can only show the rendered scene | Every sim the bridge starts runs through its own launcher, which fixes both: the head turns onto a face and settles on it as on a robot. The sim can also use your **webcam** as the robot's camera, so the simulated robot sees and follows you (see [The simulator](#the-simulator)) |
| **Testing** | Needs a daemon: the sim or the robot, and a person in front of the camera to test face tracking | An offline `fake` backend that records every command, for fast unit tests. A pytest plugin for live tests that checks what the target can actually do (motion, audio, camera, gravity compensation, faces) and skips a test instead of failing it. Its sim includes a portrait a test can show, move and hide, so face tracking is tested without a person |

## What's in the repo

| Path | What it is |
|---|---|
| [src/reachy_mini_bridge/](src/reachy_mini_bridge/) | The library: `api.py` (the verbs), `config.py`, `audio.py` (speech out, mic in), `motion.py` (the motion loop: presence, breathing, emotions), `daemon.py` (daemon lifecycle), `sim_daemon.py` (the sim launcher: face-tracking fixes, webcam camera), `robot.py` + `fake_reachy_mini.py` (the backend seam), `testing/` (a pytest harness for your own e2e tests), `tools.py` (placeholder) |
| [config.example.json](config.example.json) | Every config field with placeholder values |
| [specs/](specs/) | Design docs, one per concept, each with a status — the source of truth for how things are meant to work |
| [plans/](plans/) | Implementation plans that turned those specs into code |
| [docs/](docs/) | Reference notes: the upstream SDK, running the sim daemon, testing your project against the bridge |
| [tests/](tests/), [tests-e2e/](tests-e2e/) | The fast offline suite (default `pytest`) and the opt-in live suite against a sim or real daemon |
| [examples/](examples/) | Runnable examples, not part of the package: `control_panel/`, a Gradio control panel with a button for every verb (see [Try it from a browser](#try-it-from-a-browser)) |

This is a spec-driven project: [AGENTS.md](AGENTS.md) is the operating manual (how specs, plans and statuses work) and [specs/_overview.md](specs/_overview.md) the architecture.

## Install

Python 3.12+. The package is not on PyPI yet; add it from a checkout or a git URL:

```
uv add reachy-mini-bridge @ ../reachy-mini-bridge          # or a git+https URL
uv add "reachy-mini-bridge[sim,test] @ ../reachy-mini-bridge"
```

| Extra | Adds | You need it for |
|---|---|---|
| *(none)* | `reachy_mini`, `numpy`, `samplerate`, `tts-engine` | The `real` backend, the offline `fake`, and `say` with your own synthesizer. `tts-engine` is our small first-party engine with no provider; one of the `tts-*` extras adds one |
| `sim` | `reachy_mini[mujoco]` | The `sim` backend (MuJoCo) |
| `tts-pocket` | `tts-engine[pocket]` | The default voice for `say` on the local pocket-tts model: no key, no network once the weights are cached, but torch (hundreds of MB) |
| `tts-elevenlabs` | `tts-engine[elevenlabs]` | The default voice for `say` on ElevenLabs (`ELEVENLABS_API_KEY`); a few MB |
| `test` | `pytest` | The shipped `reachy_mini_bridge.testing` harness for your e2e tests |

Importing the package imports `reachy_mini`, which needs its native libraries installed but not a running daemon.

## Quick start

Everything goes through `ReachyMiniApi`, used as an async context manager. Nothing connects until you enter it; leaving it tears everything down. The `fake` backend needs no daemon, no hardware and no extras:

```python
import asyncio

from reachy_mini_bridge import ReachyMiniApi


async def main() -> None:
    async with ReachyMiniApi("fake") as api:
        await api.set_motors_state("enabled")
        print(await api.list_emotions())  # ['happy', 'sad', 'curious'] on the fake
        await api.play_emotion("happy")
        await api.start_head_tracking()
        frame = await api.get_camera_frame()  # numpy BGR HxWx3, or None
        await api.stop_head_tracking()


asyncio.run(main())
```

Swap `"fake"` for `"sim"` or `"real"` to talk to a daemon you already run. For anything beyond the backend name, build the api from a config (see [Configuration](#configuration)):

```python
async with ReachyMiniApi.from_json_file("robot.json") as api:
    await api.say("hello")  # voice from the config's `tts` block
```

`from_dict(...)` and `from_json(...)` take the same config as a dict or a JSON string.

### Try it from a browser

The repo ships a Gradio **control panel** — every verb a button, the api's state on screen and refreshed twice a second (motor state, attention, the mode flags, a mic level meter, the camera frame, a log), with a Stop button next to `say` and `play_emotion` that cancels the verb mid-flight. It runs from a checkout (it needs the `demo` dependency group, installed by `uv sync`):

```
uv run python -m examples.control_panel --config config.example.json   # the sim viewer, spawned for you
uv run python -m examples.control_panel                                # no config: the offline fake
```

then open `http://127.0.0.1:7860`. The example config opens the MuJoCo viewer window next to the panel and uses your **webcam** as the robot's camera: enable the motors and the simulated robot turns to follow you (see [The simulator](#the-simulator)). It needs an unlocked GUI session and, on macOS, camera permission for the terminal that runs it. Set the config's `daemon.camera.source` to `"sim"` for the rendered scene instead. Point `--config` at a `real` config to drive the robot. Design and limits: [specs/control_panel.md](specs/control_panel.md).

## What the API does

All verbs are `async`; units are human (degrees, seconds, named emotions). The underlying `reachy_mini.ReachyMini` stays reachable as `api.robot` for anything the bridge does not cover.

| Area | Verbs |
|---|---|
| Motors | `get_motors_state()`, `set_motors_state("enabled" \| "disabled" \| "gravity_compensation")` |
| Expression | `list_emotions()`, `play_emotion(name)` — the upstream recorded-moves library |
| Gaze | `start_head_tracking(weight=1.0)`, `stop_head_tracking()`, `tracking` — the daemon keeps a detected face centered; on by default (the config's `motion.tracking` flag), armed once motors are `enabled` |
| Speech out | `say(text, synth=None)`, `play_sound(file)` |
| Motion while talking | `set_wobbling(enabled)`, `wobbling` — upstream's audio-reactive head sway; on by default, set by the config's `motion.wobbling` flag |
| Staying alive | `set_presence(enabled)` / `presence`, `set_breathing(enabled)` / `breathing` — the idle behaviour between verbs; both on by default, set by the config's `motion` block |
| Mic in | `audio_input(mono=True)` async iterator of int16 PCM bytes, plus `mic_sample_rate` / `mic_channels` |
| Camera | `get_camera_frame()` — raw BGR `ndarray`, `None` when no frame is available |

Verbs that move the robot require motors `enabled` and raise `MotorsNotEnabledError` otherwise. The errors a caller catches — `BridgeError` (the base), `MotorsNotEnabledError`, `GravityCompensationUnsupportedError`, `ConfigError` — import from `reachy_mini_bridge`, next to `ReachyMiniApi`, `ReachyMiniConfig`, `SpeechSynthesizer` and `TTSEngineSynthesizer`; `DaemonError` lives in `reachy_mini_bridge.errors`.

**Talking.** `say` streams text-to-speech to the robot speaker through a `SpeechSynthesizer` — a small protocol (`sample_rate` + `stream(text)` yielding float32 mono chunks) importable from `reachy_mini_bridge`. Bring your own, or configure the default `tts-engine` adapter through the config's `tts` block. Without either, `say` raises `BridgeError`; a `tts` block that fails to build (a provider whose extra isn't installed, a missing API key) leaves the robot usable and exposes the cause on `api.synthesizer_error`. `say` returns once the utterance has finished playing; cancel the task to stop it (queued audio is flushed). Cancelling the task is how you interrupt any verb: `play_emotion` stops the motion and the emotion's sound the same way, and the session stays usable for the next verb (see [specs/api.md](specs/api.md) "Cancellation").

**Listening.** The bridge does no speech recognition. It exposes the robot's echo-cancelled microphone as a stream and you feed it to the ASR of your choice:

```python
async with ReachyMiniApi("fake") as api:
    print(api.mic_sample_rate)  # configure your ASR to this
    async for chunk in api.audio_input():  # int16 LE mono PCM bytes
        feed_my_asr(chunk)
        break  # stop iterating to stop the tap
```

Routing both directions through the bridge is what keeps the robot's hardware echo cancellation working while it speaks and listens at once.

## Backends

| Backend | What it drives | Needs |
|---|---|---|
| `real` *(default)* | The physical robot via its daemon | The robot reachable at `host:port`; for a robot plugged in over USB, the bridge can spawn its daemon for you |
| `sim` | The upstream MuJoCo mockup | The `sim` extra; a daemon you run, or one the bridge spawns for you |
| `fake` | A first-party in-process stand-in that records every command and returns synthetic audio and frames | Nothing — offline and deterministic; powers the unit tests |

### The simulator

The `sim` backend is upstream's MuJoCo simulation, started through the bridge's own launcher, `python -m reachy_mini_bridge.sim_daemon` (a config with `"daemon": {"spawn": "auto"}` does it for you). The launcher runs upstream's daemon unchanged apart from these additions ([specs/sim_daemon.md](specs/sim_daemon.md)):

- **Face tracking works.** Upstream's sim never runs its tracking step, and its tracker uses a camera matrix scaled for the real robot's sensor, which puts the head about 45° off the face. The launcher fixes both. With the viewer open, the head turns onto a face, swings a few degrees past it, and settles on it. These are bugs in the upstream simulator. The corrections are pinned by fast offline tests that step the corrected loop in MuJoCo with no daemon running, and by the live tests below.
- **Your webcam as the robot's camera.** With `"daemon": {"camera": {"source": "webcam"}}`, the sim's camera shows your computer's webcam instead of the rendered scene. Face tracking, `get_camera_frame()` and the control panel then see you, with or without the viewer window. The webcam counts as fixed where the robot's eye rests, so the head follows you without drifting. On macOS, the terminal or editor that starts the daemon needs camera permission.
- **A face to test with.** The testing package can write a scene with a portrait that a test shows, moves and hides while the daemon runs ([specs/sim_scene.md](specs/sim_scene.md)). The pytest plugin's sim always runs on it.

A sim started by hand with upstream's `reachy-mini-daemon --sim` works for motion and audio, but has none of these additions. Commands for every mode: [docs/running-the-sim-daemon.md](docs/running-the-sim-daemon.md).

## Configuration

`ReachyMiniConfig` is one declarative object, buildable from a dict, a JSON string or a JSON file. Every block is optional; [config.example.json](config.example.json) shows them all:

```json
{
  "backend": "sim",
  "robot": { "host": "127.0.0.1", "port": 8000 },
  "daemon": { "spawn": "auto", "headless": false },
  "tts": { "module": { "type": "pocket", "voice": "george" } },
  "audio": { "xvf3800": null },
  "motion": { "presence": true, "breathing": true, "wobbling": true, "tracking": true }
}
```

- `robot` — keyword arguments forwarded verbatim to upstream `ReachyMini(...)`; ignored on `fake`, so one file switches backends by changing `backend` alone.
- `daemon` — `sim`, or `real` for a robot plugged into this machine over USB (loopback `host` only). `"spawn": "auto"` reuses a daemon already listening at `host:port` or spawns one — the MuJoCo daemon for `sim`, the robot's hardware daemon for `real` (it wakes the robot, and puts it to sleep on exit) — and stops it on exit; `"always"` insists on spawning; `"never"` (default) only connects, which is what a wireless robot needs. For `sim`, `"headless": false` (the example config) opens the MuJoCo **viewer**, so you watch the robot move and the camera works; it needs an unlocked GUI session. `"headless": true` (the default) runs the sim without a window, which has no rendered camera. `"camera": {"source": "webcam"}` (the example config) makes the sim see through the computer's webcam instead — you in front of the screen are who the simulated robot follows, with or without the viewer ([specs/sim_daemon.md](specs/sim_daemon.md)). `headless`, `scene` and `camera` play no part on `real`.
- `tts` — the tts-engine module block that builds the default voice for `say`; `module.type` picks the provider (`pocket` above, the local model, needs no key; `elevenlabs` takes `api_key_env` and a `voice_id`) and the matching `tts-*` extra must be installed.
- `audio` — the XVF3800 mic-array profile applied on connect.
- `motion` — everything that shapes the robot's behaviour at rest, all `true` by default: `presence` (stay alive between verbs) and `breathing` (breathe vs. hold neutral when idle), changed at runtime with `set_presence` / `set_breathing`; `wobbling` (sway the head with every sound the robot plays), changed with `set_wobbling`; `tracking` (autonomously keep a detected face centered, once motors are enabled), changed with `start_head_tracking` / `stop_head_tracking`.

Details and validation rules: [specs/config.md](specs/config.md), [specs/daemon.md](specs/daemon.md), [docs/running-the-sim-daemon.md](docs/running-the-sim-daemon.md).

## Testing your own project

Unit-test against `ReachyMiniApi("fake")` and assert on `api.robot.commands`. For live tests, opt into the shipped pytest plugin and gate each test on the capabilities it needs:

```python
# conftest.py
pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]

# test_robot.py
from reachy_mini_bridge.testing import requires_caps


def test_it_speaks(live_api):
    requires_caps(live_api, "audio")
    api, _caps = live_api
    ...
```

The `live_api` fixture borrows a running daemon or spawns one (sim, or a USB-connected robot's), probes what actually works (`motion`, `audio`, `camera`, `gravity_compensation`, `faces`), and skips rather than fails when it can't. The sim it spawns runs the bridge's test scene — a portrait hidden until a test shows it (the `sim_scene` fixture) — so with the viewer (`REACHY_MINI_E2E_SIM_VIEWER=1`) face tracking and the attention hand-back are tested without a person ([specs/sim_scene.md](specs/sim_scene.md)). Full guide: [docs/testing-with-the-bridge.md](docs/testing-with-the-bridge.md).

## Development

```
uv sync --dev
uv run ruff check .
uv run ruff format .
uv run pyright
uv run pytest            # fast offline tier only
uv run pytest tests-e2e -rs                                  # live tier, headless sim: motion + audio
REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs     # + camera and face tracking (unlocked GUI session)
REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e -rs      # a robot plugged in over USB
```

The live tier spawns a daemon or borrows one already running at `REACHY_MINI_HOST` / `REACHY_MINI_PORT`. Every sim it spawns runs through the bridge's launcher on the test scene, so with the viewer the tracking tests show the portrait and check that the head turns onto it and settles. `-rs` prints why each test skipped: a skip means a capability or credential was missing, not a pass. The full matrix of targets and capabilities is in [AGENTS.md](AGENTS.md) "Running the live e2e tests".

Design changes start in [specs/](specs/) and are built through [plans/](plans/); [AGENTS.md](AGENTS.md) describes the workflow and status discipline. Reference notes on the upstream SDK are in [docs/reachy-mini-api.md](docs/reachy-mini-api.md).
