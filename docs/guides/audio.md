# Audio — a voice, speech, sound files, the microphone

How to make the robot talk and listen through the bridge: setting up the default voice, bringing your own synthesizer, what `say` and `play_sound` guarantee, head wobbling, and feeding the robot's echo-cancelled microphone to your own speech recognizer. Contracts: [../reference/api.md](../reference/api.md); design: [specs/audio/audio.md](../../specs/audio/audio.md).

## The one media session

The bridge opens the robot's audio once per session, for both directions: speech out through the speaker and the microphone in. Owning both is what keeps the robot's echo cancellation working while it talks and listens at once — the audio it plays is the reference the mic stream is cancelled against (in hardware by the robot's XVF3800 array; in software on a sim, where the host's GStreamer has the `webrtcdsp` element — macOS has it, Ubuntu 24.04's packages do not, [linux.md](linux.md)). Route both directions through the bridge; restarting the pipeline yourself through `bridge.robot.media` mid-session breaks the device binding on macOS ([../reference/api.md](../reference/api.md) "The escape hatch").

## A voice for `say`

`say(text)` streams text-to-speech to the speaker through a `SpeechSynthesizer`. Two ways to have one:

**The config's `tts` block** builds the default voice from the first-party [tts-engine](https://github.com/funwithagents/tts-engine): `module.type` picks the provider, the matching extra installs it, the remaining keys are the provider's own ([../reference/configuration.md](../reference/configuration.md) "`tts`").

| Provider | Extra | Needs | Emits |
|---|---|---|---|
| `pocket` | `reachy-mini-bridge[tts-pocket]` | no key, no network once the weights are cached; torch (hundreds of MB; on Linux a dev checkout takes the CPU build) | 24 kHz |
| `elevenlabs` | `reachy-mini-bridge[tts-elevenlabs]` | `ELEVENLABS_API_KEY` (or the variable `api_key_env` names) | 44.1 kHz |
| `gradium` | `reachy-mini-bridge[tts-gradium]` | `GRADIUM_API_KEY` | 48 kHz by default |

The bridge resamples whatever rate arrives to the speaker's 16 kHz and fans mono out to its channels, so the provider's rate is its own business. A block that fails to build — extra not installed, key unset — leaves the robot fully usable without a voice: the session starts, `say` raises `BridgeError` chained to the cause, and the cause sits on `bridge.synthesizer_error` right after construction for a host that wants to fail hard.

**Your own synthesizer** is any object with a fixed `sample_rate` (Hz) and a `stream(text)` returning an async iterator of float32 mono chunks in [-1, 1], shape `(n,)`. Pass it to `ReachyMiniBridge(config, synthesizer=...)` as the default voice (it wins over the `tts` block), or per call: `say(text, synth)`. The smallest complete one — a beep — is in [../getting-started.md](../getting-started.md); an engine that emits int16 converts each chunk with `reachy_mini_bridge.audio.int16_to_float32`. Keep the protocol minimal: voices, speed and SSML belong to your synthesizer's own config.

## What `say` guarantees

- **It returns when the robot has finished speaking** — not when the audio was queued. Upstream's speaker queues without pacing and synthesis runs faster than real time, so the bridge estimates the queued audio's end on the wall clock and waits for it plus a 100 ms margin. It is an estimate, identical on every backend, not a measurement at the speaker.
- **Cancelling it silences the speaker at once.** Cancel the task awaiting `say` and the queued audio is flushed, the head wobbler reset; `say` either plays the whole utterance or stops the speaker before handing control back. This is barge-in: your ASR hears the person, you cancel `say`.
- **The newest `say` wins.** A `say` called while another is in flight interrupts it: the speaker is flushed and the interrupted call raises `SpeechInterruptedError` in its own task; the new utterance plays from silence. Serialise your own `say` calls if you want them queued.
- **It needs a running session** and a synthesizer, and raises `BridgeError` otherwise. It needs no motors.
- **The fake keeps the timing**: `say` on `"fake"` takes the utterance's duration, so turn-taking logic tested against the fake behaves as on a robot.

## Sound files

`play_sound(file)` plays a sound file on the robot's own file player and returns when it has been heard (the file's duration plus the same margin). The argument is **a path on this machine**, or else **the name of one of the SDK's built-in sounds** (`"wake_up.wav"`, looked up in `reachy_mini`'s assets directory) — in that order; a name that is neither raises `FileNotFoundError`, a file whose duration cannot be read `ValueError`, both before anything plays. The player holds **one file at a time, the newest wins**, and the library's recorded emotions carry a sound each: a `play_sound` during an emotion silences the emotion's sound (the move plays on), and an emotion starting during your `play_sound` replaces it — your call raises `SoundInterruptedError`. Speech and a sound file coexist, mixed at the speaker. Cancelling `play_sound` stops the file.

## Head wobbling

Upstream's audio-reactive sway: while it is on, every sound the robot plays — speech, a file, an emotion's sound — drives a subtle motion of the head, which eases back to neutral when the audio ends. It is on by default (`motion.wobbling`), toggled at run time with `set_wobbling(enabled)`, and **paused by the bridge for the duration of every emotion**, since a recorded move already choreographs the head. The setting is daemon-wide, shared by every app on the daemon; the bridge turns it back off when the session ends.

## The microphone — your own ASR

The bridge does no speech recognition. It exposes the robot's echo-cancelled microphone as a stream and you feed it to the recognizer of your choice:

```python
async with ReachyMiniBridge("fake") as bridge:
    print(bridge.mic_sample_rate, bridge.mic_channels)  # 16000, 2 on the robot and the sim
    async for chunk in bridge.audio_input():  # int16 LE mono PCM bytes
        feed_my_asr(chunk)  # schematic: your recognizer's push call
        break  # stop iterating to stop the stream
```

- `audio_input(mono=True)` yields **int16 little-endian PCM `bytes`**, mono by default (`mono=False` keeps the channels interleaved), at `mic_sample_rate` — 16 kHz, the format streaming recognizers take as linear16. The bridge converts the capture's float32 stereo; it never resamples.
- Each call is **a subscriber of its own** over the running capture: iterate to consume, `break` to stop, and it ends on its own when the session closes, so a consumer task you await at shutdown finishes cleanly. Call it while the session runs; the check runs at the call, not at the first `async for`.
- **Several consumers at once** each call `audio_input()` and each get every chunk, in order, at their own pace — a recognizer and a wake-word detector on mono, a direction estimator on `mono=False`. A consumer that stalls for more than the 2 s the bridge buffers loses the chunks it missed (a warning is logged) and slows no one else.
- **Pre-roll for a wake word**: `audio_input(preroll_s=0.5)` starts the stream half a second before the call, from the same buffer, so a recognizer you start when the wake word fires hears the words that woke it:

  ```python
  async def on_wake_word() -> None:
      async for chunk in bridge.audio_input(preroll_s=0.5):
          feed_my_asr(chunk)
  ```
- Run the consumer as **a task of your own**, concurrently with `say`: the robot listens while it talks, the echo cancellation removing its own voice from what you hear. You own that task — cancel and await it before the session ends.

The first-party [asr-engine](https://github.com/funwithagents/asr-engine) is one natural recognizer to attach; it is not a dependency of the bridge.

## The microphone array's profile

`audio.xvf3800` in the config is a list of `[name, [values…]]` pairs the bridge writes to the robot's XVF3800 audio processor when the session starts; `null` keeps the firmware defaults, which is the tested baseline. On a USB Lite the bridge writes the profile from your machine and reads it back; on a wireless robot, whose array is out of your machine's reach, it hands the profile to the robot's daemon, which writes it. A profile that does not apply — on a sim, which has no array, it never does — logs a warning (`audio.xvf3800 profile not applied: …`) and the session opens with what the chip holds; the daemon failing the write, or being unreachable, fails the start with `BridgeError`.

## Sound on a host without a sound card

A Linux box with no audio device — a server, a CI runner — brings the daemon's audio up unavailable and `say` plays for nobody; a PulseAudio null sink gives it a device whose monitor loops playback back to capture, so the pipeline runs end to end ([testing.md](testing.md) "A Linux box without a sound card").
