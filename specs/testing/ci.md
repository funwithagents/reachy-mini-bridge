---
code:
  - .github/workflows/ci.yml
  - pyproject.toml
tests:
---

# Continuous integration

**Status:** Stable

## Purpose

Both test tiers run on GitHub's hosted runners for every pull request and every push to `main`, so the verification gate of [AGENTS.md](../../AGENTS.md) — lint, type check, tests — is a machine's verdict on each change and not only a local command. CI runs exactly what the tiers are designed to run with no credentials and no hardware ([testing.md](testing.md)): the fast tier whole, and the live tier against the headless sim the harness spawns, every test skipping where the runner cannot meet its needs and the skips read as part of the verdict. Like [testing.md](testing.md) this is a cross-cutting practice, not a runtime concept: nothing here ships in the library, and the one file that implements it is the workflow.

## Decided

### The runner

- **GitHub-hosted Linux, the free tier.** Every job runs on `ubuntu-24.04` (x86_64, four cores) — the image pinned by name, not `ubuntu-latest`, so the system GStreamer and Mesa the jobs were validated against move only when the pin is bumped on purpose. No self-hosted runner. The repo is public, so GitHub's macOS runners would cost nothing either; Linux is the platform because it has a **display-less GL backend** — MuJoCo's `egl` through Mesa — which gives the headless sim its rendered camera ([../daemon/sim_daemon.md](../daemon/sim_daemon.md) "The headless camera"), so the perception and head-tracking tests run on a runner with no window. On macOS the only GL context is the window server's, which no runner session hands out reliably; a macOS runner could run motion and audio and never the camera.
- **CI is the project's Linux target.** Development happens on macOS; the runner is where the bridge's Linux paths (the launcher's GL backend, PulseAudio sources, upstream's `pulsectl` and PyGObject dependencies) are exercised. A Linux-only failure is a bridge bug to fix, not a reason to pin CI to macOS.
- **One Python**, 3.12 — the floor of `requires-python`, the version `.python-version` names; uv installs it on the runner.
- **System packages** are apt-installed before the sync, in the workflow, which is the one place that lists them: upstream `reachy_mini` on Linux depends on PyGObject — sdist-only in the locked range, built on the runner against the girepository and cairo development headers with `pkg-config` — and on the system GStreamer: its GObject-introspection data, the base, good and bad plugin sets (bad carries `webrtcdsp` / `webrtcechoprobe`, the software echo cancellation the media server enables when no robot sound card is present), the PulseAudio plugin; the live jobs add PulseAudio itself and Mesa's EGL and OSMesa libraries for the offscreen render.

### The workflow

One file, `.github/workflows/ci.yml`. It triggers on `pull_request`, on `push` to `main`, and on `workflow_dispatch`. Concurrency is one run per ref: a newer push cancels the older run still in flight.

| Job | When | What |
|---|---|---|
| `check` | every pull request and push to `main` | `uv sync --locked`, then `ruff check .`, `ruff format --check src tests tests-e2e examples`, `pyright`, `pytest` — the fast tier, with its parallel default |
| `e2e-sim` | the same events, after `check` passes | the same environment plus PulseAudio and Mesa; a null sink loaded; `pytest tests-e2e -rs` against the headless sim the harness spawns — the real pocket-TTS test included |

- **`--locked`.** The sync fails when `uv.lock` does not match `pyproject.toml`, so a dependency edit lands with its relock or not at all.
- **Every group installs**, the `tts` providers included ([../project.md](../project.md) "Dependency groups"): on Linux the group resolves torch from the PyTorch CPU index — a 190 MB wheel the runner fetches in seconds — never the CUDA libraries, so there is nothing to leave out, and the real pocket-TTS test runs on every pull request. The group stays separate for a consumer who wants an environment without it, and the provider tests then skip on the missing module ([testing.md](testing.md) "Live tier: skip without credentials").
- **The format check covers the code directories only.** `ruff format .` rewrites the Python blocks inside Markdown files (plans, docs, the README), so the gate is `ruff format --check src tests tests-e2e examples`, the same scope a local format run uses.
- **The fast tier runs as locally**: `uv run pytest` with the `-n logical --maxprocesses 8` default of `pyproject.toml`, which on the runner's four vCPUs is four workers (`auto` would count its two physical cores, [testing.md](testing.md)); the tier is sleep-bound ([testing.md](testing.md)), so it takes roughly twice its local time.
- **The live tier runs as locally**, serial, one daemon per run: the harness spawns the headless sim with the test scene ([testing.md](testing.md) "E2E targets & capabilities"); nothing in the workflow starts a daemon by hand. `-rs` prints every skip into the job log.

### What the live job provides, and what it leaves absent

- **Audio: a PulseAudio null sink.** The daemon's media server takes the host's default source and sink when no robot sound card is present ([../audio/audio.md](../audio/audio.md)); on a runner with no sound hardware there is none, the server logs that audio is unavailable, and every `audio` test would skip. The job starts PulseAudio and loads `module-null-sink` before the tests, so the daemon finds a sink and its monitor as the source: the `audio` capability probes present, `say` and `play_sound` stream to a sink nobody hears, the mic tap reads silence, and the software AEC runs. The audio tests assert on the pipeline — a sample arriving, a `say` completing or being interrupted — never on what is heard, so silence is a valid signal.
- **The camera: offscreen on Linux.** The headless sim renders its eye camera through EGL ([../daemon/sim_daemon.md](../daemon/sim_daemon.md) "The headless camera"), so `camera` probes present and the perception, head-tracking and custom-detector tests run on the runner against the test scene's portraits. Until that launcher behaviour is built, the camera probes absent on the runner and those tests skip; the expected-skips table below changes with it.
- **Downloads, cached.** The runner has the network: the emotions library the daemon preloads and the emotion tests fetch, the YuNet model the detector loads, and the pocket-tts weights (about 800 MB) all come from the Hugging Face Hub into `~/.cache/huggingface`, which the workflow caches keyed on the lock file (a prefix restore key keeps an older cache useful; the cache stays around a gigabyte, well inside GitHub's allowance). uv's own cache is kept by the uv setup action, keyed on `uv.lock`.
- **Expected skips.** A skip is not a pass ([AGENTS.md](../../AGENTS.md) "Read the skips"); on the runner the following skip by design, and any other skip in a job log is an environment regression to investigate:

| Skips on the runner | Why |
|---|---|
| `gravity_compensation` tests | hardware on the Placo engine; a sim never has it |
| `face_markers` tests | the markers are drawn on the viewer window; no viewer in CI |
| the ElevenLabs and Gradium `say` tests | no key in CI (`require_env`) |
| `camera` and `faces` tests, until the offscreen camera is built | no frame from a headless sim before then |

- **Never in CI:** the MuJoCo viewer (no GUI session on a runner; the face-marker tests stay a local viewer run), a real robot (`REACHY_MINI_E2E_TARGET=real` is a local or on-robot command), and a daemon started outside the harness.

### Secrets and protection

- **No secret is required.** Every job is green with none: the cloud TTS tests skip on the missing key, and the pocket test needs none. The ElevenLabs and Gradium keys may later be added as repository secrets; a fork's pull request never receives a secret, so it runs the same jobs with the two cloud tests skipped.
- **The two status checks to require on `main`** are `check` and `e2e-sim`. Requiring them is a repository setting on GitHub, outside the repo.

### Time budget

With warm caches, `check` is in the order of four minutes — the system packages and the fast tier dominate: the tier is sleep-bound and the runner's four cores spread it less than a laptop's eight — and `e2e-sim` about three (the daemon's spawn, the breaths and blends the live tests wait through, the pocket model's load). Each job carries a `timeout-minutes` well under GitHub's default, so a hung daemon fails the job in minutes rather than hours.

## Relationship to the other specs

- **[testing.md](testing.md):** the two tiers, the headless target, the probed capabilities and the skip-without-credentials rule CI runs on. CI is the hosted run of that strategy, with no rule of its own about what a test does.
- **[testing_support.md](testing_support.md):** the shipped harness the live jobs drive; `REACHY_MINI_E2E_*` stays at its defaults in CI (headless sim, port 8000, loopback).
- **[../project.md](../project.md):** the dependency groups CI installs whole, the PyTorch CPU index for Linux that makes the `tts` group cheap there, `.github/workflows/` in the repo shape.
- **[../daemon/sim_daemon.md](../daemon/sim_daemon.md):** the headless camera on Linux, which turns the runner's sim into a camera target; **[../daemon/daemon.md](../daemon/daemon.md):** the headless launch recipe the harness spawns.
- **[AGENTS.md](../../AGENTS.md):** the verification gate CI automates, and the skip-reading discipline it inherits.

## Open questions

1. **Thresholds on a shared runner.** The head-tracking convergence thresholds in `reachy_mini_bridge.testing.gaze` and the live tier's timing assertions were measured on a Mac; a shared four-core VM with software rendering is slower and noisier. The first camera-enabled runs will say whether the constants hold; a loosened constant is recorded in the kit's docstrings with its runner measurement, as the Mac measurements are today. Deferred until measured.
2. **The viewer under Xvfb.** A Linux viewer on a virtual X display with Mesa's software renderer would bring the `face_markers` tests into CI. Not needed for the tier's coverage; deferred until a marker regression slips through a local run.
3. **A macOS job.** Free on a public repo and the developers' platform, but blind to the camera and slower; worth adding only if a macOS-only regression ever reaches `main`.
