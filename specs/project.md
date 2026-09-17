---
code:
  - pyproject.toml
tests:
  - tests/test_project_map.py
---

# Project

**Status:** Implemented

## Purpose

Structure and tooling for the Reachy Mini Bridge project itself: Python version, dependency/packaging management with `uv`, repo layout conventions, and development tooling.

## Decided

- **Python version:** 3.12+ minimum.
- **Package layout:** `src/` layout — `src/reachy_mini_bridge/...` — not flat, to avoid accidentally importing an uninstalled package from the repo root.
- **Dependency/venv management:** `uv`. Dev tooling lives in the `dev` dependency group (`uv sync --dev`), not in runtime `dependencies`.
- **Runtime-dependency policy.** Runtime dependencies are declared in `pyproject.toml` `[project.dependencies]` (the canonical list). A dependency is added by the implementation plan for the spec that first needs it, so `dependencies` grows as concepts are built and each concept spec names the dependency it introduces in its own text. Sourcing follows the nature of the dep:
  - **Third-party, published** (e.g. `reachy_mini`, `numpy`) — a normal, version-pinned dependency. `reachy_mini` is a **hard** runtime dep (see [robot.md](robot.md)); `numpy` is a **direct** dep too (the `FakeReachyMini` and `ReachyMiniApi` handle numpy arrays — pose matrices, camera frames — rather than relying on it transitively through `reachy_mini`).
  - **First-party sibling repos** (ours — e.g. `tts-engine`) — referenced by their **GitHub URL as a direct reference in the requirement itself** (`tts-engine @ git+https://github.com/funwithagents/tts-engine@main`), not through `[tool.uv.sources]`: a direct reference is part of the package metadata, so a downstream project resolves it with no source declaration of its own, whereas uv sources are never inherited. The reference tracks `main` while the sibling is still evolving and gets pinned to a tag or commit once it settles; `uv lock` records the exact commit either way, so a dev sync is reproducible and picking up the sibling's newer commits is a deliberate `uv lock --upgrade-package tts-engine`. (Working on both repos at once, a local `[tool.uv.sources]` path override is a one-line, uncommitted convenience.) `tts-engine` backs the *default* `say` synthesizer (see [audio.md](audio.md)) and enters this way, as a **base runtime dependency**: its own base is `numpy` plus `sounddevice`, which it imports only for its local player — the bridge injects a robot-speaker sink instead, so nothing from that path is ever loaded. Its TTS providers sit behind tts-engine's own extras, mirrored here one-to-one as the `tts-*` extras (below), so a caller who never sets a `tts` block (`say` is defined against a bridge-owned `SpeechSynthesizer` interface, and a caller may supply their own) carries the small engine and no provider SDK or model. Note **`asr-engine` is deliberately *not* a dependency**: the bridge exposes the robot's microphone as a stream and lets callers run their own ASR on top (see [audio.md](audio.md)), so nothing here embeds an ASR engine.
- **Optional extras.** `[project.optional-dependencies]` covers the upstream extras we actually need plus the optional first-party backends. For now:
  - **`sim`** → `reachy_mini[mujoco]`, adding the MuJoCo simulator so the `sim` backend works (see [robot.md](robot.md)). Symmetric with the upstream extra by design, so `reachy-mini-bridge[sim]` maps to `reachy_mini[mujoco]`.
  - **`tts-elevenlabs`** → `tts-engine[elevenlabs]` and **`tts-pocket`** → `tts-engine[pocket]`: one extra per tts-engine provider, each mapping onto tts-engine's extra of the same name, so the `tts` block's `module.type` and the extra to install read the same. `pocket` is the local model (no key, no network once the weights are cached; pulls `torch`, hundreds of MB); `elevenlabs` the cloud provider (a few MB, needs `ELEVENLABS_API_KEY`). A provider whose extra is absent is a tts-engine `ConfigError` at adapter build, which degrades to no voice (see [api.md](api.md)). The **dev group carries both** — `pocket` so the real-TTS live test runs with no credential, `elevenlabs` so the key-gated cloud test runs rather than failing on a missing extra — accepting `torch` in every dev sync as tts-engine's own dev group does; consumers install one at a time. The `demo` group takes `tts-pocket`, the provider the example config uses.
  - **`test`** → `pytest`, for the shipped testing harness `reachy_mini_bridge.testing` (see [testing_support.md](testing_support.md)): its fixtures and skip gates need pytest, which the base package must not drag in. A downstream project running e2e against `sim` installs `reachy-mini-bridge[sim,test]`; against `real`, `[test]` alone.

  The base install already includes `reachy_mini` (hard dep), so no `robot` extra is needed. The **`fake` backend needs no extra** — it's pure Python + numpy in the base package. Other upstream extras (`opencv`, `rerun`, `placo_kinematics`, …) are deliberately not mirrored yet; add one only when a spec needs it. Extras land in `pyproject.toml` with the implementation plan that first needs them, same as runtime deps.
- **Linting/formatting:** `ruff`.
- **Testing:** `pytest`, in two physically-separated tiers — a fast, deterministic, no-network default run (`tests/`, the only tier `testpaths` collects) and an opt-in live tier (`tests-e2e/`) that calls real external services. Full strategy is specced in [testing.md](testing.md).
- **Type checking:** `pyright` (`standard` mode), a dev dependency run via `uv run pyright`. Config lives in `[tool.pyright]` in `pyproject.toml`, targeting `src`, `tests`, and `tests-e2e`, pinned to the `.venv`.
- **Repo shape:**
  - `src/reachy_mini_bridge/` — the package, one module per core concept (the shipped testing harness is the one subpackage, `testing/`, see [testing_support.md](testing_support.md)).
  - `specs/` — pre-implementation design docs, one per concept (this folder).
  - `plans/` — implementation plans turning settled specs into buildable steps.
  - `tests/` at repo root, mirroring the `src/reachy_mini_bridge/` module structure.
  - `tests-e2e/` at repo root, for the live tier above — not collected by the default `pytest` run.

## Open questions

None currently.
