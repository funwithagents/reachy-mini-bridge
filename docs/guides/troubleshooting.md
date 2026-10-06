# Troubleshooting — when it does not connect, see, move or speak

The checks for each symptom, in the order to make them, each pointing at the page that owns the explanation. The errors themselves are listed in [../reference/api.md](../reference/api.md#errors).

## It does not connect — `start()` raises or hangs

- **Which daemon is it talking to?** `daemon.spawn: "never"` needs a daemon already serving at `robot.host:port`; `"auto"` borrows one that is ready there and otherwise starts one — the MuJoCo sim, or a Lite's hardware daemon over USB; a managed daemon lives on `127.0.0.1` / `localhost` only ([../reference/configuration.md](../reference/configuration.md#daemon--whether-the-bridge-starts-one)).
- **Ready, not merely listening.** A daemon answers HTTP before its backend runs; `GET /api/daemon/status` with a non-null `backend_status` is readiness. A managed daemon that never gets there is a `DaemonError` after `daemon.startup_timeout` ([running-daemons.md](running-daemons.md#gotchas)).
- **A daemon you started by hand** is reached in network mode with the local media backend; the bridge fills both in for a daemon it manages ([running-daemons.md](running-daemons.md#connecting-the-client)).
- **Nothing at the address and nothing to spawn**: upstream's own connection error propagates from `start()` as the SDK raises it ([../reference/api.md](../reference/api.md#errors)).
- **Two media daemons on one machine** share one UDP port and one camera socket whatever their HTTP ports: run one at a time ([../reference/backends-and-capabilities.md](../reference/backends-and-capabilities.md#one-local-media-daemon-at-a-time)).
- **Linux**: a daemon on the machine needs the system GStreamer and the Rust webrtc plugin, which no distribution packages ([linux.md](linux.md)).
- **`start()` outlives your timeout**: a cancelled bring-up waits for the step in flight — a spawn up to `daemon.startup_timeout` — before it propagates ([../reference/api.md](../reference/api.md#cancellation-and-concurrency)).

## No camera frame — `bridge.camera.latest()` stays `None`

- **Give it a moment.** The first frame of a fresh session can take a couple of seconds on a live daemon ([../getting-started.md](../getting-started.md#waiting-for-what-you-need)).
- **A headless sim on macOS has no camera** — no GL context without a window; on Linux it renders offscreen through Mesa's EGL ([../reference/backends-and-capabilities.md](../reference/backends-and-capabilities.md#the-matrix), [linux.md](linux.md)).
- **Permissions (macOS)**: a webcam feeding the sim needs camera access for the process that starts the daemon; a Lite's camera, for the process running the bridge ([../reference/backends-and-capabilities.md](../reference/backends-and-capabilities.md#what-each-setup-needs-installed)).
- **The wrong device opened (macOS, a robot)**: upstream opens the camera by an index that can shift; the bridge's hardware-daemon launcher reads back which device opened and rebuilds the pipeline until it is the robot's — its log says so ([../../specs/daemon/real_daemon.md](../../specs/daemon/real_daemon.md)).
- **Something else reads the camera.** Upstream's `get_frame()` has one reader; a second one beside the bridge's feed starves it silently. Sample `bridge.camera` instead ([perception-and-tracking.md](perception-and-tracking.md#beside-a-vision-graph)).

## `faces.value.active` is `False`

- **Inactive means no detector is looking, not that nobody is there** ([perception-and-tracking.md](perception-and-tracking.md#what-the-values-promise-and-what-they-do-not)).
- **Is a detector named, and a switch on?** `face_detection.detector` alone starts nothing: `face_detection.enabled` or `motion.tracking` does, or `set_face_detection(True)` / `start_head_tracking()` at run time ([../reference/configuration.md](../reference/configuration.md#face_detection--who-is-in-front-of-the-robot)).
- **No camera frame for five seconds** turns the report inactive, with one warning in the log; it is active again on the next frame. See the camera checks above.
- **The first `yunet` run downloads the weights** into the Hugging Face cache: it needs the network once.
- **A `custom` detector** with no factory registered refuses session entry with a `ValueError`; one that raises on every frame turns the report inactive after a few seconds ([custom-face-detector.md](custom-face-detector.md)).

## Nothing moves

- **Torque.** `get_motors_state()` must read `"enabled"`: `play_emotion` raises `MotorsNotEnabledError` otherwise, and the modes — breathing, tracking — hold without moving anything until it is ([../reference/api.md](../reference/api.md#verbs)).
- **Presence.** With `set_presence(False)` the motion loop commands nothing between verbs, the gaze included; `motion.presence` is on by default ([../reference/api.md](../reference/api.md#verbs)).
- **Tracking on, head still.** It needs torque and presence, and a face: `bridge.attention` reads `"watching"` while nobody is seen and `"engaged"` once the head follows someone ([perception-and-tracking.md](perception-and-tracking.md#whom-the-head-follows)).
- **A daemon-side move is running.** Upstream's `play_move`, `wake_up` and `goto_sleep` block every target for their duration, and two writers of the head fight ([../reference/api.md](../reference/api.md#the-escape-hatch)).
- **A wireless robot boots asleep** with its motors disabled ([../reference/backends-and-capabilities.md](../reference/backends-and-capabilities.md#the-matrix)).
- **On the fake nothing moves by design**: the commands are recorded on `bridge.robot` ([../getting-started.md](../getting-started.md#your-first-application)).

## No voice, no sound

- **`say` raises `BridgeError` with no voice**: the config's `tts` block failed to build — the `tts-*` extra not installed, the key unset — and the cause is on `bridge.synthesizer_error` right after construction ([audio.md](audio.md#a-voice-for-say)).
- **`play_sound` raises `FileNotFoundError`** when the name is neither a file on this machine nor one of the SDK's built-in sounds, `ValueError` when the file's duration cannot be read ([audio.md](audio.md#sound-files)).
- **A Linux host without a sound card** brings the daemon's audio up unavailable; a PulseAudio null sink gives it a device ([audio.md](audio.md#sound-on-a-host-without-a-sound-card)).
- **Cut short**: a newer `say` or sound file replaced it — `SpeechInterruptedError`, `SoundInterruptedError` ([audio.md](audio.md#what-say-guarantees)).
- **The head does not sway while it speaks**: wobbling is off, or paused for an emotion ([audio.md](audio.md#head-wobbling)).
- **On the fake nothing is heard**: `say` and `play_sound` still take their duration, and record what they would have played.
