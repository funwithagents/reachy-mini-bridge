---
code:
  - src/reachy_mini_bridge/motion.py
  - src/reachy_mini_bridge/api.py
  - src/reachy_mini_bridge/fake_reachy_mini.py
tests:
  - tests/test_api.py
  - tests/test_motion.py
  - tests-e2e/test_api.py
---

# Motion loop, presence & breathing (`motion.py`)

**Status:** Stable

## Purpose

Everything the bridge does to keep the robot **continuously alive**: the single control loop that owns the robot's target pose, the arbitration that decides which move plays when, and the idle behaviour — **breathing**, or a still neutral hold — that fills the gaps between verbs. It is the motion counterpart of [audio.md](audio.md): the api's verbs (`play_emotion`, and later the manual movement verbs) are thin over what this layer runs.

This concept exists because the robot only looks alive when *one* writer drives its target pose at a steady rate, and everything that should move the head — an emotion, a return to neutral, breathing — is composed into that one stream rather than fighting for it. Upstream gives the building blocks (the `Move` interface, its `GotoMove` interpolation, the recorded-move library, and the daemon-side face tracking and head wobbling that compose *on top of* a target pose) but no arbitration and no idle behaviour; those are what this layer adds.

### Composition model

Game-animation-style blending, split across two places:

```
   ┌─────────────────────────────────────────────────────────┐
   │  MOTION LOOP (bridge, one thread, 60 Hz)                │
   │  one primary move at a time, from a FIFO queue:         │
   │    emotion (later: goto / gesture)  → else idle move    │
   │    idle = breathing | still hold at neutral | nothing   │
   │  every transition is a short blend (minjerk GotoMove)   │
   │  emits set_target(head, antennas, body_yaw) each tick   │
   └───────────────────────────┬─────────────────────────────┘
                               ▼
   ┌─────────────────────────────────────────────────────────┐
   │  DAEMON                                                 │
   │  + face-tracking aim   (weighted blend, 0 → 1)          │
   │  + speech wobble       (offsets, audio-reactive)        │
   │  → inverse kinematics → motors                          │
   └─────────────────────────────────────────────────────────┘
```

- **Primary moves are exclusive.** One plays at a time, sequentially, from a queue.
- **Tracking and wobbling are additive, daemon-side.** They compose with whatever target the loop is sending and never enter the loop. (Tracking at full weight is the one exception — see "Interactions".)
- **Idle is not a special state.** It is the move that plays when the queue is empty; which move depends on two switches, presence and breathing.

## Core concepts / Decided

### Two switches: presence and breathing

| `presence` | `breathing` | When the queue is empty the loop… |
|---|---|---|
| on | on | plays `BreathingMove` — the robot visibly breathes |
| on | off | plays `HoldMove` — a still head at the neutral pose (the target is re-sent every tick, so tracking and wobble still compose against it) |
| off | *(ignored)* | emits **nothing**. The head stays where the last move ended; the bridge only commands the head while a verb runs |

- **Presence** is the background behaviour as a whole: while on, the robot never goes dead between verbs. Off is for a caller that drives the head itself through the raw robot, or wants the bridge's motion output limited to what its verbs ask for. Off does **not** stop emotions: the same loop plays them, it just goes quiet afterwards.
- **Breathing** selects the idle animation under presence.
- Both are **modes**: each holds until changed, both are on by default (the config's `motion` block, [config.md](config.md)), and each has a verb and a read-only property on the api — `set_presence(enabled)` / `presence`, `set_breathing(enabled)` / `breathing` ([api.md](api.md)). Neither needs motors (the loop is paused without them, below).
- **Toggle semantics**, identical for both switches:
  - *Idle when toggled* → the loop transitions at once, through the usual blend: breathing off fades the breathing plan out to neutral ("Leaving breathing" below) and holds; breathing on starts a fresh breathing plan from neutral, whose first segment starts at rest, so there is no jump; presence on blends from the present pose into the idle move. Presence **off** while idle stops commanding at once, with no easing — the caller turning it off wants the head, and the idle move is at most a breath's amplitude (5 mm) from neutral.
  - *A primary move is playing when toggled* → the switch is recorded and takes effect when the queue next drains. An emotion is never interrupted to change idle behaviour.
  - *Idempotent* → setting the current value is a no-op, not a re-transition.

### The loop — one writer, fixed rate

- **Only the motion loop calls `set_target`.** Nothing else in the bridge sends a target: not the api, not the media layer. The upstream helpers that run their own trajectory — `async_play_move` / `play_move`, `goto_target`, `wake_up`, `goto_sleep` — are **not called by the bridge** while a session is open, for two reasons verified on SDK 1.10 ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)): a client-side `async_play_move` is a second 100 Hz writer that would fight the loop's stream, and a daemon-side move (`goto_target`, `wake_up`, `goto_sleep`) holds a move guard for its duration during which the daemon **drops every `set_target` from every client** with a warning. The caller keeps the raw robot as an escape hatch ([robot.md](robot.md)); driving the head through it while presence is on produces exactly that fight, which is what `set_presence(False)` is for.
- **A dedicated thread**, not an asyncio task: the tick must not jitter when `say` or the mic tap is busy on the event loop, and `set_target` is a blocking socket send. The api talks to it through a thread-safe command queue; each primary move it submits is awaited through a future the loop resolves when the move ends (or fails), so the api's verbs never block the event loop and cancel takes effect within one tick.
- **`CONTROL_HZ = 60`**, on a monotonic clock (`t = now − move_start`). Not 100: the conversation app's breathing at ~100 Hz produces visible micro-vibrations on the Stewart platform from the tiny per-tick deltas of a 5 mm / 0.1 Hz sine (upstream issue, open). 60 Hz is the rate its current manager runs at; whether it is the right rate on hardware is open question 1.
- **The tick.** When the current move has ended (or there is none), the loop takes the next queued primary, else the idle move for the current switches; the new move is entered through a blend (below). It then evaluates the current move at `t`, sends `set_target(head=…, antennas=…, body_yaw=…)`, remembers that as the **last commanded target**, and sleeps out the remainder of the period. A move that returns `None` for a component leaves that component at the last commanded value.
- **Every transition is a blend; the loop never snaps.** Entering any move — a primary from idle, idle from a primary's end pose, one idle from another, a resume after a pause — goes through a `GotoMove` (upstream's, minjerk) of **`BLEND_S = 0.5` s** from the *source pose* to the new move's `evaluate(0)`, then the move plays from `t = 0`. The source pose is the last commanded target while the loop is commanding continuously; whenever it was **not** commanding — session start, a resume after the motors were off, presence turned back on — it is the **present pose read from the robot** (`get_current_head_pose()`, and antennas / body yaw from `get_current_joint_positions()`, body yaw being the first head joint), because the last target may be stale. `BreathingMove` emits absolute poses, so entering it directly from an arbitrary end-of-emotion pose is what produced the visible "head slowly descends" artefact in the conversation app; the blend removes it.
- **Re-anchor on request.** `reanchor()` is a loop command for the api ([api.md](api.md) "Attention"): when a daemon-side layer is about to hand the head back to the loop's stream (full-weight tracking has been discarding it), the stream may be far from where the head actually is. On the command, if the loop is idle and commanding, it drops the current idle move and re-enters the idle move on its next tick through the usual blend from the **present pose read from the robot** (exactly as after a pause); the returned future resolves when the command has been taken, so the api can send the hand-back after it. With a primary playing, an exit blend running, presence off, or the loop paused, the command is a no-op and the future resolves at once — an emotion is never interrupted, and there is nothing to re-anchor when the loop is not commanding.

### The moves

All are upstream `reachy_mini.motion.move.Move` subclasses — `duration` plus `evaluate(t) -> (head_4x4, antennas_rad, body_yaw_rad)` — so the loop treats a recorded emotion, a blend and an idle move identically.

- **Neutral** is upstream's `INIT_HEAD_POSE` (the identity, the pose `wake_up` ends at), `INIT_ANTENNAS_JOINT_POSITIONS` (`[-0.1745, 0.1745]` rad — ~10° apart on purpose, upstream's "reduce shaking at vertical"; not `[0, 0]`) and body yaw `0.0`. The constants come from `reachy_mini.reachy_mini` (SDK 1.10).
- **`BreathingMove`** — infinite duration; the idle move that reads as alive. It is a **randomised plan** of three independent tracks — the head's z offset, the right antenna, the left antenna — each a sequence of segments that **start and end at rest** (zero velocity). The move takes a seedable random source (`random.Random`) at construction: the loop builds a fresh, unseeded move at each idle entry, tests pass a seed. A plan is generated lazily as `t` grows and never changes once generated, so `evaluate(t)` stays a pure function of `t` for a given seed, as the `Move` contract expects.
  - *Head track:* alternates a **breath** and a **rest**. A breath is a raised-cosine rise-and-fall on the **z axis** — `z = BREATH_Z_M · (1 − cos(2π t / BREATH_S)) / 2`, peaking `BREATH_Z_M = 5 mm` above neutral over `BREATH_S = 5 s` — followed by a rest at neutral of a random `BREATH_REST_S = 1 to 5 s` (uniform). The plan begins with a breath, so a fresh idle shows life at once. The head rests *at* neutral (the wake-up pose) and rises from it, never dipping below; x, y and orientation stay neutral. The rests are what make the breathing organic: a robot that breathes on a fixed cycle reads as a machine.
  - *Antenna tracks:* each antenna, independently, alternates a **hold** of random `ANTENNA_HOLD_S = 0.5 to 4 s` and a **minjerk move** (upstream's `time_trajectory`, `MIN_JERK`) of random `ANTENNA_MOVE_S = 0.8 to 2.5 s` to a new random angle in `ANTENNA_MIN_RAD .. ANTENNA_MAX_RAD` = **10° to 25° outward** from vertical (uniform). Each track begins with a hold. The two antennas draw from separate streams of the random source, so their motion is asymmetric and uncorrelated — and because both only lean outward from vertical, they can never meet. "Outward" in joint terms is the sign of upstream's sleep pose (`SLEEP_ANTENNAS_JOINT_POSITIONS = [-3.05, 3.05]`, the antennas folded fully out): **negative for the right antenna, positive for the left** — `ANTENNA_OUTWARD = [-1, +1]`, joint value = sign × angle. The floor is the neutral 10° lean, not true vertical: upstream chose that offset to keep the antennas from shaking at vertical, and it makes the floor exactly `NEUTRAL_ANTENNAS`. The ceiling is the conversation app's peak sway (its 10° neutral + 15° amplitude), seen on hardware.
  - Body yaw `0.0`.
  - `evaluate(0)` is exactly neutral **at rest**, and every segment boundary is at rest, so the entry blend (minjerk, ending at zero velocity) hands off with continuous velocity and the move needs no entry envelope. A sine has no such property — its value is zero exactly where its slope is steepest — which is why the idle is built from rest-to-rest segments rather than a continuous oscillation.
- **Leaving breathing mid-plan** — `set_breathing(False)` while breathing plays, or session exit while it plays — goes through a **fade-out** rather than a plain blend: the same plan keeps playing from the moment of interruption while a minjerk-shaped envelope scales every track's offset from neutral down to zero over `BLEND_S`, landing exactly at neutral with zero velocity before whatever follows (the hold's blend, or nothing) takes over. A plain blend assumes its source is at rest, and a track caught mid-segment is not. An interruption that lands in a rest is the same fade over an offset that is already zero. Presence off while breathing stops dead by design (above).
- **`HoldMove`** — infinite duration; constant neutral.
- **Blends** are upstream `GotoMove` instances built by the loop (start pose → target pose, `BLEND_S`, minjerk).
- **Recorded emotions** are upstream `RecordedMove`s from the emotions library, resolved by the api as today ([api.md](api.md) "Emotions library"); on the `fake` backend the api's offline stub library yields real `Move`s with a short trajectory (a small nod, 0.3 s), so the loop path runs in `tests/`.

### Emotions through the loop

`play_emotion(name)` ([api.md](api.md)) resolves the move, then hands it to the loop as a primary; the loop plays it — this is the orchestration:

1. **Queue.** Primaries are FIFO. A second `play_emotion` while one plays waits its turn; cancelling a waiting one removes it from the queue.
2. **Enter.** When the move becomes current, the loop blends from the source pose to `move.evaluate(0)` over `BLEND_S` (this replaces upstream's `initial_goto_duration`, which covers only the entry and would be a daemon-side move anyway).
3. **Sound.** As the trajectory starts (after the blend, as upstream orders it), the loop starts the move's sidecar sound through `media.play_sound(str(move.sound_path))` when the move has one.
4. **Play** the trajectory for `move.duration`, evaluating at the loop rate.
5. **Complete.** The verb's future resolves when the trajectory ends. The return to neutral is **not** part of the verb: what follows is the idle behaviour's business — with presence on the idle move blends in from the end pose (so an emotion ends, eases back to neutral and breathing resumes, with nothing to queue); with presence off the head stays at the recording's end pose. Two emotions queued back-to-back blend directly from the first's end pose into the second, with no detour through neutral.
6. **Cancel / failure.** Cancelling the awaiting task (or the move raising) stops the trajectory at the next tick and stops its sound through [audio.md](audio.md)'s `MediaSession.stop_sound()` (which also resets the wobbler); the loop then behaves as at step 5 from where the cancel caught it. This keeps [api.md](api.md)'s "Cancellation" guarantees: prompt, effect stopped, session usable.

Around the move, the api adjusts the two daemon-side layers and **restores them afterwards, on every exit path** (completion, cancel, failure):

- **Face tracking is paused for the move.** If tracking is on (the api keeps the weight it last requested, as it keeps the wobbling mode), the api sends `start_head_tracking(weight=0.0)` before submitting the move and re-sends the attention state's current daemon weight after ([api.md](api.md) "Attention"). Required, not cosmetic: at weight `1.0` the daemon discards the head component of every `set_target`, so an emotion under full-weight tracking would not show at all; at intermediate weights it would show blended toward the face.
- **Wobbling is paused for the move.** If wobbling is on, the api calls `disable_wobbling()` before and `enable_wobbling()` after. Library emotions carry their own sound; letting it drive the wobbler as well puts audio-driven motion on top of the choreographed motion on the same head, which reads as twitchy. The emotion's sound still plays. A `say` running concurrently with an emotion therefore does not sway the head for that emotion's duration. This resolves [api.md](api.md)'s former open question on wobbling during `play_emotion`.

### Interactions with the daemon-side layers

| Combination | Behaviour |
|---|---|
| wobble + breathing / hold | additive — the robot reacts to audio while idle, still or breathing |
| wobble + emotion | paused by the api for the move (above) |
| tracking + breathing / hold | additive below weight 1.0; at weight 1.0 the daemon ignores the head target while a face is tracked, so only the antennas breathe and the face owns the head; once nobody has been seen for the attention grace period the api hands the head back ([api.md](api.md) "Attention") and the robot idles in full |
| tracking + emotion | paused to weight 0.0 by the api for the move (above) |
| emotion + emotion | exclusive — queued, never overlapped |
| breathing + hold | exclusive — one idle move, chosen by the switch |
| presence off + wobble / tracking | unaffected — daemon-side modes stay on; a non-breathing robot still reacts to audio and follows a face against its last target |

### Motors

The loop **pauses while the motors are not `enabled`** and sends no target at all: targets into limp or gravity-compensated motors do nothing, and upstream's `enable_motors()` pins the targets to the *present* pose before switching torque on, so a stream that kept running would snap the head from wherever it drooped to the idle pose the instant torque returned. Pause and resume are driven by the api, not by polling:

- at session start the api reads the motor state once and starts the loop paused unless it is `enabled`;
- `set_motors_state("enabled")` resumes it, `"disabled"` / `"gravity_compensation"` pause it — pausing drops the queue and fails an in-flight primary with `BridgeError`, so a `play_emotion` cut short by a rest reports it rather than hanging;
- `play_emotion` resumes it (its motor precondition has just passed — this also covers a daemon that enabled the motors itself, e.g. its boot wake-up, which the api never saw).

Every resume **re-anchors**: the first blend starts from the present pose read from the robot. A daemon-side change the api did not make (an idle-timeout `goto_sleep` on a shared robot disables torque; its own `wake_up` ends at neutral with torque on) is harmless either way: targets into disabled motors do nothing, and the next api-driven resume re-anchors.

The verbs that move the robot keep their fail-fast precondition ([api.md](api.md) "Motors"): `play_emotion` raises `MotorsNotEnabledError` rather than queueing into a paused loop.

### Lifecycle

The loop is a **`MotionSession`**, entered by `ReachyMiniApi.__aenter__` after the media session and the wobbling setup, and exited first ([api.md](api.md) "Lifecycle"):

- **Enter:** start the thread with the config's `motion.presence` / `motion.breathing`, paused unless the motors read `enabled`. Nothing moves until the first tick's blend.
- **Exit:** when presence is on and the loop is commanding, ease the head to the neutral hold (one `BLEND_S` blend, awaited) so the robot is left standing neutral for the next app, then stop the thread. With presence off, or the loop paused, just stop. Pending primaries are dropped and their futures cancelled. The daemon's own idle reset is not involved: it fires only when a *managed* app slot (a WebRTC session or an `AppManager` app) goes free, and SDK clients like the bridge bypass that lock entirely ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)) — so the bridge parks the robot itself.
- **Bring-up cancel / failure** unwinds it like every other step (the api's `AsyncExitStack`).
- The thread never outlives the session. An exception raised inside a tick (a bad pose, a failed send on a live connection) is logged, fails the in-flight primary's future when there is one, and drops the current move; the loop keeps running and re-selects on its next tick, so one bad move never kills the idle behaviour.
- **A lost connection is not a bad tick.** When `set_target` raises because the daemon is gone — upstream's builtin `ConnectionError("Lost connection with the server.")` once its client's receive loop has noticed the close, or `websockets.exceptions.ConnectionClosed` (not a `ConnectionError`) on the send that races the close — the loop logs **one** `WARNING` (`motion loop paused: lost connection to the daemon`), fails the in-flight primary and every queued one with a `BridgeError` chained to the upstream error, and **pauses** as after `pause()`: it sends nothing more, so nothing more is logged. The pause is permanent for the session: a later `submit` fails its future with the same `BridgeError` at once instead of un-pausing the loop, `resume()` is a no-op, `reanchor()` resolves at once, and `close()` skips the exit blend (there is no live socket to blend into) and returns promptly. Recovery is out of scope — upstream's client does not reconnect, so a lost connection ends the session; the caller exits and re-enters the api. Without this, a daemon that dies mid-session (USB unplugged, a crash, someone stopping a borrowed daemon) produces one warning per tick — sixty lines a second — until the app exits; the Ctrl+C case itself is removed at the source by [daemon.md](daemon.md) "The child runs in its own session".

### `fake` backend support

`FakeReachyMini` ([robot.md](robot.md)) gains the loop's consumed members: `set_target(head, antennas, body_yaw)` recorded on a dedicated `targets` list (not `commands`, which a 60 Hz stream would swamp) with the most recent kept as `last_target`; `get_current_head_pose()` and `get_current_joint_positions()` returning the last commanded values (identity / neutral before any), so the fake is a trivially consistent robot for re-anchoring. `async_play_move` leaves the consumed slice: the bridge no longer calls it. The offline emotions library yields moves with a real short trajectory. The loop itself runs unchanged on the fake, at real time, so `tests/` observe its stream: a breathing robot's recorded head targets rise from neutral and return to it, resting at neutral between breaths, and its antennas roam independently within their outward range; a hold sends a constant neutral; presence off sends nothing once idle. Tests of the plan itself seed the move and evaluate it at chosen times, with no waiting.

## Relationship to the other specs

- **[api.md](api.md):** `play_emotion` runs through this loop (and pauses tracking / wobbling around the move); `set_presence` / `set_breathing` and their properties are the switches' verbs; `set_motors_state` pauses / resumes the loop; the session enters last and exits first.
- **[config.md](config.md):** the `motion` block (`presence`, `breathing`) sets the switches' initial values.
- **[robot.md](robot.md):** the consumed slice grows `set_target` and the two pose readers, and drops `async_play_move`; the fake records targets.
- **[audio.md](audio.md):** the move's sound starts through `media.play_sound` and stops through `MediaSession.stop_sound()`; wobbling is paused around an emotion.
- **[testing.md](testing.md):** smoothness and drift cannot be asserted by a test; the live tier asserts what it can (breathing moves z; an emotion returns to neutral; a cancel stops it) and the implementation plan carries the on-robot checks.

## Open questions

1. **Tick rate on hardware.** `CONTROL_HZ = 60` and `BLEND_S = 0.5` are the values to start from. On the Reachy Mini Lite (SDK 1.10), 50 Hz is worse, not better: a per-tick step is bigger at a slower rate, and any velocity discontinuity at a handoff shows through the whole body. What keeps the stream snap-free is therefore the handoff discipline above — every idle segment boundary at rest, a fade-out on interruption — not the rate. Still open: whether residual micro-vibration remains at 60 Hz once the handoffs are clean (amplitude / deadband tuning), whether slow downward drift shows over a long idle, and the deferred case of a primary (an emotion) preempting breathing mid-segment, which hands off through a plain `blend_into` from the momentary pose — the same nonzero-velocity gap the fade-out closes on exit, not yet observed as a problem.
2. **Manual moves.** `goto`-style primaries (`set_head_pose`, `nod`, `look_at`) slot into the queue as `GotoMove`s / composed moves once [api.md](api.md)'s deferred manual verbs land; the loop needs no change for them.
3. **Listening antenna freeze.** The conversation app freezes the antennas while it listens (and blends them back over 0.4 s) as a "listening" cue. Whether the bridge offers such a cue is deferred; it would be a third idle variant or an overlay on the idle move.
4. **Rate-limiting non-connection tick faults.** A *persistent* fault that is not a lost connection (a move whose `evaluate` keeps raising, say) still logs once per tick under the "Lifecycle" contract. Logging the first and then a count every few seconds is deferred: no such flood has been observed, and a failing move is dropped on its first bad tick, so the idle move that follows is a fresh one.
