# The voice tour

One robot performs a slam in twelve voices, one per speech module of tts-engine: five local models first, then seven hosted APIs. While it speaks, the head breathes, sways with the voice and follows whoever is in front of the camera. One voice follows the next after a short handover, so an audience compares the voices without waiting between them.

## Run it

```
uv sync --extra tts-all
```

The hosted voices read `GRADIUM_API_KEY`, `CARTESIA_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY` and `MISTRAL_API_KEY`. A voice without its key is left out. The local models download into the Hugging Face cache the first time they are used.

```
uv run --extra tts-all --with onnxruntime==1.31.0 python -m examples.voice_tour \
    --config examples/voice_tour/configs/lite-usb.json --timeline take-1.json
```

Loading the voices takes about 20 s. The tour then waits for Enter, so you can start recording first.

| Config | Robot |
|---|---|
| [configs/lite-usb.json](configs/lite-usb.json) | a Reachy Mini Lite on USB |
| [configs/sim.json](configs/sim.json) | the MuJoCo sim with the viewer, your webcam as its camera, and the face markers drawn |
| `examples/configs/fake.json` | no robot: every voice is synthesized, and nothing is heard |

Both robot configs spawn the daemon (`"spawn": "auto"`) and turn on face detection and head tracking. If a daemon is already running at the address, the tour borrows it and leaves it running. For a recording session, starting the daemon in another terminal keeps its startup and the robot's wake-up out of every take:

- **Lite:** `uv run python -m reachy_mini_bridge.real_daemon --preload-datasets`
- **Sim:** `uv run mjpython -m reachy_mini_bridge.sim_daemon --scene minimal --preload-datasets --camera webcam --sim-display camera_overlay --sim-display face_markers`

`--with onnxruntime==1.31.0` matters: `reachy_mini` pins 1.27, on which Kokoro takes about 1.6 s to start speaking. The overlay applies to the tour's process only, and the daemon keeps its pinned version.

| Option | What it does |
|---|---|
| `--config FILE` | the robot's `ReachyMiniConfig` (required) |
| `--text FILE` | the text to perform: [slam.json](slam.json) by default |
| `--local-only` | leaves the hosted voices out |
| `--voice-pause SECONDS` | the pause between two voices (default 0.5) |
| `--line-pause SECONDS` | the pause between two lines of one voice (default 0.15) |
| `--timeline FILE` | writes when each part and line started, with its text, as JSON for captions |

## How it works

[app.py](app.py) opens one `ReachyMiniBridge` session and builds one `TTSEngineSynthesizer` per voice. It speaks each line with `bridge.say(text, synth)`, passing that line's voice:

1. **Before the show,** every voice is built and synthesizes one warm-up sentence, so no model loads during the performance. A voice that cannot be built is dropped with a log line, for example because of a missing key, extra or model, and the tour goes on without its part.
2. **During the show,** the parts play in order. Each line is its own `say`, which returns once the line has been heard. The tour then sleeps the pause and starts the next line.
3. **The motion** comes from the bridge: breathing as the idle move, the head swaying with the speech, and head tracking aiming at the face it sees.

The silence you hear between two lines is the pause plus the next voice's time to first audio: from about 0.1 s for the fastest voices to about 1 s for the slowest. A line can set its own `pause_s` to shorten the wait before a slow voice. A pause cannot be negative: the bridge plays one `say` at a time, and a new one interrupts the one in flight. The log gives each part's time to first audio and its gap after the previous part, to tune the pauses.

## The voices

Each voice is a file under [voice-configs/](voice-configs/): a bridge config whose `tts` block is an ordinary tts-engine engine block, so you can copy a voice into your own config as it is.

| # | Voice | Runs | Config |
|---|---|---|---|
| 1 | Pocket · George | local | [pocket-george.json](voice-configs/pocket-george.json) |
| 2 | Piper · Ryan | local | [piper-ryan-medium.json](voice-configs/piper-ryan-medium.json) |
| 3 | Kokoro · Fenrir | local | [kokoro-am-fenrir.json](voice-configs/kokoro-am-fenrir.json) |
| 4 | Qwen3 · Aiden | local | [qwen3-1.7b-aiden-bf16.json](voice-configs/qwen3-1.7b-aiden-bf16.json) |
| 5 | Phonon · Micah | local | [phonon-31m-micah.json](voice-configs/phonon-31m-micah.json) |
| 6 | Gradium · Micah | hosted | [gradium-micah.json](voice-configs/gradium-micah.json) |
| 7 | Cartesia · Zeke | hosted | [cartesia-zeke-happy.json](voice-configs/cartesia-zeke-happy.json) |
| 8 | ElevenLabs · Will | hosted | [elevenlabs-will-v3c.json](voice-configs/elevenlabs-will-v3c.json) |
| 9 | Gemini · Achird | hosted | [gemini-achird.json](voice-configs/gemini-achird.json) |
| 10 | OpenAI · Verse | hosted | [openai-verse.json](voice-configs/openai-verse.json) |
| 11 | Deepgram · Aries | hosted | [deepgram-aries.json](voice-configs/deepgram-aries.json) |
| 12 | Mistral · Hugo | hosted | [mistral-hugo.json](voice-configs/mistral-hugo.json) |

The settings that shape what you hear:

- **Qwen3** takes a spoken instruction ("Speak at a brisk, lively pace."). Without it, Aiden's pace varies and is often slow.
- **Cartesia** sets an emotion (`happy`) and a speed (1.1).
- **Kokoro** runs its full-precision fp32 model, the module's default.

Each voice also has a `gain_db` that brings it to −22 dBFS, measured on this text with [`tts-engine-level`](https://github.com/funwithagents/tts-engine/blob/main/docs/configuration.md#output-gain). The exception is Aiden: its gain is set 3 dB above the measured value by ear, because a dark voice, with most of its energy below 1 kHz, sounds quieter than its level. A different text or different voice settings can shift a gain, so measure again after changing them.

The voices were chosen in the [Reachy Mini audition](https://funwithagents.github.io/tts-engine/auditions/reachy-mini/), whose credits list the licence of each voice's samples.

## The slam

[slam.json](slam.json) gives each voice two rhyming lines, and each voice's lines talk about that voice:

- **The opening:** George introduces the robot and the idea of slamming one voice at a time.
- **The local voices:** Piper on turning letters into phonemes, Kokoro on prosody, Qwen3 on taking instructions, and Phonon on running on the desk.
- **The hosted voices:** Gradium plays the same Micah as Phonon, streamed from a server, then Cartesia's set mood, ElevenLabs' intonation, Gemini's language model, and OpenAI's live-conversation voice.
- **The ending:** Deepgram and Mistral ask which of the voices is the robot's own.

A text is a JSON file of parts. Each part has a `voice` (a file name under [voice-configs/](voice-configs/), without `.json`), a `label` for the log and the timeline, and its `lines`. A line is either a plain string or `{"text": ..., "pause_s": ...}`, whose pause replaces the default pause before that line. Since each line is spoken as its own `say`, write each one as a whole sentence: a line that ends on a comma is spoken as if the sentence ended there.
