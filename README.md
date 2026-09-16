# reachy-mini-bridge

A Python library that sits between the [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) robot and whatever drives it — a script, a service, or an LLM agent. It wraps the upstream `reachy_mini` SDK behind one async, intent-level API (`ReachyMiniApi`) whose verbs speak in human terms — *enable the motors, play "happy", follow my face, say this, give me the mic, give me a camera frame* — and runs the same code unchanged against the real robot, the MuJoCo simulator, or an offline fake.

> **Status: layers 0–1 built.** The connection seam, `ReachyMiniApi`, the audio/media session, the declarative config, and the bridge-owned daemon lifecycle (sim, or a USB-attached robot) are implemented and tested on the `real` / `sim` / `fake` backends. The agent-tools layer (`ReachyMiniTools`, exposing the same verbs as functions an LLM runtime can call) is designed but not built yet. See [specs/_index.md](specs/_index.md) for per-spec status.

## What's in the repo

| Path | What it is |
|---|---|
| [src/reachy_mini_bridge/](src/reachy_mini_bridge/) | The library: `api.py` (the verbs), `config.py`, `audio.py` (speech out, mic in), `daemon.py` (daemon lifecycle), `robot.py` + `fake_reachy_mini.py` (the backend seam), `testing/` (a pytest harness for your own e2e tests), `tools.py` (placeholder) |
| [config.example.json](config.example.json) | Every config field with placeholder values |
| [specs/](specs/) | Design docs, one per concept, each with a status — the source of truth for how things are meant to work |
| [plans/](plans/) | Implementation plans that turned those specs into code |
| [docs/](docs/) | Reference notes: the upstream SDK, running the sim daemon, testing your project against the bridge |
| [tests/](tests/), [tests-e2e/](tests-e2e/) | The fast offline suite (default `pytest`) and the opt-in live suite against a sim or real daemon |

This is a spec-driven project: [AGENTS.md](AGENTS.md) is the operating manual (how specs, plans and statuses work) and [specs/_overview.md](specs/_overview.md) the architecture.

## Install

Python 3.12+. The package is not on PyPI yet; add it from a checkout or a git URL:

```
uv add reachy-mini-bridge @ ../reachy-mini-bridge          # or a git+https URL
uv add "reachy-mini-bridge[sim,test] @ ../reachy-mini-bridge"
```

| Extra | Adds | You need it for |
|---|---|---|
| *(none)* | `reachy_mini`, `numpy`, `samplerate` | The `real` backend and the offline `fake` |
| `sim` | `reachy_mini[mujoco]` | The `sim` backend (MuJoCo) |
| `tts` | `tts-engine` | The default voice for `say`. Not needed if you pass your own synthesizer |
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

## What the API does

All verbs are `async`; units are human (degrees, seconds, named emotions). The underlying `reachy_mini.ReachyMini` stays reachable as `api.robot` for anything the bridge does not cover.

| Area | Verbs |
|---|---|
| Motors | `get_motors_state()`, `set_motors_state("enabled" \| "disabled" \| "gravity_compensation")` |
| Expression | `list_emotions()`, `play_emotion(name)` — the upstream recorded-moves library |
| Gaze | `start_head_tracking(weight=1.0)`, `stop_head_tracking()` — the daemon keeps a detected face centered |
| Speech out | `say(text, synth=None)`, `play_sound(file)` |
| Motion while talking | `set_wobbling(enabled)`, `wobbling` — upstream's audio-reactive head sway; on by default, set by the config's `wobbling` flag |
| Mic in | `audio_input(mono=True)` async iterator of int16 PCM bytes, plus `mic_sample_rate` / `mic_channels` |
| Camera | `get_camera_frame()` — raw BGR `ndarray`, `None` when no frame is available |

Verbs that move the robot require motors `enabled` and raise `MotorsNotEnabledError` otherwise. The errors a caller catches — `BridgeError` (the base), `MotorsNotEnabledError`, `GravityCompensationUnsupportedError`, `ConfigError` — import from `reachy_mini_bridge`, next to `ReachyMiniApi`, `ReachyMiniConfig`, `SpeechSynthesizer` and `TTSEngineSynthesizer`; `DaemonError` lives in `reachy_mini_bridge.errors`.

**Talking.** `say` streams text-to-speech to the robot speaker through a `SpeechSynthesizer` — a small protocol (`sample_rate` + `stream(text)` yielding float32 mono chunks) importable from `reachy_mini_bridge`. Bring your own, or configure the default `tts-engine` adapter through the config's `tts` block. Without either, `say` raises `BridgeError`; a `tts` block that fails to build (a missing API key, say) leaves the robot usable and exposes the cause on `api.synthesizer_error`. `say` returns once the utterance has finished playing; cancel the task to stop it (queued audio is flushed).

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

## Configuration

`ReachyMiniConfig` is one declarative object, buildable from a dict, a JSON string or a JSON file. Every block is optional; [config.example.json](config.example.json) shows them all:

```json
{
  "backend": "sim",
  "robot": { "host": "127.0.0.1", "port": 8000 },
  "daemon": { "spawn": "auto", "headless": true },
  "tts": { "module": { "type": "elevenlabs", "api_key_env": "ELEVENLABS_API_KEY", "voice_id": "..." } },
  "audio": { "xvf3800": null },
  "wobbling": true
}
```

- `robot` — keyword arguments forwarded verbatim to upstream `ReachyMini(...)`; ignored on `fake`, so one file switches backends by changing `backend` alone.
- `daemon` — `sim`, or `real` for a robot plugged into this machine over USB (loopback `host` only). `"spawn": "auto"` reuses a daemon already listening at `host:port` or spawns one — a headless MuJoCo daemon for `sim`, the robot's hardware daemon for `real` (it wakes the robot, and puts it to sleep on exit) — and stops it on exit; `"always"` insists on spawning; `"never"` (default) only connects, which is what a wireless robot needs. `"headless": false` opens the MuJoCo viewer; `headless` and `scene` play no part on `real`.
- `tts` — the tts-engine module block that builds the default voice for `say`.
- `audio` — the XVF3800 mic-array profile applied on connect.
- `wobbling` — sways the head with every sound the robot plays, from entry until exit (default `true`); `false` keeps the head still, and `set_wobbling` changes it at runtime.

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

The `live_api` fixture borrows a running daemon or spawns one (sim, or a USB-connected robot's), probes what actually works (`motion`, `audio`, `camera`, `gravity_compensation`), and skips rather than fails when it can't. Full guide: [docs/testing-with-the-bridge.md](docs/testing-with-the-bridge.md).

## Development

```
uv sync --dev
uv run ruff check .
uv run ruff format .
uv run pyright
uv run pytest            # fast offline tier only
uv run pytest tests-e2e  # live tier: spawns a headless sim daemon, or borrows one
```

The live tier reads `REACHY_MINI_E2E_TARGET` (`sim`, default, or `real`), `REACHY_MINI_HOST` / `REACHY_MINI_PORT`, and `REACHY_MINI_E2E_SIM_VIEWER=1` for the MuJoCo viewer. Tests that can't get a daemon skip.

Design changes start in [specs/](specs/) and are built through [plans/](plans/); [AGENTS.md](AGENTS.md) describes the workflow and status discipline. Reference notes on the upstream SDK are in [docs/reachy-mini-api.md](docs/reachy-mini-api.md).
