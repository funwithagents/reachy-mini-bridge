# The `tts` group on phonon

**Status:** Done

Implements `specs/project.md` ("Dependency groups": `tts`), `specs/testing/testing.md` ("Live tier: skip without credentials") and `specs/testing/ci.md`. The `tts` dependency group carries one provider, phonon, which is local, needs no key and no torch. The live tier keeps one real-TTS test, on phonon with the `Micah` voice. Each provider's own synthesis is tested in tts-engine's live tier.

## Scope

- `pyproject.toml`: `tts = ["reachy-mini-bridge[tts-phonon]"]`. The `pytorch-cpu` index, the `torch` source and the explicit `torch` entry go.
- `uv.lock`: relocked. torch stays in the lock only through the `tts-pocket` and `tts-all` extras, so a dev sync no longer installs it.
- `tests-e2e/test_audio.py`: the real-TTS test runs on phonon (`importorskip("ptts")`, 24 kHz). The ElevenLabs and Gradium tests go.
- `.github/workflows/ci.yml`: no TTS secrets, and the comments and cache step name follow.
- Examples: the greeter's two configs, its README and docstring, and the example-configs voice template use phonon with `Micah`.
- Docs: the configuration reference's full example and its local-model example.
- Specs: `specs/project.md` and its index row, `specs/testing/testing.md`, `specs/testing/ci.md` and its index row (the expected-skips row for the cloud tests goes). `AGENTS.md` credentials bullet.

## Steps

1. The group, the index removal, the relock.
2. The live test, the CI workflow.
3. The examples, the docs, the specs and `AGENTS.md`.

## Verification

`uv run pytest tests-e2e/test_audio.py -k real_tts -rs` against the headless sim: passed. Lint, format check, pyright (0 errors) and `uv run pytest` (747 passed, 1 skipped) all pass.
