# Discard the sim robot's audio output

**Status:** Done

Implements the settled behavior in `specs/core/config.md` (`daemon.audio.discard_output`), `specs/daemon/sim_daemon.md` ("Audio devices", "The report"), `specs/daemon/daemon.md` ("The launch command", "What the daemon reports"), `specs/audio/audio.md` ("The robot's audio devices") and `specs/testing/testing_support.md` ("Configuration via the environment"). It gives the simulated robot no speaker at all — its sound consumed in real time by a `sync=true` GStreamer `fakesink` and dropped, in the daemon and in every bridge that follows its report — for a host whose only virtual device is a cable feeding the robot's microphone (a Windows CI runner with VB-Cable). It leaves out a discarded *input*, the hardware daemon (a sim knob only), and the upstream option that would retire the substitution.

## Scope

- `src/reachy_mini_bridge/config.py` — `SimAudioSettings.discard_output` (bool, default false), rejected with an `output_device`.
- `src/reachy_mini_bridge/audio_devices.py` — `AudioDevices.discard_sink`; `resolve_devices(..., discard_output=)`; `discarding_sink` (a `sync=true` fakesink); `substitute` replacing `_build_audiosink_element` on the module's classes that define it, restored with the lookup, a module without one an `AudioDeviceError`.
- `src/reachy_mini_bridge/sim_daemon.py` — `--discard-audio-output` (argument error with `--audio-output-device`), passed to the substitution, `discard_output` in the report.
- `src/reachy_mini_bridge/daemon.py` — the flag in `launch_command`; `DaemonInfo.audio.discard_output` from the report (absent: false); `describe_audio_output`; `daemon_differences` on the output.
- `src/reachy_mini_bridge/bridge.py` — the bridge follows a discarding report on `reachy_mini.media.audio_gstreamer`; the session log line shows `discarded`.
- `src/reachy_mini_bridge/testing/_daemon.py`, `testing/fixtures.py` — `REACHY_MINI_E2E_DISCARD_AUDIO_OUTPUT`, a usage error with `REACHY_MINI_E2E_AUDIO_OUTPUT`, checked at configure time.
- `tests/test_config.py`, `tests/test_audio_devices.py`, `tests/test_sim_daemon.py`, `tests/test_daemon.py`, `tests/test_bridge.py`, `tests/test_testing_support.py` — functional tests; `tests-e2e/test_audio.py` — the discard on a live sim under the variable.
- `config.example.json`, `docs/reference/configuration.md`, `docs/guides/testing.md`, `AGENTS.md` — the consumer side.
- `.github/workflows/ci.yml`, `specs/testing/ci.md` — a separate `e2e-sim-discarded-audio` job: the `e2e-sim` environment on the default engine, `tests-e2e/test_audio.py` whole with `REACHY_MINI_E2E_DISCARD_AUDIO_OUTPUT=1`, `REACHY_MINI_E2E_REQUIRED_CAPS=motion,audio`; the job table, the expected skips (the discard test skips in the `e2e-sim` entries and runs there), the required checks; the `e2e-sim` entries unchanged.

## Steps

1. **Config.** `discard_output` read as a strict boolean, unknown keys still refused; `true` with an `output_device` a `ConfigError` naming both.
2. **The seam.** Upstream's sink builders pick a platform sink as soon as the lookup answers an id, so the discard replaces the builder: `substitute(module, AudioDevices(..., discard_sink=True))` finds the classes defined in `module` whose own `__dict__` has `_build_audiosink_element` (`GstMediaServer`, `GStreamerAudio`), sets `discarding_sink` on each, and restores the saved functions when the last holder leaves — counted and conflict-checked like the lookup.
3. **Launcher.** The flag, its exclusion, `resolve_devices(..., discard_output=)`, the report's `discard_output`.
4. **Daemon module.** The flag after the audio device flags; the report's field read with a default; an output the config discards that the daemon plays, or a speaker it names that the daemon discards, is a difference.
5. **Bridge.** `_reported_audio_devices` carries the discard into the session's substitution; the log line via `describe_audio_output`.
6. **Harness.** `audio_settings()` reads the variable (`1`/`true`/`yes`/`on`), refuses it with an output name, and `pytest_configure` calls it so the error stops the run early.
7. **Specs and docs.**
8. **CI.** The `e2e-sim-discarded-audio` job beside `e2e-sim` (not a matrix entry), and `specs/testing/ci.md` updated with it.

## Verification

- `uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`.
- Offline, against upstream's real classes in a child process: the client's session pipeline and a sound file's sink and the media server's sink are the `sync=true` fakesink, 0.5 s of audio takes 0.5 s to drop, the builders are restored.
- Live (optional): `REACHY_MINI_E2E_DISCARD_AUDIO_OUTPUT=1 uv run pytest tests-e2e/test_audio.py -rs`.
- Mark this plan `Done` (here and in [_index.md](_index.md)), and the five specs `Implemented`, only once all pass.
