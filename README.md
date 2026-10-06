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
| **The simulator** | In the MuJoCo sim, face tracking does not work: the loop never runs the tracking step, and even when it does, the tracker's camera matrix is wrong for the sim camera, so the head settles ~45° away from the face. The sim camera can only show the rendered scene | The bridge detects faces itself in the sim's camera stream and its tracker aims with the sim camera's true geometry: the head turns onto a face and settles on it as on a robot. Every sim the bridge starts runs through its own launcher, which can use your **webcam** as the robot's camera, so the simulated robot sees and follows you (see [The simulator](#the-simulator)) |
| **Testing** | Needs a daemon: the sim or the robot, and a person in front of the camera to test face tracking | An offline `fake` backend that records every command, for fast unit tests. A pytest plugin for live tests that checks what the target can actually do (motion, audio, camera, gravity compensation, faces) and skips a test instead of failing it. Its sim includes a portrait a test can show, move and hide, so face tracking is tested without a person |

## What's in the repo

| Path | What it is |
|---|---|
| [src/reachy_mini_bridge/](src/reachy_mini_bridge/) | The library: `bridge.py` (the verbs), `config.py`, `audio.py` (speech out, mic in), `motion.py` (the motion loop: presence, breathing, emotions), `daemon.py` (daemon lifecycle), `head_tracking.py` (the head tracker: a face to a look-at aim), `face_detection.py` (the detection loop behind `faces`), `yunet.py` (the shipped face detector), `sim_daemon.py` (the sim launcher: webcam camera, viewer overlay), `robot.py` + `fake_reachy_mini.py` (the backend seam), `testing/` (a pytest harness for your own e2e tests) |
| [config.example.json](config.example.json) | Every config field with placeholder values |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How the project is run today: feedback through issues, no external pull requests yet |
| [specs/](specs/) | Design docs, one per concept in folders named after the subsystem (`core/`, `motion/`, `vision/`, `audio/`, `daemon/`, `testing/`, `examples/`), each with a status — the source of truth for how things are meant to work |
| [plans/](plans/) | Implementation plans that turned those specs into code |
| [docs/](docs/) | Reference notes: the upstream SDK, running the sim daemon, the bridge on Linux, testing your project against the bridge |
| [tests/](tests/), [tests-e2e/](tests-e2e/) | The fast offline suite (default `pytest`) and the opt-in live suite against a sim or real daemon |
| [examples/](examples/) | Runnable examples, not part of the package: `control_panel/`, a Gradio control panel with a button for every verb (see [Try it from a browser](#try-it-from-a-browser)) |

This is a spec-driven project: [AGENTS.md](AGENTS.md) is the operating manual (how specs, plans and statuses work) and [specs/_overview.md](specs/_overview.md) the architecture.

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
| `sim` | `mujoco` 3.3.1+ (3.3.x) | The `sim` backend (MuJoCo). Declared directly rather than through `reachy_mini[mujoco]`, whose exact pin on 3.3.0 predates the viewer overlays the sim launcher needs; don't install that upstream extra alongside (see [docs/running-the-sim-daemon.md](docs/running-the-sim-daemon.md) "The MuJoCo version") |
| `tts-pocket` | `tts-engine[pocket]` | The default voice for `say` on the local pocket-tts model: no key, no network once the weights are cached, but torch (hundreds of MB; on Linux a dev checkout takes the CPU build from PyTorch's index) |
| `tts-elevenlabs` | `tts-engine[elevenlabs]` | The default voice for `say` on ElevenLabs (`ELEVENLABS_API_KEY`); a few MB |
| `tts-gradium` | `tts-engine[gradium]` | The default voice for `say` on Gradium (`GRADIUM_API_KEY`); a few MB |
| `test` | `pytest` | The shipped `reachy_mini_bridge.testing` harness for your e2e tests |

Importing the package imports `reachy_mini`, which needs its native libraries installed but not a running daemon.

**Platforms.** Developed on macOS; Linux is where CI runs the whole test suite, sim camera included; Windows is untested. On macOS and Windows, GStreamer comes with `reachy_mini`'s wheels. **On Linux it comes from the system**, and running a daemon on the machine — the sim, or a robot plugged in over USB — also needs the Rust GStreamer webrtc plugin, which no distribution packages; the headless sim's camera needs Mesa's EGL. The packages, the plugin's two routes, and what works without a sound card are in [docs/linux.md](docs/linux.md), which applies to a Lite's USB daemon as much as to the sim's. A client talking to a wireless robot's own daemon needs the GStreamer packages only.

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

Swap `"fake"` for `"sim"` or `"real"` to talk to a daemon you already run. For anything beyond the backend name, build the bridge from a config (see [Configuration](#configuration)):

```python
async with ReachyMiniBridge.from_json_file("robot.json") as bridge:
    await bridge.say("hello")  # voice from the config's `tts` block
```

`from_dict(...)` and `from_json(...)` take the same config as a dict or a JSON string.

Face detection and tracking are opt-in: name a detector in the config's `face_detection` block (`"detector": "yunet"`) and switch on what you want — `"enabled": true` for detection alone, or `"motion": {"tracking": true}` for the head to follow whoever is in view (tracking implies detection); the detector name alone starts nothing — see [What the API does](#what-the-api-does) and the [configuration reference](#face_detection--who-is-in-front-of-the-robot).

### Try it from a browser

The repo ships a Gradio **control panel** — every verb a button, the bridge's state on screen and refreshed twice a second (motor state, attention, the mode flags, a mic level meter, the camera frame, a log), with a Stop button next to `say` and `play_emotion` that cancels the verb mid-flight. It runs from a checkout (it needs the `demo` dependency group, and its voice the `tts` group — both installed by a plain `uv sync`):

```
uv run python -m examples.control_panel --config config.example.json   # the sim viewer, spawned for you
uv run python -m examples.control_panel                                # no config: the offline fake
```

then open `http://127.0.0.1:7860`. The example config opens the MuJoCo viewer window next to the panel and uses your **webcam** as the robot's camera, shown in the corner of that window: enable the motors and the simulated robot turns to follow you (see [The simulator](#the-simulator)). It needs an unlocked GUI session and, on macOS, camera permission for the terminal that runs it. Set the config's `daemon.camera.source` to `"sim"` for the rendered scene instead. Point `--config` at a `real` config to drive the robot. Design and limits: [specs/examples/control_panel.md](specs/examples/control_panel.md).

## What the API does

All verbs are `async`; units are human (degrees, seconds, named emotions). The underlying `reachy_mini.ReachyMini` stays reachable as `bridge.robot` for anything the bridge does not cover.

| Area | Verbs |
|---|---|
| Motors | `get_motors_state()`, `set_motors_state("enabled" \| "disabled" \| "gravity_compensation")` |
| Expression | `list_emotions()`, `play_emotion(name)` — the upstream recorded-moves library |
| Gaze | `start_head_tracking(focus=False)`, `stop_head_tracking()`, `tracking`, `tracking_focus` — the bridge's tracker keeps the reported face in view, composed into the idle motion (the head breathes while it looks), or held exactly on the face with `focus=True` (the antennas keep moving); a mode like presence, off by default (the config's `motion.tracking` flag turns it on at entry; either way it needs a `face_detection.detector`) and needing no motors — the head moves once they are `enabled`; `attention` (`"engaged"` / `"watching"` / `None`) says whether someone is being looked at. The tracker chooses whom to follow — the biggest face, kept while it is seen; a face that vanishes is waited for a second, the head holding toward where it was, before the head turns to the biggest other one. `head_tracking` — an observable `HeadTrackingReport` (`active`, `focus`, `attention`, the `track_id` of the face the head follows), waking when the head engages, switches to someone else or is handed back. Tracking runs the detection loop behind `faces` |
| Speech out | `say(text, synth=None)` — the newest `say` wins: it interrupts one in flight, which raises `SpeechInterruptedError`; `play_sound(file)` — completes when the sound has been heard, plays alongside `say`; the newest sound file wins (an emotion's included), the replaced one raising `SoundInterruptedError` |
| Motion while talking | `set_wobbling(enabled)`, `wobbling` — upstream's audio-reactive head sway; on by default, set by the config's `motion.wobbling` flag |
| Staying alive | `set_presence(enabled)` / `presence`, `set_idle("breathing" \| "hold" \| "custom")` / `idle`, `set_idle_move(factory)` / `idle_move` — the idle behaviour between verbs; set by the config's `motion` block |
| Mic in | `audio_input(mono=True)` async iterator of int16 PCM bytes, plus `mic_sample_rate` / `mic_channels` |
| Camera | `camera` — the camera feed, the one reader of the robot's camera: `camera.latest()` is the newest `CameraFrame` (`frame_id`, `ts`, `image` as a raw BGR `ndarray`, `head_pose`) or `None` when no frame is available; a property any number of consumers sample without taking frames from one another |
| Faces | `faces` — an observable report of the faces in front of the robot: every face with a `track_id` that stays the same while the person stays in view, its pixel box and head angles, and the camera `frame` they were found in (to crop and enrich, joined by `track_id`); `faces.value`, `faces.changes()` (wakes when the number of faces changes, never when one moves), `faces.wait_for(predicate)`; `set_face_detection(enabled)` / `face_detection` — off unless the config's `face_detection` block turns it on, and running whenever tracking is; needs a detector named in the config (`"yunet"`, the shipped one, or `"custom"`); `set_face_detector(factory)` / `face_detector` — your own detector for `"custom"` |

Verbs that move the robot require motors `enabled` and raise `MotorsNotEnabledError` otherwise. The errors a caller catches — `BridgeError` (the base), `MotorsNotEnabledError`, `GravityCompensationUnsupportedError`, `ConfigError` — import from `reachy_mini_bridge`, next to `ReachyMiniBridge`, `ReachyMiniConfig`, `SpeechSynthesizer` and `TTSEngineSynthesizer`; `DaemonError` lives in `reachy_mini_bridge.errors`.

**Talking.** `say` streams text-to-speech to the robot speaker through a `SpeechSynthesizer` — a small protocol (`sample_rate` + `stream(text)` yielding float32 mono chunks) importable from `reachy_mini_bridge`. Bring your own, or configure the default `tts-engine` adapter through the config's `tts` block. Without either, `say` raises `BridgeError`; a `tts` block that fails to build (a provider whose extra isn't installed, a missing API key) leaves the robot usable and exposes the cause on `bridge.synthesizer_error`. `say` returns once the utterance has finished playing; cancel the task to stop it (queued audio is flushed). Cancelling the task is how you interrupt any verb: `play_emotion` stops the motion and the emotion's sound the same way, and the session stays usable for the next verb (see [specs/core/bridge.md](specs/core/bridge.md) "Cancellation").

**Your own idle move.** Subclass `IdleMove` and return the pose as offsets from neutral in human units: `IdleOffsets(z_mm=…, roll_deg=…, pitch_deg=…, yaw_deg=…, antenna_right_deg=…, antenna_left_deg=…)`. Register the class (a factory: the loop builds a fresh move at every idle entry, with `t` starting at 0) and select the `custom` mode, in either order: `await bridge.set_idle_move(SlowNod)` then `await bridge.set_idle("custom")`, or `MotionSettings(idle="custom", idle_move=SlowNod)` in the config. `offsets(t)` runs at 60 Hz on the motion thread, so keep it fast and start it at rest. See [specs/motion/motion.md](specs/motion/motion.md) "Custom idle moves".

**Seeing.** `bridge.camera` is the one reader of the robot's camera: a thread pulls upstream's one-shot `get_frame()` and publishes the newest frame, stamped with its time (and the head pose at that time when the backend gives a capture time), for any number of consumers to sample — a display, an agent tool, the face detector, a vision graph. Upstream hands each frame out once, so two readers would silently steal frames from each other; the feed is why they don't. Its frames are shared and read-only (copy before drawing on one). A vision library built on latest-value sampling plugs onto it directly, no adapter and no second reader:

```python
hands = HandStage(bridge.camera, target_fps=30)  # any latest-value graph whose upstream has latest() → frame_id, ts, image
```

**Face detection is opt-in, and the detector is yours to pick.** `face_detection.detector: "yunet"` selects the shipped detector, run once `face_detection.enabled` or `motion.tracking` is on (or `set_face_detection(True)` / `start_head_tracking()` at runtime) — upstream's own YuNet model, wrapped by the bridge, no new dependency, the weights downloaded into the Hugging Face cache on first use. `"custom"` runs yours on the camera feed — an object with `detect(frame_bgr, ts)` returning `PixelFace`s, registered as a factory with `FaceDetectionSettings(face_detector=MyDetector)` or `set_face_detector(MyDetector)` — another library's detector (vision-modules' MediaPipe landmarker, say) plugs in this way, the bridge itself shipping YuNet alone to stay lightweight. Either way the bridge runs it once per new frame off the event loop, at most `target_fps` times a second, follows every face by `track_id`, and its tracker chooses whom the head follows. With no detector named (the default) nothing is detected and nothing tracks. The shipped wrapper is the worked example of a custom one: [docs/custom-face-detector.md](docs/custom-face-detector.md).

**Listening.** The bridge does no speech recognition. It exposes the robot's echo-cancelled microphone as a stream and you feed it to the ASR of your choice:

```python
async with ReachyMiniBridge("fake") as bridge:
    print(bridge.mic_sample_rate)  # configure your ASR to this
    async for chunk in bridge.audio_input():  # int16 LE mono PCM bytes
        feed_my_asr(chunk)
        break  # stop iterating to stop the tap
```

Routing both directions through the bridge is what keeps the robot's hardware echo cancellation working while it speaks and listens at once.

## Using it from an agent

An LLM agent drives the robot through the same `ReachyMiniBridge`: its tools are plain functions you write, each calling one verb. The verbs take and return JSON-friendly values in human terms, so most tools are a docstring and one line; perception is read from the bridge and encoded for your model inside the tool (a camera frame as base64 JPEG with the image library of your choice). Register them with the agent runtime you use:

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

    async def who_is_there() -> int:
        """Count the faces the robot currently sees."""
        return len(bridge.faces.value.faces)

    return [play_emotion, list_emotions, say, who_is_there]
```

Cancelling the task that runs a tool stops the action on the robot (speech flushed, the move stopped) and leaves the session ready for the next call. A runtime that calls tools synchronously can run the bridge on a background event loop and submit each call to it, as the [control panel](examples/control_panel/) does.

## Backends

| Backend | What it drives | Needs |
|---|---|---|
| `real` *(default)* | The physical robot via its daemon | The robot reachable at `host:port`; for a robot plugged in over USB, the bridge can spawn its daemon for you. Tested on a Reachy Mini Lite only; the wireless robot is untested (see [Project status](#project-status)), and it differs in ways the bridge has not been checked against, such as booting asleep with its motors disabled ([docs/reachy-mini-api.md](docs/reachy-mini-api.md)) |
| `sim` | The upstream MuJoCo mockup | The `sim` extra; a daemon you run, or one the bridge spawns for you |
| `fake` | A first-party in-process stand-in that records every command and returns synthetic audio and frames | Nothing — offline and deterministic; powers the unit tests |

### The simulator

The `sim` backend is upstream's MuJoCo simulation, started through the bridge's own launcher, `python -m reachy_mini_bridge.sim_daemon` (a config with `"daemon": {"spawn": "auto"}` does it for you). The launcher runs upstream's daemon unchanged apart from these additions ([specs/daemon/sim_daemon.md](specs/daemon/sim_daemon.md)):

- **Face tracking works.** The bridge detects faces itself, in the camera stream the daemon serves, and aims with its own tracker and a pinhole of the sim's eye camera — upstream's daemon-side tracking, which the sim never steps and whose camera matrix would put the head about 45° off the face, is left as upstream ships it and never armed. With the viewer open and `"face_detection": {"detector": "yunet"}`, the head turns onto a face and settles on it, breathing. The tracker's convergence is pinned by fast offline tests that project the test scene's portrait through the sim camera, and by the live tests below.
- **Your webcam as the robot's camera.** With `"daemon": {"camera": {"source": "webcam"}}`, the sim's camera shows your computer's webcam instead of the rendered scene. Face tracking, `bridge.camera` and the control panel then see you, with or without the viewer window. The bridge's tracker treats the webcam as fixed where the robot's eye rests, so the head follows you without drifting. On macOS, the terminal or editor that starts the daemon needs camera permission.
- **See what it sees.** With `"daemon": {"headless": false, "sim_displays": {"camera_overlay": true}}`, the viewer window shows the camera stream in its top-right corner — your webcam, or the rendered eye camera. The example config has it on.
- **See where it looks, and where it places you.** `"sim_displays": {"robot_gaze": true, "face_markers": true}` adds, in the viewer's 3D scene, a line along the robot's gaze and an ellipsoid on each face the bridge detects (green for the one the head follows). Only the viewer shows them; the robot's camera never does. Details: [docs/running-the-sim-daemon.md](docs/running-the-sim-daemon.md).
- **A face to test with.** The testing package can write a scene with a portrait that a test shows, moves and hides while the daemon runs ([specs/testing/sim_scene.md](specs/testing/sim_scene.md)). The pytest plugin's sim always runs on it.

A sim started by hand with upstream's `reachy-mini-daemon --sim` works for motion and audio, but has none of these additions. Commands for every mode: [docs/running-the-sim-daemon.md](docs/running-the-sim-daemon.md).

## Configuration

`ReachyMiniConfig` is one declarative object, buildable from a dict, a JSON string or a JSON file. **Every block is optional** — `ReachyMiniConfig()` is a valid config (the real robot, upstream's connection defaults, no daemon management, no voice, everything at rest switched on). [config.example.json](config.example.json) lists every field with placeholder values; the tables below give each one's default and effect.

```json
{
  "backend": "sim",

  "robot": {
    "host": "127.0.0.1",
    "port": 8000
  },

  "daemon": {
    "spawn": "auto",
    "headless": false,
    "camera": {
      "source": "webcam"
    }
  },

  "tts": {
    "module": {
      "type": "pocket",
      "voice": "george"
    }
  },

  "face_detection": {
    "detector": "yunet",
    "enabled": true,
    "width": 320,
    "target_fps": null
  },

  "motion": {
    "presence": true,
    "idle": "breathing",
    "wobbling": true,
    "tracking": true
  }
}
```

That is [config.example.json](config.example.json) in brief — the MuJoCo viewer, started for you, seeing through your webcam, with a local voice.

### `backend`

`"real"` (default), `"sim"` or `"fake"` — see [Backends](#backends). Changing this one string moves a config between the robot and the simulator; every other block is accepted on both, even where it does nothing (the MuJoCo knobs on `real`). The offline `fake` has no daemon, so a `daemon.spawn` other than `"never"` is a `ConfigError` there — set it to `"never"` (or drop the `daemon` block) and the same file runs on the fake.

### `robot` — how to reach the robot

Keyword arguments forwarded verbatim to upstream's `ReachyMini(...)`, so their names and defaults are upstream's, not the bridge's. Unknown keys are rejected at config time (checked against the upstream signature) rather than failing at connect time. The ones you are likely to set:

| Field | Default | What it does |
|---|---|---|
| `host` | `"reachy-mini.local"` | Where the daemon listens. A wireless robot's IP or hostname; `"127.0.0.1"` for a local daemon |
| `port` | `8000` | The daemon's port |
| `connection_mode` | `"auto"` | Transport to the daemon: `"auto"`, `"localhost_only"` or `"network"` (upstream 1.10's values; the bridge checks the key, upstream the value) |
| `media_backend` | `"default"` | How audio/video are carried; `"local"` is what a local daemon serves |
| `timeout` | `5.0` | Seconds to wait on the connection |
| `robot_name` | `"reachy_mini"` | The robot's name on the daemon |
| `automatic_body_yaw` | `true` | Upstream's automatic body-yaw following |
| `log_level` | `"INFO"` | Upstream client log level |

Two keys are **reserved**: `use_sim` (derived from `backend`) and `spawn_daemon` (use `daemon.spawn`) — either one is a config error pointing you at the right field. On `fake` the block is validated but unused. When the bridge manages the daemon (`daemon.spawn` other than `"never"`), it fills in what a local daemon needs for anything you left unset — `host` `127.0.0.1`, `port` `8000`, `connection_mode` `"network"`, `media_backend` `"local"` — and `host`, if you do set it, must be a loopback address.

### `daemon` — whether the bridge starts one

| Field | Default | What it does |
|---|---|---|
| `spawn` | `"never"` | `"never"`: only connect, to a daemon you run (what a wireless robot needs). `"auto"`: reuse one already listening at `host:port`, else start one and stop it on exit. `"always"`: insist on starting one — a port already in use is an error |
| `headless` | `true` | *sim only.* `true` runs MuJoCo with no window: motion and audio, and on Linux the rendered camera too (offscreen through EGL); on macOS no camera. `false` opens the **viewer** (under `mjpython` on macOS), so you watch the robot and the `sim` camera works everywhere; needs an unlocked GUI session |
| `scene` | `null` | *sim only.* An upstream scene name (`"empty"`, `"minimal"`), or the path of a scene `.xml` for the bridge's launcher — how the test scene's portrait gets loaded ([specs/testing/sim_scene.md](specs/testing/sim_scene.md)) |
| `camera` | `{"source": "sim"}` | *sim only.* What the sim's camera shows — see the table below |
| `preload_datasets` | `true` | Downloads the recorded-move datasets in the background at startup, so the first `play_emotion` doesn't wait on a download. Readiness isn't delayed either way |
| `startup_timeout` | `45.0` | Seconds to wait for a spawned daemon to become ready (a positive, finite number) |

`spawn` other than `"never"` needs `backend` `"sim"` or `"real"` (`fake` has no daemon). On `real` it starts the hardware daemon of a robot plugged into **this machine** over USB — it wakes the robot, and puts it to sleep on exit. `headless`, `scene` and `camera` are MuJoCo knobs and play no part on `real`.

**`daemon.camera`** — the sim's eyes ([specs/daemon/sim_daemon.md](specs/daemon/sim_daemon.md)):

| Field | Default | What it does |
|---|---|---|
| `source` | `"sim"` | `"sim"` renders the scene from the robot's eye camera (the viewer, or headless on Linux). `"webcam"` relays your computer's camera instead, so the person in front of the screen is who the simulated robot sees and follows — headless or viewer |
| `device` | `null` | *webcam only.* `null` is the default camera; an integer is a macOS device index, a string a Linux device path (`/dev/video0`) |
| `hfov_deg` | `70.0` | *webcam only.* The camera's horizontal field of view in degrees, which the tracker's intrinsics derive from — match it to your camera for an accurate aim |

### `tts` — the default voice for `say`

A [tts-engine](https://github.com/funwithagents/tts-engine) `engine` block, carried through verbatim: `module.type` picks the provider, and the matching `tts-*` extra must be installed. The remaining keys are the provider's own.

The local model (`tts-pocket`), no key and no network once its weights are cached:

```json
"tts": {
  "module": {
    "type": "pocket",
    "voice": "george",
    "device": "auto"
  }
}
```

Or the cloud provider (`tts-elevenlabs`), whose key is read from the named environment variable:

```json
"tts": {
  "module": {
    "type": "elevenlabs",
    "api_key_env": "ELEVENLABS_API_KEY",
    "voice_id": "..."
  }
}
```

Or Gradium (`tts-gradium`), keyed the same way:

```json
"tts": {
  "module": {
    "type": "gradium",
    "api_key_env": "GRADIUM_API_KEY",
    "voice_id": "..."
  }
}
```

Whatever the provider, `say` plays audio that arrives at the speaker's 16 kHz as is and resamples any other rate to it.

Omit the block and `say` raises unless you pass your own `SpeechSynthesizer`. A block that fails to build — extra not installed, API key unset — leaves the robot fully usable without a voice and puts the cause on `bridge.synthesizer_error`.

### `audio` — the microphone array

| Field | Default | What it does |
|---|---|---|
| `xvf3800` | `null` | The XVF3800 audio-processor profile applied when the media session starts, as a list of `[name, [values…]]` pairs. `null` keeps the firmware defaults |

### `face_detection` — who is in front of the robot

| Field | Default | What it does | Runtime verb |
|---|---|---|---|
| `detector` | `null` | Which detector the bridge runs on the camera feed's frames: `null` none (no detection, no tracking — `enabled` or `tracking` on is then a config error); `yunet` the shipped detector, upstream's model run by the bridge (nothing to install; the weights download into the Hugging Face cache on first use); `custom` your own, registered from code with `FaceDetectionSettings(face_detector=...)` or `set_face_detector(...)` (see [docs/custom-face-detector.md](docs/custom-face-detector.md)); session entry refuses `custom` with none registered | `set_face_detector` |
| `enabled` | `false` | Runs the detection loop from session entry, so `bridge.faces` reports who is there. Needs no motors, needs a `detector`. The loop also runs whenever `motion.tracking` is on, whatever this says | `set_face_detection` |
| `width` | `320` | The width the shipped detector works at — its cost per frame against its precision. 320 px is upstream's own; 640 quadruples the cost and halves the landmark error; `null` detects on the full frame. A custom detector's width is its author's | — |
| `target_fps` | `null` | A ceiling on detections per second, for any detector: the loop skips frames. `null` detects once per new camera frame. The lever for the wireless robot, whose camera streams at 30 fps. The loop logs the detector's mean time and rate once, 10 s in | — |

### `motion` — what the robot does at rest

`presence` and `wobbling` default to `true`, `tracking` to `false` (it needs a `face_detection.detector`) and `idle` to `"breathing"`; each has a runtime verb that changes it while the session is entered.

| Field | Default | What it does | Runtime verb |
|---|---|---|---|
| `presence` | `true` | Fills every idle moment with the idle move, so the robot never looks dead between verbs. `false` commands the head only while a verb runs — for a caller driving the head itself. Emotions play either way | `set_presence` |
| `idle` | `"breathing"` | Which idle move presence plays: `breathing` (slow breaths with rests, the head roaming, the antennas flicking), `hold` (a still neutral) or `custom` (your own `IdleMove`, registered from code with `MotionSettings(idle_move=...)` or `set_idle_move`; the hold until one is registered). Ignored while `presence` is off | `set_idle` / `set_idle_move` |
| `wobbling` | `true` | Sways the head with every sound the robot plays. `false` keeps it still while audio plays | `set_wobbling` |
| `tracking` | `false` | The bridge's tracker keeps the reported face in view from session entry, the head breathing while it looks. Needs no motors (the head moves once they are `enabled`) but a `face_detection.detector` (`true` with none is a config error). `false` leaves it off until you call `start_head_tracking()` | `start_head_tracking` / `stop_head_tracking` |

Validation rules and the reasoning behind each block: [specs/core/config.md](specs/core/config.md), [specs/daemon/daemon.md](specs/daemon/daemon.md), [docs/running-the-sim-daemon.md](docs/running-the-sim-daemon.md).

## Testing your own project

Unit-test against `ReachyMiniBridge("fake")` and assert on `bridge.robot.commands`. For live tests, opt into the shipped pytest plugin and gate each test on the capabilities it needs:

```python
# conftest.py
pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]

# test_robot.py
from reachy_mini_bridge.testing import requires_caps


def test_it_speaks(live_bridge):
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge
    live_bridge.run(bridge.say("hello", my_synth))  # every bridge call runs on the harness's loop
```

The `live_bridge` fixture borrows a running daemon or spawns one for the whole run (sim, or a USB-connected robot's) — one bridge session per test file over it — probes what actually works (`motion`, `audio`, `camera`, `gravity_compensation`, `faces`), and skips rather than fails when it can't — unless `REACHY_MINI_E2E_REQUIRED_CAPS` names capabilities the run must have, which is how the bridge's CI fails on a sim that comes up without its camera. The sim it spawns runs the bridge's test scene — a portrait hidden until a test shows it (the `sim_scene` fixture) — so with the viewer (`REACHY_MINI_E2E_SIM_VIEWER=1`) face tracking and the hand-back to the idle motion are tested without a person ([specs/testing/sim_scene.md](specs/testing/sim_scene.md)). Full guide: [docs/testing-with-the-bridge.md](docs/testing-with-the-bridge.md).

## Development

```
uv sync                  # every group: the tooling, the sim, the TTS providers
uv run ruff check .
uv run ruff format src tests tests-e2e examples
uv run pyright
uv run pytest            # fast offline tier only
uv run pytest tests-e2e -rs                                  # live tier, headless sim: motion + audio (+ camera on Linux)
REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs     # the viewer: camera and face tracking on macOS too (unlocked GUI session)
REACHY_MINI_E2E_TARGET=real uv run pytest tests-e2e -rs      # a robot plugged in over USB
```

The live tier spawns a daemon or borrows one already running at `REACHY_MINI_HOST` / `REACHY_MINI_PORT`. Every sim it spawns runs through the bridge's launcher on the test scene, so with the viewer the tracking tests show the portrait and check that the head turns onto it and settles. `-rs` prints why each test skipped: a skip means a capability or credential was missing, not a pass. The full matrix of targets and capabilities is in [AGENTS.md](AGENTS.md) "Running the live e2e tests".

Design changes start in [specs/](specs/) and are built through [plans/](plans/); [AGENTS.md](AGENTS.md) describes the workflow and status discipline. Reference notes on the upstream SDK are in [docs/reachy-mini-api.md](docs/reachy-mini-api.md).

## License

MIT — see [LICENSE](LICENSE).
