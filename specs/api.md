---
code:
  - src/reachy_mini_bridge/api.py
tests:
---

# Interaction API (`ReachyMiniApi`)

**Status:** Stable

## Purpose

`ReachyMiniApi` is the high-level, intention-level surface for driving the robot. Where the upstream SDK speaks in 4x4 pose matrices and radians, `ReachyMiniApi` speaks in **human intent and human units**: "look at this point", "nod", "play the happy emotion", "say this", in degrees / seconds / named emotions. It orchestrates the lower-level [client.md](client.md) primitives (composing `goto_target`/`set_target` calls, recorded moves, and media) into single semantic verbs.

It is the layer a human, service, or agent codes against directly, and the layer [tools.md](tools.md) wraps for LLM/agent use. It returns plain, JSON-friendly Python values so it composes cleanly into tools.

### Async-native

`ReachyMiniApi` is **async-native**. Audio forces it (see [audio.md](audio.md)): speech synthesis is `async`, and a **live microphone stream** runs concurrently with playback and with the robot moving — full-duplex interaction (a caller's ASR consuming the mic while `say` speaks, plus barge-in) keeps both audio paths on one event loop.

- The upstream SDK's **blocking** calls (`goto_target`, …) run under `asyncio.to_thread(...)` so a move never stalls the audio loops.
- The upstream `ReachyMini` is **synchronous** and mirrors the SDK 1:1; this layer is where async lives.
- A consuming agent runtime that needs **sync** tool callables gets the sync↔async bridging at the [tools.md](tools.md) layer (a managed background loop).

## Core concepts / Decided

- **Constructed from a backend string.** `ReachyMiniApi(backend="real"|"sim"|"fake", **opts)` builds the robot internally (see [client.md](client.md)) — the real `reachy_mini.ReachyMini` for `real`/`sim`, our `FakeReachyMini` for `fake` — so the Api is fully unit-testable on the `fake` backend. A `connect(...)` convenience mirrors it, and an optional `robot=` override lets a test inject a pre-seeded fake.
- **Raw access preserved.** The underlying robot object stays reachable through the Api as `api.robot` (a.k.a. `api.raw`) so nothing is a dead end — for `real`/`sim` it *is* the full native `ReachyMini`.
- **Human units.** Angles in **degrees**, durations in **seconds**, positions in a documented frame. The Api converts to the upstream's matrices/radians internally (via `reachy_mini.utils.create_head_pose` and friends).
- **Idempotent, forgiving verbs.** Methods validate/clamp to safe ranges and fail with clear `ValueError`s rather than sending bad poses downstream.

### v1 scope — the minimum for interaction

v1 is deliberately narrow: the smallest verb set that makes the robot a **conversational, face-following** presence — **talk, listen, express, and follow the person it's talking to**. Manual movement/gaze and rich perception are explicitly **deferred to post-v1** (see below). Final method names/signatures settle before this spec goes `Stable`.

**In scope for v1:**

- **Audio out** — see [audio.md](audio.md)
  - `say(text, ...)` — synthesize speech via a **pluggable** `SpeechSynthesizer` ([audio.md](audio.md)); the default adapter wraps our first-party [`tts-engine`](../../tts-engine) (optional `tts` extra), but callers can supply any synthesizer. On a real/sim robot the audio is routed through the robot speaker (required for echo cancellation), not the local device. `say` is an async task; **stopping speech in v1 is cancelling that task** (see the barge-in note under Deferred).
  - `play_sound(...)` — play a sound file / built-in sound (upstream `media.play_sound`).
- **Audio in (microphone)** — see [audio.md](audio.md)
  - `audio_input()` / `mic_sample_rate` — an async stream of the robot's **echo-cancelled** microphone PCM, for the caller to feed their **own** ASR. The bridge embeds no ASR engine and exposes no recognizer; it provides clean mic audio and manages the media session so echo cancellation applies. The stream is shaped to drop easily into common ASR engines (see [audio.md](audio.md)).
- **Expression**
  - `play_emotion(name, ...)` — play a named recorded move from the emotions library (`RecordedMoves(...).get(name)` + play), with `list_emotions()` to enumerate. Backed by the upstream library and its HF-hub caching — see the Emotions library note below. Requires motors enabled (it moves the robot).
- **Attention / gaze (autonomous)**
  - `start_head_tracking(...)` / `stop_head_tracking()` — wrap the daemon-side face tracker (upstream `start_head_tracking(weight=...)` / `stop_head_tracking()`). The robot **autonomously** keeps a detected face centered; this is *not* a manual control loop the bridge runs. Reading the current target (`get_tracked_face()`) is deferred with the rest of perception. **Requires motors enabled** (see Motors below).
- **Motors / torque**
  - `set_motors_state(state)` — caller-facing torque control through a single verb taking `"enabled"` / `"disabled"` / `"gravity_compensation"`, **decoupled from the connection**: staying connected does not mean staying energized, so a caller can rest the motors while still talking/listening, then re-engage for motion. `"gravity_compensation"` is a *gentle* rest — the head holds its position instead of drooping — versus `"disabled"` (torque off, limp). The bridge does **not** set a state on connect and does **not** auto-toggle around individual moves (that would trip the upstream `enable_motors()` "pins to the present pose" gotcha and add a click/settle per move); the state, once set, holds until the caller changes it. Orchestration: the one verb dispatches to the matching upstream primitive (`enable_motors` / `disable_motors` / `enable_gravity_compensation`).
  - `get_motors_state()` — read the current state (same three values) so a caller can check **before** calling a movement verb, rather than discovering it via the fail-fast error. Returns the daemon's `motor_control_mode` in JSON-friendly form; "ready to move" is `state == "enabled"`.

**Motor torque is an explicit precondition, enforced by fail-fast — not auto-managed.** The audio verbs (`say`, `audio_input`, `play_sound`) need no motors. `play_emotion` and `start_head_tracking` **move the robot**, so they require torque `Enabled`; if it isn't, they **raise a clear error** rather than silently enabling motors or sending a command that does nothing. This is observable, not guessed: the daemon reports `motor_control_mode` ∈ `Enabled` / `Disabled` / `GravityCompensation` via the daemon client (`robot.client.get_status().backend_status` — the public `ReachyMini` has no motor-mode getter, so the Api reads it the way the SDK itself does), so a verb reads the *actual* state before acting. Verified upstream: `wake_up()` / `goto_sleep()` are **not** state gates — pure `goto_target` + `play_sound` animations, no `is_awake` flag — and connecting (`__enter__`) does **not** auto-enable motors; torque is a genuinely separate, caller-owned concern.

**Emotions library — delegated to the SDK, not re-implemented.** `play_emotion` / `list_emotions` wrap the upstream `reachy_mini.motion.recorded_move.RecordedMoves`, defaulting to its `DEFAULT_EMOTIONS_DATASET` (`pollen-robotics/reachy-mini-emotions-library`). Loading and caching are the SDK's: `RecordedMoves` resolves from the standard Hugging Face hub cache **offline-first** (`snapshot_download(..., local_files_only=True)`), falling back to a network download only on a cache miss — so the bridge keeps **no cache of its own**. Two things the implementation must honor: (1) constructing `RecordedMoves` does blocking disk/network IO, so the bridge builds it **lazily, once per connection** (cached instance, never at import) and off the event loop via `asyncio.to_thread`; (2) on a real/sim robot the daemon **preloads** the default datasets at startup, so the first call is normally a cache hit — on the `fake`/offline path the emotions library is stubbed (no HF access), consistent with [audio.md](audio.md)'s fake media. v1 keeps `play_emotion(name)` **literal** (name in → move played); the conversation app's compact-intent→move resolution and curated random-fallback pool are a caller/agent concern, not part of the bridge.

### Deferred to post-v1

Named here so the boundary is explicit; each is a straightforward addition once v1 lands:

- **Manual movement & gaze:** `look_at(...)` (image-pixel `look_at_image` / 3D-world `look_at_world` dispatch), `set_head_pose(roll, pitch, yaw, duration, ...)`, and composed gestures `nod(...)` / `shake_head(...)` / `turn(yaw, ...)`. These are what makes open question 2 (frame/units conventions) load-bearing — so that question is a **post-v1** concern.
- **`wake()` / `sleep()`** — thin wraps of the upstream `wake_up()` / `goto_sleep()` personality emotes. Optional session bookends; not a prerequisite for anything (see torque note above).
- **`set_antennas(left, right, ...)`** — direct antenna control in degrees.
- **Rich perception:** `get_view(...)` (camera frame), `get_head_pose()`, `get_imu()`, `get_tracked_face()`, and sound direction of arrival (`get_sound_direction()` / DoA). Their JSON-friendly return shapes settle with this batch, alongside [tools.md](tools.md) (see open questions). Note v1 ships an agent that can hear, speak, and follow a face but is otherwise **"blind"** (no `get_view`) — an accepted first-pass limitation.
- **`stop_talking()` / true barge-in** — flushing already-queued speaker audio via `media.audio.clear_player()` (see [audio.md](audio.md)). v1 relies on cancelling the `say` task plus keeping the speaker look-ahead buffer small, so a cancel stops speech near-immediately; the explicit flush is the upgrade path when aggressive buffering or hard barge-in is needed.

### Orchestration is the point

Verbs that don't map 1:1 to an upstream call are where this layer earns its keep. In v1 that's `play_emotion` resolving a name to a recorded move (and managing torque so the move actually plays). The richer orchestration — `look_at` choosing the right upstream helper, `nod`/`shake_head` sequencing poses — arrives with the deferred movement verbs. Pure pass-throughs (e.g. `play_sound`, and later `get_imu`) are thin by design.

## Open questions

1. **Error type when motors aren't enabled (v1).** The exact exception a movement verb raises when `get_motors_state()` isn't `"enabled"` — settles with [client.md](client.md)'s error taxonomy.
2. **Frame/units conventions (post-v1).** Exact axes, origin, and angle conventions for the deferred manual gaze/pose verbs (`look_at` / `set_head_pose`), so tools and callers agree (upstream world frame: x forward, y left, z up). Settles when those verbs land.
3. **Perception return shapes (post-v1).** JSON-friendly return types for the deferred perception verbs (`get_view` / `get_head_pose` / `get_imu` / `get_tracked_face` / DoA), settled alongside [tools.md](tools.md). The only v1 return shapes are the mic stream's (in [audio.md](audio.md)) and `list_emotions()`.
