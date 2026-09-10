# Agent instructions

Start with [specs/_overview.md](specs/_overview.md) for the global view of the project (architecture, the three layers, the backends), then [specs/_index.md](specs/_index.md) for the index of the specs and their status — before making design decisions or writing code — it lists each spec and whether it's still open ("Draft"/"Not started"), design-validated ("Stable"), or built ("Implemented"). For what's been (or is being) built, see [plans/_index.md](plans/_index.md), which lists each implementation plan and its status ("Todo"/"In progress"/"Done").

## Project map

Where things live. This is a coarse, module-level map — for the full file inventory use `git ls-files`; for design detail follow the spec links.

### Top-level layout

| Path | What's there |
|---|---|
| `README.md` | Packaging front page — short intro + doc pointers |
| `src/reachy_mini_bridge/` | The library itself — one module per core concept (see below) |
| `specs/` | Pre-implementation design docs, one per concept, each with a `**Status:**` — indexed by [specs/_index.md](specs/_index.md) |
| `plans/` | Implementation plans turning settled specs into buildable steps — indexed by [plans/_index.md](plans/_index.md) |
| `tests/` | Fast, deterministic, no-network tests; mirrors the `src/reachy_mini_bridge/` module structure |
| `tests-e2e/` | Opt-in live tests that call real external services (not collected by default `pytest`) |
| `docs/` | Reference notes (not specs) — e.g. [docs/reachy-mini-api.md](docs/reachy-mini-api.md), what we learned about the upstream `reachy_mini` SDK |

### `src/reachy_mini_bridge/` modules

<!-- One row per concept module. Keep this in sync with the code (a test enforces it). -->

| Module | Role | Spec |
|---|---|---|
| `src/reachy_mini_bridge/robot.py` | Connection seam to upstream `reachy_mini` — `AnyReachyMini` union alias + `build_robot` backend factory | [specs/robot.md](specs/robot.md) |
| `src/reachy_mini_bridge/fake_reachy_mini.py` | First-party `FakeReachyMini` stand-in (imports no `reachy_mini`) — records commands, returns synthetic perception/audio | [specs/robot.md](specs/robot.md) |
| `src/reachy_mini_bridge/api.py` | `ReachyMiniApi` high-level interaction verbs (motors, expression, gaze, audio) | [specs/api.md](specs/api.md) |
| `src/reachy_mini_bridge/audio.py` | Audio & media session — `say` via a pluggable `SpeechSynthesizer` to the robot speaker + echo-cancelled mic stream for the caller's ASR + conversion helpers | [specs/audio.md](specs/audio.md) |
| `src/reachy_mini_bridge/errors.py` | Bridge exception hierarchy — `BridgeError` base + `MotorsNotEnabledError` | [specs/api.md](specs/api.md) |
| `src/reachy_mini_bridge/tools.py` | `ReachyMiniTools` agent/LLM tools (placeholder; Draft) | [specs/tools.md](specs/tools.md) |
| `src/reachy_mini_bridge/testing/` | Shipped testing harness (package) — `live_api` fixture, `requires_caps`, `require_env` for consumers' e2e tests (`fixtures.py` plugin, private `_daemon.py`, `support.py`) | [specs/testing_support.md](specs/testing_support.md) |

**Keep this map current:** when you add, rename, or remove a top-level `src/reachy_mini_bridge/` module or a root directory, update the map in the same change — same discipline as keeping spec/plan statuses honest (below). A test (`tests/test_project_map.py`) enforces that every `src/reachy_mini_bridge/*.py` module appears here and vice-versa — and that the spec frontmatter (see below) stays honest too.

## Keeping statuses current

Specs and plans both carry a status, and you are responsible for keeping it honest as work progresses — update it in the same change that does the work, not as an afterthought:

- **Spec status** (`**Status:**` line near the top of each spec, and the Status column in [specs/_index.md](specs/_index.md)) tracks *design maturity* and *whether the code reflects the spec*, as a lifecycle: `Not started` → `Draft` (open questions remain) → `Stable` (design settled, reviewed and validated — open questions are deferrals only — but **not necessarily implemented yet**) → `Implemented` (a `Done` plan has built it and the code matches the spec). Keep the `**Status:**` line and the index row in sync.
  - **`Stable` is the design-review gate, not an implementation claim.** Promote `Draft` → `Stable` once the core design is settled and its remaining open questions are genuine deferrals (not load-bearing unknowns) — this is where the design is validated *before* code is written. No implementation is required to be `Stable`.
  - **`Implemented` means code matches.** Promote `Stable` → `Implemented` only once a plan implementing it is `Done` (lint, type check, tests all pass — see Verification). This is the one transition that asserts design and code are in sync.
  - **When you edit an `Implemented` spec in a way that requires new code, set its status to `Updated` in the same change.** `Updated` means the design is settled but the existing implementation now lags it — a stronger warning than `Stable`, because there is stale code to fix, not just code to write. Then write a new implementation plan for the gap (see below) and, once that plan is `Done`, flip the spec back to `Implemented`. This `Implemented → Updated → Implemented` loop keeps a spec's status an honest signal of whether the code actually matches it — never leave a re-designed spec sitting at `Implemented`.
  - A purely editorial edit to a `Stable` or `Implemented` spec (typos, clarifications, reordering — nothing that changes what the code should do) keeps its status; it does **not** need `Updated`.
- **Plan status** (`**Status:**` line near the top of each plan, and the Status column in [plans/_index.md](plans/_index.md)) tracks *implementation progress*: `Todo` → `In progress` → `Done`. Mark a plan `Done` only once it's implemented and verified (lint, type check, tests all pass — see Verification). Keep the `**Status:**` line and the index row in sync.
- Whenever you add a spec or plan, add its row to the relevant `_index.md`; whenever you change a status, change it in both the file and the index.

## Spec frontmatter

Every spec opens with a YAML frontmatter block naming the code and tests it governs:

```
---
code:
  - src/reachy_mini_bridge/<module>.py
tests:
  - tests/test_<module>.py
---
```

This is the **spec → code/tests** mapping — the inverse of the module → spec column in the Project map above. Its job is to give the **spec-drift checks** an explicit, version-controlled scope: the exact files to diff a spec against, so a checker never has to guess which code implements a given spec. `code:` names the implementation the spec specifies; `tests:` names the tests that pin its behavior (may be empty/absent).

The mapping is **many-to-many**: a file can be governed by several specs, so the same path legitimately appears in more than one spec's frontmatter.

**Keep it current** (same discipline as statuses): when you move, rename, or delete a file a spec governs — or add a new `src/reachy_mini_bridge/` module — update the affected spec's `code:`/`tests:` in the same change. `tests/test_project_map.py` enforces three invariants: every listed path exists, every spec declares a non-empty `code:` list, and every concept module in `src/reachy_mini_bridge/` is named by at least one spec (`__init__.py` is exempt as package glue).

## Testing

- Write functional tests: exercise what a feature/function actually does (inputs → outputs, state changes, side effects), not just that it runs or matches its signature.
- Avoid trivial/tautological tests — e.g. asserting a constant, asserting an object is not `None`, asserting a mock was called. If a test would pass for a broken implementation, it's not worth writing.
- Prefer driving the public API the way a real caller would over asserting on internals.

### Live/e2e tests

Some tests call real external services over the network. They live in `tests-e2e/`, a directory separate from `tests/`, so the default `uv run pytest` never runs them — no network access or credentials are needed for the normal dev loop. Run them explicitly, and only when you actually want to verify against a live service. Tests that lack their required credentials should **skip**, not fail, so the tier is safe to run with only the keys you happen to have.

## Implementation plans

- Write implementation plans as files in the [plans](plans/) folder.
- Name each file `YYYYMMDDHHmm_plan-title.md`: a compact date-time prefix, then an underscore, then a kebab-case title (words separated by `-`).
  - Example: `202607201830_world-registry-refactor.md`
- Give each plan a `**Status:**` line just under its title (`Todo`/`In progress`/`Done`) and add a row for it to [plans/_index.md](plans/_index.md). Keep both current as work progresses (see "Keeping statuses current" above).
- Start from [plans/_plan-template.md](plans/_plan-template.md).

## Verification

After any code change, run linting, type checking, and tests, and fix any failures before considering the work done.

## Commands

```
uv sync --dev
uv run ruff check .
uv run ruff format .
uv run pyright
uv run pytest
```
