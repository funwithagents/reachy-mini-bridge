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
- **Capture is the *processed* stream.** `media.get_audio_sample()` returns the XVF3800's post-processed output (echo-cancelled, denoised, AGC'd) at `media.get_input_audio_samplerate()`. **Format (per the SDK source): `float32`, interleaved, `get_input_channels()` channels (2 — stereo — in the current GStreamer backend), at 16 kHz.** Opening a raw `sounddevice` `InputStream` against the USB mic array bypasses the chip entirely and defeats the whole point. **This is why the mic stream must come from the bridge, not from the caller opening a device.**
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
      def sample_rate(self) -> int: ...  # Hz of the chunks below
      def stream(self, text: str) -> AsyncIterator[np.ndarray]:  # float32 mono, [-1, 1]
          ...
  ```

  `say` depends only on this. Keep it **deliberately minimal** — text in, PCM out. Rich features (voices, SSML, rate) stay inside the concrete synthesizer and its own config; the seam carries only what the robot audio loop needs. (Voice/options selection is an open question below.)

- **Format: match the consumer.** The synthesizer emits **float32 mono numpy in `[-1, 1]`** because *its* consumer is the robot speaker — `media.push_audio_sample` takes float32, and the rate conversion we must do anyway (synth rate → robot rate) is a float operation. So the output side is float32 end-to-end with no dtype juggling in the hot path, and numpy is self-describing (dtype fixed, shape carries channels) so no out-of-band format descriptor is needed. This is the same "match the consumer" rule the mic stream follows in the other direction (where the consumer is an ASR engine, so the format is int16 — see below).

- **`tts-engine` is the default adapter, shipped under an optional extra.** A `TTSEngineSynthesizer` adapts our first-party [`tts-engine`](../../tts-engine) to `SpeechSynthesizer`. It lives behind a `tts` extra (`reachy-mini-bridge[tts]`, see [project.md](project.md)) so the bridge core does **not** hard-depend on `tts-engine` / `sounddevice` / a cloud TTS SDK — a caller supplying their own synthesizer pulls none of it. `say` raises a clear error if no synthesizer is configured and the default extra isn't installed.

- **The adapter needs `tts-engine` to expose its PCM stream.** `tts-engine`'s `TTSEngine.speak` currently swallows audio into a local `sounddevice` player. The clean adapter wants a **sink abstraction** on `tts-engine` (a hand-off to be raised against that repo); until it lands, the adapter can drive `tts-engine`'s module layer directly (`load_module` + `module.stream(..., callback)`), at the cost of coupling to internals. `tts-engine` emits **int16** natively, so this adapter does the trivial `int16 → float32` (`/32768.0`) to meet the `SpeechSynthesizer` contract — a conversion float-native synthesizers (most neural TTS) skip.

- **The robot sink.** `media.push_audio_sample` takes **float32 `[-1, 1]`** with no rate argument; the pipeline runs at a fixed 16 kHz. The robot side is read from the SDK getters rather than hardcoded, because the channel count is version-dependent: `get_output_audio_samplerate()` reports the rate (16 kHz) and `get_output_channels()` the channel count (2 — stereo — on the current backend). With the synthesizer emitting float32 mono, the sink **resamples `synth.sample_rate` → `get_output_audio_samplerate()`** and **fans mono out to `get_output_channels()`** (duplicating the channel when >1). The rate comes from the `SpeechSynthesizer` contract's required `synth.sample_rate` on the input side and the getters on the robot side — never guessed. Matching the speaker's rate and channel layout is a **robot detail the sink owns**, kept out of the synthesizer contract so a synthesizer stays robot-agnostic and reusable.

  Two decisions on this path:

  - **16 kHz-native is the default; the resample is skipped in the common case.** The resample is **skipped entirely when `synth.sample_rate == get_output_audio_samplerate()` (16 kHz)** — the default adapter is configured for this (`tts-engine`'s ElevenLabs module at `pcm_16000`), so a normal install resamples nothing and preferring a 16 kHz-native synthesizer config is the documented recommendation. (The cheap mono→stereo channel fan is a copy, not a resample, and still applies whenever `get_output_channels() > 1` — so `say` is only a *pure* passthrough when the speaker is also mono.)
  - **When a synthesizer emits another rate, resample with `samplerate` (streaming).** The conversion runs on a *live stream of chunks*, so it needs a **stateful** resampler that carries filter state across chunk boundaries. `samplerate` (the Python binding to libsamplerate / "Secret Rabbit Code") provides exactly that via `Resampler(converter, channels=1).process(chunk, ratio, end_of_input=...)` — high-quality sinc conversion with no boundary clicks. It was chosen on **license + capability + portability**:
    - **License fits a permissive core.** `samplerate` is **MIT** and libsamplerate is **BSD-2-Clause**. This is why it is preferred over `soxr`, which is **LGPL-2.1-or-later** (libsoxr, statically bundled) — usable as a pip dependency, but we avoid putting an LGPL package in every install.
    - **Streaming state.** `scipy.signal.resample_poly` is one-shot/stateless — resampling each chunk independently zero-pads at the edges and produces periodic clicks in continuous speech — so it is *not* used despite being available transitively via `reachy_mini`; `resampy` is permissive (ISC) but likewise one-shot and pulls in `numba`; a hand-vendored resampler is rejected.
    - **Portability.** `samplerate` ships prebuilt wheels for every target platform — manylinux **aarch64** (the robot) and x86_64, and macOS arm64/universal2 — so no on-device source build.

    Because it is permissive and small, `samplerate` is a **base runtime dependency** (not an optional extra) — so `say` works at any synthesizer rate with no extra install and no lazy-import/error machinery. The resample lives behind a small internal helper in this module (e.g. `resample_to_16k(chunks, src_rate)`) so the backing library stays swappable.

  On the `fake`/local-dev path the sink may instead play to a local device (no AEC — dev only).

### Mic in — a clean audio stream for the caller's own ASR

- **The bridge exposes the echo-cancelled mic as a single async-iterator stream; it does no ASR.** The one public primitive is `audio_input()` — an **async iterator** yielding `bytes` (**int16 LE mono**, i.e. linear16) — consumed the idiomatic Python way, with a `mic_sample_rate` property alongside:

  ```python
  async for chunk in api.audio_input():  # int16 LE mono PCM bytes
      feed_my_asr(chunk)
  ```

  Frames are sourced by looping `media.get_audio_sample()` inside the managed session, so `audio_input()` is a **tap** over the already-running capture: iterate to consume, stop iterating (`break`) to stop. This is the whole ASR story on the bridge side: clean, echo-cancelled audio anyone can consume. **Int16 LE mono is the "match the consumer" contract** — the consumer is an ASR engine, and streaming ASR (Deepgram, Google, Whisper-streaming, …) universally wants linear16 PCM. It is a *bridge-owned contract*, not the raw capture format (see next bullet).

- **The mic path converts, but never resamples.** Capture is **float32, interleaved, `get_input_channels()` channels (stereo on the current backend), at 16 kHz** (per the SDK — see [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)), so meeting the int16-LE-mono contract takes two cheap per-chunk conversions: **downmix to mono** (average the channels, or take one, when `get_input_channels() > 1`) and **float32 → int16** (`clip[-1,1] · 32767`). The rate needs no conversion: `get_input_audio_samplerate()` is **16 kHz**, already the streaming-ASR norm, so it passes through untouched. The bridge reads that value and exposes it as `mic_sample_rate` so the caller configures their ASR to it; any rare rate-matching then lands in the ASR backend. Resampling lives **only** on the TTS output path (synth rate → the speaker rate), and even there is skippable when the synthesizer already emits the speaker rate.

- **One understandable primitive — the async iterator — is the whole public surface.** "Easy to connect any ASR" means: a documented, stable format (rate/channels/encoding, endianness) and a **pull-based async iterator** a consumer drains at its own pace — the canonical Python streaming idiom (`async for`), readable without docs. The bridge ships **only** this; it deliberately does *not* expose a queue-shaped source object (`start() -> asyncio.Queue[bytes]`, `stop()`) or callback registration, because those are plumbing shapes trivially built *from* the iterator when a specific consumer wants one (a queue adapter is a few lines: iterate and `put_nowait`). **Multi-consumer fan-out is deferred** (YAGNI): the normal case is a single ASR consumer; fan-out adds per-consumer backpressure and lifecycle that most callers never need. The bridge depends on no ASR engine — adapting this iterator to a given engine's injection shape is the caller's few lines.

### Shared

- **XVF3800 config applied on session start.** The session applies a tuned audio profile via `media.audio.apply_audio_config(...)` after the pipeline is up (mirroring the upstream conversation app's startup config: NS floors, echo-tail suppression, AGC gain). The profile is a named default the bridge ships; overridable by callers.

- **Barge-in.** Interrupting playback mid-utterance flushes queued speaker audio via `media.audio.clear_player()` (falling back to the deprecated `clear_output_buffer()` on older SDKs). Exposed so [api.md](api.md) / [tools.md](tools.md) can offer a "stop talking" action.

- **Local-device audio is a dev-only fallback, not a co-equal mode.** Playing TTS to / reading a mic from local `sounddevice` devices is fine on a laptop / Lite dev box **without AEC**. On the robot it is wrong: it defeats echo cancellation. So `say` and the mic stream default to the robot media path when connected to a real/sim robot; the local path is an explicit opt-in for offline development against the `fake` backend.

- **Dependencies this concept introduces.** `tts-engine` enters as an **optional** dependency under the `tts` extra (first-party sibling repo — local path dependency now, pinned git URL later, per [project.md](project.md)) — not a base runtime dependency. **`asr-engine` is *not* a dependency of the bridge** at all. `numpy` is already a direct dependency (frame/sample handling). `samplerate` (MIT; wraps BSD-2-Clause libsamplerate) enters as a **base** runtime dependency — the streaming resampler for the TTS output path (see "The robot sink"); it is permissive and small enough to sit in the core, so no resampler extra and no lazy import.

- **`fake` backend support.** The `FakeReachyMini` ([client.md](client.md)) must satisfy whatever media members this session calls (`start_recording`, `get_audio_sample`, `get_input_audio_samplerate`, `start_playing`, `push_audio_sample`, `audio.apply_audio_config`, `audio.clear_player`), returning/recording synthetic audio so the say/mic stack is exercisable offline and in `tests/`. `tests/` also use a trivial fake `SpeechSynthesizer` (emits silence/a tone), needing no `tts-engine` import.

## Relationship to the other specs (changes required)

- **[client.md](client.md):** the `media` audio primitives listed under Background above are part of the consumed slice `FakeReachyMini` must implement (the `RobotClient` union type-checks the calls against it). (Noted in client.md.)
- **[api.md](api.md):** becomes async-native; `say` routes through this layer's sink over a pluggable `SpeechSynthesizer`; a microphone-access verb (`audio_input()` + `mic_sample_rate`) joins Perception. No ASR verbs — the caller runs ASR on the exposed stream. (Noted in api.md.)
- **[project.md](project.md):** `tts-engine` is an optional `tts` extra with the default synthesizer adapter, not a base dependency; `asr-engine` is not a dependency. (Noted in project.md.)
- **[tools.md](tools.md):** if a consuming runtime needs sync tool callables, the sync↔async bridging lives at the tools layer (a managed background loop), not by making the api sync.

## Open questions

1. **Robot I/O format — confirm on hardware against the pinned SDK.** The design now reads the SDK getters (`get_input_channels` / `get_output_channels` / `get_input_audio_samplerate` / `get_output_audio_samplerate`) rather than hardcoding, because the facts vary by SDK version: this repo's GStreamer backend reports **float32 stereo @ 16 kHz** both directions, while the upstream conversation app pushes **mono** to `push_audio_sample`. The conversions (capture → int16 mono; synth mono → speaker rate/channels) are settled; what needs confirming on real hardware with the pinned `reachy_mini` is the **actual channel counts and dtype the getters report**, so the fake and the conversion helpers match reality. This is the same fact that gates [client.md](client.md)'s media consumed-slice signatures.
2. **Audio-format residuals (bridge contracts).** The bridge's own contracts are decided (`SpeechSynthesizer` emits float32 mono `[-1, 1]` numpy; the mic stream yields int16 LE mono `bytes` — see the synthesizer and mic sections). Residuals: exact numpy chunk **shape** (`(n,)` vs `(n, 1)`); whether to accept int16 from a synthesizer as a convenience for authors who have it; and whether the mic contract should ever expose more than mono.
2. **Voice / synthesis options on the seam.** Whether `say`/`SpeechSynthesizer.stream` grow an options argument (voice, rate) or those stay entirely inside the concrete synthesizer's config is deferred; keep the seam minimal until a real need appears.
3. **XVF3800 default profile.** Whether the bridge ships the upstream conversation-app profile verbatim, tunes its own, or exposes named profiles (e.g. "conversation" vs. "far-field") is deferred; needs testing on hardware.
4. **Full-duplex vs. half-duplex default.** Whether the mic stream is gated (or auto-barge-in armed) while `say` is playing, given AEC quality in practice, is deferred until measured on real hardware.
5. **DoA / beamforming exposure.** How `get_DoA()` and any beam-steering surface as verbs (and whether they belong here or in [api.md](api.md) perception) is deferred alongside the perception return-shape question in [api.md](api.md).
