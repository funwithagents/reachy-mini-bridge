---
code:
  - src/reachy_mini_bridge/api.py
tests:
---

# Interaction API (`ReachyMiniApi`)

**Status:** Draft

## Purpose

`ReachyMiniApi` is the high-level, intention-level surface for driving the robot. Where the upstream SDK speaks in 4x4 pose matrices and radians, `ReachyMiniApi` speaks in **human intent and human units**: "look at this point", "nod", "play the happy emotion", "say this", in degrees / seconds / named emotions. It orchestrates the lower-level [client.md](client.md) primitives (composing `goto_target`/`set_target` calls, recorded moves, and media) into single semantic verbs.

It is the layer a human, service, or agent codes against directly, and the layer [tools.md](tools.md) wraps for LLM/agent use. It returns plain, JSON-friendly Python values so it composes cleanly into tools.

### Async, not sync

Earlier drafts made this layer **synchronous** to mirror the upstream SDK. That is reversed: `ReachyMiniApi` is **async-native**. The forcing reason is audio (see [audio.md](audio.md)): speech synthesis is `async`, and a **live microphone stream** must run concurrently with playback and with the robot moving — a per-call `asyncio.run()` cannot interleave a continuously-draining mic stream with other actions. Full-duplex interaction (a caller's ASR consuming the mic while `say` speaks, barge-in) needs both audio paths on one event loop.

- The upstream SDK's **blocking** calls (`goto_target`, `wake_up`, …) are wrapped in `asyncio.to_thread(...)` so a move never stalls the audio loops.
- The [client.md](client.md) seam stays **sync** (it mirrors the upstream SDK 1:1 — that's its isolation job); this layer is where async lives.
- If a consuming agent runtime needs **sync** tool callables, the sync↔async bridging is done at the [tools.md](tools.md) layer (a managed background loop), not by making this layer sync.

## Core concepts / Decided

- **Constructed over a client.** `ReachyMiniApi(client: RobotClient)` takes the [client.md](client.md) seam (`real`, `sim`, or `fake`), so the Api is fully unit-testable against the fake robot. A convenience constructor (e.g. `ReachyMiniApi.connect(...)`) may build the client for callers who don't need to.
- **Raw access preserved.** The underlying client / raw upstream instance stays reachable through the Api (e.g. `api.client` / `api.raw`) so nothing is a dead end.
- **Human units.** Angles in **degrees**, durations in **seconds**, positions in a documented frame. The Api converts to the upstream's matrices/radians internally (via `reachy_mini.utils.create_head_pose` and friends).
- **Idempotent, forgiving verbs.** Methods validate/clamp to safe ranges and fail with clear `ValueError`s rather than sending bad poses downstream.

### v1 capability groups

All four groups are in scope for the first version (final method names/signatures settle before this spec goes `Stable`):

- **Movement & gaze**
  - `look_at(...)` — gaze at a target; dispatches to the upstream image-pixel (`look_at_image`) or 3D-world (`look_at_world`) helper depending on the argument form.
  - `set_head_pose(roll, pitch, yaw, duration, ...)` — absolute head orientation in degrees.
  - `nod(...)`, `shake_head(...)`, `turn(yaw, ...)` — composed gestures built from `goto_target` sequences.
  - `wake()` / `sleep()` — wrap `wake_up()` / `goto_sleep()`.
- **Expression**
  - `play_emotion(name, ...)` — play a named recorded move from the emotions library (`RecordedMoves(...).get(name)` + play), with `list_emotions()` to enumerate.
  - `set_antennas(left, right, ...)` — antenna angles in degrees.
- **Perception**
  - `get_view(...)` — a camera frame as a numpy array and/or JPEG bytes.
  - `get_head_pose()` — current head pose, reported in human units.
  - `get_imu()` — IMU reading.
  - Face tracking on/off and `get_tracked_face()`; sound direction of arrival (`get_sound_direction()`), where the hardware/backend supports it.
- **Audio in (microphone)** — see [audio.md](audio.md)
  - `audio_input()` / `mic_sample_rate` — an async stream of the robot's **echo-cancelled** microphone PCM, for the caller to feed their **own** ASR. The bridge embeds no ASR engine and exposes no recognizer; it provides clean mic audio and manages the media session so echo cancellation applies. The stream is shaped to drop easily into common ASR engines (see [audio.md](audio.md)).
- **Audio out**
  - `say(text, ...)` — synthesize speech via a **pluggable** `SpeechSynthesizer` ([audio.md](audio.md)); the default adapter wraps our first-party [`tts-engine`](../../tts-engine) (optional `tts` extra), but callers can supply any synthesizer. On a real/sim robot the audio is routed through the robot speaker (required for echo cancellation), not the local device.
  - `play_sound(...)` — play a sound file / built-in sound (upstream `media.play_sound`).
  - `stop_talking()` — barge-in: flush queued speaker audio (see [audio.md](audio.md)).

### Orchestration is the point

Verbs that don't map 1:1 to an upstream call are where this layer earns its keep — `look_at` choosing the right upstream helper, `nod`/`shake_head` sequencing poses, `play_emotion` resolving a name to a recorded move. Pure pass-throughs (e.g. `get_imu`) are thin by design.

## Open questions

1. **`say` / microphone audio wiring — moved to [audio.md](audio.md).** The shape is decided there: `say` uses a bridge-owned **pluggable** `SpeechSynthesizer` (default adapter over first-party [`tts-engine`](../../tts-engine), optional `tts` extra); the microphone is **exposed as a stream** for the caller's own ASR (the bridge does not embed ASR). The two integration details that used to sit here are resolved:
   - **Async → sync** is resolved by this layer being **async-native** (see "Async, not sync" above) — no per-call `asyncio.run()`.
   - **Audio routing** (local device vs. robot speaker/mic through the daemon media pipeline, and why echo cancellation requires the robot path) is specified in [audio.md](audio.md). This layer's `say` / microphone verbs are thin wrappers over that.
2. **Frame/units conventions to document.** Exact axes, origin, and angle conventions for `look_at`/`set_head_pose` need to be stated so tools and callers agree (upstream world frame is x forward, y left, z up).
3. **Emotions library source.** Which HF dataset(s) back `play_emotion` by default, and how loading/caching is handled (it is network + HF-hub at call time), is deferred to the implementation plan.
4. **Return shapes for perception.** Exact JSON-friendly return types for `get_view` / `get_imu` / `get_tracked_face` settle alongside [tools.md](tools.md), since tools need them serializable.
