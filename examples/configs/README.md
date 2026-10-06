# Config profiles

One short `ReachyMiniConfig` per way of running the bridge — copy the one that matches what you have and pass it to `ReachyMiniBridge.from_json_file(...)` (or `uv run python -m examples.control_panel --config <file>`). Each is the minimal setup for its target: the repo's [config.example.json](../../config.example.json) is the opposite, the inventory of every field in the maximal setup (the viewer, a webcam, a detector, a voice). Every field is documented in [docs/reference/configuration.md](../../docs/reference/configuration.md); what each setup gives and how far it is validated, in one table, in [docs/reference/backends-and-capabilities.md](../../docs/reference/backends-and-capabilities.md).

| Profile | For | Install | Devices and first-use downloads | Capabilities | Validated |
|---|---|---|---|---|---|
| [fake.json](fake.json) | development and tests with no robot and no simulator | `reachy-mini-bridge` | none | motion, audio, camera — all synthetic, recorded on `bridge.robot` | CI, the fast tier |
| [lite-usb.json](lite-usb.json) | a Reachy Mini Lite plugged into this machine over USB; the bridge starts the hardware daemon, wakes the robot and puts it to sleep on exit | `reachy-mini-bridge`; `reachy-mini[placo_kinematics]` for gravity compensation | the robot on USB; camera and microphone permission for the process (macOS); the recorded-moves library and the YuNet weights into the Hugging Face cache on first use | motion, audio with hardware echo cancellation, camera, face detection and tracking | the author's Lite, macOS |
| [sim-rendered-camera.json](sim-rendered-camera.json) | the MuJoCo simulator with its rendered eye camera, in the viewer window | `reachy-mini-bridge[sim]` | an unlocked GUI session (the viewer; `mjpython` on macOS); the recorded-moves library | motion, audio with software echo cancellation (where GStreamer has `webrtcdsp`), camera; faces only when something is in the scene | CI headless on Linux (where the camera renders offscreen: set `"headless": true` there), the viewer locally |
| [sim-webcam.json](sim-webcam.json) | the simulator seeing through your computer's webcam, in the viewer window with the webcam picture in its corner: it detects and follows **you** | `reachy-mini-bridge[sim]` | a webcam, with camera permission for the process that starts the daemon (macOS); an unlocked GUI session (the viewer; `mjpython` on macOS); the YuNet weights | motion, audio, camera (your webcam, treated as fixed at the robot's eye), face detection and tracking | locally, by hand |
| [wireless.json](wireless.json) | a wireless Reachy Mini on the network, running its own daemon | `reachy-mini-bridge` (GStreamer packages only on Linux) | the robot's address in `robot.host`; the YuNet weights (detection runs on the host) | expected: motion, audio and camera over WebRTC, detection and tracking on the host | **untested** — the author has no wireless robot; the robot boots asleep with motors disabled and streams its camera at 30 fps, differences the bridge has not been checked against. A report of what happened, working or not, is welcome as an issue |

The two sim profiles open the viewer window with the camera picture in its corner. To run one without the viewer, set `"headless": true` **and** drop the display, which needs the viewer (the config is rejected otherwise): the webcam works headless too, the rendered camera only on Linux.

```json
"headless": true,
"sim_displays": {"camera_overlay": false}
```

The other profiles run headless. None carries a voice: add a `tts` block ([docs/guides/audio.md](../../docs/guides/audio.md)) once the matching `tts-*` extra is installed, or pass your own synthesizer. The profiles are parsed by the bridge's tests, so they stay valid as the config evolves.
