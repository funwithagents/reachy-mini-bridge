# CI on GitHub's hosted runners, and the `tts` dependency group

**Status:** In progress

Implements [specs/testing/ci.md](../specs/testing/ci.md) in full and the `tts` group, the demo group and the CPU torch index of [specs/project.md](../specs/project.md) ("Dependency groups"), with the provider-skip rule of [specs/testing/testing.md](../specs/testing/testing.md) ("Live tier: skip without credentials"). It delivers:

- **the dependency groups as specified**: `tts` carrying the three provider extras, default locally and installed in CI; `dev` down to the tooling and the `sim` / `test` extras; `demo` without `tts-pocket`; `torch` from the PyTorch CPU index on Linux;
- **the three TTS provider tests skipping on a missing provider**, before their key gate;
- **the workflow**, `.github/workflows/ci.yml`: `check`, then `fast-tier` and `e2e-sim` in parallel, on every pull request and push to `main`, every dependency group installed (the shakeout measured the `tts` group at a few seconds on CPU torch, so the nightly-only job first planned for it was dropped — "Linux shakeout" below);
- **the project's first Linux run**, with whatever that shakes out of the bridge fixed and recorded here;
- the docs and `AGENTS.md` saying so.

It deliberately leaves out the offscreen camera of the headless sim on Linux — [202610051630_offscreen-sim-camera-on-linux.md](202610051630_offscreen-sim-camera-on-linux.md), which builds on a green `e2e-sim` — so in this plan the `camera` and `faces` tests skip on the runner, as [ci.md](../specs/testing/ci.md)'s expected-skips table says they do until then. It also leaves branch protection (a GitHub setting) to the owner, noted in Verification.

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md) (statuses, the project map rule — this plan adds a root directory; Verification); [specs/testing/ci.md](../specs/testing/ci.md) in full; [specs/project.md](../specs/project.md) "Dependency groups"; [specs/testing/testing.md](../specs/testing/testing.md) "Live tier: skip without credentials"; `pyproject.toml` in full (its comments carry the reasoning the edits replace); `tests-e2e/test_audio.py` from the real-TTS test on; memory notes: `ruff format .` reflows Markdown — format the code directories only; never `echo` a bare `=====` in a zsh command.
- **Do the steps in order**, with the check after each:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

- **Steps 4 to 6 run on GitHub**, from a branch and a pull request opened for this plan: the runner is the only Linux the project has. Iterate the workflow on that branch; what a Linux failure changes in `src/` is a bridge fix with its own spec consequences, recorded in the "Linux shakeout" section at the end of this file as it happens — this plan's one open-ended step.
- **Read the skips** (`-rs`) on every live run, on the runner as locally: the set must be exactly the expected-skips table of [ci.md](../specs/testing/ci.md) (with the camera row, in this plan).
- **Write specs affirmatively**; keep the `**Status:**` line and the `_index.md` row in sync at each flip.
- **Do not commit** unless asked — except that Steps 4 to 6 need the branch pushed; ask before the first push.

## Scope

- `pyproject.toml` — the groups (`dev`, `tts`, `demo`), `default-groups`, the `pytorch-cpu` index and the `torch` source; the comments rewritten to match; `uv.lock` relocked.
- `tests-e2e/test_audio.py` — `pytest.importorskip` in the three provider tests.
- `.github/workflows/ci.yml` (new) — the three jobs.
- `specs/testing/ci.md` — the workflow path added to `code:` once the file exists (`tests/test_project_map.py` requires every listed path to exist); status to `Implemented` at the end. `specs/project.md`, `specs/testing/testing.md`, `specs/_index.md` — status to `Implemented` at the end (the `testing.md` flip waits for the second plan if it is already under way; see Step 7).
- `AGENTS.md` — `.github/` in the top-level layout table; the credentials bullet of "Running the live e2e tests" (the `tts` group, the skip on a missing provider); a line under "Verification" naming CI as the same gate on a runner; "Commands" unchanged (`uv sync --dev` still installs every default group).
- `README.md` — the extras table's `tts-pocket` row and the "Development" block (a plain `uv sync` carries the providers; CI opts out), the control panel section (the voice needs the `tts` group); `docs/testing-with-the-bridge.md` — the TTS provider tests skip without the group.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Check command green on `main`; `uv run pytest tests-e2e -rs` green headless (read the skips; note the count, it is the local baseline Step 2 compares against).

### Step 1 — The groups and the CPU torch index

**Files:** `pyproject.toml`, `uv.lock`.

- `[dependency-groups]`: `dev` = `pyright`, `pytest`, `ruff`, `reachy-mini-bridge[sim]`, `reachy-mini-bridge[test]`, `pytest-xdist` (the provider line removed); new `tts = ["reachy-mini-bridge[tts-elevenlabs,tts-gradium,tts-pocket]"]`; `demo = ["gradio>=5", "reachy-mini-bridge[sim]"]`. `[tool.uv] default-groups = ["dev", "demo", "tts"]`.
- `[[tool.uv.index]]` `name = "pytorch-cpu"`, `url = "https://download.pytorch.org/whl/cpu"`, `explicit = true`; `[tool.uv.sources]` gains `torch = [{ index = "pytorch-cpu", marker = "sys_platform == 'linux'" }]` next to the workspace self-reference.
- Rewrite the comments: the dev-group comment no longer claims the providers; the `tts` group's comment says default-locally / `--no-group tts` in CI and why torch comes from the CPU index on Linux; the demo comment drops the pocket sentence.
- `uv lock`, then verify in `uv.lock`: the `torch` package entry carries a `download.pytorch.org/whl/cpu` wheel for the Linux platform and no `nvidia-*` dependency for `sys_platform == 'linux'`; the macOS wheels unchanged. `uv sync` (plain) leaves the local env as it was (`uv pip show torch` answers). `uv sync --no-group tts` removes torch and the providers (`uv pip show torch` fails; `python -c "import pocket_tts"` fails), the fast tier is still green in that env, and a plain `uv sync` restores them.

### Step 2 — The provider tests skip on a missing provider

**Files:** `tests-e2e/test_audio.py`.

- In `test_say_with_real_tts_speaks_through_the_robot`: `pytest.importorskip("pocket_tts")` as the first line after the capability gate, before the synthesizer is built; the docstring's "runs on every dev sync (the dev group carries `tts-pocket`)" becomes the `tts` group, skipping without it. The ElevenLabs test gains `pytest.importorskip("elevenlabs")`, the Gradium one `pytest.importorskip("gradium")`, each before its `require_env`, so a missing extra reads as the skip reason rather than a missing key.
- **Live proof, twice:** in the `--no-group tts` env, `uv run pytest tests-e2e -rs` headless is green with the three provider tests skipped on the module (the reason names it); after a plain `uv sync`, the same run has the pocket test passing again and the cloud ones skipped on the key — the Step 0 baseline.

### Step 3 — The workflow file and the docs

**Files:** `.github/workflows/ci.yml` (new), `specs/testing/ci.md` (frontmatter), `AGENTS.md`, `README.md`, `docs/testing-with-the-bridge.md`.

- `ci.yml` as [ci.md](../specs/testing/ci.md) "The workflow" specifies: `on: pull_request`, `push: branches: [main]`, `workflow_dispatch`; `concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }`; `timeout-minutes` on every job (15 for `check`, 20 for `fast-tier`, 30 for `e2e-sim`); `runs-on: ubuntu-24.04`, pinned.
  - `check`: `actions/checkout`; `apt-get install` of the system packages (a first list, confirmed in Step 4: `libgirepository1.0-dev libcairo2-dev pkg-config gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-pulseaudio gstreamer1.0-tools`); `astral-sh/setup-uv` with `enable-cache: true` and `cache-dependency-glob: uv.lock`; `uv sync --locked` (every group); ruff check, format as `--check` over `src tests tests-e2e examples`, pyright.
  - `fast-tier`: `needs: check`; the same setup; `uv run pytest`.
  - `e2e-sim`: `needs: check` (parallel with `fast-tier`); the same setup plus `pulseaudio` and Mesa (`libegl1 libgl1-mesa-dri libosmesa6` — unused until the second plan, harmless now); `actions/cache` on `~/.cache/huggingface` keyed on `hf-${{ runner.os }}-${{ hashFiles('uv.lock') }}` with the `hf-${{ runner.os }}-` restore prefix; a step starting PulseAudio and loading the null sink (`pulseaudio --start --exit-idle-time=-1`, `pactl load-module module-null-sink sink_name=ci`, `pactl set-default-sink ci`, `pactl set-default-source ci.monitor`); `uv run pytest tests-e2e -rs`.
- `ci.md` frontmatter: `.github/workflows/ci.yml` added to `code:` (the file now exists; the project-map test passes).
- `AGENTS.md`: a `.github/workflows/` row in the top-level layout table ("The CI workflow — spec: specs/testing/ci.md"); the credentials bullet: the pocket test needs the `tts` group (default locally; a sync without it skips the three provider tests); under "Verification": CI runs the same gate on every pull request and push to `main`, and a live skip that the spec's table does not expect is a failure to read.
- `README.md`: the `tts-pocket` row notes the `tts` group; the "Development" block keeps `uv sync --dev` and gains one line that the providers come with it and that CI syncs without them; the control panel paragraph says the voice needs the `tts` group. `docs/testing-with-the-bridge.md`: the provider tests skip without the group.
- Check command green; `tests/test_project_map.py` passes with the new frontmatter path.

### Step 4 — The `check` job green on the runner (the Linux shakeout)

**Where:** a branch and a pull request for this plan; the Actions log.

- Push; read the `check` job. Iterate in this order, each a commit on the branch: the apt list (PyGObject's build, GStreamer's introspection data); the sync (`--locked` must pass — a lock drift is a Step 1 miss); ruff and pyright (Linux-only typing differences, if any); the fast tier. A fast-tier failure that is a bridge Linux bug (a path, a device name, a platform check) is fixed in `src/` with its test, and listed in "Linux shakeout" below with the spec it touches, if any.
- Done when `check` is green twice in a row on the branch, the second run from warm caches, and its duration recorded below.

### Step 5 — The `e2e-sim` job green on the runner

- Read the job: the daemon must spawn (the harness log lines are forwarded into pytest's output), `audio` must probe present (the null sink), the run must end with exactly the expected skips — in this plan: `gravity_compensation`, `face_markers`, the two cloud provider tests, and every `camera` / `faces` test. `motion` and `audio` tests pass, the pocket test among them.
- Iterate on the PulseAudio step, the Hugging Face cache (the emotions library fetched once, hit after), and any Linux failure of the daemon spawn (`scrubbed_env`, the port flags, upstream's Linux media path), as in Step 4.
- Done when green twice, the second from warm caches; duration and skip count recorded below.

### Step 6 — The pocket test on the runner

- Dropped as a separate job ("Linux shakeout"): the `e2e-sim` job installs the group. Confirm in its log that torch came from `download.pytorch.org` and no `nvidia-*` wheel was fetched, that the pocket model landed in the Hugging Face cache (a hit on the next run), and that the real-TTS test passes with the cloud tests skipped on the key.

### Step 7 — Statuses

- `specs/testing/ci.md` → `Implemented`; `specs/project.md` → `Implemented`; `specs/_index.md` rows in sync. `specs/testing/testing.md`: its `Updated` status covers both this plan's skip rule and the second plan's camera row — flip it to `Implemented` here only if the second plan is `Done` too, else leave it `Updated` and let that plan flip it.
- This plan `Done` here and in [_index.md](_index.md).

## Verification

- Check command green locally in both envs (plain sync, `--no-group tts`).
- `uv run pytest tests-e2e -rs` headless green locally, the pocket test passing on a plain sync and skipping on the module without the group.
- On GitHub: `check`, `fast-tier` and `e2e-sim` green on the pull request, twice; the skip set of the live job exactly the expected table of [ci.md](../specs/testing/ci.md).
- Owner's step, outside the repo: require `check` and `e2e-sim` as status checks on `main` in the repository settings.

## Linux shakeout

What the runner found that the Mac never had, filled in during Steps 4 and 5 — one line per change: the symptom, the fix, the spec it touches.

- **pyright on Linux: `"Gst" is unknown import symbol`** at the six `from gi.repository import Gst` sites (`audio.py`, `real_daemon.py`, `sim_daemon.py`, `tests/test_audio.py`). On macOS the bundle's `gi` lives on a `.pth` path pyright cannot see, so the sites carried `# pyright: ignore[reportMissingImports]`; on Linux PyGObject resolves and `gi.repository` is built at runtime, so the name is `reportAttributeAccessIssue` instead. Fix: the parenthesized import form with the missing-import ignore on the `from` line and the attribute ignore on each name line (ruff's import sorter wants that form at this line length). No spec: the imports are an implementation detail of the GStreamer seams.
- **`uv run` re-syncs to the default groups — and the `tts` group turned out to cost nothing.** The first green run had the pocket test *passing* and the cloud tests skipping on the key, not the module: `uv sync --locked --no-group tts` installed 110 packages, then the first `uv run` re-synced to `default-groups` and installed the `tts` group before ruff ran. The log priced it: CPU torch (187 MB) and eleven small packages in 3 s, the pocket model's first download inside a live step of 123 s against 107 s locally, the fast tier at 148 s with or without it. The reason to keep the group out of CI had been sized on the CUDA build; with the CPU index it is gone, so the design changed (`ci.md`, `project.md`): every job installs every group with a plain `uv sync --locked`, the pocket test runs on every pull request, and the nightly `e2e-tts` job, the `schedule` trigger and the `UV_NO_SYNC` workaround briefly added for the re-sync all go. The import gates on the provider tests stay — right for any environment without the group. (Locally, `uv run` after a `--no-group` sync re-adds the group the same way; `uv run --no-sync` is how to test without it.)
- **Deprecations in the run's annotations:** the action majors in use (`checkout@v4`, `cache@v4`, `setup-uv@v6`) target Node 20, which the runners are retiring — bumped to `checkout@v7`, `cache@v6`, `setup-uv@v10`; `ubuntu-latest` migrates to Ubuntu 26 on 2026-10-19 — the jobs pin `ubuntu-24.04`, the image the shakeout validated (spec: `ci.md` "The runner").
- **One job for the static gate and the fast tier read badly.** Lint, format and types are a verdict in a minute and a half; the fast tier is three minutes of sleeping; serial in one job, the e2e job waited on both. Split: `check` (static) first, then `fast-tier` and `e2e-sim` side by side — a run is the static gate plus the slower tier. Spec: `ci.md` "The workflow" (three required checks).
- **Two xdist workers on four vCPUs.** The fast tier's `-n auto` counts *physical* cores when psutil is importable (it is, through `reachy_mini`), and the runner's four vCPUs are two hyperthreaded cores: `created: 2/2 workers`, the tier at 148 s against 42 s locally. Fix: `-n logical` in `addopts` — four workers on the runner, unchanged on a Mac (no SMT, logical = physical). Spec: `testing.md` (the parallel-run sentence), `ci.md`, `AGENTS.md`.
- **What needed no fix on the first run:** the apt list as written (PyGObject built from its sdist against it), `uv sync --locked --no-group tts`, ruff check and the format check.

## Measurements

- `check`, warm caches: — min
- `fast-tier`, warm caches: — min
- `e2e-sim`, warm caches: — min; skips: —
