---
code:
  - src/reachy_mini_bridge/api.py
tests:
---

# Interaction API (`ReachyMiniApi`)

**Status:** Draft

## Purpose

`ReachyMiniApi` is the high-level, intention-level surface for driving the robot. Where the upstream SDK speaks in 4x4 pose matrices and radians, `ReachyMiniApi` speaks in **human intent and human units**: "look at this point", "nod", "play the happy emotion", "say this", in degrees / seconds / named emotions. It orchestrates the lower-level [client.md](client.md) primitives (composing `goto_target`/`set_target` calls, recorded moves, and media) into single semantic verbs.

It is the layer a human, service, or agent codes against directly, and the layer [tools.md](tools.md) wraps for LLM/agent use. It is **synchronous** (matching the upstream SDK) and returns plain, JSON-friendly Python values so it composes cleanly into tools.

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
- **Audio out**
  - `say(text, ...)` — speak text via our first-party [`tts-engine`](../../tts-engine) (`TTSEngine.speak`). Referenced as a local path dependency now, a git URL later (see [project.md](project.md), "Runtime-dependency policy").
  - `play_sound(...)` — play a sound file / built-in sound (upstream `media.play_sound`).

### Orchestration is the point

Verbs that don't map 1:1 to an upstream call are where this layer earns its keep — `look_at` choosing the right upstream helper, `nod`/`shake_head` sequencing poses, `play_emotion` resolving a name to a recorded move. Pure pass-throughs (e.g. `get_imu`) are thin by design.

## Open questions

1. **`say` / TTS integration shape (resolved: backend, open: wiring).** The TTS backend is decided — our first-party [`tts-engine`](../../tts-engine) (`TTSEngine.speak`), sourced per [project.md](project.md). Two integration details remain for the implementation plan:
   - **Async → sync.** `TTSEngine.speak` is `async`; `ReachyMiniApi` is sync, so `say` must drive the coroutine (e.g. `asyncio.run` / a managed loop) behind a sync signature.
   - **Audio routing.** `tts-engine` streams to a local output device (sounddevice); the robot's speaker lives behind the daemon's media pipeline. Decide whether `say` plays to the local device (fine for a laptop/Lite setup) or feeds samples to the robot via `media.push_audio_sample` — likely a configurable output target.
2. **Frame/units conventions to document.** Exact axes, origin, and angle conventions for `look_at`/`set_head_pose` need to be stated so tools and callers agree (upstream world frame is x forward, y left, z up).
3. **Emotions library source.** Which HF dataset(s) back `play_emotion` by default, and how loading/caching is handled (it is network + HF-hub at call time), is deferred to the implementation plan.
4. **Return shapes for perception.** Exact JSON-friendly return types for `get_view` / `get_imu` / `get_tracked_face` settle alongside [tools.md](tools.md), since tools need them serializable.
