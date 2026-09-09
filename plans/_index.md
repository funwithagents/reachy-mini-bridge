# Implementation plans

Implementation plans for Reachy Mini Bridge — each plan turns a settled part of a spec (see [specs/_index.md](../specs/_index.md)) into concrete, buildable steps. Plans are ordered by their date-time filename prefix (`YYYYMMDDHHmm_`).

## Plans

<!-- One row per plan, chronological by filename prefix. Keep the Status column in sync with each plan's `**Status:**` line. -->

| Plan | Description | Status |
|---|---|---|
| [202609041726_client-seam-and-fake-reachy-mini.md](202609041726_client-seam-and-fake-reachy-mini.md) | Build the `client.py` seam: `RobotClient` union alias, `build_robot(...)` factory (lazy upstream import), and the `FakeReachyMini` stand-in for the v1 consumed slice; adds `numpy`/`reachy_mini` base deps + `sim` extra | Done |
| [202609071702_e2e-targets-and-capability-gating.md](202609071702_e2e-targets-and-capability-gating.md) | Turn the sim-only e2e fixture into a target-selectable (`sim` headless/headfull, `real`), capability-probing harness with a `requires_caps(...)` skip gate | Done |
| [202609081557_api-and-audio-layer.md](202609081557_api-and-audio-layer.md) | Build the coupled api + audio layer: async-native `ReachyMiniApi` (motors, expression, gaze, audio verbs) over the media session (`SpeechSynthesizer`, say sink, mic tap, conversion helpers, tts adapter); adds `samplerate` base dep + `tts` extra + `errors.py`, fast `tests/` + a capability-gated `tests-e2e/` tier (with a `live_api` fixture) that confirms the real audio format | Done |

## Status legend

- **Todo** — written, not yet started
- **In progress** — actively being implemented
- **Done** — implemented, verified (lint/type-check/tests pass), and merged
