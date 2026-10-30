# The voice tour

One robot performs a slam in twelve parts, each in a different text-to-speech voice, one per speech module of tts-engine: five local models, then seven hosted APIs. The robot looks at whoever is in front of it, the head breathes and sways with every voice, and one voice follows the next in a short handover, so an audience compares voices rather than waits for them.

It is one bridge and one `TTSEngineSynthesizer` per voice, each handed to `bridge.say(text, synth)` for its part ([app.py](app.py)). Every engine is built and warmed before the show. A voice that cannot be built (no key, a missing extra, a missing model) is dropped with one line, and the tour goes on without its part.

## Install

```
uv sync --extra tts-all
```

- **Keys:** the hosted voices read `GRADIUM_API_KEY`, `CARTESIA_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` and `MISTRAL_API_KEY`. `--local-only` needs none.
- **Kokoro's model:** the int8 export is not on the Hugging Face Hub. Download `kokoro-v1.0.int8.onnx` (114 MB) from the `model-files-v1.1` release of [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx/releases) into `examples/voice_tour/models/` (git-ignored).

The other local models download into the Hugging Face cache on first use.

## Run it

The configs have `"spawn": "auto"`: the tour starts the daemon itself, right after loading the voices, and stops it at the end. For a take, start the daemon first and leave it running, and the tour borrows it, so its startup and the robot's wake-up stay out of the take and a retake costs only the engines' load:

| Robot | Daemon, in its own terminal (optional) | Config |
|---|---|---|
| Lite on USB | `uv run python -m reachy_mini_bridge.real_daemon --preload-datasets` | [configs/lite-usb.json](configs/lite-usb.json) |
| Sim with the viewer and your webcam | `uv run mjpython -m reachy_mini_bridge.sim_daemon --scene minimal --preload-datasets --camera webcam --sim-display camera_overlay --sim-display face_markers` | [configs/sim.json](configs/sim.json) |

Then, with the Lite's config:

```
uv run --extra tts-all --with onnxruntime==1.31.0 python -m examples.voice_tour \
    --config examples/voice_tour/configs/lite-usb.json --timeline take-1.json
```

Both configs turn on face detection and head tracking, so the head follows you while the robot talks. Stay in the camera's view; with nobody there, it just breathes. The tour loads the voices, which takes about 20 s, then waits for Enter: start the recording, then press it. `examples/configs/fake.json` runs the whole tour with no robot, every voice synthesized and nothing heard.

**Keep `--with onnxruntime==1.31.0`.** `reachy_mini` pins `onnxruntime==1.27.0`, on which Kokoro runs about 8× slower: 1.6 s to first audio, a long silence in the middle of the tour. The overlay applies to this one command, and the daemon is a separate process, so it keeps its pinned version.

| Option | What it does |
|---|---|
| `--config FILE` | the robot's `ReachyMiniConfig` (required) |
| `--text FILE` | the parts to perform: [slam.json](slam.json) by default |
| `--local-only` | leaves the hosted voices out |
| `--voice-pause SECONDS` | added between two voices (default 0.5), on top of the handover's own time, unless a line sets its own |
| `--line-pause SECONDS` | added between two lines of a voice (default 0.15), on top of the same, unless a line sets its own |
| `--timeline FILE` | writes each part's voice, label, times and lines as JSON, for a recording's captions |

## The handovers

Every line is its own `say`, so the silence between two voices or two lines is the pause plus what a handover takes anyway: the end of the audio, then the next first audio. The script logs each part's time to first audio and its gap to the previous part. [slam.json](slam.json) shortens the pause into the slower voices with a `pause_s` on their first line, and runs about 1 min 50 s. One take, measured on an Apple M5 Pro on a home connection, with the `fake` backend, so without the speaker path (about 0.2 s more on a robot):

| Into | Pause | First audio | Gap |
|---|---:|---:|---:|
| Piper · Ryan | 0.5 s | 73 ms | 0.57 s |
| Kokoro · Fenrir | 0.3 s | 320 ms | 0.62 s |
| Qwen3 · Aiden | 0 | 1048 ms | 1.05 s |
| Phonon · Micah | 0.5 s | 433 ms | 0.94 s |
| Gradium · Micah | 0.5 s | 191 ms | 0.69 s |
| Cartesia · Zeke | 0.35 s | 169 ms | 0.52 s |
| ElevenLabs · Will | 0.5 s | 201 ms | 0.70 s |
| Gemini · Achird | 0.15 s | 556 ms | 0.71 s |
| OpenAI · Verse | 0 | 656 ms | 0.66 s |
| Deepgram · Aries | 0.5 s | 214 ms | 0.71 s |
| Mistral · Hugo | 0.2 s | 1113 ms | 1.31 s |

First audio moves from one take to the next: Phonon took 49 ms and Mistral 393 ms on another take. Qwen3 starts in about 240 ms when it has just been used and about 1 s after 20 s idle, so its gap cannot go below that with `pause_s` alone.

## Change the tour

- **A text** is a JSON file of parts, each with its `voice` (a file name under [voice-configs/](voice-configs/)), its `label` and its `lines`. A line is its text, or `{"text": ..., "pause_s": ...}` to set the pause added before it, in place of the voice or line pause. A pause cannot be negative, because a `say` started during another interrupts it. Each line is its own `say`, so it should read as a whole sentence: a line ending on a comma is spoken as if it ended there.
- **A voice** is a bridge config whose `tts` block is an ordinary tts-engine engine block, so a voice you like can be copied into your own config as it is. Each carries the `gain_db` that brings it to −22 dBFS, measured on this text; another text or voice settings can shift it ([`tts-engine-level`](https://github.com/funwithagents/tts-engine/blob/main/docs/configuration.md#output-gain) measures a new value).

Written with the voices from the [Reachy Mini audition](https://funwithagents.github.io/tts-engine/auditions/reachy-mini/), where the licences of each voice's samples are listed in its credits.
