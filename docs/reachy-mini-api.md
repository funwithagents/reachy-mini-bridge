# Upstream `reachy_mini` API — reference notes

Reference notes on the upstream [`pollen-robotics/reachy_mini`](https://github.com/pollen-robotics/reachy_mini) SDK — the package this project bridges. It's background for designing [`client.md`](../specs/client.md) / [`api.md`](../specs/api.md) / [`tools.md`](../specs/tools.md), **not** a spec: nothing here is a commitment about *our* code, and it can lag upstream. Verify against the installed version before relying on a detail — this reflects `main` as read on 2026-09-04.

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

- **Client ↔ daemon architecture.** `ReachyMini` is a *client*; it talks to a **daemon** that owns the hardware (motors, camera, mic, speaker). The daemon runs on the robot (wireless) or locally (Lite / `spawn_daemon`), or is a **mockup/MuJoCo simulation** (`use_sim=True`). This is why the SDK needs a running daemon and can't be exercised headless in unit tests — the seam in [`client.md`](../specs/client.md) exists to mock it.
- **Heavy native deps.** GStreamer-based media, HF-hub downloads for emotions, numpy/scipy. Another reason our deterministic `tests/` tier must not import it.

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
  - **Native mic format (per SDK source, this checkout):** `get_audio_sample()` returns a **`float32` numpy array, interleaved, `get_input_channels()` channels — `2` (stereo) in the GStreamer backend** (`AudioBase.get_audio_sample` → `np.frombuffer(sample, np.float32).reshape(-1, 2)`; the appsink caps are `F32LE, channels=2, rate=16000`), at the rate from `get_input_audio_samplerate()` = **16 kHz**. The conversation app's `record_loop` forwards this frame as-is; its own streaming handler does any mono downmix. A consumer wanting 16 kHz / mono / linear16 (the streaming-ASR norm) applies a **stereo→mono downmix and float32→int16 conversion** (no resample — the rate already matches), driven off the getters rather than hardcoded assumptions.
- IMU: `mini.imu()` → dict of accel/gyro/etc.
- Face tracking (daemon-side): `start_head_tracking(weight=1.0)` / `stop_head_tracking()` / `get_tracked_face(...)`.

### Audio out
- `media.play_sound("file.wav")` — plays a file through the daemon speaker.
- `media.push_audio_sample(np.float32[...])` / `start_playing()` / `stop_playing()` — stream raw samples to the speaker.
  - **Native output format (per SDK source, this checkout):** `push_audio_sample` expects a **`float32`** numpy array normalized to `[-1, 1]`, at the **16 kHz** pipeline rate, with no rate argument. The rate and channel count are queryable: `get_output_audio_samplerate()` returns 16 kHz and `get_output_channels()` returns `2`. The expected **channel count is version-dependent** — this checkout's playback appsrc caps are `F32LE, channels=2` (stereo interleaved), while `reachy_mini_conversation_app`'s `play_loop` downmixes to **mono** before pushing. Read `get_output_channels()` and match it (duplicating a mono synth stream when it reports 2). A synthesizer at 16 kHz needs no resample, only the dtype/channel match.
- `enable_wobbling()` / `disable_wobbling()` — audio-reactive head motion synced to whatever audio is playing.
- **No text-to-speech upstream** — that's why our `say` uses our own `tts-engine` (see [`api.md`](../specs/api.md)). Note `tts-engine` streams to a local device, so routing TTS through the robot speaker means feeding `push_audio_sample`.

### Motors / lifecycle
- `enable_motors(ids=None)` / `disable_motors(ids=None)`, `enable_gravity_compensation()` / `disable_gravity_compensation()`.
- **Gotcha:** `enable_motors()` pins all targets to the *present* pose before enabling torque — so `set_target(X); enable_motors()` will **not** drive to `X`. Call `set_target` *after* `enable_motors`.
- **The daemon lifecycle moves the motor state — a fresh daemon boots with motors *enabled*.** With `wake_up_on_start=True` (the daemon default, and what a `--sim`/local daemon uses) the **boot sequence** runs `set_motor_control_mode(Enabled)` and *then* the wake-up emote — so a freshly-started daemon reports `motor_control_mode == "enabled"` (verified against a fresh `--sim` daemon: connect and read `get_status().backend_status.motor_control_mode`). Symmetrically, `goto_sleep()` (run on graceful stop when `goto_sleep_on_stop=True`, the default) **disables** the motors at its end. Note the enable/disable are *not* inside `wake_up()`/`goto_sleep()` — those are the `goto_target` + `play_sound` animations; the enable is the boot step just before `wake_up()`, and the disable is `goto_sleep()`'s final line. A **wireless** robot instead boots **asleep** (wakes on a button/REST press), so it starts disabled. Net: the motor mode a client observes is set by the **daemon's** wake/sleep lifecycle, not by connecting — read it, don't assume a default. (An idle-timeout can also `goto_sleep` mid-session on a shared robot, disabling torque under you — another reason to read.)
- `set_automatic_body_yaw(bool)`, `cancel_move()`, media `release_media()` / `acquire_media()` (hand camera/mic to direct OpenCV/sounddevice access).

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
