# Finite startup timeout, required capabilities, media by locality

**Status:** Done

Implements the remaining runtime and harness findings of the [repository consistency review](../analysis/20261005_repository-consistency-review.md) — R6 (a non-finite `daemon.startup_timeout` defeats the bounded readiness wait), V1 (a Linux camera, audio or daemon regression turns into a green, skipped live job) and V2 (the shipped harness forces `media_backend="local"` on a remote robot) — in [config.md](../specs/core/config.md) ("Validation"), [testing_support.md](../specs/testing/testing_support.md) ("Public surface", "Configuration via the environment"), [testing.md](../specs/testing/testing.md) ("Capabilities are probed, not assumed", "The harness") and [ci.md](../specs/testing/ci.md) ("What the live job provides"). The documentation findings (D1–D4) and the documentation reorganization stay separate work; wireless hardware is not validated here.

## Scope

- `src/reachy_mini_bridge/config.py` — `daemon.startup_timeout` must be a positive **finite** number (`inf` / `nan`, which Python's JSON parser accepts, are a `ConfigError`).
- `src/reachy_mini_bridge/testing/_daemon.py` — `required_capabilities()` from `REACHY_MINI_E2E_REQUIRED_CAPS`; `robot_options(host, port)`, the `robot` block the harness's bridges connect with, its `media_backend` chosen by the host's locality (`"local"` on loopback, upstream's `"default"` auto-detection — WebRTC for a network client — elsewhere) or by `REACHY_MINI_E2E_MEDIA_BACKEND`; an unavailable daemon (spawn failure, missing sim extra, remote `real` address) **fails** instead of skipping when capabilities are required.
- `src/reachy_mini_bridge/testing/fixtures.py` — `live_bridge` builds its config from `robot_options` and, after probing, `check_required_capabilities(caps)` fails the fixture (every test of the module errors) when a required capability was not probed.
- `tests-e2e/test_custom_faces.py` — its own session connects through the same `robot_options` and runs the same required check.
- `.github/workflows/ci.yml` — the live job requires `motion,audio,camera,faces`.
- `tests/test_config.py`, `tests/test_testing_support.py` — the regressions below.
- `specs/core/config.md`, `specs/testing/testing_support.md`, `specs/testing/testing.md`, `specs/testing/ci.md`, `docs/testing-with-the-bridge.md`, `README.md`, `AGENTS.md` — the finite requirement, the two environment variables, the media selection, the required capabilities in CI.

No new module, dependency or root directory; the spec statuses stay `Implemented` (the harness spec is edited and implemented in the same change).

## Steps

1. **R6 — a finite timeout.** In `DaemonConfig.from_dict`, reject a `startup_timeout` that is not a finite number (`math.isfinite`) with the message `'daemon.startup_timeout' must be a positive finite number`. Tests: `inf`, `-inf` and `nan` through `from_dict`, and the JSON spellings `Infinity` / `NaN` through `from_json`, all `ConfigError`; the existing valid values still parse.

2. **V2 — media by locality.** Add `robot_options(host, port)` to `_daemon.py`: `connection_mode="network"`, the address, and `media_backend` from `media_backend(host)` — the `REACHY_MINI_E2E_MEDIA_BACKEND` value when set, else `"local"` for a host in `LOOPBACK_HOSTS`, else `"default"` (upstream auto-detects: with `connection_mode="network"` that is its WebRTC streaming path, the one a wireless robot serves). `live_bridge` and the custom-faces fixture build their `robot` block from it. Tests: loopback hosts give `local`, a LAN address gives `default`, the override wins on both.

3. **V1 — required capabilities.** Add `required_capabilities()` to `_daemon.py`: the comma-separated, case-insensitive set in `REACHY_MINI_E2E_REQUIRED_CAPS` (empty / unset: nothing required). Route every "the environment cannot provide a daemon" exit of `managed_daemon` (remote `real` address, missing `mujoco`, `DaemonError` from the spawn) through one helper that calls `pytest.fail` when the set is non-empty and `pytest.skip` otherwise, the message unchanged. Add `check_required_capabilities(caps)` to `fixtures.py`: `pytest.fail` naming the missing and the probed capabilities; `live_bridge` and `live_bridge_custom_faces` call it right after probing, inside the `try` so the bridge still stops. Set `REACHY_MINI_E2E_REQUIRED_CAPS: motion,audio,camera,faces` on the live job's test step. Tests: parsing (unset, empty, spaces, case); a `DaemonError` from the scripted spawn skips without the variable and fails with it; the remote `real` address likewise; `check_required_capabilities` passes on a superset and fails naming the missing ones.

4. **The specs and the docs.** `config.md`: the field table and the validation list say finite. `testing_support.md`: the public-surface paragraph says the `robot` block's media backend follows the host's locality; the environment list gains the two variables; the required-capabilities rule is stated next to `requires_caps`. `testing.md`: "Capabilities are probed" states the rule's two outcomes (skip by default, fail where required) and "The harness" says media by locality. `ci.md`: the live job requires the four capabilities, so a run that comes up without its camera, audio or daemon fails rather than skips; the expected-skips table is unchanged, and the relationship line no longer says every `REACHY_MINI_E2E_*` stays at its default. `docs/testing-with-the-bridge.md`: the variables table and the own-it-or-borrow-it paragraph. `README.md`: the `startup_timeout` row and the harness sentence. `AGENTS.md`: the wireless row and the "Read the skips" bullet. The two harness modules' docstrings follow.

5. **Verification and the record.** The gate below; the live tier locally on the viewer sim, once with `REACHY_MINI_E2E_REQUIRED_CAPS=motion,audio,camera,faces` set, which must pass unchanged. Mark the plan `Done` here and in the index with the results.

## Verification

```sh
uv run pytest tests/test_config.py tests/test_testing_support.py
uv run ruff check .
uv run ruff format --check src tests tests-e2e examples
uv run pyright
uv run pytest
git diff --check
REACHY_MINI_E2E_SIM_VIEWER=1 REACHY_MINI_E2E_REQUIRED_CAPS=motion,audio,camera,faces uv run pytest tests-e2e -rs
```

The CI job itself is verified by its next run on `main`; a wireless robot's media path stays unvalidated (the README's caveat stands).

## Completion record — 2026-10-06

Implemented as scoped. `daemon.startup_timeout` rejects `inf` / `-inf` / `nan` and the JSON spellings `Infinity` / `-Infinity` / `NaN`. The harness's `robot` block comes from `robot_options(host, port)` (`media_backend` `local` on loopback, upstream's `default` elsewhere, `REACHY_MINI_E2E_MEDIA_BACKEND` overriding) in both `live_bridge` and the custom-faces session. `REACHY_MINI_E2E_REQUIRED_CAPS` makes a missing capability (`check_required_capabilities`, after every probe) and an unavailable daemon (remote `real` address, missing `mujoco`, a failed spawn) fail instead of skip; the CI live step sets it to `motion,audio,camera,faces`. Specs, docs, README and AGENTS updated; statuses unchanged.

- Ruff lint and format check: passed. Pyright: 0 errors.
- Fast suite: **706 passed in 47 s** (16 new tests). `git diff --check`: passed.
- Live tier, viewer sim, `REACHY_MINI_E2E_REQUIRED_CAPS=motion,audio,camera,faces`: **30 passed, 2 skipped** (the by-design `gravity_compensation` and motor-mode skips), 3 min 31 s.
- The failure path, live: `REACHY_MINI_E2E_REQUIRED_CAPS=gravity_compensation` on `tests-e2e/test_motors.py` (headless sim) errors every test at setup with `the target lacks required capability/ies: gravity_compensation (probed: audio, faces, motion)`.

Not verified here: the CI job itself (its next run on `main`), and the WebRTC media path on a wireless robot.
