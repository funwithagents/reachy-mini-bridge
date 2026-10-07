# The documentation's stale statements corrected, the developer paths completed, two more guards

**Status:** Done

An editorial pass, no spec changing what the code should do: the documentation items of the [consistency review of 2026-10-07](../analysis/20261007_project-consistency-and-documentation.md) — its stale or overbroad statements corrected, its developer-path improvements added, the two documentation guards it found missing written — and the motion loop's hardware validation recorded, its on-robot checklist walked by the author on a Reachy Mini Lite. Deliberately leaves out two of the review's suggestions: the runnable speech-and-listening example (a new example needs a spec and a project-map row of its own, like the control panel, so it is tracked in `analysis/global_analysis.md` for a pass of its own), and copying the webrtc plugin's install commands into the Linux guide (installing it is upstream's concern — the guide points at upstream's pinned action — and a copied commit and checksum would rot outside the bridge's CI, which follows upstream's action by one ref).

## Scope

- `plans/202609162000_motion-loop-presence-and-breathing.md` — `Done`: the step 8 checklist marked walked (the author, a Lite over USB, `reachy_mini` 1.10.0, 2026-10-07); `specs/motion/motion.md` and its index row `Stable` → `Implemented` (a `Done` plan built it and the code matches); `specs/_overview.md`'s status paragraph without the pending hardware check.
- `specs/_overview.md`, `specs/core/config.md`, `docs/reference/backends-and-capabilities.md` — one file switches real ↔ sim by `backend` alone; the `fake`, having no daemon, also wants `daemon.spawn` at `never` or the block absent.
- `specs/daemon/daemon.md` — `status_url` builds the URL from the host as given (the managed daemon's host is IPv4 loopback); the IPv6 bracketing claim dropped.
- `specs/daemon/sim_daemon.md` — the clients of the camera stream named as the bridge's camera feed over upstream's `get_frame()`, not the retired `get_camera_frame()`.
- `specs/core/bridge.md` — the live motor test named where it lives (`tests-e2e/test_motors.py`), the fake's mapping test in `tests/test_bridge.py`.
- `README.md` — what the project-map test guarantees (the map and the statuses), the two test tiers being what establishes that the code does what a spec says.
- `docs/reference/api.md` — "Construction" says what runs there: the `from_*` validation before the bridge exists, the observables and the feed object, the voice's provider built (a local model's weights loaded) with its failure reported through `synthesizer_error`, everything else in `start()`; an "On this page" line of section links.
- `docs/reference/configuration.md` — "Validation" says when it runs (at parse, in the `from_*` paths) and that the `tts` block is validated at construction, by tts-engine.
- `docs/getting-started.md` — pinning the bridge's commit does not pin `tts-engine@main` or the `reachy-mini` range: lock the project's own graph and move it deliberately.
- `docs/guides/testing.md` — the live tier run serially: why, `-n 0`, and the tier-local conftest hook the bridge uses (the plugin forces no worker count).
- `tests/test_docs_consistency.py` — `test_every_heading_fragment_names_a_heading_of_its_page` (a link's `#fragment` resolves to a heading of its page, GitHub's slug rule) and `test_every_test_reference_names_a_test_that_exists` (every `tests/….py::test_…` mention in the documentation names an existing test — what would have caught the moved motor test).
- This plan and [the plans index](_index.md).

## Steps

1. Record the hardware validation and flip the motion statuses (the index rows with them).
2. Correct the stale statements, one home per fact: the backend switch qualified where it was overstated, the IPv6 and camera-verb leftovers removed, the moved test renamed, the README's guard claim scoped.
3. Complete the developer paths in the consumer references and guides.
4. Write the two guards; run them against the documentation and fix what they find.

## Verification

`uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` — all green on 2026-10-07; the test-reference guard fails on the documentation as it stood before step 2 (the motor test's old path).
