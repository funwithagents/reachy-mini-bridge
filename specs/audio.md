---
code:
  - src/reachy_mini_bridge/audio.py
tests:
---

# Audio & media session (`audio.py`)

**Status:** Draft

## Purpose

Everything about getting sound **into** and **out of** the robot correctly: the shared media session, routing synthesized speech to the robot speaker, exposing the robot's microphone as a clean audio stream, and keeping the robot's on-board **acoustic echo cancellation (AEC)** working so a listener (ASR) doesn't hear the robot's own voice.

This concept exists because audio on Reachy Mini is **not** a pair of independent laptop `sounddevice` streams. It is a single daemon-owned pipeline with a hardware voice processor in the middle. Playing audio and reading the mic are *robot* operations that must go through that pipeline. Everything above ([api.md](api.md)'s `say` and microphone verbs) is a thin wrapper over what this layer sets up.

### Why TTS lives in the bridge but ASR does not

The dividing line is **coupling to the robot**:

- **TTS output is a robot capability.** Audio only reaches the speaker via the daemon (`media.push_audio_sample`), and AEC only works if playback goes through that pipeline. So the bridge **must own the output path**. To avoid welding the bridge to one TTS implementation, it owns that path behind a **`SpeechSynthesizer` interface** the bridge defines — [`tts-engine`](../../tts-engine) is the default adapter; any caller can supply their own.
- **ASR is not robot-coupled.** Transcription is just computation over an audio stream — nothing about it needs the robot SDK. The *only* robot-specific part is obtaining the **echo-cancelled microphone audio**. So the bridge's job for ASR is simply to **expose that mic stream as a clean, well-specified API** and let the caller attach whatever ASR they want. The bridge embeds **no** ASR engine and defines no recognizer interface.

Net: the bridge **owns TTS** (behind a pluggable synthesizer) and **enables ASR** (by serving clean mic audio) without owning it.

## Background: how audio actually works on Reachy Mini

Read this before the design; it is the reason for every decision below.

- **There is an XMOS XVF3800 voice processor** between the physical mic array / speaker and the daemon. In firmware it does **AEC**, noise suppression, AGC, and beamforming / direction-of-arrival. It is the reason a conversational robot can listen while it talks.
- **AEC needs the far-end reference.** The XVF3800 cancels the robot's own speech from the mic signal by subtracting what is being played. It only has that reference if playback goes **through the daemon** (`media.push_audio_sample` after `media.start_playing`). If TTS is played on a laptop `sounddevice` output instead, the chip never sees the reference, and the mic (and therefore any ASR) picks up the robot's own voice with no cancellation. **This is exactly why the bridge must own TTS output.**
- **Capture is the *processed* stream.** `media.get_audio_sample()` returns the XVF3800's post-processed output (echo-cancelled, denoised, AGC'd, mono) at `media.get_input_audio_samplerate()`. Opening a raw `sounddevice` `InputStream` against the USB mic array bypasses the chip entirely and defeats the whole point. **This is why the mic stream must come from the bridge, not from the caller opening a device.**
- **The chip is tunable at runtime** via `robot.media.audio.apply_audio_config(params, verify=..., write_settle_seconds=...)` — noise-suppression floors, echo-tail suppression, AGC max gain, etc. The upstream conversation app applies a tuned profile at startup; without it you get firmware defaults.
- **Both directions run concurrently and continuously.** The upstream reference (`reachy_mini_conversation_app/console.py`) opens `start_recording()` + `start_playing()` once, then runs a record loop (`get_audio_sample` → consumer) and a play loop (producer → `push_audio_sample`) as simultaneous async tasks for the life of the session.

The upstream capability surface we rely on (see [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)):
`media.start_recording()` / `stop_recording()`, `media.get_audio_sample()`, `media.get_input_audio_samplerate()`, `media.start_playing()` / `stop_playing()`, `media.push_audio_sample(np.float32)`, `media.audio.apply_audio_config(...)`, `media.audio.clear_player()` (barge-in flush), `media.get_DoA()`.

## Core concepts / Decided

- **One media session, owned here.** A single object (working name `MediaSession`) opens `start_recording()` + `start_playing()`, applies the XVF3800 config, and owns teardown (`stop_recording()` / `stop_playing()`). It is opened once per connection and shared by both output (`say`) and input (mic stream) — never opened/closed per utterance. Lifecycle is tied to the [client.md](client.md) connection (context manager), not to individual verbs. Owning **both** directions is what makes AEC work: TTS out through the pipeline is the reference the mic-in stream is cancelled against.

- **Async is mandatory here, which forces [api.md](api.md) async.** Playback and a live mic stream must run concurrently on one event loop, and `tts-engine`'s synthesis is `async`. Streaming the mic while the robot is also moving / speaking needs true concurrency, not a per-call `asyncio.run()`. This concept therefore *requires* [api.md](api.md) to be **async-native** (see [api.md](api.md) "Async, not sync"). The upstream SDK's blocking motion calls are wrapped in `asyncio.to_thread(...)` at the api layer so a move never stalls the audio loops.

### TTS out — a bridge-owned `SpeechSynthesizer` interface

- **The bridge defines the interface; the implementation is pluggable.** The bridge's real need from "TTS" is small: turn text into a stream of PCM chunks at a known rate, which this layer resamples and pushes to the robot speaker. That is a narrow Protocol the bridge owns:

  ```python
  class SpeechSynthesizer(Protocol):
      """Turns text into a stream of PCM audio the media layer can play."""
      @property
      def sample_rate(self) -> int: ...                          # Hz of the chunks below
      def stream(self, text: str) -> AsyncIterator[np.ndarray]:  # float32 mono, [-1, 1]
          ...
  ```

  `say` depends only on this. Keep it **deliberately minimal** — text in, PCM out. Rich features (voices, SSML, rate) stay inside the concrete synthesizer and its own config; the seam carries only what the robot audio loop needs. (Voice/options selection is an open question below.)

- **Format: match the consumer.** The synthesizer emits **float32 mono numpy in `[-1, 1]`** because *its* consumer is the robot speaker — `media.push_audio_sample` takes float32, and the rate conversion we must do anyway (synth rate → robot rate) is a float operation. So the output side is float32 end-to-end with no dtype juggling in the hot path, and numpy is self-describing (dtype fixed, shape carries channels) so no out-of-band format descriptor is needed. This is the same "match the consumer" rule the mic stream follows in the other direction (where the consumer is an ASR engine, so the format is int16 — see below).

- **`tts-engine` is the default adapter, shipped under an optional extra.** A `TTSEngineSynthesizer` adapts our first-party [`tts-engine`](../../tts-engine) to `SpeechSynthesizer`. It lives behind a `tts` extra (`reachy-mini-bridge[tts]`, see [project.md](project.md)) so the bridge core does **not** hard-depend on `tts-engine` / `sounddevice` / a cloud TTS SDK — a caller supplying their own synthesizer pulls none of it. `say` raises a clear error if no synthesizer is configured and the default extra isn't installed.

- **The adapter needs `tts-engine` to expose its PCM stream.** `tts-engine`'s `TTSEngine.speak` currently swallows audio into a local `sounddevice` player. The clean adapter uses a **sink abstraction** requested of `tts-engine` (hand-off: [../docs/handoff-tts-engine-sink-abstraction.md](../docs/handoff-tts-engine-sink-abstraction.md)); until that lands, the adapter can drive `tts-engine`'s module layer directly (`load_module` + `module.stream(..., callback)`), at the cost of coupling to internals. `tts-engine` emits **int16** natively, so this adapter does the trivial `int16 → float32` (`/32768.0`) to meet the `SpeechSynthesizer` contract — a conversion float-native synthesizers (most neural TTS) skip.

- **The robot sink.** The speaker wants **float32 mono @ 16 kHz** (`media.push_audio_sample` takes float32 `[-1, 1]`; there is no rate argument or output-rate getter — 16 kHz is the fixed XVF3800 pipeline rate, same as input; see [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)). With the synthesizer already emitting float32 mono, the sink's only remaining step is a **resample from `synth.sample_rate` to 16 kHz** — and that is **skippable when `synth.sample_rate == 16000`** (e.g. `tts-engine`'s ElevenLabs module configured for `pcm_16000`), making `say` a straight passthrough. On the `fake`/local-dev path it may instead play to a local device (no AEC — dev only).

### Mic in — a clean audio stream for the caller's own ASR

- **The bridge exposes the echo-cancelled mic as a stream; it does no ASR.** The primitive is an async stream of PCM plus its format, e.g. an `audio_input()` async iterator yielding `bytes` (**int16 LE mono**, i.e. linear16) with a `mic_sample_rate` property, sourced by looping `media.get_audio_sample()` inside the managed session. This is the whole ASR story on the bridge side: clean, echo-cancelled audio anyone can consume. **Int16 here follows the same "match the consumer" rule as the float32 synth side** — this stream's consumer is an ASR engine, and streaming ASR (`asr-engine`, Deepgram, Google, Whisper-streaming) universally wants linear16 PCM.

- **No resampling on the mic path — the native format already matches.** `media.get_audio_sample()` natively returns **int16, mono** at the rate reported by `media.get_input_audio_samplerate()` (the XVF3800 voice pipeline runs at **16 kHz**) — see [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md). That is exactly the mic-stream contract above and exactly `asr-engine`'s default `AudioFormat` (16 kHz / mono / linear16), so the bridge does **not** resample, convert dtype, or remix: it passes frames through at their native rate (just `ndarray.tobytes()`), defensively collapsing to mono only if a config ever delivers multichannel. It **exposes `mic_sample_rate`** (the value from `get_input_audio_samplerate()`) so the caller configures their ASR to the native rate; any rare rate-matching then lands in the ASR backend, never here. Resampling remains only on the TTS **output** path (synth rate → the robot's 16 kHz speaker rate), and even there it is skippable when the synthesizer emits 16 kHz mono.

- **"Easy to connect any ASR" is the design goal for this API.** The stream must be trivial to feed into common ASR engines. Concretely that means: a documented, stable format (rate/channels/encoding, endianness); a pull-based async iterator that a consumer can drain at its own pace; and — as a convenience — an optional queue-shaped source object (`start() -> asyncio.Queue[bytes]`, `stop()`) so it drops straight into engines that consume that shape (our first-party [`asr-engine`](../../asr-engine) accepts exactly this via its `audio_source=` injection, so a user picks it up in a few lines — see [../docs/handoff-asr-engine-robot-integration.md](../docs/handoff-asr-engine-robot-integration.md)). The bridge does **not** depend on `asr-engine`; that doc is guidance for a *caller* who chooses it.

### Shared

- **XVF3800 config applied on session start.** The session applies a tuned audio profile via `media.audio.apply_audio_config(...)` after the pipeline is up (mirroring the upstream conversation app's startup config: NS floors, echo-tail suppression, AGC gain). The profile is a named default the bridge ships; overridable by callers.

- **Barge-in.** Interrupting playback mid-utterance flushes queued speaker audio via `media.audio.clear_player()` (falling back to the deprecated `clear_output_buffer()` on older SDKs). Exposed so [api.md](api.md) / [tools.md](tools.md) can offer a "stop talking" action.

- **Local-device audio is a dev-only fallback, not a co-equal mode.** Playing TTS to / reading a mic from local `sounddevice` devices is fine on a laptop / Lite dev box **without AEC**. On the robot it is wrong: it defeats echo cancellation. So `say` and the mic stream default to the robot media path when connected to a real/sim robot; the local path is an explicit opt-in for offline development against the `fake` backend.

- **Dependencies this concept introduces.** `tts-engine` enters as an **optional** dependency under the `tts` extra (first-party sibling repo — local path dependency now, pinned git URL later, per [project.md](project.md)) — not a base runtime dependency. **`asr-engine` is *not* a dependency of the bridge** at all. `numpy` is already a direct dependency (frame/sample handling). Resampling needs a resampler (`scipy.signal` is available transitively via `reachy_mini`; whether to depend on it directly or vendor a small polyphase resample is an open question below).

- **`fake` backend support.** The `FakeRobot` ([client.md](client.md)) must satisfy whatever media members this session calls (`start_recording`, `get_audio_sample`, `get_input_audio_samplerate`, `start_playing`, `push_audio_sample`, `audio.apply_audio_config`, `audio.clear_player`), returning/recording synthetic audio so the say/mic stack is exercisable offline and in `tests/`. `tests/` also use a trivial fake `SpeechSynthesizer` (emits silence/a tone), needing no `tts-engine` import.

## Relationship to the other specs (changes required)

- **[client.md](client.md):** the `RobotClient` Protocol must include the `media` audio primitives listed under Background above, and `FakeRobot` must implement them. (Noted in client.md.)
- **[api.md](api.md):** becomes async-native; `say` routes through this layer's sink over a pluggable `SpeechSynthesizer`; a microphone-access verb (`audio_input()` + `mic_sample_rate`) joins Perception. No ASR verbs — the caller runs ASR on the exposed stream. (Noted in api.md.)
- **[project.md](project.md):** `tts-engine` is an optional `tts` extra with the default synthesizer adapter, not a base dependency; `asr-engine` is not a dependency. (Noted in project.md.)
- **[tools.md](tools.md):** if a consuming runtime needs sync tool callables, the sync↔async bridging lives at the tools layer (a managed background loop), not by making the api sync.

## Open questions

1. **Resampler choice (TTS output path only).** The **mic path needs no resampler** — native int16 mono @ 16 kHz passes straight through (see above) — and the **output path needs one only when the synthesizer's rate differs from 16 kHz** (skippable if it emits 16 kHz mono, e.g. ElevenLabs `pcm_16000`). Where that resample lives (a shared helper in this module) and what backs it (`scipy.signal.resample_poly`, `soxr`, or a vendored polyphase) — including quality/latency trade-offs for real-time streaming — is deferred to the implementation plan. Preferring 16 kHz-native synthesizer config, to avoid the resample entirely, is a reasonable default to consider there.
2. **Audio-format contract (decided; residuals deferred).** Decided per the "match the consumer" rule: `SpeechSynthesizer` emits **float32 mono `[-1, 1]` numpy** (speaker-facing), the mic stream yields **int16 LE mono `bytes`** (ASR-facing). Residuals for the implementation plan: exact numpy chunk **shape** (`(n,)` vs `(n, 1)`); whether to offer a convenience helper / accept int16 from a synthesizer for authors who have it; and whether either seam ever needs to carry a channel count rather than fixing mono.
3. **Voice / synthesis options on the seam.** Whether `say`/`SpeechSynthesizer.stream` grow an options argument (voice, rate) or those stay entirely inside the concrete synthesizer's config is deferred; keep the seam minimal until a real need appears.
4. **Mic stream shape.** Whether the public primitive is the async iterator, the queue-source object, or both (and multi-consumer fan-out if two things want the mic at once) is deferred to the implementation plan, driven by what real ASR integrations need.
5. **XVF3800 default profile.** Whether the bridge ships the upstream conversation-app profile verbatim, tunes its own, or exposes named profiles (e.g. "conversation" vs. "far-field") is deferred; needs testing on hardware.
6. **Full-duplex vs. half-duplex default.** Whether the mic stream is gated (or auto-barge-in armed) while `say` is playing, given AEC quality in practice, is deferred until measured on real hardware.
7. **DoA / beamforming exposure.** How `get_DoA()` and any beam-steering surface as verbs (and whether they belong here or in [api.md](api.md) perception) is deferred alongside the perception return-shape question in [api.md](api.md).
