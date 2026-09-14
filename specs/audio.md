---
code:
  - src/reachy_mini_bridge/audio.py
tests:
  - tests/test_audio.py
  - tests-e2e/test_api.py
---

# Audio & media session (`audio.py`)

**Status:** Implemented

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
`media.start_recording()` / `stop_recording()`, `media.get_audio_sample()`, `media.get_input_audio_samplerate()`, `media.start_playing()` / `stop_playing()`, `media.push_audio_sample(np.float32)`, `media.audio.apply_audio_config(...)`, `media.audio.clear_player()` (barge-in flush), plus the format getters `media.get_input_channels()` / `get_output_audio_samplerate()` / `get_output_channels()`. (`media.get_DoA()` is not consumed yet — see open question 5.)

## Core concepts / Decided

- **One media session, owned here.** A single object (working name `MediaSession`) opens `start_recording()` + `start_playing()`, applies the XVF3800 config, and owns teardown (`stop_recording()` / `stop_playing()`). It is opened once per connection and shared by both output (`say`) and input (mic stream) — never opened/closed per utterance. Lifecycle is tied to the [robot.md](robot.md) connection (context manager), not to individual verbs. Owning **both** directions is what makes AEC work: TTS out through the pipeline is the reference the mic-in stream is cancelled against.

- **Async is mandatory here, which forces [api.md](api.md) async.** Playback and a live mic stream must run concurrently on one event loop, and `tts-engine`'s synthesis is `async`. Streaming the mic while the robot is also moving / speaking needs true concurrency, not a per-call `asyncio.run()`. This concept therefore *requires* [api.md](api.md) to be **async-native** (see [api.md](api.md) "Async-native"). The upstream SDK's blocking motion calls are wrapped in `asyncio.to_thread(...)` at the api layer so a move never stalls the audio loops.

### TTS out — a bridge-owned `SpeechSynthesizer` interface

- **The bridge defines the interface; the implementation is pluggable.** The bridge's real need from "TTS" is small: turn text into a stream of PCM chunks at a known rate, which this layer resamples and pushes to the robot speaker. That is a narrow Protocol the bridge owns:

  ```python
  class SpeechSynthesizer(Protocol):
      """Turns text into a stream of PCM audio the media layer can play."""

      @property
      def sample_rate(self) -> int: ...  # Hz of the chunks below
      def stream(
          self, text: str
      ) -> AsyncIterator[np.ndarray]:  # float32 mono [-1, 1], shape (n,)
          ...
  ```

  `say` depends only on this. Keep it **deliberately minimal** — text in, PCM out. Rich features (voices, SSML, rate) stay inside the concrete synthesizer and its own config; the seam carries only what the robot audio loop needs. (Voice/options selection is an open question below.)

- **Format: match the consumer.** The synthesizer emits **float32 mono numpy in `[-1, 1]`** because *its* consumer is the robot speaker — `media.push_audio_sample` takes float32, and the rate conversion we must do anyway (synth rate → robot rate) is a float operation. So the output side is float32 end-to-end with no dtype juggling in the hot path, and numpy is self-describing (dtype fixed, shape carries channels) so no out-of-band format descriptor is needed. This is the same "match the consumer" rule the mic stream follows in the other direction (where the consumer is an ASR engine, so the format is int16 — see below).

  **Chunk shape is 1-D `(n,)`.** A mono chunk has no channel axis to carry, so it is a flat vector of samples — the shape TTS engines emit natively, and the shape the streaming resampler (`Resampler(channels=1)`) and the `int16→float32` divide operate on directly, so a synthesizer author writes no reshaping. The channel axis appears only in the sink's mono→speaker fan-out (a robot detail the sink owns — see "The robot sink"), never in the synthesizer contract.

  **The contract is float32-only; int16 is converted by a public helper, never accepted implicitly.** `say`/the sink never dtype-sniff a chunk — a synthesizer *must* yield float32, keeping the hot path single-dtype and the Protocol's promise crisp. Authors whose engine emits int16 (most non-neural TTS, including `tts-engine`) call the bridge's public `int16_to_float32(...)` (see "Conversion helpers" below) — one blessed, tested line, rather than each author re-deriving the `/32768.0` divisor.

- **`tts-engine` is the default adapter, shipped under an optional extra.** A `TTSEngineSynthesizer` adapts our first-party [`tts-engine`](../../tts-engine) to `SpeechSynthesizer`. It lives behind a `tts` extra (`reachy-mini-bridge[tts]`, see [project.md](project.md)) so the bridge core does **not** hard-depend on `tts-engine` / `sounddevice` / a cloud TTS SDK — a caller supplying their own synthesizer pulls none of it. `TTSEngineSynthesizer` is the *shipped* adapter, not an implicit default instance: it needs a tts-engine config (module, voice). A caller either constructs it (from a `TTSEngineConfig` or the equivalent raw `engine`-block dict) and passes it to `ReachyMiniApi(config, synthesizer=...)` or per call to `say(text, synth)`, or puts that same tts-engine `engine` block under the `tts` key of the bridge's `ReachyMiniConfig` ([config.md](config.md)), from which `ReachyMiniApi` builds the adapter itself as the default synthesizer — the one-config path to a talking robot. An explicit `synthesizer=` wins over the `tts` block. `say` raises a clear `BridgeError` whenever no synthesizer is configured or passed, whether or not the extra is installed.

- **The adapter plugs into `tts-engine`'s sink abstraction.** `TTSEngine(config, sink=...)` accepts an `AudioSink` (`feed(chunk: bytes)` / `drain()`) in place of its default local `sounddevice` player, and `TTSEngine.say(text)` pushes int16 chunks into it. `TTSEngineSynthesizer` supplies a queue-backed sink that marshals those pushes onto the event loop, bridging tts-engine's push model to the pull-based `stream()` iterator the contract requires. `tts-engine` emits **int16** natively, so the adapter meets the `SpeechSynthesizer` contract via the bridge's public `int16_to_float32(...)` helper (see "Conversion helpers") — a conversion float-native synthesizers (most neural TTS) skip.

- **The robot sink.** `media.push_audio_sample` takes **float32 `[-1, 1]`** with no rate argument; the pipeline runs at a fixed 16 kHz. The robot side is read from the SDK getters rather than hardcoded, because the channel count is version-dependent: `get_output_audio_samplerate()` reports the rate (16 kHz) and `get_output_channels()` the channel count (2 — stereo — on the current backend). With the synthesizer emitting float32 mono, the sink **resamples `synth.sample_rate` → `get_output_audio_samplerate()`** and **fans mono out to `get_output_channels()`** (duplicating the channel when >1). The rate comes from the `SpeechSynthesizer` contract's required `synth.sample_rate` on the input side and the getters on the robot side — never guessed. Matching the speaker's rate and channel layout is a **robot detail the sink owns**, kept out of the synthesizer contract so a synthesizer stays robot-agnostic and reusable.

  Two decisions on this path:

  - **The resample is skipped whenever the synth already emits the speaker rate.** It is **skipped entirely when `synth.sample_rate == get_output_audio_samplerate()` (16 kHz)** — so a 16 kHz-native synthesizer config resamples nothing, and preferring one is the documented recommendation. Note that the current `tts-engine` default (`TTSEngineSynthesizer`) is **not** 16 kHz-native: its ElevenLabs module emits **44.1 kHz** (mp3_44100_128 decoded to 44100), so the sink resamples 44100 → 16000 on that path — which is exactly why `samplerate` is a base dependency, not why it can be avoided. The 16 kHz-native skip becomes the common case only once `tts-engine` gains a 16 kHz output config. (The cheap mono→stereo channel fan is a copy, not a resample, and still applies whenever `get_output_channels() > 1` — so `say` is only a *pure* passthrough when the synth is 16 kHz **and** the speaker is mono.)
  - **When a synthesizer emits another rate, resample with `samplerate` (streaming).** The conversion runs on a *live stream of chunks*, so it needs a **stateful** resampler that carries filter state across chunk boundaries. `samplerate` (the Python binding to libsamplerate / "Secret Rabbit Code") provides exactly that via `Resampler(converter, channels=1).process(chunk, ratio, end_of_input=...)` — high-quality sinc conversion with no boundary clicks. It was chosen on **license + capability + portability**:
    - **License fits a permissive core.** `samplerate` is **MIT** and libsamplerate is **BSD-2-Clause**. This is why it is preferred over `soxr`, which is **LGPL-2.1-or-later** (libsoxr, statically bundled) — usable as a pip dependency, but we avoid putting an LGPL package in every install.
    - **Streaming state.** `scipy.signal.resample_poly` is one-shot/stateless — resampling each chunk independently zero-pads at the edges and produces periodic clicks in continuous speech — so it is *not* used despite being available transitively via `reachy_mini`; `resampy` is permissive (ISC) but likewise one-shot and pulls in `numba`; a hand-vendored resampler is rejected.
    - **Portability.** `samplerate` ships prebuilt wheels for every target platform — manylinux **aarch64** (the robot) and x86_64, and macOS arm64/universal2 — so no on-device source build.

    Because it is permissive and small, `samplerate` is a **base runtime dependency** (not an optional extra) — so `say` works at any synthesizer rate with no extra install and no lazy-import/error machinery. The resample lives behind a small internal stateful helper in this module (`_StreamResampler`, with a passthrough fast path when the rates match) so the backing library stays swappable.


### Mic in — a clean audio stream for the caller's own ASR

- **The bridge exposes the echo-cancelled mic as a single async-iterator stream; it does no ASR.** The one public primitive is `audio_input(mono=True)` — an **async iterator** yielding `bytes` (**int16 LE**, i.e. linear16) — consumed the idiomatic Python way, with `mic_sample_rate` and `mic_channels` properties alongside:

  ```python
  async for chunk in api.audio_input():  # int16 LE mono PCM bytes
      feed_my_asr(chunk)
  ```

  Frames are sourced by looping `media.get_audio_sample()` inside the managed session, so `audio_input()` is a **tap** over the already-running capture: iterate to consume, stop iterating (`break`) to stop. This is the whole ASR story on the bridge side: clean, echo-cancelled audio anyone can consume. **Int16 LE mono is the "match the consumer" contract** — the consumer is an ASR engine, and streaming ASR (Deepgram, Google, Whisper-streaming, …) universally wants linear16 PCM. It is a *bridge-owned contract*, not the raw capture format (see next bullet).

- **Mono is the default; raw multichannel is an explicit opt-in — and the channel count is read, never hardcoded.** `audio_input(mono=True)` (the default) downmixes to a single channel — the ASR drop-in. `audio_input(mono=False)` yields the **raw interleaved** capture at `mic_channels` channels, for advanced callers (beamforming / direction-of-arrival, see the DoA question). Either way the channel count is whatever `media.get_input_channels()` reports, surfaced as the **`mic_channels`** property — never assumed. This is what makes the stream **backend-robust**: real hardware, the MuJoCo sim, and the `fake` may each report a different channel count, but the mono default normalizes them all to the identical 1-channel contract, so a caller's ASR code is the same everywhere. A caller who took the raw stream and later wants mono uses the public `downmix_to_mono(...)` helper (see "Conversion helpers"); `mono=True` is exactly that helper applied in the tap.

- **The mic path converts, but never resamples.** Capture is **float32, interleaved, `get_input_channels()` channels (stereo on the current backend), at 16 kHz** (per the SDK — see [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)), so meeting the int16-LE-mono contract takes two cheap per-chunk conversions: **downmix to mono** (`downmix_to_mono(...)`, applied only when `mono=True` and `get_input_channels() > 1`) and **float32 → int16** (`float32_to_int16(...)`, `clip[-1,1] · 32767`, always). With `mono=False` the downmix is skipped and the interleaved channels pass through. The rate needs no conversion: `get_input_audio_samplerate()` is **16 kHz**, already the streaming-ASR norm, so it passes through untouched. The bridge reads that value and exposes it as `mic_sample_rate` so the caller configures their ASR to it; any rare rate-matching then lands in the ASR backend. Resampling lives **only** on the TTS output path (synth rate → the speaker rate), and even there is skippable when the synthesizer already emits the speaker rate.

- **One understandable primitive — the async iterator — is the whole public surface.** "Easy to connect any ASR" means: a documented, stable format (rate/channels/encoding, endianness) and a **pull-based async iterator** a consumer drains at its own pace — the canonical Python streaming idiom (`async for`), readable without docs. The bridge ships **only** this; it deliberately does *not* expose a queue-shaped source object (`start() -> asyncio.Queue[bytes]`, `stop()`) or callback registration, because those are plumbing shapes trivially built *from* the iterator when a specific consumer wants one (a queue adapter is a few lines: iterate and `put_nowait`). **Multi-consumer fan-out is deferred** (YAGNI): the normal case is a single ASR consumer; fan-out adds per-consumer backpressure and lifecycle that most callers never need. The bridge depends on no ASR engine — adapting this iterator to a given engine's injection shape is the caller's few lines.

### Shared

- **Conversion helpers are public, tested, and shared by both paths.** The three per-chunk conversions the audio paths need are exposed as plain module-level functions so callers reuse them instead of re-deriving the constants (getting the `32768` vs `32767` asymmetry wrong is the classic bug):
  - `int16_to_float32(pcm)` → float32 `[-1, 1]` (`/ 32768.0`) — for synthesizer authors meeting the float32-only `SpeechSynthesizer` contract; the default `tts-engine` adapter uses it.
  - `float32_to_int16(pcm)` → int16 (`clip[-1, 1] · 32767`) — used on the mic path to meet the linear16 contract.
  - `downmix_to_mono(pcm, channels)` → single channel (average the channels) — used by `audio_input(mono=True)`, and available to a caller who took the raw `mono=False` stream.

  These are the bridge's own code (the say sink and the mic tap call them), so they are exercised by `tests/`, not courtesy exports.

- **XVF3800 config applied on session start — when a caller supplies one.** `MediaSession(robot, audio_config=...)` (surfaced as the `audio.xvf3800` block of `ReachyMiniConfig`, see [config.md](config.md)) applies the given profile via `media.audio.apply_audio_config(...)` after the pipeline is up, the way the upstream conversation app applies its startup config (NS floors, echo-tail suppression, AGC gain). With no profile given the firmware defaults stand — the bridge ships no default profile yet (open question 3).

- **Barge-in.** Interrupting playback mid-utterance flushes queued speaker audio via `media.audio.clear_player()` (`MediaSession.clear_player()`; the pinned `reachy-mini>=1.10` has it, so no legacy fallback). Exposed so [api.md](api.md) / [tools.md](tools.md) can offer a "stop talking" action.

- **No local-device audio path.** Playing TTS to / reading a mic from local `sounddevice` devices would work on a laptop / Lite dev box but **without AEC**, and on the robot it is wrong: it defeats echo cancellation. So `say` and the mic stream always go through the robot media path; on the `fake` backend that path is the fake's recorded pushes and synthetic capture (see "`fake` backend support"). An audible local-device fallback for offline development is not implemented — see open question 6.

- **Dependencies this concept introduces.** `tts-engine` enters as an **optional** dependency under the `tts` extra (first-party sibling repo — local path dependency now, pinned git URL later, per [project.md](project.md)) — not a base runtime dependency. **`asr-engine` is *not* a dependency of the bridge** at all. `numpy` is already a direct dependency (frame/sample handling). `samplerate` (MIT; wraps BSD-2-Clause libsamplerate) enters as a **base** runtime dependency — the streaming resampler for the TTS output path (see "The robot sink"); it is permissive and small enough to sit in the core, so no resampler extra and no lazy import.

- **`fake` backend support.** The `FakeReachyMini` ([robot.md](robot.md)) must satisfy whatever media members this session calls (`start_recording` / `stop_recording`, `get_audio_sample`, `get_input_channels`, `get_input_audio_samplerate`, `start_playing` / `stop_playing`, `push_audio_sample`, `get_output_channels`, `get_output_audio_samplerate`, `audio.apply_audio_config`, `audio.clear_player`), returning/recording synthetic audio so the say/mic stack is exercisable offline and in `tests/`. The fake reports **2 channels** (stereo), matching the documented real GStreamer backend, and yields matching-shape synthetic capture — so `tests/` deterministically exercise **both** the `mono=True` downmix path and the `mono=False` raw passthrough (were the fake mono, the downmix would never run in tests). `tests/` also use a trivial fake `SpeechSynthesizer` (emits silence/a tone), needing no `tts-engine` import.

## Relationship to the other specs

- **[robot.md](robot.md):** the `media` members listed under Background above are part of the consumed slice `FakeReachyMini` implements (the `AnyReachyMini` union type-checks the calls against it).
- **[api.md](api.md):** is async-native because of this layer; `say` routes through this layer's sink over a pluggable `SpeechSynthesizer`; the microphone verbs (`audio_input()` + `mic_sample_rate` / `mic_channels`) sit alongside perception. No ASR verbs — the caller runs ASR on the exposed stream.
- **[project.md](project.md):** `tts-engine` is an optional `tts` extra with the default synthesizer adapter, not a base dependency; `asr-engine` is not a dependency.
- **[tools.md](tools.md):** if a consuming runtime needs sync tool callables, the sync↔async bridging lives at the tools layer (a managed background loop), not by making the api sync.

## Open questions

1. **Robot I/O format — rates + dtype confirmed on sim; exact channel count still pending real hardware.** The design reads the SDK getters (`get_input_channels` / `get_output_channels` / `get_input_audio_samplerate` / `get_output_audio_samplerate`) rather than hardcoding, because the facts vary by SDK version. `tests-e2e/test_api.py::test_real_audio_format_matches_the_fake_assumptions` now confirms **against the live sim daemon** that input and output sample rates are **16 kHz** and capture dtype is **float32**, with the capture's channel layout self-consistent with `get_input_channels()`. What remains is confirming the **exact channel count (the fake assumes 2 / stereo) on real hardware with the XVF3800**, since the sim's audio path differs from the physical mic array; the conversions (capture → int16 mono; synth mono → speaker rate/channels) are settled and backend-robust regardless. This is the same fact that gates [robot.md](robot.md)'s media consumed-slice signatures.
2. **Voice / synthesis options on the seam.** Whether `say`/`SpeechSynthesizer.stream` grow an options argument (voice, rate) or those stay entirely inside the concrete synthesizer's config is deferred; keep the seam minimal until a real need appears.
3. **XVF3800 default profile.** Whether the bridge ships the upstream conversation-app profile verbatim, tunes its own, or exposes named profiles (e.g. "conversation" vs. "far-field") is deferred; needs testing on hardware.
4. **Full-duplex vs. half-duplex default.** Whether the mic stream is gated (or auto-barge-in armed) while `say` is playing, given AEC quality in practice, is deferred until measured on real hardware.
5. **DoA / beamforming exposure.** How `get_DoA()` and any beam-steering surface as verbs (and whether they belong here or in [api.md](api.md) perception) is deferred alongside the perception return-shape question in [api.md](api.md).
6. **Local-device dev fallback.** Whether to add an explicit opt-in that plays `say` audio to / reads a mic from local `sounddevice` devices when running against the `fake` backend (audible offline development, no AEC) is deferred until a real need appears; today the fake records pushes and yields synthetic capture.
