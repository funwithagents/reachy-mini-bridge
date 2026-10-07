# Backends and capabilities

What each way of running the bridge needs, what it gives you, and how far it has been validated. One matrix; every other page links here rather than repeating it. The probes behind the capability names are the testing harness's ([specs/testing/testing.md](../../specs/testing/testing.md) "Capabilities are probed, not assumed").

## The three backends

| Backend | What it drives | Needs |
|---|---|---|
| `real` *(default)* | The physical robot through its daemon | The robot reachable at `robot.host:port`. For a robot plugged in over USB (a Reachy Mini Lite), the bridge spawns the hardware daemon for you (`daemon.spawn: "auto"`); a wireless robot runs its own, which the bridge connects to |
| `sim` | Upstream's MuJoCo simulation, run through the bridge's own launcher | The `sim` extra (`reachy-mini-bridge[sim]`); a daemon you run, or one the bridge spawns for you |
| `fake` | A first-party in-process stand-in that records every command and returns synthetic audio and frames | Nothing — offline and deterministic; it powers the bridge's unit tests and yours |

The same code runs on all three; one config field, `backend`, moves between the robot and the simulator, and the `fake` — which has no daemon — takes the same file once `daemon.spawn` is `never` or the block is absent ([configuration.md](configuration.md#backend)). The `fake` keeps real timing on the verbs that span time (`say`, `play_sound`, `play_emotion`), so turn-taking logic tested against it behaves as on a robot.

## The matrix

A **capability** is something the running target actually provides, probed at session start by the test harness and inferred from nothing. The columns:

- `motion` — the daemon reports a status and takes targets. `audio` — a microphone sample arrives on the open media session, and the speaker plays. `camera` — a frame arrives on `bridge.camera`. `faces` — a face can be put in front of the camera without a person: the bridge's test scene (every sim the harness spawns) holds portraits a test shows and moves. `gravity_compensation` — the hardware daemon runs the Placo kinematics engine. `face_markers` — the viewer draws the faces the bridge sends it.
- **Validated** says whether the setup is exercised by the bridge's own tests: *CI* on every push (Linux, [specs/testing/ci.md](../../specs/testing/ci.md)); *local* in the author's runs on macOS; *untested* means the code path exists and follows upstream's documented behaviour, and nothing more.

| Setup | Config | `motion` | `audio` | `camera` | `faces` | `gravity_compensation` | Validated |
|---|---|---|---|---|---|---|---|
| Fake, offline | `"backend": "fake"` | ✅ recorded | ✅ synthetic | ✅ synthetic | ✅ a stub detector in tests | ❌ | CI (the fast tier) |
| Sim, headless, **Linux** | `"backend": "sim"`, `"daemon": {"spawn": "auto"}` | ✅ | ✅ host device or a PulseAudio null sink; no software AEC on Ubuntu 24.04 | ✅ rendered offscreen through Mesa's EGL | ✅ | ❌ | CI (the live tier) |
| Sim, headless, **macOS** | same | ✅ | ✅ host device, software AEC | ❌ no GL context without a window | ✅ loads, nothing looks at it | ❌ | local |
| Sim, viewer | `"headless": false` | ✅ | ✅ | ✅ (needs an unlocked GUI session; `mjpython` on macOS) | ✅ | ❌ | local (macOS); `face_markers` too |
| Sim, webcam | `"daemon": {"camera": {"source": "webcam"}}`, headless or viewer | ✅ | ✅ | ✅ your computer's camera, treated as fixed at the robot's eye | ❌ the portrait is invisible to a webcam; you are the face | ❌ | local, by hand |
| Lite over USB, daemon spawned | `"backend": "real"`, `"daemon": {"spawn": "auto"}` | ✅ | ✅ hardware AEC (XVF3800) | ✅ | ❌ no scene on a robot | ✅ with `reachy-mini[placo_kinematics]` | local (macOS) |
| Lite over USB, **Linux** | same | expected ✅ | expected ✅ | expected ✅ | ❌ | expected ✅ | untested — follows from upstream's code; the daemon needs the Linux setup below |
| Wireless robot | `"backend": "real"`, `"robot": {"host": "<robot>"}`, `"daemon": {"spawn": "never"}` | expected ✅ | expected ✅ over WebRTC | expected ✅ over WebRTC, 30 fps | ❌ | expected ✅ | **untested** — the author has no wireless robot; it differs in ways not checked (it boots asleep with motors disabled, its camera streams at 30 fps) |

Read a ❌ in `camera` as "the camera tests skip": a macOS headless sim serves motion and audio and nothing on `bridge.camera`, so face detection publishes an inactive report after five seconds of no frame.

## What each setup needs installed

| Setup | Python packages | Native | First-use downloads | Devices and permissions |
|---|---|---|---|---|
| Fake | `reachy-mini-bridge` | `reachy_mini`'s own native libraries (GStreamer comes with its wheels on macOS and Windows; from the system on Linux — [../guides/linux.md](../guides/linux.md)). No daemon | none | none |
| Sim | `reachy-mini-bridge[sim]` (MuJoCo 3.3.x, declared directly — do not install `reachy-mini[mujoco]` beside it) | GStreamer as above; on Linux also the Rust `webrtcsink` plugin for any daemon, Mesa's EGL for the headless camera | the recorded-moves library (Hugging Face cache) on the first `play_emotion` or preloaded at daemon start; the YuNet weights on the first detection | a sound device, or a PulseAudio null sink; the viewer an unlocked GUI session; a webcam the camera permission for the process that starts the daemon (macOS) |
| Lite over USB | `reachy-mini-bridge`; `reachy-mini[placo_kinematics]` for gravity compensation | GStreamer; on Linux also the `webrtcsink` plugin | the recorded-moves library; the YuNet weights | the robot on USB (serial port auto-detected); camera and microphone permission for the process (macOS) |
| Wireless | `reachy-mini-bridge` | GStreamer packages only (the robot runs the daemon) | the recorded-moves library; the YuNet weights (detection runs on the host) | the robot on the network |
| A voice | one `tts-*` extra per provider: `tts-pocket` (local model, pulls torch), `tts-elevenlabs`, `tts-gradium` (cloud, a key each) | — | pocket's model weights on first use | a key in the named environment variable for a cloud provider |

The extras in full, with what each adds, are in the [README](../../README.md#install); the daemon launch commands and their platform notes in [../guides/running-daemons.md](../guides/running-daemons.md).

## Platforms

Developed on macOS; Linux is where CI runs both test tiers, the sim's camera included; Windows is untested. On Linux, GStreamer is the system's and a daemon on the machine — the sim, or a Lite over USB — also needs the Rust GStreamer webrtc plugin, which no distribution packages: [../guides/linux.md](../guides/linux.md) has the packages, the plugin's two routes, and what works without a sound card. A client that only talks to a wireless robot's own daemon needs the GStreamer packages alone.

## One local media daemon at a time

A daemon the bridge spawns binds its HTTP API to the config's `robot.host:port`, so two sims on one machine take two **HTTP** ports — which keeps their APIs apart and nothing more. Upstream's media server binds a fixed UDP port and a fixed camera socket path whatever the HTTP address, so two media-on daemons on one host are not isolated from each other; run one media daemon at a time, the second `--no-media` ([../guides/running-daemons.md](../guides/running-daemons.md)).
