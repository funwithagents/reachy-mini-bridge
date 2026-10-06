# Consumer documentation reorganisation — a `docs/` entrance, references extracted from the README, guides, profiles, and the checks that keep one home per fact

**Status:** Done

**Done (2026-10-06):** every step implemented and verified — `ruff check`, `ruff format` on the code directories, `pyright`, the fast tier (738 tests: the four documentation examples executed on the fake, every local link resolving, every status matching its index, the config inventory complete in `config.example.json` and the configuration reference, the five profiles parsing). The two analysis documents this plan implemented were deleted; `analysis/global_analysis.md` keeps the audio-profile item. Departure from the review's suggestion of pointer pages at the old `docs/` paths: none were left — every in-repo link was updated, and the project pins consumers to commits.

Editorial: gives the bridge's consumer documentation one entrance and one home per fact, as the untracked analyses `analysis/documentation_reorganization.md` and `analysis/20261006_repository-consistency-review.md` (2026-10-06) lay out — the two agree on the destination, the review adds the integration contracts the API reference must carry, the runnable connection profiles, and the consistency guards. No design changes, so no spec changes status; the only code is the documentation tests. It deliberately leaves out the one remaining runtime gap the review names (the audio profile's `apply_audio_config` result, tracked in `analysis/global_analysis.md`), which is a design decision for the audio spec, not documentation.

## Scope

- `README.md` — shortened to the landing page: introduction, support status, what the bridge adds, install, the fake quick start, the control panel, documentation links, development pointer, license. Its API survey, agent example, backends, simulator, configuration, testing and development sections move to the pages below.
- `docs/index.md` — new: navigation by developer task.
- `docs/getting-started.md` — new: the first application (fake-runnable), connection choices, lifecycle, waiting for the capabilities an application uses, the agent tools example.
- `docs/reference/api.md` — new: the public API (imports, verbs, properties, data types, errors), lifecycle, cancellation and concurrency, units, the extension contracts.
- `docs/reference/configuration.md` — new: the configuration reference (the README's field tables, every bridge-owned field with its default and validation).
- `docs/reference/backends-and-capabilities.md` — new: the one OS/target matrix, expected capabilities apart from validated support, native dependencies and first-use downloads.
- `docs/guides/audio.md` — new: voice setup, speech, sound files, the microphone.
- `docs/guides/perception-and-tracking.md` — new: the complete tracking setup and how to consume the reports.
- `docs/guides/custom-idle-move.md` — new: a complete, runnable idle move.
- `docs/guides/custom-face-detector.md` ← `docs/custom-face-detector.md`, `docs/guides/testing.md` ← `docs/testing-with-the-bridge.md`, `docs/guides/running-daemons.md` ← `docs/running-the-sim-daemon.md`, `docs/guides/linux.md` ← `docs/linux.md`, `docs/internals/upstream-sdk-notes.md` ← `docs/reachy-mini-api.md` — moved (`git mv`), their cross-links updated, their README links pointed at the references. No pointer pages at the old paths: every in-repo link is updated in this change, and the project pins consumers to commits.
- `examples/configs/` — new: `fake.json`, `lite-usb.json`, `sim-rendered-camera.json`, `sim-webcam.json`, `wireless.json` and a `README.md` saying what each needs installed, which devices and capabilities it gives, and its validation status.
- `AGENTS.md` (the project map's `docs/` and `examples/` rows, the e2e section's links), `CONTRIBUTING.md`, `specs/project.md`, `specs/_overview.md`, `specs/testing/ci.md`, `specs/testing/testing_support.md`, `specs/daemon/daemon.md`, `specs/audio/audio.md`, `specs/vision/user_perception.md`, `specs/vision/camera.md`, `src/reachy_mini_bridge/*.py` docstrings, `pyproject.toml`, `.github/workflows/ci.yml` — the moved paths.
- `tests/test_docs_examples.py` — the moved paths; the getting-started application and the custom idle move guide's example run on the fake too.
- `tests/test_docs_consistency.py` — new: every local Markdown link in tracked documentation resolves; every spec's and plan's `**Status:**` matches its index row; `config.example.json` and the configuration reference name every bridge-owned config field, and nothing else; every profile under `examples/configs/` parses as a `ReachyMiniConfig`.
- `specs/project.md` frontmatter — the new test files.
- `plans/_index.md`, this file — status.

## Steps

1. **Move** the five existing pages with `git mv`, then update every link to them across the repository (`grep -rn 'docs/'` outside `plans/` and `analysis/`).
2. **Extract** the API, configuration and backends references from the README, aligned with the specs they summarise (`bridge.md`, `config.md`, `audio.md`, `user_perception.md`, `head_tracking.md`, `observable.md`, `testing.md`'s capability matrix). The API reference lists the integration contracts the review names: one event loop owns a session; `async with` or `start()` / `stop()`, overlapping lifecycle calls unsupported; emotions queue, the newest speech and the newest sound win; modes need no motors, `play_emotion` does; applications own their tasks; `faces.value` versus `faces.changes()`; `active=False` means unknown; track ids are continuity, not identity; the units of every public value; what competes with the camera reader and the target writer.
3. **Write** the getting-started page, the task guides and the index; the complete extension examples (synthesizer, detector, idle move) are runnable, the schematic snippets labelled.
4. **Shorten** the README to the landing page and link the pages; update `CONTRIBUTING.md`'s pointers.
5. **Profiles** under `examples/configs/` with their README.
6. **Tests**: the docs example tests on the new paths and the two new examples; the consistency checks.
7. **Verify** (below), delete the two analysis files, mark this plan `Done`.

## Verification

```
uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
```

The new documentation tests pass: the extracted examples run on the fake, every local link resolves, every status matches its index, the config inventory is complete, every profile parses. Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass.
