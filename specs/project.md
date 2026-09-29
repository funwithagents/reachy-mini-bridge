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
  - **Third-party, published** (e.g. `reachy_mini`, `numpy`) — a normal, version-pinned dependency. `reachy_mini` is a **hard** runtime dep (see [robot.md](core/robot.md)); `numpy` is a **direct** dep too (the `FakeReachyMini` and `ReachyMiniApi` handle numpy arrays — pose matrices, camera frames — rather than relying on it transitively through `reachy_mini`).
  - **First-party sibling repos** (ours — e.g. `tts-engine`) — referenced by their **GitHub URL as a direct reference in the requirement itself** (`tts-engine @ git+https://github.com/funwithagents/tts-engine@main`), not through `[tool.uv.sources]`: a direct reference is part of the package metadata, so a downstream project resolves it with no source declaration of its own, whereas uv sources are never inherited. The reference tracks `main` while the sibling is still evolving and gets pinned to a tag or commit once it settles; `uv lock` records the exact commit either way, so a dev sync is reproducible and picking up the sibling's newer commits is a deliberate `uv lock --upgrade-package tts-engine`. (Working on both repos at once — here or in a downstream project — a bare `[tool.uv.sources]` path entry for `tts-engine` is *not* enough: uv rejects it as "conflicting URLs for package `tts-engine`" against the direct reference. The path source has to be paired with `[tool.uv] override-dependencies = ["tts-engine"]`, which replaces every requirement on the package with the overriding one and lets the local checkout win. Either way it is an uncommitted convenience, never the declared dependency: the plain-name-plus-path-source form that would avoid the conflict resolves `tts-engine` from PyPI, where it does not exist, for every consumer without a source of its own.) `tts-engine` backs the *default* `say` synthesizer (see [audio.md](audio/audio.md)) and enters this way, as a **base runtime dependency**: its own base is `numpy` plus `sounddevice`, which it imports only for its local player — the bridge injects a robot-speaker sink instead, so nothing from that path is ever loaded. Its TTS providers sit behind tts-engine's own extras, mirrored here one-to-one as the `tts-*` extras (below), so a caller who never sets a `tts` block (`say` is defined against a bridge-owned `SpeechSynthesizer` interface, and a caller may supply their own) carries the small engine and no provider SDK or model. Note **`asr-engine` is deliberately *not* a dependency**: the bridge exposes the robot's microphone as a stream and lets callers run their own ASR on top (see [audio.md](audio/audio.md)), so nothing here embeds an ASR engine.
- **Optional extras.** `[project.optional-dependencies]` covers the upstream extras we actually need plus the optional first-party backends. For now:
  - **`sim`** → `mujoco>=3.3.1,<3.4`, the MuJoCo simulator the `sim` backend needs (see [robot.md](core/robot.md)). Deliberately *not* `reachy_mini[mujoco]`: that extra is the single requirement `mujoco==3.3.0`, an exact pin from February 2025 that a dependent cannot loosen (pip constraints only narrow; uv's `override-dependencies` is honoured for the root project only, so a downstream project would silently get 3.3.0), and the sim launcher wants the passive viewer's overlays (`set_images` / `set_texts`, 3.3.1+ — [sim_daemon.md](daemon/sim_daemon.md)). Requiring MuJoCo directly keeps the pin out of every consumer's graph; `<3.4` stays on patch releases of the version upstream tests on (the daemon also passes the offline and headless e2e tiers on 3.14.0). Installing `reachy_mini[mujoco]` alongside brings the pin back as a resolver conflict, which the docs say not to do. Maintenance: when bumping `reachy-mini`, check its `mujoco` extra is still only `mujoco` — anything it grows must be mirrored here. The upstream report is [../docs/upstream-mujoco-version-pin.md](../docs/upstream-mujoco-version-pin.md).
  - **`tts-elevenlabs`** → `tts-engine[elevenlabs]`, **`tts-gradium`** → `tts-engine[gradium]` and **`tts-pocket`** → `tts-engine[pocket]`: one extra per tts-engine provider, each mapping onto tts-engine's extra of the same name, so the `tts` block's `module.type` and the extra to install read the same. `pocket` is the local model (no key, no network once the weights are cached; pulls `torch`, hundreds of MB); `elevenlabs` and `gradium` the cloud providers (a few MB each, needing `ELEVENLABS_API_KEY` / `GRADIUM_API_KEY`). A provider whose extra is absent is a tts-engine `ConfigError` at adapter build, which degrades to no voice (see [api.md](core/api.md)). The **dev group carries all three** — `pocket` so the real-TTS live test runs with no credential, `elevenlabs` and `gradium` so the key-gated cloud tests run rather than failing on a missing extra — accepting `torch` in every dev sync as tts-engine's own dev group does; consumers install one at a time. The `demo` group takes `tts-pocket`, the provider the example config uses.
  - **`test`** → `pytest`, for the shipped testing harness `reachy_mini_bridge.testing` (see [testing_support.md](testing/testing_support.md)): its fixtures and skip gates need pytest, which the base package must not drag in. A downstream project running e2e against `sim` installs `reachy-mini-bridge[sim,test]`; against `real`, `[test]` alone.

  The base install already includes `reachy_mini` (hard dep), so no `robot` extra is needed. The **`fake` backend needs no extra** — it's pure Python + numpy in the base package. Other upstream extras (`opencv`, `rerun`, `placo_kinematics`, …) are deliberately not mirrored yet; add one only when a spec needs it. Extras land in `pyproject.toml` with the implementation plan that first needs them, same as runtime deps.
- **License and distribution:** **MIT**, in a root `LICENSE` file and declared in `pyproject.toml` as a PEP 639 SPDX expression (`license = "MIT"` with `license-files = ["LICENSE"]`), so a built wheel or sdist carries both the expression and the text. `[project.urls]` names the public GitHub repo (homepage, repository, issues). The repo is public on GitHub and not published to PyPI: a consumer installs it by git URL (`git+https://github.com/funwithagents/reachy-mini-bridge`), which is also why every first-party sibling it depends on must itself be publicly readable — a private `git+https` reference would resolve for us and fail for everyone else.
- **Linting/formatting:** `ruff`.
- **Testing:** `pytest`, in two physically-separated tiers — a fast, deterministic, no-network default run (`tests/`, the only tier `testpaths` collects) and an opt-in live tier (`tests-e2e/`) that calls real external services. Full strategy is specced in [testing.md](testing/testing.md).
- **Type checking:** `pyright` (`standard` mode), a dev dependency run via `uv run pyright`. Config lives in `[tool.pyright]` in `pyproject.toml`, targeting `src`, `tests`, and `tests-e2e`, pinned to the `.venv`.
- **Repo shape:**
  - `src/reachy_mini_bridge/` — the package, one module per core concept (the shipped testing harness is the one subpackage, `testing/`, see [testing_support.md](testing/testing_support.md)).
  - `specs/` — pre-implementation design docs, one per concept (this folder).
  - `plans/` — implementation plans turning settled specs into buildable steps.
  - `tests/` at repo root, mirroring the `src/reachy_mini_bridge/` module structure.
  - `tests-e2e/` at repo root, for the live tier above — not collected by the default `pytest` run.

## Open questions

None currently.
