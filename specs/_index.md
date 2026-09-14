# Specs index

The index of concept specs and their status. For the project overview — what Reachy Mini Bridge is and how the layers fit together — see [_overview.md](_overview.md).

## Specs

<!-- One row per concept spec. Keep the Status column in sync with each spec's `**Status:**` line. -->

| Spec | Description | Status |
|---|---|---|
| [project.md](project.md) | Project structure and tooling: Python version, packaging with uv, layout conventions | Implemented |
| [testing.md](testing.md) | Testing strategy: two-tier `tests/`/`tests-e2e/` split, functional-test philosophy, skip-without-credentials live tier, e2e targets (sim headless/headfull, real) with probed capabilities | Implemented |
| [robot.md](robot.md) | Connection seam to upstream `reachy_mini` (`robot.py` + `fake_reachy_mini.py`): `real`/`sim` use `ReachyMini` directly, `fake` is a first-party `FakeReachyMini`; `AnyReachyMini` is a union type alias (no Protocol, no adapter) | Implemented |
| [api.md](api.md) | `ReachyMiniApi` — high-level interaction verbs in human units (motors, expression, gaze, perception, audio) over the robot seam; constructed from a `ReachyMiniConfig` | Implemented |
| [config.md](config.md) | `ReachyMiniConfig` — one declarative config (backend, upstream `robot` kwargs, `daemon` management, tts-engine `tts` block, `audio` profile) buildable from a dict / JSON string / JSON file, mirroring tts-engine's `TTSEngineConfig` | Implemented |
| [daemon.md](daemon.md) | Bridge-owned `reachy-mini-daemon` lifecycle for `sim`: own-it-or-borrow-it, readiness on `backend_status`, headless/viewer launch recipes, GStreamer env scrub, teardown of what it started — shared by `ReachyMiniApi` and the testing harness | Implemented |
| [audio.md](audio.md) | Audio & media session: `say` via a pluggable `SpeechSynthesizer` (tts-engine default) routed to the robot speaker, and the echo-cancelled mic exposed as a stream for the caller's own ASR — keeping the XVF3800 echo cancellation working | Implemented |
| [tools.md](tools.md) | `ReachyMiniTools` — the API exposed as plain typed, docstring'd functions for agent/LLM runtimes | Draft |
| [testing_support.md](testing_support.md) | `reachy_mini_bridge.testing` — the shipped, importable e2e harness (`live_api` fixture, `requires_caps`) so consumers test their own code against the `fake`/`sim`/`real` backends; its daemon plumbing wraps `daemon.py` | Implemented |
| _(add concept specs here — see [_spec-template.md](_spec-template.md))_ | | |

Each spec also opens with a YAML **frontmatter** block declaring the `code:` and `tests:` files it governs — the spec → code/tests mapping the spec-drift checks use to scope what they compare. Keep it current when files move, and see [AGENTS.md](../AGENTS.md) ("Spec frontmatter") for the full convention.

## Status legend

- **Not started** — no design decisions made yet
- **Draft** — actively being brainstormed/defined, contains open questions
- **Stable** — design settled, reviewed and validated (open questions are deferrals only), **ready to implement but not necessarily implemented yet**. This is the design-review gate, before code is written.
- **Implemented** — a **Stable** spec that a `Done` plan has built: the code now exists and matches the spec (design and code in sync)
- **Updated** — an **Implemented** spec since edited in a way that needs new code, so the code no longer matches it; a new implementation plan is needed (or in progress) to catch up. Returns to **Implemented** once that plan is `Done`.
