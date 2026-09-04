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
  - **Third-party, published** (e.g. `reachy_mini`, `numpy`) — a normal, version-pinned dependency. `reachy_mini` is a **hard** runtime dep (see [client.md](client.md)); `numpy` is a **direct** dep too (the `FakeRobot` and `ReachyMiniApi` handle numpy arrays — pose matrices, camera frames — rather than relying on it transitively through `reachy_mini`).
  - **First-party sibling repos still evolving** (ours — e.g. `tts-engine`) — referenced as a **local path dependency** (via `[tool.uv.sources]`) during development so our changes flow without republishing, then migrated to a **pinned git URL** (or a published release) for production once stable. `tts-engine` backs `say` (see [api.md](api.md)) and enters this way.
- **Optional extras.** `[project.optional-dependencies]` mirrors the upstream extras we actually need. For now that is just:
  - **`sim`** → `reachy_mini[mujoco]`, adding the MuJoCo simulator so the `sim` backend works (see [client.md](client.md)). Symmetric with the upstream extra by design, so `reachy-mini-bridge[sim]` maps to `reachy_mini[mujoco]`.

  The base install already includes `reachy_mini` (hard dep), so no `robot` extra is needed. The **`fake` backend needs no extra** — it's pure Python + numpy in the base package. Other upstream extras (`opencv`, `rerun`, `placo_kinematics`, …) are deliberately not mirrored yet; add one only when a spec needs it. Extras land in `pyproject.toml` with the implementation plan that first needs them, same as runtime deps.
- **Linting/formatting:** `ruff`.
- **Testing:** `pytest`, in two physically-separated tiers — a fast, deterministic, no-network default run (`tests/`, the only tier `testpaths` collects) and an opt-in live tier (`tests-e2e/`) that calls real external services. Full strategy is specced in [testing.md](testing.md).
- **Type checking:** `pyright` (`standard` mode), a dev dependency run via `uv run pyright`. Config lives in `[tool.pyright]` in `pyproject.toml`, targeting `src`, `tests`, and `tests-e2e`, pinned to the `.venv`.
- **Repo shape:**
  - `src/reachy_mini_bridge/` — the package, one module per core concept.
  - `specs/` — pre-implementation design docs, one per concept (this folder).
  - `plans/` — implementation plans turning settled specs into buildable steps.
  - `tests/` at repo root, mirroring the `src/reachy_mini_bridge/` module structure.
  - `tests-e2e/` at repo root, for the live tier above — not collected by the default `pytest` run.

## Open questions

None currently.
