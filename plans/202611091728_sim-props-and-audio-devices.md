# Sim props from any image, extensible capabilities, the sim robot's audio devices

**Status:** Todo

Implements the settled behavior in `specs/testing/sim_scene.md` ("A prop is an image on a plane", "A pool of portraits", "The testing harness"), `specs/testing/testing_support.md` ("Public surface", "Props and capabilities a consumer declares", "Configuration via the environment"), `specs/testing/testing.md`, `specs/testing/ci.md`, `specs/core/config.md` (`daemon.audio`), `specs/daemon/daemon.md` ("The launch command", "What the daemon reports"), `specs/daemon/sim_daemon.md` ("Audio devices") and `specs/audio/audio.md` ("The robot's audio devices"). It lets a consumer's e2e tier put its own images in the sim scene, declare capabilities of its own, run its own bridge over the harness's daemon, and give the sim robot a virtual microphone and speaker that every bridge follows. It leaves out image conversion (PNG only, the consumer converts), the upstream option that would retire the device substitution, and provisioning a virtual microphone on CI's Linux runner.

## Scope

- `src/reachy_mini_bridge/testing/sim_scene.py` — `ImagePlane` (one of `height` / `width`, the other from the PNG's `IHDR`), the PNG check, `image_pool`, `face_pool` on top of it at `FACE_HEIGHT_M`, `write_test_scene(out_dir, props=None)`, materials `prop_<stem>`; `FacePlane` removed; `DEFAULT_FACE_SIZE` derived from the bundled portrait.
- `src/reachy_mini_bridge/testing/hooks.py` (new) — the hook specifications `pytest_reachy_mini_scene_props` and `pytest_reachy_mini_capabilities`.
- `src/reachy_mini_bridge/testing/support.py` — the run's vocabulary (built-ins + `props:<kind>` + declared) behind `check_capability_names`; `faces` leaves `CAPABILITIES`.
- `src/reachy_mini_bridge/testing/fixtures.py` — `pytest_addhooks`; the hooks collected in `pytest_configure` (props validated, kinds registered, declared names checked, then `REQUIRED_CAPS`); `live_daemon` public; `probed_capabilities(bridge, address)` probing `props:<kind>` and the declared probes; `face_scene` / `sim_scene` gates on `props:face`.
- `src/reachy_mini_bridge/testing/_daemon.py` — the scene written with the declared props; `DaemonConfig.audio` from `REACHY_MINI_E2E_AUDIO_INPUT` / `_OUTPUT`.
- `src/reachy_mini_bridge/testing/__init__.py` — export `robot_options`.
- `src/reachy_mini_bridge/config.py` — `SimAudioSettings`, `DaemonConfig.audio`.
- `src/reachy_mini_bridge/audio_devices.py` (new) — `resolve`, `substitute` (counted, conflict-checked), the device-listing seam.
- `src/reachy_mini_bridge/sim_daemon.py` — `--audio-input-device` / `--audio-output-device`, resolved before the daemon starts, the substitution on `reachy_mini.media.media_server`, `audio` in the report.
- `src/reachy_mini_bridge/daemon.py` — audio flags in `launch_command`; `DaemonInfo.audio`; `daemon_differences` for a named device the daemon does not open.
- `src/reachy_mini_bridge/bridge.py` — the bridge's report read before the robot is built; the substitution installed for the session on a loopback daemon that reports devices, removed at teardown.
- `tests/test_sim_scene.py`, `tests/test_testing_support.py`, `tests/test_config.py`, `tests/test_daemon.py`, `tests/test_sim_daemon.py`, `tests/test_bridge.py`, `tests/test_audio_devices.py` (new) — functional tests for the above.
- `tests-e2e/conftest.py` — declares the `card_1` prop (a generated landscape PNG); `tests-e2e/test_scene_props.py` (new) — the card spawned changes the frame, despawned restores it; `tests-e2e/test_audio.py` — the device test under `REACHY_MINI_E2E_AUDIO_INPUT`; every `faces` gate → `props:face`.
- `.github/workflows/ci.yml` — `REACHY_MINI_E2E_REQUIRED_CAPS=motion,audio,camera,props:face,props:card`.
- `docs/guides/testing.md`, `docs/reference/configuration.md`, `docs/reference/backends-and-capabilities.md`, `config.example.json`, `README.md`, `AGENTS.md` — the consumer-facing side.

## Steps

1. **Scene props.** In `sim_scene.py`: `_png_size(path)` reads the signature and `IHDR` (`ValueError` naming the file and "convert it to PNG" otherwise); `ImagePlane` with `size(aspect)`; `image_pool(kind, images, *, count, height, width)` (kind non-empty, not ending in `_<n>`); `face_pool` → `image_pool("face", …, height=FACE_HEIGHT_M)`; `write_test_scene(out_dir, props=None)` validating sizes and images before writing, materials `prop_<stem>`; the no-free-prop message names `image_pool(count=...)`; `DEFAULT_FACE_SIZE` = the bundled portrait at `FACE_HEIGHT_M`. Update `tests/test_sim_scene.py`.
2. **Hooks and vocabulary.** `testing/hooks.py` with the two specs (`firstresult` off). `support.py`: a module-level registry (`register_capabilities(names)`, `vocabulary()`), `check_capability_names` against it; `faces` out of `CAPABILITIES`. `fixtures.py`: `pytest_addhooks`; `pytest_configure` calls both hooks, writes nothing yet but validates the declared props (a dry `write_test_scene` into a temporary directory, names clashing with the pool's refused), registers `props:<kind>` for `face` and every declared kind and the declared capability names (a built-in, a `props:` name or a duplicate is a `UsageError`), then checks `REQUIRED_CAPS`. Store the props and the probes for the run.
3. **Probing.** `probed_capabilities(bridge, address)`: the built-in probes from the bridge's robot / camera / mic; `props:<kind>` from one `SimSceneClient.bodies()` read; each declared probe called with the bridge, an exception failing the fixture (`pytest.fail` naming the capability). `live_bridge` / `live_capabilities` call it with the bridge.
4. **Harness.** `live_daemon` public (session); `_daemon.managed_daemon` takes the declared props and writes `face_pool() + props`; `_daemon.audio_settings()` from the two env vars into `DaemonConfig.audio`; `robot_options` exported.
5. **Config.** `SimAudioSettings` (`input_device`, `output_device`: `null` or non-empty string), `DaemonConfig.audio`, unknown keys refused. Update `tests/test_config.py`, `config.example.json`.
6. **Devices.** `audio_devices.py`: `resolve(name, direction, *, devices=None)` over upstream's `gst_monitor_devices` (behind a seam) and `find_audio_device([d], …, target_name=name)` per device; `AudioDeviceError` (a `BridgeError`) naming the devices; `substitute(module, input_id, output_id)` a context manager replacing `module.get_audio_device`, counted per module, refusing other ids while held.
7. **Sim launcher.** Parse the two flags; resolve them before `upstream_main.main()` (argument error on failure); install the substitution on `reachy_mini.media.media_server` for the run; `audio` in the report. `daemon.py`: the audio flags after the camera flags; `DaemonInfo.audio` from the report (absent → `None`); a named device the daemon does not open is a difference.
8. **Bridge.** In `start()`, for a `sim` / `real` backend on a loopback host: read `/api/bridge/daemon` at the configured address before `connect()`; when it names devices, resolve them and enter the substitution on `reachy_mini.media.audio_gstreamer` in the session's exit stack before the robot is built. Keep `_read_daemon_info` reusing the pre-read report.
9. **e2e.** `tests-e2e/conftest.py` declares `card_1`, a PNG generated at configure time (zlib + struct, 300×200 stripes); `test_scene_props.py` gated on `camera` + `props:card`; `test_audio.py` device test gated on `REACHY_MINI_E2E_AUDIO_INPUT`; rename the `faces` gates; CI's required caps.
10. **Docs.** The testing guide (the two hooks, `live_daemon` + `robot_options`, `props:<kind>`, the audio env vars), the configuration reference (`daemon.audio`), the capabilities matrix, README's testing row, AGENTS.md.

## Verification

- `uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`.
- Live: `uv run pytest tests-e2e -rs` (headless; the card test skips on macOS without a camera), `REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs` (the card test runs), and the device test with `REACHY_MINI_E2E_AUDIO_INPUT` naming a real input of this Mac. CI runs the headless tier on Linux with `props:card` required.
- Mark this plan `Done` (here and in [_index.md](_index.md)), and the eight specs `Implemented`, only once all pass.
