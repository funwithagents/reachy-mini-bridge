# API reference — `ReachyMiniBridge`

The public surface of the bridge: what you import, the verbs and the values, the errors, and the contracts that hold across them — lifecycle, cancellation, concurrency, units, and the three extension points. This page is the consumer's home for those contracts; the normative design, with its rationale, is in the specs it links.

On this page: [Imports](#imports) · [Construction](#construction) · [Lifecycle](#lifecycle) · [Verbs](#verbs) · [Properties and reports](#properties-and-reports) · [Errors](#errors) · [Cancellation and concurrency](#cancellation-and-concurrency) · [Units](#units) · [Extension contracts](#extension-contracts) · [The escape hatch](#the-escape-hatch).

All verbs are `async`. What they take is human — named emotions, `"enabled"` motors, seconds, degrees and millimetres for a motion offset; what the reports carry states its own unit on each field ("Units" below). The underlying `reachy_mini.ReachyMini` stays reachable as `bridge.robot` for anything the bridge does not cover ("The escape hatch" below).

## Imports

From `reachy_mini_bridge`:

| Name | What it is |
|---|---|
| `ReachyMiniBridge` | The bridge — a session with verbs |
| `ReachyMiniConfig` | The declarative config ([configuration.md](configuration.md)) |
| `SpeechSynthesizer`, `TTSEngineSynthesizer` | The voice protocol, and the shipped tts-engine adapter |
| `CameraFrame` | A camera frame: `frame_id`, `ts`, `image`, `head_pose` |
| `Face`, `FaceReport`, `PixelFace`, `FaceDetector` | The perception values, and the two types a custom detector works with |
| `HeadTrackingReport` | The head tracker's state |
| `Observable` | The type of `bridge.faces` and `bridge.head_tracking` |
| `IdleMove`, `IdleOffsets` | The base class and the value type of a custom idle move |
| `BridgeError`, `MotorsNotEnabledError`, `GravityCompensationUnsupportedError`, `SpeechInterruptedError`, `SoundInterruptedError`, `ConfigError` | The errors a caller catches |

From `reachy_mini_bridge.errors`: `DaemonError` (a managed daemon that could not be started or found). From `reachy_mini_bridge.config`: `FaceDetectionSettings`, `MotionSettings`, `DaemonConfig`, `AudioSettings` — the blocks, for a config assembled in code. From `reachy_mini_bridge.audio`: `int16_to_float32`, `float32_to_int16`, `downmix_to_mono` — the conversion helpers for a synthesizer or an ASR path. From `reachy_mini_bridge.fake_reachy_mini`: `FakeReachyMini`, to narrow `bridge.robot` in a test. From `reachy_mini_bridge.testing`: the live-test harness ([../guides/testing.md](../guides/testing.md)).

## Construction

```python
ReachyMiniBridge(config: ReachyMiniConfig | str = "real", *, synthesizer: SpeechSynthesizer | None = None)
ReachyMiniBridge.from_dict(data, *, synthesizer=None)
ReachyMiniBridge.from_json(text, *, synthesizer=None)
ReachyMiniBridge.from_json_file(path, *, synthesizer=None)
```

A string is the backend-name shorthand for a config with everything else at its default. An explicit `synthesizer` wins over the config's `tts` block, which is then not consumed. Construction opens no connection to the robot and starts no thread. The config is validated as the `from_*` constructors build it — a `ConfigError` is raised there, before the bridge exists; a `ReachyMiniConfig` assembled in code is not validated ([configuration.md](configuration.md#validation)). Construction then builds the observables (`faces`, `head_tracking`) and the camera feed object, so a consumer can subscribe before the session starts, and the voice: a `tts` block has its tts-engine provider built here — a local model such as `pocket` loads its weights at this point, before any session — and a provider that fails to build leaves the bridge without a voice rather than failing construction. `bridge.config` is the config it was built from; `bridge.synthesizer_error` the cause when the `tts` block failed to build a voice (`None` otherwise) — `say` then raises `BridgeError` chained to that cause; a host that wants hard failure checks it after construction. Everything else — the daemon, the connection, the media session, the detector's model — comes up in `start()`, below.

## Lifecycle

The bridge is a session. `await bridge.start()` brings everything up in order — the managed daemon when configured, the robot connection, the media session, the camera feed, wobbling, the detection loop with the head tracker, the motion loop — and `await bridge.stop()` tears it down in reverse: the motion loop eases the head to neutral, detection stops, wobbling is turned back off (the setting is shared by every app on the daemon), the camera feed, the media session, the connection and an owned daemon close, each even when another fails; the modes reset to the config's values so `start()` may follow.

```python
async with ReachyMiniBridge.from_json_file("robot.json") as bridge:   # start() … stop()
    ...

bridge = ReachyMiniBridge.from_json_file("robot.json")                 # a host with lifecycle hooks
await bridge.start()
...
await bridge.stop()
```

- **`async with` is the recommended form**: the block stops the session on every way out, a cancel included. The `start()` / `stop()` pair is for hosts with lifecycle hooks of their own — a web framework's lifespan, a GUI, a pytest fixture, an agent runtime.
- **One event loop owns a session.** The bridge is loop-bound: its detection loop is an asyncio task on the loop that started it, its observables publish there. Start and stop it on the same loop, and await its verbs there. A synchronous host runs the bridge on a background loop and submits calls to it, as the [control panel](../../examples/control_panel/) does.
- **Overlapping lifecycle calls are not supported.** `start()` on a running bridge raises `BridgeError`; `stop()` on a stopped one is a no-op. Calling `stop()` while `start()` is still running, or two `start()`s at once, is outside the contract — cancel the `start()` task instead, which unwinds what it started.
- **A failed or cancelled start leaks nothing**: the steps already up are undone in reverse and the error (or the cancel) propagates. A repeated cancel during that cleanup is absorbed; the cleanup completes.
- **A cancelled `stop()` completes too**: the teardown is owned once begun — the head eased to neutral and the motion thread joined before the connection closes — and the cancel propagates after it.
- **`bridge.running`** says whether the session is up. `bridge.robot`, `say`, `play_sound`, `audio_input` and the mode verbs need a running session and raise `BridgeError` otherwise; `faces`, `head_tracking`, `camera` and the mode properties read at any time (their inactive values outside a session).
- **A daemon the bridge started is stopped on exit** — a robot's goes to sleep; a borrowed daemon (`daemon.spawn: "auto"` finding one, or `"never"`) is left running.
- **Applications own their tasks.** A task you start around the bridge — an ASR consumer over `audio_input()`, a subscriber on `faces.changes()` — is yours to cancel and await; leaving the `async with` block does not cancel it. The mic tap ends on its own when the session closes, so a task draining it finishes cleanly when awaited. A `changes()` iterator does not: the observables belong to the bridge object and outlive its sessions ([specs/core/observable.md](../../specs/core/observable.md) "Semantics") — a subscriber is told of the close through a published value (`faces` and `head_tracking` turn inactive) and then waits for the next session's first publication. Cancel and await such a task yourself, or `break` on the inactive report, before awaiting it at shutdown:

```python
async def watch_faces(bridge: ReachyMiniBridge) -> None:
    async for report in bridge.faces.changes():
        print("present" if report.faces else "nobody")


async with ReachyMiniBridge.from_json_file("robot.json") as bridge:
    watcher = asyncio.create_task(watch_faces(bridge))
    try:
        await run_the_application(bridge)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
```

## Verbs

| Area | Verbs | Needs motors |
|---|---|---|
| Motors | `get_motors_state()` → `"enabled"` \| `"disabled"` \| `"gravity_compensation"`; `set_motors_state(state)` | — |
| Expression | `list_emotions()` → the names of the upstream recorded-moves library; `play_emotion(name)` — plays the move with its sound, returns when the trajectory ends | yes |
| Speech out | `say(text, synth=None)` — returns when the robot has finished speaking; `play_sound(file)` — a sound file, returns when it has been heard | no |
| Mic in | `audio_input(mono=True)` — an async iterator of int16 LE PCM `bytes`; `mic_sample_rate`, `mic_channels` | no |
| Gaze | `start_head_tracking(focus=False)`, `stop_head_tracking()` — the head follows the face the tracker chooses | no (a mode: the head moves once motors are `enabled` and `presence` is on) |
| Perception | `set_face_detection(enabled)` — run the detector so `faces` reports; `set_face_detector(factory)` — register the factory the `"custom"` detector is built from (the detector *name* is config-only) | no |
| Staying alive | `set_presence(enabled)`, `set_idle("breathing" \| "hold" \| "custom")`, `set_idle_move(factory)`, `set_wobbling(enabled)` | no (modes) |

**Motors.** Verbs that move the robot — `play_emotion` — require motors `"enabled"` and raise `MotorsNotEnabledError` otherwise, before sending anything. `"gravity_compensation"` needs the daemon's Placo kinematics engine; without it `set_motors_state("gravity_compensation")` raises `GravityCompensationUnsupportedError` without sending the mode (sending it would make the daemon drop the connection). The modes — tracking, presence, idle, wobbling, detection — need no motors: they hold, and take effect on the head once torque is on. The motion loop pauses while motors are not enabled, so nothing is commanded into limp motors; a `play_emotion` in flight when the motors are disabled fails with `BridgeError`.

**Expression.** `play_emotion` resolves the named move (the library downloads into the Hugging Face cache on first use), then plays it through the bridge's motion loop as the one primary move: a blend in, the trajectory with its sidecar sound, and the idle move blending back in afterwards. Emotions **queue in order** — a second `play_emotion` waits for the first to end — and never overlap. Head wobbling is paused for the move's duration.

**Speech out.** `say` streams the synthesizer's audio to the robot speaker, resampled to the speaker's rate and fanned out to its channels. It returns when the utterance has been heard; completion is the sink's wall-clock estimate of the queued audio's end plus a 100 ms margin, not a measurement at the speaker. **The newest `say` wins**: one called while another is in flight interrupts it — the speaker is flushed, the interrupted call raises `SpeechInterruptedError` in its own task, the new utterance plays from silence. `play_sound(file)` plays a file on the robot's own file player, which holds one file at a time: **the newest sound file wins**, an emotion's sidecar sound included — a `play_sound` during an emotion silences the emotion (which plays on), an emotion's sound starting during a `play_sound` makes that call raise `SoundInterruptedError`. Speech and a sound file coexist: they are mixed at the speaker.

**Mic in.** `audio_input()` is a tap over the already-running capture: iterate to consume, `break` to stop; it ends on its own when the session closes. Routing both directions through the bridge is what keeps the robot's echo cancellation working while it speaks and listens at once. The bridge does no speech recognition.

**Gaze.** The tracker chooses whom to follow — the biggest face of a minimum size, kept while it is seen; a face that vanishes is waited for a second, the head holding toward where it was, before the head turns to the biggest other face; after two seconds with nobody the head eases back into the idle move. `focus=False` composes the aim into the idle move (the head looks at the person and breathes); `focus=True` holds the head exactly on the face, the antennas keeping their motion. Tracking implies detection, and needs a `face_detection.detector` in the config: `start_head_tracking()` with none raises `ValueError`. The head moves on the aim only with motors `"enabled"` **and** `presence` on — `set_presence(False)` has the loop command nothing, the gaze included, while the tracking mode holds.

## Properties and reports

| Property | Type | What it reads |
|---|---|---|
| `camera` | `CameraFeed` | The one reader of the robot's camera. `camera.latest()` is the newest `CameraFrame`, or `None` before the first frame; `camera.running`, `camera.published_count` |
| `faces` | `Observable[FaceReport]` | The faces in front of the robot as the detection loop last saw them |
| `head_tracking` | `Observable[HeadTrackingReport]` | Whom the head follows, and whether it does |
| `tracking`, `tracking_focus`, `attention` | `bool`, `bool`, `str \| None` | The tracking mode's record; its focus flag; `"engaged"` while a face is followed, `"watching"` while tracking is on and nobody is, `None` when tracking is off |
| `face_detection`, `face_detector` | `bool`, factory \| `None` | The caller's detection switch; the registered custom detector's factory |
| `presence`, `idle`, `idle_move`, `wobbling` | `bool`, `str`, factory \| `None`, `bool` | The idle behaviour's switches |
| `mic_sample_rate`, `mic_channels` | `int`, `int` | The capture format behind `audio_input` (16 kHz; stereo on the robot and the sim) |
| `robot`, `raw` | `ReachyMini \| FakeReachyMini` | The upstream object, while running ("The escape hatch") |
| `running`, `config`, `synthesizer_error` | `bool`, `ReachyMiniConfig`, `Exception \| None` | Session state; the config; a voice that failed to build |

### The values

```python
@dataclass(frozen=True)
class CameraFrame:
    frame_id: int                  # 1, 2, 3, … per feed: the key a result is matched on
    ts: float                      # the frame's time on the monotonic clock (capture when known, else arrival)
    image: NDArray[np.uint8]       # H×W×3 BGR, shared and read-only: copy before drawing on it
    head_pose: NDArray | None      # the 4×4 head pose at capture, when the backend stamps its frames

@dataclass(frozen=True)
class Face:
    x: float                       # normalised image position in [-1, 1]: x right, y down, (0, 0) the centre
    y: float                       #   (the nose when the detector gives one, else the box's centre)
    roll: float | None             # head roll in radians; None when the detector gives neither eyes nor orientation
    size: float                    # the box's height as a fraction of the frame height
    track_id: int                  # the same positive integer for the same person from frame to frame; never reused
    bbox: tuple[float, float, float, float]   # x, y, width, height in pixels of the report's frame
    pitch: float | None            # head pitch / yaw in radians, from a detector that fits a head; None otherwise
    yaw: float | None

@dataclass(frozen=True)
class FaceReport:
    faces: tuple[Face, ...]        # everyone the detector reports, by track_id (oldest track first)
    ts: float                      # the frame's time
    source: str | None             # "yunet" | "custom"; None when the config names no detector
    active: bool                   # a detector is looking. False means UNKNOWN, not nobody
    head_pose: NDArray | None      # the pose the frame was captured from, when known
    frame: CameraFrame | None      # the frame the faces were found in; None while inactive
    frame_id: int                  # property: frame.frame_id, 0 without a frame

@dataclass(frozen=True)
class HeadTrackingReport:
    active: bool                   # the tracker runs (tracking on, inside a session)
    focus: bool                    # it holds the head exactly on the face
    attention: str | None          # "engaged" | "watching" | None
    track_id: int | None           # the face the head follows; None unless engaged
    ts: float                      # the time of the face report behind the last aim; 0.0 before any
```

### Reading and subscribing

An `Observable[T]` is a value you read and subscribe to ([specs/core/observable.md](../../specs/core/observable.md)):

- **`.value`** is the current state, read from any thread with no await. The face report is refreshed on every detection (fresh coordinates), so `faces.value` is always the newest.
- **`.changes()`** is an async iterator of *published* values from the moment it is driven. The producer decides what counts as a change: `faces.changes()` wakes when the **number of faces changes** (a rise at once; a drop once it has held for 0.3 s), and when detection starts or stops — **never when a face moves**. `head_tracking.changes()` wakes when the head engages someone, hands the head back, switches to someone else, or when tracking or focus is switched. A subscriber that falls behind gets the latest value, not every intermediate one. Cancelling the task blocked in `async for` detaches it.
- **`.wait_for(predicate)`** returns the current value if it already matches, else the first *published* value that does — so it reacts to publications, like `changes()`, not to every silent refresh of `.value`.

```python
report = bridge.faces.value                                   # the newest state, any time
if report.active and report.faces:
    person = report.faces[0]                                  # by track_id, not by importance

async for report in bridge.faces.changes():                   # someone appeared / left; detection on / off
    print(len(report.faces) if report.active else "not looking")

followed = bridge.head_tracking.value.track_id                # whom the head follows (None: nobody)
```

**What the reports mean.** `active=False` means *no detector is looking* — detection off, or no camera frame reaching it for five seconds — and such a report always carries no faces; never read it as an empty room. The face list is ordered by `track_id`, oldest track first: index zero is the longest-seen face, **not** the one the head follows — that is `head_tracking.value.track_id`. A `track_id` is **geometric continuity**, not a recognised identity: the same id follows a person through a short run of missed detections; two people crossing, or someone leaving and coming back, can change ids. Keep your own map from `track_id` to who it is if you recognise people. `frame` is a reference to the shared camera frame the faces were found in: crop with `bbox` (clamped to the image) and **copy before drawing**.

## Errors

| Error | Raised by | Meaning |
|---|---|---|
| `BridgeError` | any verb | The base: a refused call (no voice configured, a verb outside a session, `start()` on a running bridge, a bring-up step that failed, an emotion cut short by a motor rest) |
| `MotorsNotEnabledError` | `play_emotion` | Motors are not `"enabled"`; nothing was sent |
| `GravityCompensationUnsupportedError` | `set_motors_state("gravity_compensation")` | The daemon's kinematics engine does not support it; nothing was sent |
| `SpeechInterruptedError` | `say` | A newer `say` replaced this one |
| `SoundInterruptedError` | `play_sound` | A newer sound file (an emotion's included) replaced this one |
| `DaemonError` | `start()` | The managed daemon could not be started, found or reached in time (`reachy_mini_bridge.errors`) |
| `ConfigError` | the `from_*` constructors | An invalid config; a `ValueError`, not a `BridgeError` |
| `ValueError` | `start_head_tracking`, `set_face_detection(True)`, `set_face_detector`, `set_idle_move`, `set_idle`, `set_motors_state`, `play_emotion`, `play_sound` | A mode that needs a detector with none configured; a factory that is not callable or builds the wrong thing; an unknown idle mode; an unknown motor state; an emotion name the library does not have; a sound file whose duration cannot be read |
| `FileNotFoundError` | `play_sound` | The name is neither a file on this machine nor one of the SDK's built-in sounds; nothing played |
| upstream's own exceptions | `start()`, `bridge.robot` | A connection the SDK could not make — no daemon at `robot.host:port` with nothing to spawn — propagates from `start()` as `reachy_mini` raises it, unwrapped (a managed daemon's failures are the bridge's `DaemonError`); what you call on `bridge.robot` raises what the SDK raises |

Every `BridgeError` subclass carries a message a tool can return to an agent as the reason.

## Cancellation and concurrency

**Cancelling the awaiting task is how you interrupt a verb** ([specs/core/bridge.md](../../specs/core/bridge.md) "Cancellation"), with three guarantees: the cancel returns promptly; the verb's effect stops with it; and the robot and the session stay usable for the next verb. What "the effect stops" means depends on the verb's class:

- **Spanning verbs** — `say`, `play_sound`, `play_emotion`, the `audio_input` stream — run for as long as their effect, and the cancel ends it: queued speech flushed, a sound file stopped, a trajectory no longer commanded, the wobbler reset. There is no rewind: the head stays where the cancel caught it, and the idle move blends in from there. A cancel owns only its own effect — cancelling a `say` does not stop a `play_sound`, cancelling an emotion waiting in the queue stops nothing that plays.
- **Instant verbs** — `set_motors_state`, `get_motors_state`, `start_head_tracking` / `stop_head_tracking`, `set_wobbling`, `set_presence` / `set_idle` / `set_idle_move`, `set_face_detection`, `list_emotions` — hand one short command to the SDK or the motion loop. The cancel returns promptly, but **an accepted command completes**: cancelling `set_motors_state("enabled")` once the command is accepted does not keep the motors from enabling, nor the motion loop from resuming with them. Likewise a `set_face_detection(False)` or `stop_head_tracking` that has begun stopping the detector finishes releasing it before the cancel propagates, while a `set_face_detection(True)` cancelled during its detector's build leaves the switch off and discards the build. What outlives the call is a mode, undone by its counterpart verb (`set_motors_state("disabled")`, `stop_head_tracking`, …), not by the cancel.
- **Bring-up, `start()`**, is the one spanning verb whose cancel waits: the step in flight (a daemon spawn, the robot connect, a media start) cannot be interrupted in its thread, so it finishes and is undone before the `CancelledError` propagates — up to `daemon.startup_timeout` for a spawn. An `asyncio.timeout` around the `async with` therefore bounds the *start* of the cleanup, not its end: budget for the step when you set one.

What runs together, and who wins:

| | Behaviour |
|---|---|
| `play_emotion` + `play_emotion` | queued in order, never overlapped |
| `say` + `say` | the newest wins; the older raises `SpeechInterruptedError` |
| `play_sound` + `play_sound`, or an emotion's sound | the newest file wins; the replaced call raises `SoundInterruptedError`; a replaced emotion plays on silent |
| `say` + `play_sound` | both play, mixed at the speaker |
| `say` + `play_emotion` | both run; wobbling is paused for the emotion, so speech does not sway the head while it plays |
| tracking + idle move | composed: the head looks at the face and breathes (`focus` holds it still on the face) |
| tracking + emotion | the emotion plays as recorded; the gaze fades back in after it |
| a mode verb + the same mode verb | one at a time per mode; the last call's value stands |

**Blocking SDK calls run off the event loop** (`asyncio.to_thread`), and the motion loop's 60 Hz target stream runs on its own thread, so moving never stalls audio. A detector's `detect` runs on a worker thread, one call at a time.

## Units

| Value | Unit |
|---|---|
| Motion offsets (`IdleOffsets`) | millimetres (`z_mm`) and degrees (`roll_deg`, `pitch_deg`, `yaw_deg`, `antenna_*_deg`) |
| A face's position (`Face.x`, `Face.y`) | normalised image coordinates in [-1, 1]; `size` a fraction of the frame height |
| A face's box (`Face.bbox`, `PixelFace.bbox`) | pixels of the full camera frame |
| A face's head angles (`roll`, `pitch`, `yaw`; `PixelFace.orientation`) | **radians**, as seen from the camera |
| A head pose (`CameraFrame.head_pose`, `FaceReport.head_pose`) | a 4×4 matrix in metres, world frame (x forward, y left, z up) |
| Times (`ts`) | seconds on the bridge's monotonic clock (`time.monotonic()`) |
| Camera image | `uint8` BGR, H×W×3 |
| Mic chunks (`audio_input`) | `bytes` of int16 little-endian PCM, mono by default, at `mic_sample_rate` (16 kHz) |
| Synthesizer output (`SpeechSynthesizer.stream`) | `float32` mono numpy in [-1, 1], shape `(n,)`, at `synth.sample_rate` |

## Extension contracts

Three seams take your code, each with a complete program that runs on the fake: a synthesizer in [../getting-started.md](../getting-started.md), a detector in [../guides/custom-face-detector.md](../guides/custom-face-detector.md#a-complete-program), an idle move in [../guides/custom-idle-move.md](../guides/custom-idle-move.md).

**A voice — `SpeechSynthesizer`** ([specs/audio/audio.md](../../specs/audio/audio.md)). Any object with a `sample_rate` (int, Hz, fixed) and a `stream(text)` returning an async iterator of float32 mono chunks in [-1, 1], shape `(n,)`. Pass it to `ReachyMiniBridge(config, synthesizer=...)` as the default voice, or per call to `say(text, synth)`. An engine that emits int16 converts with `int16_to_float32`. The bridge resamples to the speaker's rate; your rate is yours.

**A detector — `FaceDetector`** ([specs/vision/user_perception.md](../../specs/vision/user_perception.md) "Custom detectors"). Any object with `detect(frame_bgr, ts) -> Sequence[PixelFace]`, called once per new frame on a worker thread, never concurrently; an optional `close()`. Registered as a zero-argument **factory** (a class is one) through `FaceDetectionSettings(face_detector=...)` or `set_face_detector(...)`, with `face_detection.detector: "custom"`; the loop builds a fresh detector from it at every detection start. Faces come back in pixels of the frame given, unsmoothed; the frame is shared and read-only.

**An idle move — `IdleMove`** ([specs/motion/motion.md](../../specs/motion/motion.md) "Custom idle moves"). Subclass it and implement `offsets(t) -> IdleOffsets`, the pose as offsets from neutral `t` seconds into the idle entry; optionally `gaze_offsets(t)`, the motion around the tracked face (neutral by default: the head sits on the aim). Registered as a factory through `MotionSettings(idle="custom", idle_move=...)` or `set_idle_move(...)`, built fresh at every idle entry so `t` starts at 0. `offsets` runs at 60 Hz on the motion thread: fast, pure, continuous, starting at rest (zero velocity at `t = 0`); every value finite. A move that raises or returns a bad value costs one warning and the hold until another is registered.

## The escape hatch

`bridge.robot` is the upstream `reachy_mini.ReachyMini` on `real` and `sim` (the `FakeReachyMini` on `fake`), while the session runs. Everything upstream offers is there, with two things to know:

- **Two writers of the head fight.** The motion loop is the one writer of the robot's target pose at 60 Hz. Upstream's `set_target`, `goto_target`, `look_at_*`, `play_move`, `wake_up` and `goto_sleep` write it too, and a daemon-side move blocks every target for its duration. To drive the head yourself, turn presence off (`set_presence(False)`): the loop then commands nothing between verbs, and the head is yours until the next `play_emotion`.
- **The camera has one reader.** Upstream hands each frame out once: `robot.media.get_frame()` beside `bridge.camera` steals frames from the detector and every other consumer, silently. Read frames from `bridge.camera.latest()`.
- **Do not restart the media pipeline.** `robot.media.stop_recording()` / `start_recording()` mid-session reopens the audio on the system's default devices on macOS, so speech and the mic silently leave the robot ([../internals/upstream-sdk-notes.md](../internals/upstream-sdk-notes.md)). The bridge opens the pipeline once per session.
- **Upstream's own face tracking** (`robot.start_head_tracking`) is never armed by the bridge; arming it yourself makes the daemon discard the client's head target.
