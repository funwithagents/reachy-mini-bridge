# Specs index

The index of concept specs and their status. For the project overview — what Reachy Mini Bridge is and how the layers fit together — see [_overview.md](_overview.md).

## Specs

<!-- One row per concept spec. Keep the Status column in sync with each spec's `**Status:**` line. -->

| Spec | Description | Status |
|---|---|---|
| [project.md](project.md) | Project structure and tooling: Python version, packaging with uv, layout conventions | Implemented |
| [testing.md](testing.md) | Testing strategy: two-tier `tests/`/`tests-e2e/` split, functional-test philosophy, skip-without-credentials live tier, e2e targets (sim headless/headfull, real) with probed capabilities | Implemented |
| [robot.md](robot.md) | Connection seam to upstream `reachy_mini` (`robot.py` + `fake_reachy_mini.py`): `real`/`sim` use `ReachyMini` directly, `fake` is a first-party `FakeReachyMini`; `AnyReachyMini` is a union type alias (no Protocol, no adapter) | Updated |
| [api.md](api.md) | `ReachyMiniApi` — high-level interaction verbs in human units (motors, expression, gaze, perception, audio, head wobbling, presence & breathing) over the robot seam; constructed from a `ReachyMiniConfig` | Updated |
| [config.md](config.md) | `ReachyMiniConfig` — one declarative config (backend, upstream `robot` kwargs, `daemon` management — including the sim's `camera` source, rendered or a host webcam — tts-engine `tts` block, `audio` profile, and the `motion` block's four switches — presence, breathing, wobbling, tracking, all on by default) buildable from a dict / JSON string / JSON file, mirroring tts-engine's `TTSEngineConfig` | Updated |
| [daemon.md](daemon.md) | Bridge-owned `reachy-mini-daemon` lifecycle for `sim` and a USB-attached `real` robot: own-it-or-borrow-it, readiness on `backend_status`, headless/viewer/hardware launch recipes (every sim on the bridge's sim daemon launcher), GStreamer env scrub, the child in its own session (a terminal Ctrl+C reaches the bridge only), teardown of what it started — shared by `ReachyMiniApi` and the testing harness | Implemented |
| [audio.md](audio.md) | Audio & media session: `say` via a pluggable `SpeechSynthesizer` (tts-engine default) routed to the robot speaker — completing when the utterance has been heard, flushing on cancel — the echo-cancelled mic exposed as a stream for the caller's own ASR, and upstream's audio-reactive head wobbling on the speaker path — keeping the XVF3800 echo cancellation working | Implemented |
| [motion.md](motion.md) | Motion loop, presence & breathing (`motion.py`): the one thread that owns `set_target` at 60 Hz, arbitrating exclusive primary moves (emotions, played through it with their sound) over an idle move — breathing (random rests between breaths, independently roaming antennas), a still neutral hold, or nothing — every transition a short blend; `presence` / `breathing` switches, pause without motors, tracking and wobbling paused around an emotion, one warning then pause on a lost daemon connection | Stable |
| [tools.md](tools.md) | `ReachyMiniTools` — the API exposed as plain typed, docstring'd functions for agent/LLM runtimes | Draft |
| [testing_support.md](testing_support.md) | `reachy_mini_bridge.testing` — the shipped, importable e2e harness (`live_api` fixture, `requires_caps`) so consumers test their own code against the `fake`/`sim`/`real` backends; its daemon plumbing wraps `daemon.py` | Implemented |
| [sim_scene.md](sim_scene.md) | `testing/sim_scene.py` — the bridge's test scene: a hidden-by-default portrait plane in a generated scene file (`write_test_scene` / `FacePlane`) loaded through the sim daemon launcher with an extension that installs a `SceneDirector` (mocap bodies driven from MuJoCo's control callback: place, timed move, hide/show) and a `/api/sim-scene` router on the daemon; `SimSceneClient`; `DaemonConfig.scene` ending in `.xml` selects it; every harness-spawned sim runs it, `faces` capability, `sim_scene` fixture; face e2e tests assert the head converges on the face | Implemented |
| [sim_daemon.md](sim_daemon.md) | `sim_daemon.py` — the launcher every bridge-spawned MuJoCo daemon runs through (`python -m reachy_mini_bridge.sim_daemon`): upstream's daemon plus three face-tracking corrections — tracking stepped each control tick, the tracker's intrinsics a pinhole of the active camera (upstream's mis-scaled matrix puts the head ~45° off the face), fixed-camera aim geometry for a webcam — and a `sim` / `webcam` camera source (a host camera relayed into the sim's camera stream, headless or viewer); `SimDaemonExtension` hooks the test scene builds on | Implemented |
| [control_panel.md](control_panel.md) | `examples/control_panel` — a Gradio control panel over `ReachyMiniApi` started from a config file: a gradio-free `ControlPanelController` (the api session on a background loop, sync verbs, stoppable `say` / `play_emotion`, `snapshot()`, mic meter) plus the Blocks UI (state on a timer, one control group per verb group); `demo` dependency group; tested on the `fake` | Implemented |
| _(add concept specs here — see [_spec-template.md](_spec-template.md))_ | | |

Each spec also opens with a YAML **frontmatter** block declaring the `code:` and `tests:` files it governs — the spec → code/tests mapping the spec-drift checks use to scope what they compare. Keep it current when files move, and see [AGENTS.md](../AGENTS.md) ("Spec frontmatter") for the full convention.

## Status legend

- **Not started** — no design decisions made yet
- **Draft** — actively being brainstormed/defined, contains open questions
- **Stable** — design settled, reviewed and validated (open questions are deferrals only), **ready to implement but not necessarily implemented yet**. This is the design-review gate, before code is written.
- **Implemented** — a **Stable** spec that a `Done` plan has built: the code now exists and matches the spec (design and code in sync)
- **Updated** — an **Implemented** spec since edited in a way that needs new code, so the code no longer matches it; a new implementation plan is needed (or in progress) to catch up. Returns to **Implemented** once that plan is `Done`.
