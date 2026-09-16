# Implementation plans

Implementation plans for Reachy Mini Bridge — each plan turns a settled part of a spec (see [specs/_index.md](../specs/_index.md)) into concrete, buildable steps. Plans are ordered by their date-time filename prefix (`YYYYMMDDHHmm_`).

## Plans

<!-- One row per plan, chronological by filename prefix. Keep the Status column in sync with each plan's `**Status:**` line. -->

| Plan | Description | Status |
|---|---|---|
| [202609041726_client-seam-and-fake-reachy-mini.md](202609041726_client-seam-and-fake-reachy-mini.md) | Build the `client.py` seam: `RobotClient` union alias, `build_robot(...)` factory (lazy upstream import), and the `FakeReachyMini` stand-in for the v1 consumed slice; adds `numpy`/`reachy_mini` base deps + `sim` extra | Done |
| [202609071702_e2e-targets-and-capability-gating.md](202609071702_e2e-targets-and-capability-gating.md) | Turn the sim-only e2e fixture into a target-selectable (`sim` headless/headfull, `real`), capability-probing harness with a `requires_caps(...)` skip gate | Done |
| [202609081557_api-and-audio-layer.md](202609081557_api-and-audio-layer.md) | Build the coupled api + audio layer: async-native `ReachyMiniApi` (motors, expression, gaze, audio verbs) over the media session (`SpeechSynthesizer`, say sink, mic tap, conversion helpers, tts adapter); adds `samplerate` base dep + `tts` extra + `errors.py`, fast `tests/` + a capability-gated `tests-e2e/` tier (with a `live_api` fixture) that confirms the real audio format | Done |
| [202609091100_rename-robotclient-to-anyreachymini.md](202609091100_rename-robotclient-to-anyreachymini.md) | Rename the seam's `RobotClient` union alias → `AnyReachyMini` and import `reachy_mini` normally (drop `TYPE_CHECKING`/lazy import); fixes the `.client`-on-a-`Client` name clash and IDE go-to-definition landing on the fake | Done |
| [202609091430_split-fake-and-rename-client-to-robot.md](202609091430_split-fake-and-rename-client-to-robot.md) | Split `FakeReachyMini` (+ helpers) into a new `fake_reachy_mini.py`; rename `client.py`/`client.md` → `robot.py`/`robot.md` (the seam keeps just `AnyReachyMini` + `build_robot`); split the seam tests into `test_robot.py` + `test_fake_reachy_mini.py` | Done |
| [202609091700_camera-frame-perception-verb.md](202609091700_camera-frame-perception-verb.md) | Add a `get_camera_frame` perception verb (raw BGR `ndarray`\|`None`, mirroring upstream `media.get_frame`) to the v1 api; move the live camera test onto the API and delete `tests-e2e/test_robot.py` (its motor-state check is already covered at the API layer) | Done |
| [202609101200_ship-testing-support-harness.md](202609101200_ship-testing-support-harness.md) | Promote the live-tier harness from `tests-e2e/` into a shipped `reachy_mini_bridge.testing` package (opt-in pytest plugin, `test` extra, `live_api`/`requires_caps`/`require_env`) with a consumer guide, so downstream projects test against `fake`/`sim`/`real` without re-deriving the daemon lifecycle | Done |
| [202609141536_config-and-daemon-lifecycle.md](202609141536_config-and-daemon-lifecycle.md) | Build `ReachyMiniConfig` (`from_dict`/`from_json`/`from_json_file`; `robot` kwargs verbatim, `daemon`, `tts`, `audio` blocks) and the bridge-owned `daemon.py` (own-it-or-borrow-it sim daemon); construct `ReachyMiniApi` from the config with robot construction moved into `__aenter__`, the `tts` block building the default synthesizer; rebuild `testing/_daemon.py` on `daemon.py` | Done |
| [202609141800_degradable-default-voice.md](202609141800_degradable-default-voice.md) | A `tts` block whose adapter cannot be built (e.g. `api_key_env` unset) no longer fails `ReachyMiniApi` construction: the api comes up with no voice, `synthesizer_error` exposes the cause, `say` raises `BridgeError` chained to it (the missing `tts` extra stays a hard `ConfigError`) | Done |
| [202609141900_say-completion-and-wobbling.md](202609141900_say-completion-and-wobbling.md) | `say` completes when the utterance has finished playing (wall-clock playback-end tracker + tail margin) and flushes the speaker on cancel or synthesizer failure (fixes `specs/_tts-bug.md`); upstream's audio-reactive head wobbling, on by default through a top-level `wobbling` config flag applied on entry (off again at exit) plus a `set_wobbling` verb and `wobbling` property; fake grows the two toggles; live tests for completion time and observable sway | Done |

## Status legend

- **Todo** — written, not yet started
- **In progress** — actively being implemented
- **Done** — implemented, verified (lint/type-check/tests pass), and merged
