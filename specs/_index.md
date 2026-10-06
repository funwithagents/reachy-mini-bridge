# Specs index

The index of concept specs and their status. For the project overview — what Reachy Mini Bridge is and how the layers fit together — see [_overview.md](_overview.md).

## Specs

<!-- One row per concept spec, under the folder it lives in. Keep the Status column in sync with each spec's `**Status:**` line. -->

Specs are grouped in folders named after the subsystem they specify — the same words as the config blocks — with the repo-wide `project.md` at the root. Basenames are unique across folders.

### Repo-wide

The project's own structure and tooling — conventions for the whole repo rather than one subsystem.

| Spec | Description | Status |
|---|---|---|
| [project.md](project.md) | Project structure and tooling: Python version, packaging with uv, the dependency groups (`dev`, `tts`, `demo`; CPU torch on Linux), layout conventions | Implemented |

### core/

The layers and what they share: the connection seam, the interaction api, the config it's built from, and the one event mechanism.

| Spec | Description | Status |
|---|---|---|
| [robot.md](core/robot.md) | Connection seam to upstream `reachy_mini` (`robot.py` + `fake_reachy_mini.py`): `real`/`sim` use `ReachyMini` directly, `fake` is a first-party `FakeReachyMini`; `AnyReachyMini` is a union type alias (no Protocol, no adapter); the daemon's head tracking outside the consumed slice | Implemented |
| [bridge.md](core/bridge.md) | `ReachyMiniBridge` — the object a caller holds to drive the robot, a session with the `start()` / `stop()` lifecycle (`async with` sugar over it; every session it owns has the same pair): high-level interaction verbs in human units (motors, expression, gaze through the bridge's own head tracker, perception — the camera feed `bridge.camera` and the observable `faces` report — audio, head wobbling, presence & the idle move — breathing, hold, or a caller's custom one) over the robot seam; constructed from a `ReachyMiniConfig`; detection and tracking opt-in, needing a configured detector; every verb cancellable — `play_sound` a spanning verb, the newest sound file winning | Implemented |
| [config.md](core/config.md) | `ReachyMiniConfig` — one declarative config (backend, upstream `robot` kwargs, `daemon` management — including the sim's `camera` source, rendered or a host webcam — tts-engine `tts` block, `audio` profile, the `face_detection` block — the detector (`null` / `yunet` / `custom`, with a Python-only `face_detector`; none by default), the detection switch, and the cost knobs `width` (the width the shipped detector works at) and `target_fps` (a ceiling on detections per second) — and the `motion` block — presence, the idle mode (`breathing` / `hold` / `custom`) with its Python-only `idle_move`, wobbling, tracking (off by default, needing a detector)) buildable from a dict / JSON string / JSON file, mirroring tts-engine's `TTSEngineConfig` | Implemented |
| [observable.md](core/observable.md) | `Observable[T]` (`observable.py`) — a value a caller reads directly and subscribes to: `value`, `changes()` (an async iterator under the cancellation contract, latest-wins for a slow subscriber), `wait_for`, and the owner-defined notion of a change (`set` publishes, `update` is silent); the bridge's one event mechanism, first used by `bridge.faces` | Implemented |

### motion/

The motion loop that owns the robot's pose, and the head tracker whose aim it composes (the config's `motion` block).

| Spec | Description | Status |
|---|---|---|
| [motion.md](motion/motion.md) | Motion loop, presence & breathing (`motion.py`): the one thread that owns `set_target` at 60 Hz, arbitrating exclusive primary moves (emotions, played through it with their sound) over an idle move — breathing (breaths with random rests, the head roaming in roll/pitch/yaw, the antennas roaming and flicking independently), a still neutral hold, a caller's custom `IdleMove` (offsets from neutral in human units, built by a registered factory), or nothing — every transition a short blend, every animated idle move faded out to neutral when left; the **gaze layer** composing the head tracker's aim into the idle move by the tracking weight (faded in and out, left out of a primary) and the commanded-pose history the tracker aims against; `presence` switch and `idle` mode, pause without motors, wobbling paused around an emotion, one warning then pause on a lost daemon connection | Stable |
| [head_tracking.md](motion/head_tracking.md) | Head tracking (`head_tracking.py`): the bridge's own tracker turning the reported target face into a look-at aim — upstream's geometry with a bridge camera model (the client's calibration on a robot, the bridge's pinhole in the sim, a fixed camera for a webcam), the head pose at the frame's time from the motion loop's history, a 2 s loss timeout — handed to the motion loop's gaze layer; tracking a mode needing no motors but a configured detector, off by default, `attention` derived from it; whom the head follows chosen here — the biggest face, held while seen, the head waiting toward a vanished face's last position before switching to the biggest other one; its state published as `bridge.head_tracking` (`HeadTrackingReport`: active, focus, attention, the `track_id` of the face the head follows) | Implemented |

### vision/

The camera pipeline: the one feed over the robot's camera and the detection loop that runs over it (the config's `face_detection` block, the daemon's camera source).

| Spec | Description | Status |
|---|---|---|
| [camera.md](vision/camera.md) | Camera feed (`camera.py`): `bridge.camera`, the one reader of the robot's camera — a thread over upstream's one-shot `get_frame()` publishing the newest `CameraFrame` (`frame_id`, `ts`, the shared read-only BGR image, the head pose at the frame's time when the feed can stand behind it — a pose only ever attached to a capture time) for any number of consumers to sample: the detection loop, a caller's display, a vision graph plugged onto it by shape (no vision dependency); `get_camera_frame()` retired; the fake's paced frames | Implemented |
| [user_perception.md](vision/user_perception.md) | User perception (`face_detection.py`, `yunet.py`): how the bridge perceives the people in front of the robot — a detection loop running a face detector over the camera feed's frames — the shipped `yunet` (upstream's own model, wrapped as a bridge `FaceDetector`; no new dependency) or a developer's — any other detector, a vision library's included, plugged in as `custom` — at most `target_fps` times a second, following every face by a `track_id` — publishing an `Observable[FaceReport]` (`bridge.faces`: the faces with their pixel boxes and track ids, the frame they were found in; subscribe to count changes, debounced) that the head tracker and a caller's code consume — a client enriches it by `track_id`; opt-in — the `face_detection` block names the detector, none by default; the daemon's tracking untouched | Implemented |

### audio/

The media session: speech out to the robot speaker, the echo-cancelled mic in (the config's `tts` and `audio` blocks).

| Spec | Description | Status |
|---|---|---|
| [audio.md](audio/audio.md) | Audio & media session: `say` via a pluggable `SpeechSynthesizer` (tts-engine default) routed to the robot speaker — completing when the utterance has been heard, flushing on cancel — the echo-cancelled mic exposed as a stream for the caller's own ASR, upstream's audio-reactive head wobbling on the speaker path, and the one sound file player — owned by the session, each file played to its end (`play_sound` completing when heard, stopping on cancel), the newest file winning, emotions' sounds included — keeping the XVF3800 echo cancellation working | Implemented |

### daemon/

The bridge-owned daemon lifecycle and the two launchers every bridge-spawned daemon runs through (the config's `daemon` block).

| Spec | Description | Status |
|---|---|---|
| [daemon.md](daemon/daemon.md) | Bridge-owned `reachy-mini-daemon` lifecycle for `sim` and a USB-attached `real` robot: own-it-or-borrow-it, readiness on `backend_status`, headless/viewer/hardware launch recipes (every sim on the bridge's sim daemon launcher), GStreamer env scrub, the child in its own session (a terminal Ctrl+C reaches the bridge only) with its log lines forwarded into the bridge's, teardown of what it started — shared by `ReachyMiniBridge` and the testing harness | Implemented |
| [real_daemon.md](daemon/real_daemon.md) | `real_daemon.py` — the launcher every bridge-spawned hardware daemon runs through (`python -m reachy_mini_bridge.real_daemon`): upstream's `reachy-mini-daemon` in-process plus the macOS camera check — upstream opens the camera by an `avfvideosrc device-index` whose order moves between opens, a draw against the Mac's built-in camera that, lost, leaves the daemon without video; the launcher reads which device opened (`device-name`) and rebuilds round-robin until it is the robot's | Implemented |
| [sim_daemon.md](daemon/sim_daemon.md) | `sim_daemon.py` — the launcher every bridge-spawned MuJoCo daemon runs through (`python -m reachy_mini_bridge.sim_daemon`): upstream's daemon with a `sim` / `webcam` camera source (any host camera, cropped and scaled into the sim's camera stream, headless or viewer), the headless camera on Linux (the eye camera rendered offscreen through EGL, so a CI runner's sim has frames), the `--sim-display` wiring of the sim displays ([sim_displays.md](daemon/sim_displays.md)), and the `SimDaemonExtension` hooks the test scene builds on; the daemon's own tracking left as upstream ships it | Implemented |
| [sim_displays.md](daemon/sim_displays.md) | `sim_displays.py` — what the MuJoCo viewer shows besides the scene, one `--sim-display` / `daemon.sim_displays` switch each, all sharing the viewer handle: `camera_overlay`, the camera stream as a picture in the view's corner; and, in the 3D scene, drawn for the person watching and never for the eye camera (the viewer's `user_scn`), `robot_gaze`, the eye camera's optical axis — the one the tracker aligns — as a line; `face_markers`, an ellipsoid per detected face at the pose the bridge estimates (the camera model, a depth from the face's size, the head pose of its frame — the tracker's own `frame_head_pose`), pushed by a bridge-side publisher to `/api/sim/displays/face_markers` on the daemon | Implemented |

### testing/

The testing strategy, the shipped testing harness, and the test scene it runs every sim on.

| Spec | Description | Status |
|---|---|---|
| [testing.md](testing/testing.md) | Testing strategy: two-tier `tests/`/`tests-e2e/` split, functional-test philosophy, skip-without-credentials live tier, e2e targets (sim headless/headfull, real) with probed capabilities — the headless sim with its camera on Linux; a missing TTS provider skips like a missing key | Implemented |
| [ci.md](testing/ci.md) | Continuous integration on GitHub's hosted Linux runners: three jobs side by side — `check` (lint, format, types), `fast-tier`, `e2e-sim` (the live tier on the headless sim the harness spawns, a PulseAudio null sink for audio, the camera offscreen, the pocket-TTS test on CPU torch) on every PR and push to `main`; `uv sync --locked` of every group, the apt packages, the caches, the expected skips; no secrets, no viewer, no robot | Implemented |
| [testing_support.md](testing/testing_support.md) | `reachy_mini_bridge.testing` — the shipped, importable e2e harness (`live_bridge` fixture, `requires_caps`) so consumers test their own code against the `fake`/`sim`/`real` backends; its daemon plumbing wraps `daemon.py`; `live_bridge` configures the shipped `yunet` detector with detection and tracking on | Implemented |
| [sim_scene.md](testing/sim_scene.md) | `testing/sim_scene.py` — the bridge's test scene: a pool of hidden portrait planes in a generated scene file (`write_test_scene` / `face_pool` / `FacePlane`; three by default, one texture per image) that a test spawns, moves and despawns — as many at once as it needs — loaded through the sim daemon launcher with an extension that installs a `SceneDirector` (mocap bodies driven from MuJoCo's control callback: place, timed move, hide/show) and an inject router on the daemon (`/api/sim/inject/bodies`); `SimSceneClient`; `DaemonConfig.scene` ending in `.xml` selects it; every harness-spawned sim runs it, `faces` capability, `sim_scene` fixture; face e2e tests assert the head converges on the face | Implemented |

### examples/

The example apps in the repo's `examples/` directory.

| Spec | Description | Status |
|---|---|---|
| [control_panel.md](examples/control_panel.md) | `examples/control_panel` — a Gradio control panel over `ReachyMiniBridge` started from a config file: a gradio-free `ControlPanelController` (the bridge session on a background loop, sync verbs, stoppable `say` / `play_sound` / `play_emotion`, `snapshot()` — with the face count and the detection mode — mic meter) plus the Blocks UI (state on a timer, one control group per verb group, a Detection checkbox); `demo` dependency group; tested on the `fake` | Implemented |

_(add concept specs under the folder of their subsystem — see [_spec-template.md](_spec-template.md) — and a row here.)_

Each spec also opens with a YAML **frontmatter** block declaring the `code:` and `tests:` files it governs — the spec → code/tests mapping the spec-drift checks use to scope what they compare. Keep it current when files move, and see [AGENTS.md](../AGENTS.md) ("Spec frontmatter") for the full convention.

## Status legend

- **Not started** — no design decisions made yet
- **Draft** — actively being brainstormed/defined, contains open questions
- **Stable** — design settled, reviewed and validated (open questions are deferrals only), **ready to implement but not necessarily implemented yet**. This is the design-review gate, before code is written.
- **Implemented** — a **Stable** spec that a `Done` plan has built: the code now exists and matches the spec (design and code in sync)
- **Updated** — an **Implemented** spec since edited in a way that needs new code, so the code no longer matches it; a new implementation plan is needed (or in progress) to catch up. Returns to **Implemented** once that plan is `Done`.
