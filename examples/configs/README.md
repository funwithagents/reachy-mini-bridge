# Config profiles

One short `ReachyMiniConfig` per way of running the bridge — copy the one that matches what you have and pass it to `ReachyMiniBridge.from_json_file(...)` (or `uv run python -m examples.control_panel --config <file>`). Each is the minimal setup for its target: the repo's [config.example.json](../../config.example.json) is the opposite, the inventory of every field in the maximal setup (the viewer, a webcam, a detector, a voice). Every field is documented in [docs/reference/configuration.md](../../docs/reference/configuration.md); what each setup gives and how far it is validated, in one table, in [docs/reference/backends-and-capabilities.md](../../docs/reference/backends-and-capabilities.md).

| Profile | For | What differs in the file | Devices and first-use downloads |
|---|---|---|---|
| [fake.json](fake.json) | development and tests with no robot and no simulator | the backend alone: no daemon, no detector | none |
| [lite-usb.json](lite-usb.json) | a Reachy Mini Lite plugged into this machine over USB; the bridge starts the hardware daemon, wakes the robot and puts it to sleep on exit | `real` with `daemon.spawn: "auto"`; the `yunet` detector with tracking on | the robot on USB; camera and microphone permission for the process (macOS); the recorded-moves library and the YuNet weights into the Hugging Face cache on first use |
| [sim-rendered-camera.json](sim-rendered-camera.json) | the MuJoCo simulator with its rendered eye camera, in the viewer window — there is nothing to detect in it unless the test scene shows a portrait | `sim` spawned in the viewer, `daemon.camera.source: "sim"`, the camera overlay on; the `yunet` detector with tracking on | an unlocked GUI session (the viewer; `mjpython` on macOS); the recorded-moves library and the YuNet weights |
| [sim-webcam.json](sim-webcam.json) | the simulator seeing through your computer's webcam, in the viewer window with the webcam picture in its corner: it detects and follows **you** | as above with `daemon.camera.source: "webcam"` and its `hfov_deg` | a webcam, with camera permission for the process that starts the daemon (macOS); an unlocked GUI session (the viewer; `mjpython` on macOS); the recorded-moves library and the YuNet weights |
| [wireless.json](wireless.json) | a wireless Reachy Mini on the network, running its own daemon — **untested** by the author, who has no wireless robot; a report of what happened, working or not, is welcome as an issue | `real` with `robot.host` set and `daemon.spawn: "never"`; the detector at `target_fps: 10` against the robot's 30 fps stream | the robot's address in `robot.host`; the YuNet weights (detection runs on the host) |

What each setup gives (motion, audio, camera, faces, gravity compensation), on which OS, and how far it has been validated is the one matrix in [docs/reference/backends-and-capabilities.md](../../docs/reference/backends-and-capabilities.md#the-matrix).

The two sim profiles open the viewer window with the camera picture in its corner. To run one without the viewer, set `"headless": true` **and** drop the display, which needs the viewer (the config is rejected otherwise): the webcam works headless too, the rendered camera only on Linux.

```json
"headless": true,
"sim_displays": {"camera_overlay": false}
```

The other profiles run headless. None carries a voice: add a `tts` block ([docs/guides/audio.md](../../docs/guides/audio.md)) once the matching `tts-*` extra is installed, or pass your own synthesizer. The profiles are parsed by the bridge's tests, so they stay valid as the config evolves.
