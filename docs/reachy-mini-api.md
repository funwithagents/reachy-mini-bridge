# Upstream `reachy_mini` API — reference notes

Reference notes on the upstream [`pollen-robotics/reachy_mini`](https://github.com/pollen-robotics/reachy_mini) SDK — the package this project bridges. It's background for designing [`robot.md`](../specs/robot.md) / [`api.md`](../specs/api.md) / [`tools.md`](../specs/tools.md), **not** a spec: nothing here is a commitment about *our* code, and it can lag upstream. Verify against the installed version before relying on a detail — this reflects `main` as read on 2026-09-04.

## The shape of it

- **One public class: `reachy_mini.ReachyMini`** (plus `ReachyMiniApp` for daemon-hosted apps). It is **synchronous** and **context-managed**:

  ```python
  from reachy_mini import ReachyMini
  from reachy_mini.utils import create_head_pose

  with ReachyMini() as mini:
      mini.goto_target(
          head=create_head_pose(z=10, roll=15, degrees=True, mm=True),
          duration=1.0,
      )
  ```

- **Client ↔ daemon architecture.** `ReachyMini` is a *client*; it talks to a **daemon** that owns the hardware (motors, camera, mic, speaker). The daemon runs on the robot (wireless) or locally (Lite / `spawn_daemon`), or is a **mockup/MuJoCo simulation** (`use_sim=True`). This is why the SDK needs a running daemon and can't be exercised headless in unit tests — the seam in [`robot.md`](../specs/robot.md) exists to mock it.
- **Heavy native deps.** GStreamer-based media, HF-hub downloads for emotions, numpy/scipy. Importing it needs those native libs installed (not a live daemon), so it is a base dependency the `tests/` tier imports normally; the `FakeReachyMini` stand-in is what keeps that tier daemon-free ([robot.md](../specs/robot.md)).

### Constructor (key args)

`ReachyMini(robot_name="reachy_mini", host="reachy-mini.local", port=8000, connection_mode="auto", spawn_daemon=False, use_sim=False, timeout=5.0, automatic_body_yaw=True, media_backend="default", ...)`

- `connection_mode`: `"auto"` (localhost, then network) / `"localhost_only"` / `"network"`.
- `media_backend`: `"default"`/`"local"` (same machine as daemon), `"webrtc"` (remote client), `"no_media"` (headless).

## Coordinate & unit conventions (important)

- **Head pose is a 4×4 homogeneous matrix** in the world frame. Build it with `reachy_mini.utils.create_head_pose(x, y, z, roll, pitch, yaw, mm=False, degrees=True)`. Rotation is `scipy` Euler `"xyz"`. `mm=True` treats position as millimetres.
- **World frame**: origin at the neutral head position, **x forward, y left, z up**.
- **Angles at the raw SDK level are radians**; `create_head_pose` accepts degrees and converts. Antennas are `[right, left]` in radians. `body_yaw` in radians.
- **Our [`api.md`](../specs/api.md) speaks degrees/seconds/named-emotions** and does this conversion internally — callers of the bridge shouldn't touch matrices.

## Capability map (what we wrap)

### Motion
- `set_target(head=4x4, antennas=[r,l], body_yaw=float)` — **immediate**, no smoothing; for real-time control loops (≥10 Hz). At least one arg required.
- `goto_target(head=None, antennas=None, duration=0.5, method=MIN_JERK, body_yaw=0.0)` — **smooth** task-space interpolation over `duration`; **blocking** (waits for completion). `method` ∈ `linear`/`minjerk`/`ease_in_out`/`cartoon`. `body_yaw=None` keeps current yaw.
- Semantic helpers already in the SDK:
  - `look_at_image(u, v, duration=1.0, perform_movement=True)` — gaze at a camera pixel; needs a calibrated camera. Returns the computed 4×4 pose.
  - `look_at_world(x, y, z, duration=1.0, perform_movement=True)` — gaze at a 3D point (metres, world frame). Returns the computed 4×4 pose.
  - `wake_up()` / `goto_sleep()` — canned wake/sleep emotes (+ sounds).
- Joint-space state: `get_current_joint_positions()` → `(head[7], antennas[2])` radians; `get_current_head_pose()` → 4×4; `get_present_antenna_joint_positions()`.

### Expression
- **Recorded moves** (emotions) come from **Hugging Face datasets**:
  ```python
  from reachy_mini.motion.recorded_move import RecordedMoves

  moves = RecordedMoves("pollen-robotics/reachy-mini-emotions-library")
  moves.list_moves()  # -> ["happy", ...]
  mini.play_move(moves.get("happy"), initial_goto_duration=1.0)
  ```
  Loading hits the network / HF hub at call time.
- Antennas double as **buttons** (readable) as well as motors.

### Perception
- Camera: `mini.media.get_frame()` → numpy BGR array; `get_frame_jpeg()` → bytes.
- Audio in: `media.get_audio_sample()`, `get_input_audio_samplerate()`, `get_input_channels()`; **direction of arrival** `media.get_DoA()` → `(angle, ok)`.
  - **Native mic format (SDK source, confirmed on a USB Lite):** `get_audio_sample()` returns a **`float32` numpy array, interleaved, `get_input_channels()` channels — `2` (stereo) in the GStreamer backend** (`AudioBase.get_audio_sample` → `np.frombuffer(sample, np.float32).reshape(-1, 2)`; the appsink caps are `F32LE, channels=2, rate=16000`), at the rate from `get_input_audio_samplerate()` = **16 kHz**. On hardware each sample is a 10 ms `(160, 2)` block. The conversation app's `record_loop` forwards this frame as-is; its own streaming handler does any mono downmix. A consumer wanting 16 kHz / mono / linear16 (the streaming-ASR norm) applies a **stereo→mono downmix and float32→int16 conversion** (no resample — the rate already matches), driven off the getters rather than hardcoded assumptions.
- IMU: `mini.imu()` → dict of accel/gyro/etc.
- Face tracking (daemon-side): `start_head_tracking(weight=1.0)` / `stop_head_tracking()` / `get_tracked_face(...)`. **Not wired in the MuJoCo backend** (1.10): `step_head_tracking()` — the half that drains the detector's observations and latches the aim — is called only from `RobotBackend`'s loop, never from `MujocoBackend.run()`, so on a stock sim the detector runs but `get_tracked_face()` never reports a face and the head never follows. And the tracker's intrinsics are wrong in the sim: `FaceTracker` derives its matrix with `intrinsics_for_size`, which rescales every `K` from the robot's 3840×2592 sensor calibration, but `MujocoCameraSpecs.K` is a 1280×720 matrix (itself a ~53° field of view where the MJCF `eye_camera` has `fovy` 80°) — at the tracker's 320×180 the principal point lands at (53, 25) instead of (160, 90), so the head converges ~45° off the face, and a re-engage from a small weight freezes on an aim IK cannot reach. `GenericWebcamSpecs` (mockup-sim) is mis-scaled the same way. The bridge's sim daemon launcher corrects both gaps ([../specs/sim_daemon.md](../specs/sim_daemon.md)).

### Audio out
- `media.play_sound("file.wav")` — plays a file through the daemon speaker.
- `media.push_audio_sample(np.float32[...])` / `start_playing()` / `stop_playing()` — stream raw samples to the speaker.
  - **Native output format (SDK source, confirmed on a USB Lite):** `push_audio_sample` expects a **`float32`** numpy array normalized to `[-1, 1]`, at the **16 kHz** pipeline rate, with no rate argument. The rate and channel count are queryable: `get_output_audio_samplerate()` returns 16 kHz and `get_output_channels()` returns `2`. The expected **channel count is version-dependent** — this checkout's playback appsrc caps are `F32LE, channels=2` (stereo interleaved), while `reachy_mini_conversation_app`'s `play_loop` downmixes to **mono** before pushing. Read `get_output_channels()` and match it (duplicating a mono synth stream when it reports 2). A synthesizer at 16 kHz needs no resample, only the dtype/channel match.
- `enable_wobbling()` / `disable_wobbling()` — audio-reactive head motion synced to whatever audio is playing. Off by default; no getter. How it works (SDK 1.10 source):
  - **Two halves.** `enable_wobbling()` arms an SDK-side `HeadWobbler` on the client's local GStreamer playback (local media backend only; a warning and no-op on `webrtc`) *and* sends `SetWobblingCmd` so the daemon's media server wobbles its own sounds (wake-up/sleep, incoming WebRTC audio). The daemon's `goto_sleep` / `reset_to_sleep` quiesce the daemon-side half (`_quiesce_aim_sources`) without telling the client.
  - **Audio tap.** Every playback path (`push_audio_sample` and `play_sound`) ends in a `tee`: one branch to the speaker (EQ after the tee), one to an appsink with `sync=True` that fires when the buffer is *heard*, feeding `HeadWobbler.feed(pcm, now)`.
  - **Analysis** (`reachy_mini/motion/speech_tapper.py`, `SwayRollRT`): 50 ms hops; RMS dBFS; VAD with hysteresis (on ≥ −35 dBFS, off after 250 ms ≤ −45 dBFS); envelope (attack ~1 hop, release ~250 ms, follow gain 0.65); loudness gain mapping [−46, −18] dBFS → [0, 1] (+4 dB offset, γ 0.9) × `SWAY_MASTER = 1.5`; six free-running sine oscillators scaled by loudness × envelope — pitch 2.2 Hz / 4.5°, yaw 0.6 Hz / 7.5°, roll 1.3 Hz / 2.25°, x/y/z 0.35/0.45/0.25 Hz / 4.5/3.75/2.25 mm (base amplitudes, ×1.5 max). Phases from a fixed seed (7). The motion tracks loudness, not syllables.
  - **Scheduling.** `HeadWobbler.feed` schedules one `GLib.timeout_add` per hop at its playback instant (stale hops dropped; a generation counter voids pending hops on `stop`/`reset`), sending `SetSpeechOffsetsCmd` — 20 Hz steps, no smoothing beyond the envelope and the motors.
  - **Application.** `AbstractBackend.set_speech_offsets` stores the six values and sets `ik_required`; `update_target_head_joints_from_ik` composes them with `compose_world_offset` (translation added in world axes, rotation about the head's own origin) *after* the face-tracking blend and *before* IK — so the sway rides on top of any held pose, `goto`, recorded move or tracking aim.
  - **Reset to neutral** on `stop_playing`, `clear_player` (wobbler `reset`), `disable_wobbling`, EOS of a `play_sound` file, and the daemon's sleep paths. The push path has no EOS, so an abruptly stopped push stream that is not flushed can leave the last hop's offset applied until the next audio.
- **Device selection, and the macOS restart gotcha** (SDK 1.10, measured on macOS 26 / GStreamer 1.28.6 with a USB Lite). `GStreamerAudio` finds the card named "Reachy Mini Audio" through `Gst.DeviceMonitor` and, on macOS, sets it as `unique-id` on `osxaudiosink` / `osxaudiosrc` once, in its constructor — falling back to the default devices when no such card is present. That happens regardless of sim vs real, so a sim daemon and its clients use a plugged-in robot's speaker and mic. Recording and playback live in **one** pipeline, so `stop_recording()` and `stop_playing()` both take the whole pipeline to `NULL`. After that, the next `start_recording()` / `start_playing()` opens both elements on the **system default devices** (CoreAudio IDs 91 → 80 for the speaker, 91 → 75 for the mic): the `unique-id` is still set but is no longer resolved. Setting `unique-id` again while the pipeline is `NULL`, before starting it, keeps the robot's card. `play_sound` is unaffected — it builds a fresh sink on every call.
- **No text-to-speech upstream** — that's why our `say` uses our own `tts-engine` (see [`api.md`](../specs/api.md)). Note `tts-engine` streams to a local device, so routing TTS through the robot speaker means feeding `push_audio_sample`.

### Motors / lifecycle
- `enable_motors(ids=None)` / `disable_motors(ids=None)`, `enable_gravity_compensation()` / `disable_gravity_compensation()`.
- **Gotcha:** `enable_motors()` pins all targets to the *present* pose before enabling torque — so `set_target(X); enable_motors()` will **not** drive to `X`. Call `set_target` *after* `enable_motors`.
- **The daemon lifecycle moves the motor state — a fresh daemon boots with motors *enabled*.** With `wake_up_on_start=True` (the daemon default, and what a `--sim`/local daemon uses) the **boot sequence** runs `set_motor_control_mode(Enabled)` and *then* the wake-up emote — so a freshly-started daemon reports `motor_control_mode == "enabled"` (verified against a fresh `--sim` daemon: connect and read `get_status().backend_status.motor_control_mode`). Symmetrically, `goto_sleep()` (run on graceful stop when `goto_sleep_on_stop=True`, the default) **disables** the motors at its end. Note the enable/disable are *not* inside `wake_up()`/`goto_sleep()` — those are the `goto_target` + `play_sound` animations; the enable is the boot step just before `wake_up()`, and the disable is `goto_sleep()`'s final line. A **wireless** robot instead boots **asleep** (wakes on a button/REST press), so it starts disabled. Net: the motor mode a client observes is set by the **daemon's** wake/sleep lifecycle, not by connecting — read it, don't assume a default. (An idle-timeout can also `goto_sleep` mid-session on a shared robot, disabling torque under you — another reason to read.)
- `set_automatic_body_yaw(bool)`, `cancel_move()`, media `release_media()` / `acquire_media()` (hand camera/mic to direct OpenCV/sounddevice access).
- **Cancelling a move** (SDK 1.10, verified on the sim and a USB Lite). `async_play_move` starts the move's sidecar sound fire-and-forget (`media_manager.play_sound`) and then ticks the trajectory with `await asyncio.sleep`; it has no cancellation handling. Cancelling the awaiting task stops the trajectory within a tick (the head stays at its last target) but the sound plays to the end of the file — and with wobbling on, the wobbler that `play_sound` armed keeps swaying the head to it, so a cancelled emotion looks like it is still being performed. Nothing public stops just that sound: `clear_player()` flushes only the push `appsrc`, the daemon's `POST /api/media/stop_sound` stops the daemon's *own* player (wake/sleep sounds, WebRTC uploads), and `cancel_move()` calls `stop_playing()`, which takes the **shared** record + playback pipeline to `NULL` — capture returns `None` from then on and pushes are dropped with a `GST_FLOW_FLUSHING` warning until a restart (which on macOS hits the device gotcha above). On the local backend the sound runs on a `playbin` kept as `media.audio._playbin`; setting it to `NULL` stops the sound and leaves the pipeline intact (that is `MediaServer.stop_sound()`'s body on the daemon side). What the bridge does: [../specs/audio.md](../specs/audio.md) "Stopping a sound file".

### Driving the target pose — one writer at a time (SDK 1.10, read from the daemon source)

What the bridge's motion loop ([../specs/motion.md](../specs/motion.md)) is built around:

- **A daemon-side move blocks every `set_target`.** `goto_target`, `wake_up`, `goto_sleep` and the daemon's own `play_move` take a move guard (`_try_start_move` / `_end_move` in the abstract backend) for the move's duration; while it is held, `process_command` **ignores** every `SetTargetCmd` / `SetFullTargetCmd` / `SetHeadJointsCmd` / `SetBodyYawCmd` / `SetAntennasCmd` from *any* client, logging `Ignoring set_target command: a move is currently running.` A control loop streaming targets therefore must not call those helpers mid-session; it interpolates its own transitions (upstream's `GotoMove` from `reachy_mini.motion.goto` is the reusable piece).
- **The client-side `async_play_move` is its own writer.** It evaluates the move at `play_frequency` (default **100 Hz**) and sends per-component targets, with the sidecar sound started fire-and-forget just before the loop. Two writers fight, so a loop that owns `set_target` evaluates recorded moves itself.
- **Composition order in the daemon** (`update_target_head_joints_from_ik`): the client's target head pose → blended toward the face-tracking aim by the tracking weight → speech (wobble) offsets composed on top → IK. At tracking weight **≥ 1.0** `set_target_head_pose` returns before flagging IK, so the head component of a `set_target` is **discarded** and only antennas / body yaw apply; below 1.0 the target shows through the blend. A lost face decays the aim back to `INIT_HEAD_POSE` after 2 s. **The daemon applies the requested weight only at a face detection** (`set_tracking_face` copies `_tracking_requested_weight` into `_tracking_weight`; until the first detection the weight is `0` and the client's head target flows) **and keeps it after the face is lost**: the lost-face recentre moves the aim to neutral but leaves the weight at `1.0` and the aim set, so head components stay discarded — the head sits dead at neutral — until tracking is re-armed at weight `0` (`enable_head_tracking(0.0)` → `clear_tracking_aim()`, which also pauses the detector thread) or stopped. A lower non-zero request alone changes nothing until the next detection. `get_tracked_face()` returns the `face_target` of the last status message (`get_status(wait=False)` returns it without a round trip, but still clears the client's `status_received` event). Bridge-side handling: [../specs/api.md](../specs/api.md) "Attention".
- **Neutral constants** live in `reachy_mini.reachy_mini` (not `reachy_mini.utils`): `INIT_HEAD_POSE = np.eye(4)`, `INIT_ANTENNAS_JOINT_POSITIONS = [-0.1745, 0.1745]` ("~10° offset to reduce shaking at vertical"), plus the `SLEEP_*` pair. `wake_up()` ends at exactly that pose. Body yaw is the first of the seven head joints returned by `get_current_joint_positions()`.
- **The daemon's idle reset does not concern SDK clients.** `request_idle_reset()` (a debounced `reset_to_sleep`, 1.5 s grace, 15 s on a handoff) fires when the `RobotAppLock`'s managed app slot becomes free — a WebRTC session ending or an `AppManager` local app exiting. Per the lock's own docstring, "SDK clients talking to the daemon directly over LAN/WebSocket bypass it entirely", so a plain `ReachyMini` client disconnecting triggers nothing; only a daemon *stop* (`goto_sleep_on_stop`) parks the robot.
- **Breathing at ~100 Hz shivers.** The conversation app's breathing (5 mm z sine at 0.1 Hz, ±15° counter-phase antennas at 0.5 Hz, from its `moves.py`) driven through `set_target` at ~100 Hz produces visible micro-vibrations of the Stewart platform and a slow downward drift (upstream issue, open; zero vibration when the pose is held via a single `goto_target`). Its manager now ticks at 60 Hz; the fix is not settled upstream.

## Safety limits (SDK auto-clamps)

| Joint | Range |
|---|---|
| Head pitch / roll | ±40° |
| Head yaw | ±180° |
| Body yaw | ±160° |
| Head-body yaw delta | ≤ 65° (ship head + body yaw in one call to coordinate) |

## Motion philosophy in one line

`goto_target()` for gestures ≥ ~0.5 s (smooth, blocking); `set_target()` inside real-time loops (immediate, non-blocking). See the `motion-philosophy` skill below.

## Alternate transport

The daemon also exposes a **REST API** at `http://<daemon>:8000/api` (interactive docs at `/docs`) and a JS/WebRTC SDK. We standardise on the Python client; REST is a fallback worth knowing exists.

## Upstream "skills" worth reading

The upstream repo ships agent-oriented skill docs under [`skills/`](https://github.com/pollen-robotics/reachy_mini/tree/main/skills). Most relevant to this bridge, roughly in priority order:

| Skill | Why read it |
|---|---|
| [`ai-integration.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/ai-integration.md) | LLM-agent control of the robot — most directly relevant to [`tools.md`](../specs/tools.md). |
| [`interaction-patterns.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/interaction-patterns.md) | Designing how users interact with the robot — informs [`api.md`](../specs/api.md)'s verbs. |
| [`motion-philosophy.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/motion-philosophy.md) | `goto_target` vs `set_target` — the decision behind our movement verbs. |
| [`symbolic-motion.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/symbolic-motion.md) | Rhythmic/repetitive motion (nod, sway, dance) — reference for composed gestures. |
| [`control-loops.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/control-loops.md) | Real-time reactivity (face tracking, games) — relevant if perception verbs go live/streaming. |
| [`safe-torque.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/safe-torque.md) | Motor modes & safe torque handling — the `enable_motors` gotcha above. |
| [`create-app.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/create-app.md) | `ReachyMiniApp` structure — relevant only if we ever ship a daemon-hosted app. |
| [`debugging.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/debugging.md) | Diagnosing misbehaving apps. |
| [`testing-apps.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/testing-apps.md) | Upstream's app-testing approach (note: ours is specced in [`testing.md`](../specs/testing.md)). |
| [`setup-environment.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/setup-environment.md) | Installing/connecting for the first time. |
| [`deep-dive-docs.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/deep-dive-docs.md) | Index into the fuller upstream docs when a detail is missing here. |
| [`rest-api.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/rest-api.md) · [`create-js-app.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/skills/create-js-app.md) | Web/JS transports — lower priority; Python-only for now. |

Fuller SDK docs also live at <https://huggingface.co/docs/reachy_mini> and the upstream [`AGENTS.md`](https://github.com/pollen-robotics/reachy_mini/blob/main/AGENTS.md).
